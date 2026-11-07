#!/usr/bin/env python3
"""PreToolUse hook:
1. When `ask_advisor` is called: records {session_id, tool_call_id, cwd, args, owner_pid} for exact session binding,
   and clears any pending stuck steer. Always exits 0.
2. When `Bash`/`Edit`/`Write` is called: checks if `on_tool_failure.py` queued a `pending_steer` for this session,
   without importing advisor_core on the common no-steer path. If so, pops it (one-shot, never loops) and exits 2
   with the steer message on stderr so the model calls `ask_advisor({})` or changes approach before retrying.
"""

import fcntl
import glob
import json
import os
import re
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_ROOT)

HOME = os.environ.get("KIMI_CODE_HOME") or os.path.expanduser("~/.kimi-code")
DATA_DIR = os.path.join(HOME, "advisor")
ADVISOR_TOOL_SUFFIX = "__ask_advisor"


def trace(payload, outcome):
    path = os.path.join(DATA_DIR, "hooks.jsonl")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        lock_path = path + ".lock"
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                if os.path.exists(path) and os.path.getsize(path) > (1 << 20):
                    try:
                        os.replace(path, path + ".1")
                    except OSError:
                        pass
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "event": payload.get("hook_event_name"),
                        "tool": payload.get("tool_name"),
                        "session": payload.get("session_id"),
                        "outcome": outcome,
                    }, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError:
        pass


def main(payload):
    tool_name = str(payload.get("tool_name") or "")
    session_id = payload.get("session_id")

    if tool_name.endswith(ADVISOR_TOOL_SUFFIX):
        import advisor_core as core

        core.record_pending(payload)
        wire = core.find_session_wire(session_id)
        if wire:
            with core.SessionState(wire) as st:
                st.data.pop("pending_steer", None)
                st.data["last_stuck_step"] = -999
        return 0

    if not session_id:
        return 0
    hits = glob.glob(os.path.join(HOME, "sessions", "*", session_id, "agents", "main", "wire.jsonl"))
    if not hits:
        return 0
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", hits[0])
    state_path = os.path.join(DATA_DIR, "state", f"{safe}.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            if "pending_steer" not in f.read():
                return 0
    except OSError:
        return 0

    import advisor_core as core

    with core.SessionState(hits[0]) as st:
        steer = st.data.pop("pending_steer", None)
    if not steer or not steer.get("message"):
        return 0
    sys.stderr.write(steer["message"])
    return 2


if __name__ == "__main__":
    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        rc = main(payload)
        trace(payload, f"rc={rc}")
        sys.exit(rc)
    except Exception as e:  # noqa: BLE001
        trace(payload, f"crash {type(e).__name__}")
        try:
            import advisor_core as core

            core.log_event({"decision": "pretool_error", "error": f"{type(e).__name__}: {e}"[:300]})
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0)
