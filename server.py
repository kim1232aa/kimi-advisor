#!/usr/bin/env python3
"""kimi-advisor MCP server (stdio, JSON-RPC 2.0, stdlib only)."""

import importlib
import json
import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advisor_core as core  # noqa: E402

_CORE_PATH = os.path.abspath(core.__file__)
_core_mtime = os.path.getmtime(_CORE_PATH) if os.path.exists(_CORE_PATH) else 0
_reload_lock = threading.Lock()


def _ensure_fresh_core():
    global _core_mtime
    try:
        m = os.path.getmtime(_CORE_PATH)
        if m != _core_mtime:
            with _reload_lock:
                if m != _core_mtime:
                    importlib.reload(core)
                    _core_mtime = m
    except OSError:
        pass

SERVER_INFO = {"name": "kimi-advisor", "version": "0.2.0"}
SUPPORTED_PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
_out_lock = threading.Lock()

TOOLS = [
    {
        "name": "ask_advisor",
        "description": (
            "Escalate to a stronger reviewer model for guidance. Call it (1) when stuck — errors recurring, approach "
            "not converging, results that don't fit — and (2) before declaring done on any task longer than a few "
            "steps, tracked with TodoList, or modifying files/configs/containers/services (write deliverables and run "
            "verification first, then call ask_advisor BEFORE marking the last todo done or sending your final reply; "
            "skip only on pure read-only lookups or trivial edits). Takes no parameters: your full conversation is "
            "forwarded automatically, so do not paste context. A fast Jev screen may skip low-stakes calls; calling "
            "again in the same turn overrides that. User instructions, AGENTS.md, and active permission-mode rules "
            "always override the advisor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Only when the user asked the advisor something specific; otherwise omit."},
                "force": {"type": "boolean", "description": "Only when the user explicitly asked for an advisor review: skips screening and the budget."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "advisor_config",
        "description": (
            "Show or change kimi-advisor settings (~/.kimi-code/advisor/config.json). With no arguments it returns the "
            "current settings, the model pool (aliases with notes) and the efforts each pool model supports, plus usage "
            "stats. Pass any setting to change it; every value is validated. Only change settings when the user asks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Advisor model alias from config.toml [models]"},
                "effort": {"type": "string", "enum": ["off", "low", "medium", "high", "xhigh", "max"], "description": "off = provider default"},
                "jev_enabled": {"type": "boolean", "description": "Jev pre-screening of ask_advisor calls"},
                "jev_preset": {"type": "string", "enum": ["strict", "pi", "loose"], "description": "strict = skips fewest calls, pi = pi-advisor-flow default, loose = skips most"},
                "stuck_enabled": {"type": "boolean", "description": "pi-warden stuck guard (AttemptWindow + Jev same_strategy >= 0.7)"},
                "done_enabled": {"type": "boolean", "description": "pi-warden done-check guard (mutations vs freshChecks + Jev doneQuestions)"},
                "completion_gate": {"type": "boolean", "description": "At Stop, when the reply claims done after file changes and checks passed, ask Jev whether an advisor review is needed and require ask_advisor only if so (default false)"},
                "disable_same_model": {"type": "boolean", "description": "Skip when executor and advisor are the same model"},
                "context_max_chars": {"type": "integer", "minimum": 0, "description": "Max chars of conversation sent per call; 0 = full (fit to the advisor's window)"},
                "max_calls_per_session": {"type": "integer", "minimum": 0, "description": "0 = unlimited"},
                "max_output_tokens": {"type": "integer", "minimum": 256},
                "redact_secrets": {"type": "boolean", "description": "Redact credential-shaped strings before sending"},
                "timeout_s": {"type": "integer", "minimum": 10},
                "list_models": {"type": "boolean", "description": "Also list every usable alias in config.toml, not just the pool"},
            },
            "additionalProperties": False,
        },
    },
]


def send(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def tool_ask_advisor(args):
    rec = core.claim_pending(args)
    if rec:
        wire = core.locate_wire(rec["session_id"], rec["tool_call_id"])
        return core.consult(args, wire_path=wire, call_id=rec["tool_call_id"], binding="hook")
    wire, call_id = core.scan_pending_call(args)
    return core.consult(args, wire_path=wire, call_id=call_id, binding="scan" if wire else "none")


def tool_advisor_config(args):
    changes = {k: v for k, v in args.items() if k in core.SETTING_KEYS}
    patch = core.build_settings_patch(changes)
    cfg = core.save_config_patch(patch) if patch else core.load_config()
    efforts = {m["alias"]: m["efforts"] for m in core.list_models()}
    pool = [dict(m, efforts=efforts.get(m["alias"], [])) for m in core.model_pool()]
    out = {
        "config_path": core.CONFIG_PATH,
        "changed": sorted(changes),
        "settings": core.settings_view(cfg),
        "current_model_efforts": efforts.get(core.configured_models(cfg)[0], []),
        "model_pool": pool,
        "jev_presets": core.JEV_PRESETS,
        "stats": core.stats(),
    }
    if args.get("list_models"):
        out["all_models"] = core.list_models()
    return json.dumps(out, ensure_ascii=False, indent=2), False


HANDLERS = {"ask_advisor": tool_ask_advisor, "advisor_config": tool_advisor_config}


def handle_call(msg_id, params):
    _ensure_fresh_core()
    name = params.get("name")
    args = params.get("arguments") or {}
    try:
        handler = HANDLERS.get(name)
        if not handler:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
            return
        text, is_error = handler(args)
        send({"jsonrpc": "2.0", "id": msg_id, "result": text_result(text, is_error)})
    except core.AdvisorError as e:
        send({"jsonrpc": "2.0", "id": msg_id, "result": text_result(f"{name} failed: {e}", True)})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        send({"jsonrpc": "2.0", "id": msg_id, "result": text_result(f"{name} crashed: {type(e).__name__}: {e}", True)})


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[-1]
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        threading.Thread(target=handle_call, args=(msg_id, msg.get("params") or {}), daemon=True).start()
    elif msg_id is not None:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        handle(msg)


if __name__ == "__main__":
    main()
