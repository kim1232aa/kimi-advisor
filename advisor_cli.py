#!/usr/bin/env python3
"""kimi-advisor settings from the shell, no model involved.

Usage:
  python3 advisor_cli.py [show]                  show current settings and usage stats
  python3 advisor_cli.py pool                    show the curated model pool ([secondary_model.models])
  python3 advisor_cli.py models                  show every usable model alias in config.toml
  python3 advisor_cli.py set <key> <value>       change one setting (keys below)
  python3 advisor_cli.py reset                   revert ~/.kimi-code/advisor/config.json to defaults

Keys for `set`:
  model <alias>              advisor model alias (e.g. 9router/cc/claude-opus-5-5, avt/gemini-3.8-flash)
  effort <level>             off | low | medium | high | xhigh | max
  jev on|off                 enable/disable Jev screening
  jev-preset <preset>        strict | pi | loose (pi = pi-advisor-flow default: 0.85/0.35)
  stuck on|off               enable/disable pi-warden stuck guard (AttemptWindow + same_strategy >= 0.7)
  done on|off                enable/disable pi-warden done-check guard (mutations vs freshChecks + doneQuestions)
  rules on|off               enable/disable pi-warden AGENTS.md per-section rules guard (P(violation) >= 0.7)
  completion-gate on|off     on a done claim after file changes + passing checks, require ask_advisor if Jev says so (default off)
  same-model-skip on|off     skip consultation when executor and advisor are the same model
  context <chars>|full       max chars of conversation sent per call; full = fit to advisor window
  budget <n>|unlimited       max consultations per session
  max-output <tokens>        token cap for the advisor's reply (minimum 256, default 8192)
  redact on|off              redact credential-shaped strings before sending (default on)
  timeout <seconds>          HTTP timeout for the advisor model call (minimum 10, default 300)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advisor_core as core  # noqa: E402

KEY_MAP = {
    "model": "model",
    "effort": "effort",
    "jev": "jev_enabled",
    "jev-preset": "jev_preset",
    "stuck": "stuck_enabled",
    "done": "done_enabled",
    "rules": "rules_enabled",
    "completion-gate": "completion_gate",
    "same-model-skip": "disable_same_model",
    "context": "context_max_chars",
    "budget": "max_calls_per_session",
    "max-output": "max_output_tokens",
    "redact": "redact_secrets",
    "timeout": "timeout_s",
}


def show():
    v = core.settings_view()
    jev_note = f"{v['jev_preset']} (skip if negligible>={core.load_config()['jev']['skip_confidence']} & self>={0.5 + core.load_config()['jev']['noul_margin']:.2f})"
    print(f"config file      {core.CONFIG_PATH}")
    print(f"model            {v['model']}")
    print(f"effort           {v['effort']}")
    print(f"jev screening    {'on' if v['jev_enabled'] else 'off'} · preset {jev_note}")
    print(f"stuck guard      {'on' if v['stuck_enabled'] else 'off'} (pi-warden AttemptWindow=12, min_failures=3, cooldown=3, same_strategy>=0.7)")
    print(f"done-check       {'on' if v['done_enabled'] else 'off'} (pi-warden freshChecks + claims_done>=0.7)")
    print(f"rules guard      {'on' if v.get('rules_enabled', True) else 'off'} (pi-warden AGENTS.md per-section rules, P(violation)>=0.7)")
    print(f"completion gate  {'on' if v['completion_gate'] else 'off'} (done claim after file changes -> ask_advisor if Jev says so)")
    print(f"same-model skip  {'on' if v['disable_same_model'] else 'off'}")
    print(f"context cap      {v['context_max_chars']}")
    print(f"session budget   {v['max_calls_per_session']}")
    print(f"max output       {v['max_output_tokens']} tokens")
    print(f"redact secrets   {'on' if v['redact_secrets'] else 'off'}")
    print(f"HTTP timeout     {v['timeout_s']}s")
    s = core.stats()
    print(f"stats            consulted {s['consulted']}, skipped jev {s['skipped_jev']}, same-model {s['skipped_same_model']}, "
          f"budget {s['skipped_budget']}, stuck-steered {s['stuck_steered']} (+{s['stuck_quick_repeat']} quick-repeat), "
          f"done-blocked {s['done_blocked']}, rules-blocked {s.get('rules_blocked', 0)}, errors {s['error']}, avg {s['avg_latency_s']}s / {s['avg_input_tokens']} in-tok")
    runs = ", ".join(f"{k} {n}" for k, n in sorted(s["hook_runs"].items())) or "none recorded"
    print(f"hook runs        {runs}" + (f" (last {s['hook_last_run']})" if s["hook_last_run"] else ""))


def main(argv):
    if not argv or argv[0] in ("show", "status"):
        show()
    elif argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
    elif argv[0] == "pool":
        for m in core.model_pool():
            print(f"{m['alias']:<35} {m['note']}")
    elif argv[0] == "models":
        for m in core.list_models():
            print(f"{m['alias']:<45} {m['provider_type']:<17} efforts={','.join(m['efforts']) or '-'}")
    elif argv[0] == "set" and len(argv) == 3:
        raw_key = argv[1].lower()
        if raw_key not in KEY_MAP:
            raise SystemExit(f"unknown key {argv[1]!r}; valid keys: {', '.join(KEY_MAP)}")
        patch = core.build_settings_patch({KEY_MAP[raw_key]: argv[2]})
        core.save_config_patch(patch)
        show()
    elif argv[0] == "reset":
        if os.path.exists(core.CONFIG_PATH):
            os.remove(core.CONFIG_PATH)
        show()
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except core.AdvisorError as e:
        sys.exit(f"error: {e}")
