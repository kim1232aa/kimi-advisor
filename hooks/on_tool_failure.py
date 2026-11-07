#!/usr/bin/env python3
"""PostToolUseFailure hook: runs pi-warden's AttemptWindow + quickRepeat + evaluateStuck when a tool call fails.

Observation-only in Kimi Code (always exits 0). When stuck or quick-repeat triggers, records a one-shot
`pending_steer` in the session's state file so the very next PreToolUse (`Bash`/`Edit`/`Write`) blocks once
with exit 2 and steers the executor to call `ask_advisor({})` or change approach.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(payload):
    import advisor_core as core
    import warden_guards as wg

    cfg = core.load_config()
    sc = cfg.get("stuck") or {}
    if not sc.get("enabled", True):
        return 0
    tool_name = str(payload.get("tool_name") or "")
    if tool_name.endswith(core.ADVISOR_TOOL_SUFFIX):
        return 0
    session_id = payload.get("session_id")
    wire = core.find_session_wire(session_id)
    if not wire:
        return 0

    time.sleep(0.15)
    parsed = getattr(core, "parse_wire_current_turn", core.parse_wire)(wire)
    turn_entries, tk, task_text, _ = core.current_turn_slice(parsed["entries"])
    redact_fn = core.redact if cfg.get("redact_secrets", True) else (lambda s: s)
    attempts = wg.build_attempts_since_consult(turn_entries, window_limit=sc.get("window", 12), redact_fn=redact_fn)

    # Also append the current failing call from hook payload if wire hasn't flushed its tool.result yet
    call_id = payload.get("tool_call_id") or payload.get("tool_use_id")
    flushed_ids = {e.get("id") for e in turn_entries if e.get("kind") == "tool_result" and e.get("id")}
    appended_unflushed = False
    if (call_id and call_id not in flushed_ids) or (not call_id and (not attempts or not attempts[-1]["failed"])):
        attempts.append(
            wg.make_attempt(
                tool_name,
                payload.get("tool_input") or {},
                str(payload.get("tool_output") or payload.get("error") or "tool call failed"),
                True,
                redact_fn=redact_fn,
            )
        )
        attempts = attempts[-sc.get("window", 12):]
        appended_unflushed = True
    if not attempts:
        return 0

    with core.SessionState(wire) as st:
        if st.data.get("turn_key") != tk:
            st.data["turn_key"] = tk
            st.data["quick_repeated_keys"] = []
            st.data["last_stuck_step"] = -999
            st.data.pop("pending_steer", None)

        if sc.get("repeat_steer", True):
            seen_keys = set(st.data.get("quick_repeated_keys") or [])
            rep = wg.check_quick_repeat(attempts, seen_keys)
            if rep:
                seen_keys.add(rep["key"])
                st.data["quick_repeated_keys"] = list(seen_keys)
                st.data["pending_steer"] = {"turn": tk, "kind": "quick_repeat", "message": wg.quick_repeat_nudge(rep)}
                core.log_event({"session": session_id, "decision": "stuck_quick_repeat", "call": rep["call"][:120]})
                return 0

        failures = sum(1 for a in attempts if a["failed"])
        step_now = wg.count_attempts_since_consult(turn_entries) + (1 if appended_unflushed else 0)
        cooldown = sc.get("cooldown", 3)
        if step_now < st.data.get("last_stuck_step", -999):
            st.data["last_stuck_step"] = -999
        if failures < sc.get("min_failures", 3) or (step_now - st.data.get("last_stuck_step", -999)) < cooldown:
            return 0
        st.data["last_stuck_step"] = step_now

    try:
        verdict = wg.evaluate_stuck(attempts, task_text, cfg, core.jev_judge)
    except Exception as e:  # noqa: BLE001
        core.log_event({"session": session_id, "decision": "stuck_error", "error": str(e)[:300]})
        return 0

    if not verdict.get("stuck"):
        core.log_event({"session": session_id, "decision": "stuck_pass", "failures": failures, "judgment": verdict.get("judgment")})
        return 0

    kimi_cfg = core.load_kimi_config()
    executor = parsed.get("executor_model")
    same_model = core.all_same_model(executor, cfg, kimi_cfg)
    msg = wg.stuck_nudge(verdict, can_consult_advisor=not same_model)
    with core.SessionState(wire) as st:
        st.data["pending_steer"] = {"turn": tk, "kind": "stuck", "message": msg}
    core.log_event({
        "session": session_id,
        "decision": "stuck_steered",
        "source": verdict.get("source"),
        "failures": failures,
        "reasons": verdict.get("reasons"),
        "judgment": verdict.get("judgment"),
    })
    return 0


if __name__ == "__main__":
    import advisor_core as core

    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        rc = main(payload)
        core.trace_hook(payload, f"rc={rc}")
        sys.exit(rc)
    except Exception as e:  # noqa: BLE001
        core.trace_hook(payload, f"crash {type(e).__name__}")
        core.log_event({"decision": "stuck_hook_crash", "error": f"{type(e).__name__}: {e}"[:300]})
        sys.exit(0)
