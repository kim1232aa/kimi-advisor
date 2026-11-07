#!/usr/bin/env python3
"""Stop hook: pi-warden `done-check`, with the advisor only when Jev says one is needed.

1. Fast path: no project file mutated this turn (`mutations == 0`), or checks passed and either the completion gate
   is off or `ask_advisor` was already called -> exit 0 without any Jev call.
2. Jev `doneQuestions` first. If the reply does not claim the work is done (partial progress, a blocker, a question
   to the user), exit 0.
3. Claims done without a passing check since the last mutation -> exit 2 with `doneNudge` (run the check). The nudge
   also asks for `ask_advisor` only when Jev's advisor screen (pi-advisor-flow stakes/self-answerable) says a review
   is needed.
4. Claims done after passing checks, completion gate on, no `ask_advisor` yet -> Jev advisor screen; exit 2 asking for
   `ask_advisor` only when Jev says a review is needed.
Same-model executor/advisor, Jev disabled, or a Jev failure never requires the advisor. At most one Stop block per
user prompt (`done_checked_turns`), so it can never loop.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


EXIT_REASON = {"why": ""}


def allow(why):
    EXIT_REASON["why"] = why
    return 0


def jev_wants_advisor(core, parsed, cfg, session_id, wire, tk, trigger):
    """Returns (True, jev_detail) when Jev judges an advisor review is needed, else (False, reason)."""
    kimi_cfg = core.load_kimi_config()
    executor = parsed.get("executor_model")
    if core.all_same_model(executor, cfg, kimi_cfg):
        return False, "same_model"
    if not cfg["jev"]["enabled"]:
        return False, "jev_disabled"
    try:
        skip, detail = core.screen(parsed["entries"], cfg)
    except core.JevError as e:
        core.log_event({"session": session_id, "decision": "done_screen_error", "trigger": trigger, "error": str(e)[:300]})
        return False, "jev_error"
    if skip:
        core.log_event({"session": session_id, "decision": "skipped_jev", "trigger": trigger, "jev": detail})
        return False, "jev_skip"
    with core.SessionState(wire) as st:
        st.data["prescreened_turn"] = tk
        st.data["prescreened_detail"] = detail
    return True, detail


def _mark_turn_gated(core, wire, tk):
    with core.SessionState(wire) as st:
        done_turns = st.data.get("done_checked_turns") or []
        if tk not in done_turns:
            st.data["done_checked_turns"] = (done_turns + [tk])[-50:]


def jev_note(detail):
    return f"P(negligible)={detail['negligible']:.2f}, P(self-answerable)={detail['self_answerable']:.2f}"


def main(payload):
    if payload.get("stop_hook_active"):
        return allow("stop_hook_active")
    if payload.get("agent_id") not in (None, "", "main"):
        return allow("subagent_active")
    import advisor_core as core
    import warden_guards as wg

    cfg = core.load_config()
    done_cfg = cfg.get("done") or {}
    done_on = done_cfg.get("enabled", True)
    rules_on = (cfg.get("rules") or {}).get("enabled", True)
    if not done_on and not rules_on:
        return allow("done_disabled")
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    wire = core.find_session_wire(session_id)
    if not wire:
        return allow("no_wire")

    time.sleep(0.2)
    parsed = getattr(core, "parse_wire_current_turn", core.parse_wire)(wire)
    if (parsed or {}).get("in_flight_subagent"):
        return allow("subagent_active")
    turn_entries, tk, task_text, final_assistant = core.current_turn_slice(parsed["entries"])
    if not tk:
        return allow("no_user_turn")
    if not final_assistant:
        return allow("no_final_assistant")

    redact_fn = core.redact if cfg.get("redact_secrets", True) else (lambda s: s)
    evidence = wg.collect_turn_evidence(turn_entries, cwd=cwd, redact_fn=redact_fn)
    advisor_called = any(
        e["kind"] == "tool_call" and str(e.get("name") or "").endswith(core.ADVISOR_TOOL_SUFFIX)
        for e in turn_entries
    )
    need_test_check = done_on and wg.needs_done_check(evidence)
    gate_on = done_on and done_cfg.get("completion_gate", False)
    ops_changes = evidence.get("ops_mutations", 0) if gate_on else 0
    suspect_fact = rules_on and wg.needs_unverified_fact_check(evidence, final_assistant)

    if evidence["mutations"] == 0 and ops_changes == 0 and not suspect_fact:
        return allow("no_mutations")

    with core.SessionState(wire) as st:
        done_turns = st.data.get("done_checked_turns") or []
        if tk in done_turns:
            return allow("turn_already_gated")

    # Zero-mutation turn with suspect unverified external fact claims -> 1 Jev call on fact/verification rules
    if evidence["mutations"] == 0 and ops_changes == 0 and suspect_fact:
        if not cfg["jev"]["enabled"]:
            return allow("no_mutations")
        try:
            violated = wg.evaluate_turn_rules_with_jev(
                task_text, final_assistant, evidence, cwd, cfg, core, redact_fn, fact_only=True
            )
        except Exception as e:  # noqa: BLE001
            core.log_event({"session": session_id, "decision": "rules_error", "error": str(e)[:300]})
            return allow("rules_jev_error")
        if not violated:
            return allow("no_mutations")
        _mark_turn_gated(core, wire, tk)
        core.log_event({"session": session_id, "decision": "rules_blocked", "violated": [r["name"] for r in violated]})
        sys.stderr.write(wg.rules_nudge(violated))
        return 2

    if not need_test_check and advisor_called:
        return allow("checked_and_advisor_called")
    if not need_test_check and not gate_on and not rules_on:
        return allow("checks_passed")

    # Path A: unverified code changes (no passing check since last Edit/Write) -> 1 Jev call (doneQuestions), require test only
    if need_test_check:
        try:
            verdict = wg.evaluate_done_with_jev(task_text, final_assistant, evidence, cfg, core.jev_judge, redact_fn)
        except Exception as e:  # noqa: BLE001
            core.log_event({"session": session_id, "decision": "done_error", "error": str(e)[:300]})
            return allow("done_jev_error")
        if not verdict.get("claimed"):
            return allow("not_claiming_done")
        if not verdict.get("unverified"):
            return allow("claimed_not_checkable")
        nudge = wg.done_nudge(verdict, cwd=cwd)
        _mark_turn_gated(core, wire, tk)
        core.log_event({
            "session": session_id,
            "decision": "done_blocked",
            "mutations": evidence["mutations"],
            "reasons": verdict.get("reasons"),
            "judgment": verdict.get("judgment"),
            "advisor_required": False,
        })
        sys.stderr.write(nudge)
        return 2

    # Fast-exit for tiny 1-file typo edits with no suspect diff patterns
    if evidence["mutations"] < 2 and ops_changes == 0 and not wg.is_nontrivial_single_mutation(evidence, task_text):
        return allow("minor_single_file" if gate_on else "checks_passed")

    # Path B: completion_gate on (and >=2 mutations or ops_changes), no ask_advisor yet -> 1 batched Jev call (done + screen + AGENTS.md rules)
    if gate_on and not advisor_called and (evidence["mutations"] >= 2 or ops_changes > 0):
        kimi_cfg = core.load_kimi_config()
        if core.all_same_model(parsed.get("executor_model"), cfg, kimi_cfg):
            return allow("gate_same_model")
        if not cfg["jev"]["enabled"]:
            return allow("gate_jev_disabled")
        try:
            gate_res = wg.evaluate_stop_gate_batched(
                task_text, final_assistant, evidence, parsed["entries"], cfg, core, redact_fn, cwd=cwd
            )
        except Exception as e:  # noqa: BLE001
            core.log_event({"session": session_id, "decision": "done_screen_error", "trigger": "stop_gate", "error": str(e)[:300]})
            return allow("gate_jev_error")
        if gate_res.get("violated_rules"):
            violated = gate_res["violated_rules"]
            _mark_turn_gated(core, wire, tk)
            core.log_event({"session": session_id, "decision": "rules_blocked", "violated": [r["name"] for r in violated]})
            sys.stderr.write(wg.rules_nudge(violated))
            return 2
        if not gate_res["claimed"]:
            return allow("not_claiming_done")
        info = gate_res["detail"]
        if gate_res["skip"]:
            core.log_event({"session": session_id, "decision": "skipped_jev", "trigger": "stop_gate", "jev": info})
            return allow("gate_jev_skip")
        with core.SessionState(wire) as st:
            st.data["prescreened_turn"] = tk
            st.data["prescreened_detail"] = info
        _mark_turn_gated(core, wire, tk)
        change_desc = (
            f"modifying {evidence['mutations']} project file(s)"
            if evidence["mutations"] > 0
            else f"performing {ops_changes} state-changing operation(s)"
        )
        core.log_event({
            "session": session_id,
            "decision": "done_advisor_required",
            "mutations": evidence["mutations"],
            "ops_mutations": ops_changes,
            "jev": info,
        })
        sys.stderr.write(
            f"[kimi-advisor completion gate] You report this task as done after {change_desc}, "
            f"and Jev judged the change needs a second opinion ({jev_note(info)}). "
            "Call `ask_advisor({})` now for a final review. If the Advisor returns `Verdict: sound`, "
            "reply with a brief one-line confirmation rather than repeating your full summary."
        )
        return 2

    # Path C: checks passed, completion_gate off (or nontrivial single-file edit) -> 1 Jev call for AGENTS.md rules
    if rules_on and cfg["jev"]["enabled"] and wg.is_nontrivial_single_mutation(evidence, task_text):
        try:
            violated = wg.evaluate_turn_rules_with_jev(
                task_text, final_assistant, evidence, cwd, cfg, core, redact_fn, fact_only=False
            )
        except Exception as e:  # noqa: BLE001
            core.log_event({"session": session_id, "decision": "rules_error", "error": str(e)[:300]})
            return allow("checks_passed")
        if violated:
            _mark_turn_gated(core, wire, tk)
            core.log_event({"session": session_id, "decision": "rules_blocked", "violated": [r["name"] for r in violated]})
            sys.stderr.write(wg.rules_nudge(violated))
            return 2

    return allow("checks_passed")


if __name__ == "__main__":
    import advisor_core as core

    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        rc = main(payload)
        core.trace_hook(payload, f"rc={rc}" + (f" {EXIT_REASON['why']}" if rc == 0 and EXIT_REASON["why"] else ""))
        sys.exit(rc)
    except Exception as e:  # noqa: BLE001
        core.trace_hook(payload, f"crash {type(e).__name__}")
        core.log_event({"decision": "stop_hook_crash", "error": f"{type(e).__name__}: {e}"[:300]})
        sys.exit(0)
