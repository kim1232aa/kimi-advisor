#!/usr/bin/env python3
"""Direct port of pi-warden v2.4.0 `stuck.js` + `evidence.js` + `done.js` for Kimi Code.

- Stuck guard (`AttemptWindow`, `quickRepeat`, `evaluateStuck`, `buildStuckEvidence`):
  12-attempt sliding window per turn; offline exact-repeat & quick-repeat detection; Jev `stuckQuestions`
  (`same_strategy >= 0.7`, `approach_change`, `progress`) when `latest.failed` and `failures >= 3` with `cooldown >= 3`.
- Done-check (`classifyToolResult`, `freshChecks`, `needsDoneCheck`, `evaluateDone`):
  tracks project file mutations (`Edit`/`Write`) vs test/build/lint check runs (`check-pass`/`check-fail`) after the
  last mutation; when `needsDoneCheck` holds at turn end, Jev `doneQuestions` (`claims_done >= 0.7`,
  `verification_applies >= 0.5`, `claims_verified`, `outcome`) decides whether an unverified "done" must be sent back.
"""

import hashlib
import json
import os
import re

# ---------------------------------------------------------------- shared helpers

READ_TOOLS = {"read", "grep", "glob", "find", "ls"}
EDIT_TOOLS = {"edit", "write"}

POLL_COMMAND = re.compile(
    r"\b(?:sleep|watch|wait)\b|\bgh\s+(?:run|pr)\s+(?:watch|checks|view|list)\b"
    r"|\bgit\s+status\b|\btail\s+-[fF]\b|\b(?:ps|pgrep)\b"
)

READ_ONLY_HEADS = {
    "cat", "head", "tail", "less", "more", "wc", "sort", "uniq", "cut", "tr", "diff",
    "ls", "pwd", "stat", "file", "du", "df", "tree", "which", "type", "whereis",
    "ps", "top", "env", "printenv", "date", "uname", "whoami", "id", "hostname",
    "grep", "egrep", "fgrep", "rg", "ag", "fd", "jq", "yq",
}
READ_ONLY_GIT = {"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-remote", "describe", "shortlog", "blame"}

OUTPUT_FAILURE_RE = re.compile(
    r"(?:^|\n)\s*Command (?:failed|exited) with (?:exit )?code:? [1-9]\d*\b"
    r"|(?:^|\n)\s*\(timed out\)\s*$",
    re.I,
)


def output_reports_failure(text):
    return bool(OUTPUT_FAILURE_RE.search(text or ""))


def result_failed(is_error, text=""):
    return bool(is_error) or output_reports_failure(text)


def _head(text, limit):
    return text if len(text) <= limit else f"{text[:limit]}… [{len(text) - limit} more chars]"


def _tail(text, limit):
    return text if len(text) <= limit else f"[{len(text) - limit} earlier chars] …{text[-limit:]}"


def _head_tail(text, h, t):
    if len(text) <= h + t:
        return text
    return f"{text[:h]}… [{len(text) - h - t} middle chars] …{text[-t:]}"


def normalise_output(text):
    """pi-warden stuck.js normaliseOutput: strip durations, hex addresses, 5+ digit numbers, timestamps."""
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|s|m|h|µs|us|ns)\b", "#t", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "0x#", text)
    text = re.sub(r"\d{5,}", "#", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?", "#date", text)
    return text


def is_read_only_command(command):
    """Conservative subset of pi-warden guard.js isReadOnlyCommand."""
    cmd = (command or "").strip()
    if not cmd or re.search(r"\$\(|`|<\(", cmd):
        return False
    stripped = re.sub(r"\d?>\s*&\s*\d", "", cmd)
    stripped = re.sub(r"&?\d?>\s*/dev/null", "", stripped)
    if ">" in stripped:
        return False
    for seg in re.split(r"\n|;|&&|\|\||\||&", stripped):
        tokens = seg.strip().split()
        idx = 0
        while idx < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[idx]):
            idx += 1
        if idx >= len(tokens):
            continue
        head = os.path.basename(tokens[idx])
        rest = " ".join(tokens[idx + 1:])
        if head == "git":
            sub = tokens[idx + 1] if idx + 1 < len(tokens) else ""
            if not sub or "--output" in rest:
                return False
            if sub in READ_ONLY_GIT:
                continue
            if sub == "branch" and not re.search(r"\s-[a-zA-Z]*[dDmMcCu]|--(?:delete|move|copy|set-upstream)", " " + rest):
                continue
            if sub == "remote" and all(t.startswith("-") for t in tokens[idx + 2:]):
                continue
            return False
        if head == "docker":
            sub = tokens[idx + 1] if idx + 1 < len(tokens) else ""
            if sub in ("ps", "images", "inspect", "logs", "version", "info", "stats", "top", "port"):
                continue
            return False
        if head == "find" and re.search(r"-(?:delete|exec\w*|ok\w*|fprint\w*|fls)\b", seg):
            return False
        if head == "sed":
            if re.search(r"(?:^|\s)-(?:[a-zA-Z]*i|--in-place)\b", " " + rest):
                return False
            continue
        if head not in READ_ONLY_HEADS:
            return False
    return True


def is_outside_project(input_args, cwd):
    """pi-warden done.js isOutsideProject + /tmp scratch exclusion."""
    if not isinstance(input_args, dict):
        return False
    path = input_args.get("path") or input_args.get("file_path")
    if not isinstance(path, str) or not path.strip():
        return False
    base = os.path.realpath(cwd or "/root")
    target = os.path.realpath(os.path.join(base, os.path.expanduser(path)))
    if not base.startswith("/tmp") and (target == "/tmp" or target.startswith("/tmp/")):
        return True
    rel = os.path.relpath(target, base)
    return rel == ".." or rel.startswith("../") or os.path.isabs(rel)


# ---------------------------------------------------------------- pi-warden done.js: runner & check classification

PM_SCRIPT = r"(?:npm|pnpm|yarn|bun)(?:\s+-{1,2}[^\s]+(?:\s+[^\s]+)?)*\s+(?:run\s+(?:test|check|lint|typecheck|build|verify|ci)|(?:test|check|lint|typecheck|build|verify))\b"
YARN_WORKSPACE = r"yarn\s+workspace\s+\S+\s+(?:run\s+)?(?:test|check|lint|typecheck|build|verify|ci)\b"
MAKE_TARGET = r"make(?:\s+-[^\s]+(?:\s+[^\s]+)?)*\s+(?:test|check|lint|build)\b"
PY_COMPILE = r"python[\d.]*\s+-m\s+(?:py_compile|compileall|unittest|pytest|mypy|ruff|flake8|pylint|black\s+--check)\b"
RUNNER_SOURCE = (
    f"{PM_SCRIPT}|{YARN_WORKSPACE}|{PY_COMPILE}"
    r"|(?:npx|pnpm|bunx)\s+(?:tsc|jest|vitest|mocha|eslint|biome|prettier\s+--check)\b"
    r"|pytest|jest|vitest|mocha|tsc|eslint|biome\s+check|ruff|mypy|flake8|pylint|black\s+--check"
    r"|cargo\s+(?:test|check|build|clippy)|go\s+(?:test|vet|build)"
    f"|{MAKE_TARGET}"
    r"|mvn\s+(?:test|verify)|gradle\w*\s+(?:test|check|build)|dotnet\s+(?:test|build)"
    r"|node\s+(?:--test|-c|--check)|deno\s+(?:test|check|lint)|rspec|rake\s+test|mix\s+test"
    r"|phpunit|swift\s+(?:test|build)|xcodebuild\s+test|ctest|zig\s+(?:test|build)|unittest"
)
CHECK_COMMAND_RE = re.compile(rf"\b(?:{RUNNER_SOURCE})\b")
RUNNER_START_RE = re.compile(rf"^(?:[^\s;&|()]*/)?(?:{RUNNER_SOURCE})\b")
WRAPPER_HEAD_RE = re.compile(
    r"^(?:timeout(?:\s+(?:-s|--signal|-k|--kill-after)(?:=|\s+)\S+)*(?:\s+-{1,2}\S+)*\s+\S+"
    r"|time(?:\s+-p)?|env(?:\s+-{1,2}\S+)*|npx(?:\s+-{1,2}[^\s]+)*|bunx(?:\s+-{1,2}[^\s]+)*"
    r"|pnpm\s+exec|yarn|uv\s+run(?:\s+--with\s+\S+|\s+-{1,2}[^\s]+)*|poetry\s+run"
    r"|(?:[^\s;&|()]*/)?python[\d.]*\s+-m)\s+"
)
ENV_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
SHELL_C_RE = re.compile(r"^(?:bash|sh|zsh|dash|ksh|fish)\s+-[a-zA-Z]*c\s+(?:'([\s\S]*)'|\"([\s\S]*)\")\s*$")
ZERO_TESTS_RE = re.compile(
    r"Tests:\s+0 total\b|No tests found|\[no tests to run\]|running 0 tests\b|\u2139 tests 0\b|no tests ran|Ran 0 tests\b",
    re.I,
)
SILENT_RUNNER_RE = re.compile(r"\b(?:tsc|eslint|flake8|py_compile|node\s+(?:-c|--check))\b|\bgo\s+(?:vet|build)\b")
PASS_THROUGH = {"tail", "head", "tee", "cat"}
GENERIC_FAILURE_RE = re.compile(
    r"^\s*(?:make(?:\[\d+\])?:\s+\*\*\*.*Error \d+|npm error (?:Lifecycle script .* failed|code ELIFECYCLE)"
    r"|npm ERR! code ELIFECYCLE|ELIFECYCLE\s+Command failed with exit code \d+|error Command failed with exit code \d+)",
    re.M,
)
SEPARATORS_RE = re.compile(r"\n|;|&&|\|\||\||(?<!>)&")


def _strip_heredocs_and_echo_data(command):
    """Strip non-executed heredoc bodies so runner names inside data don't masquerade as checks."""
    cmd = re.sub(r"\\\r?\n", "", command or "")
    cmd = re.sub(
        r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?[^\n]*\n[\s\S]*?\n\1\b",
        lambda m: m.group(0).split("\n", 1)[0] + "\n[heredoc]",
        cmd,
    )
    return cmd


def _command_pieces(command):
    pieces = []
    start = 0
    for m in SEPARATORS_RE.finditer(command):
        idx = m.start()
        pieces.append({"text": command[start:idx], "sep": m.group(0), "next": ""})
        start = m.end()
    pieces.append({"text": command[start:], "sep": None, "next": ""})
    for i in range(len(pieces)):
        pieces[i]["next"] = pieces[i + 1]["text"] if i + 1 < len(pieces) else ""
    return pieces


def _starts_runner(segment, depth=0):
    rest = re.sub(r"^\(+|\)+$", "", segment or "").strip()
    while True:
        rest = ENV_ASSIGN_RE.sub("", rest).lstrip()
        if RUNNER_START_RE.search(rest):
            return True
        if depth < 4:
            m = SHELL_C_RE.match(rest)
            inner = (m.group(1) or m.group(2)) if m else None
            if inner is not None and any(_starts_runner(p["text"], depth + 1) for p in _command_pieces(inner)):
                return True
        w = WRAPPER_HEAD_RE.match(rest)
        if not w:
            return False
        rest = rest[w.end():]


def _hidden_cause(pieces, index, command):
    own = pieces[index]
    if own["sep"] == "&":
        return {"sep": "&", "after": own["next"]}
    if own["sep"] == "|" and not re.search(r"\bpipefail\b", command):
        return {"sep": "|", "after": own["next"]}
    fail_fast = bool(re.search(r"(?:^|\n|\s)set\s+(?:-[a-zA-Z]*e[a-zA-Z]*|-o\s+errexit)\b", command))
    for at in range(index, len(pieces)):
        sep = pieces[at]["sep"]
        if sep == "||":
            return {"sep": "||", "after": pieces[at]["next"]}
        if sep in (";", "\n") and any(p["text"].strip() for p in pieces[at + 1:]):
            if sep == "\n" and fail_fast:
                continue
            after_nonempty = [p["text"].strip() for p in pieces[at + 1:] if p["text"].strip()]
            if len(after_nonempty) == 1 and re.match(r"^(?:echo|printf)\b.*(?:\$\?|PIPESTATUS)", after_nonempty[0]):
                return None
            return {"sep": ";", "after": pieces[at]["next"]}
    return None


def _runner_segments(command):
    pieces = _command_pieces(command)
    runners = []
    for idx, piece in enumerate(pieces):
        if _starts_runner(piece["text"]):
            runners.append({"text": piece["text"], "hides": _hidden_cause(pieces, idx, command)})
    return runners


def _ran_no_tests(output):
    tail = (output or "")[-6000:]
    if not ZERO_TESTS_RE.search(tail):
        return False
    for line in (output or "").splitlines():
        if re.match(r"^\s*running [1-9]\d* tests?\b", line) or (re.match(r"^\s*ok\s", line) and "[no tests to run]" not in line):
            return False
    return True


def _silent_output(output):
    lines = [ln.strip() for ln in (output or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("> ") and not re.match(r"^\(no output\)$", ln, re.I)
             and ln != "Command executed successfully."]
    return len(lines) == 0


def _silent_runner_pass(runner, output):
    hides = runner.get("hides")
    if not hides or hides.get("sep") != "|" or output is None:
        return False
    head = (hides.get("after") or "").strip().split()[0].lstrip("-") if (hides.get("after") or "").strip() else ""
    return head in PASS_THROUGH and bool(SILENT_RUNNER_RE.search(runner["text"])) and _silent_output(output)


def summary_failure(tail, command=""):
    for m in re.finditer(r"\u2139 fail (\d+)", tail):
        if int(m.group(1)) > 0:
            return True
    if re.search(r"^Tests:\s+[^\n]*?\b[1-9]\d* failed\b", tail, re.M):
        return True
    if re.search(r"\b[1-9]\d* (?:failed|errors?)\b[^\n]*\bin [\d.]+s\b", tail):
        return True
    if re.search(r"^FAILED\s*\((?:failures|errors)=\d+", tail, re.M):
        return True
    if re.search(r"^test result: FAILED\.", tail, re.M) or re.search(r"^FAIL\b", tail, re.M) or re.search(r"^--- FAIL:", tail, re.M):
        return True
    if re.search(r"\berror TS\d{4}:", tail):
        return True
    if re.search(r"^\s*(?:\S+:\s*)?(?:Tests|Test Files)\s+[^\n]*?\b[1-9]\d* failed\b", tail, re.M):
        return True
    if re.search(r"^Found [1-9]\d* errors?\b(?![^\n]*\(\d+ fixed, 0 remaining\))", tail, re.M):
        return True
    if re.search(r"\b(?:Would|would) reformat\b|\b\d+ files? would be reformatted\b|Code style issues found", tail):
        return True
    eslint = re.search(r"\u2716 (\d+) problems? \((\d+) errors?, (\d+) warnings?\)", tail)
    if eslint and "--max-warnings" not in command and int(eslint.group(2)) > 0:
        return True
    if re.search(r"^\s*[1-9]\d* (?:failing|failed|fail)\s*$", tail, re.M) or "Some tests failed." in tail:
        return True
    deno = re.search(r"\b(?:ok|FAILED)\s*\|[^\n]*?(\d+) passed\s*\|\s*(\d+) failed", tail)
    if deno and int(deno.group(2)) > 0:
        return True
    if re.search(r"error: could not compile|^SyntaxError:|^IndentationError:", tail, re.M):
        return True
    return False


def summary_pass(tail, command=""):
    if re.search(r"\u2139 (?:tests|pass|fail) \d+", tail):
        return True
    if re.search(r"^Tests:\s+[^\n]*?\d+ total", tail, re.M):
        return True
    if re.search(r"^=+ \d+ passed.*? in [\d.]+s =+$", tail, re.M) or re.search(r"^\d+ passed\b.* in [\d.]+s", tail, re.M):
        return True
    if re.search(r"^Ran [1-9]\d* tests? in [\d.]+s\s*\n\s*OK\b", tail, re.M):
        return True
    if re.search(r"^test result: ok\.", tail, re.M) or re.search(r"^ok\s+\S+\s+[\d.]+s$", tail, re.M):
        return True
    if re.search(r"^\s*(?:\S+:\s*)?(?:Tests|Test Files)\s+[^\n]*?\b\d+ passed\b", tail, re.M):
        return True
    if re.search(r"^Success: no issues found\b|^All checks passed!|All matched files use Prettier code style!", tail, re.M):
        return True
    if re.search(r"^Found \d+ errors? \(\d+ fixed, 0 remaining\)", tail, re.M):
        return True
    if re.search(r"\b\d+ files? (?:already formatted|would be left unchanged\.)|^Checked \d+ files?\b.*No fixes applied\.", tail, re.M):
        return True
    eslint = re.search(r"\u2716 (\d+) problems? \((\d+) errors?, (\d+) warnings?\)", tail)
    if eslint and "--max-warnings" not in command and int(eslint.group(2)) == 0:
        return True
    if re.search(r"^\s*\d+ (?:passing|passed) \(", tail, re.M) or "All tests passed!" in tail:
        return True
    if re.search(r"^\s*\d+ pass\s*$|^Ran \d+ tests? across \d+ files?\.", tail, re.M):
        return True
    deno = re.search(r"\b(?:ok|FAILED)\s*\|[^\n]*?(\d+) passed\s*\|\s*(\d+) failed", tail)
    if deno and int(deno.group(2)) == 0:
        return True
    if re.search(r"^\s*Finished\b.*\btargets?\b.*\bin \d+(?:\.\d+)?s\b", tail, re.M) and re.search(r"\bcargo\s+(?:build|check|clippy)\b", command):
        return True
    return False


def check_summary(output, command=""):
    tail = (output or "")[-6000:]
    if summary_failure(tail, command):
        return "fail"
    return "pass" if summary_pass(tail, command) else None


def classify_tool_result(tool_name, input_args, is_error, output, cwd=None):
    """pi-warden done.js classifyToolResult: returns 'mutation' | 'check-pass' | 'check-fail' | 'read' | 'unknown'."""
    tool = (tool_name or "").lower()
    args = input_args if isinstance(input_args, dict) else {}
    failed = result_failed(is_error, output)
    if tool in EDIT_TOOLS:
        return "unknown" if (cwd and is_outside_project(args, cwd)) else "mutation"
    if tool in READ_TOOLS:
        return "read"
    if tool != "bash":
        return "unknown"
    raw_cmd = args.get("command") if isinstance(args.get("command"), str) else ""
    if not raw_cmd:
        return "unknown"
    command = _strip_heredocs_and_echo_data(raw_cmd)
    summary = check_summary(output, command) if output is not None else None
    runners = _runner_segments(command)
    if runners or summary is not None:
        if output is not None and _ran_no_tests(output):
            return "unknown"
        generic = bool(runners and output is not None and GENERIC_FAILURE_RE.search(output[-6000:]))
        verdict = "fail" if generic else summary
        some_visible = any(r["hides"] is None or _silent_runner_pass(r, output) for r in runners)
        all_covered = all(verdict is not None or r["hides"] is None or _silent_runner_pass(r, output) for r in runners)
        hidden = next((r["hides"] for r in runners if r["hides"] is not None), None)
        if hidden is not None and verdict is None and not all_covered:
            return "check-fail" if (failed and some_visible) else "unknown"
        return "check-fail" if (verdict == "fail" or failed) else "check-pass"
    return "read" if is_read_only_command(raw_cmd) else "unknown"


OPS_MUTATION_CMD_RE = re.compile(
    r"(?:^|[;&|]\s*|\s&&\s*|\n\s*)(?:sudo\s+)?"
    r"(?:"
    r"docker\s+(?:build|tag|push|rm|rmi|stop|start|restart|kill|commit|update|compose\s+(?:[^\n;&|]*\s+)?(?:up|down|restart|start|stop|rm|build|pull|push))\b"
    r"|docker-compose\s+(?:[^\n;&|]*\s+)?(?:up|down|restart|start|stop|rm|build|pull|push)\b"
    r"|(?:systemctl|service|supervisorctl|pm2|1pctl)\s+(?:start|stop|restart|reload|enable|disable|delete)\b"
    r"|git\s+(?:commit|push|merge|rebase|cherry-pick|reset|revert|tag|am|apply)\b"
    r"|sed\s+-[a-zA-Z]*i\b"
    r")",
    re.I,
)


def is_ops_mutation(tool_name, input_args, is_error, output):
    """True when a tool call mutates task/container/service/git state outside Edit/Write."""
    if result_failed(is_error, output):
        return False
    tool = (tool_name or "").lower()
    args = input_args if isinstance(input_args, dict) else {}
    if tool == "todolist":
        todos = args.get("todos")
        return bool(isinstance(todos, list) and todos and all(isinstance(t, dict) and t.get("status") == "done" for t in todos))
    if tool != "bash":
        return False
    raw_cmd = args.get("command") if isinstance(args.get("command"), str) else ""
    if not raw_cmd or is_read_only_command(raw_cmd):
        return False
    return bool(OPS_MUTATION_CMD_RE.search(_strip_heredocs_and_echo_data(raw_cmd)))


def collect_turn_evidence(turn_entries, cwd=None, redact_fn=None):
    """Build pi-warden done.js Evidence across the turn's tool_call/tool_result pairs."""
    redact_fn = redact_fn or (lambda s: s)
    evidence = {"mutations": 0, "ops_mutations": 0, "checks": [], "checks_before_mutation": 0}
    pending = {}
    for e in turn_entries:
        if e["kind"] == "tool_call":
            pending[e.get("id")] = e
        elif e["kind"] == "tool_result":
            call = pending.pop(e.get("id"), None)
            if not call:
                continue
            try:
                args = json.loads(call["text"]) if isinstance(call.get("text"), str) else (call.get("text") or {})
            except json.JSONDecodeError:
                args = {}
            outcome = classify_tool_result(call["name"], args, e.get("error"), e.get("text") or "", cwd)
            if outcome == "mutation":
                evidence["mutations"] += 1
                evidence["checks_before_mutation"] = len(evidence["checks"])
            elif outcome in ("check-pass", "check-fail"):
                cmd = args.get("command") if isinstance(args, dict) else ""
                label = redact_fn(cmd[:200] + ("…" if len(cmd) > 200 else "")) if cmd else "check"
                evidence["checks"].append({"call": label, "passed": outcome == "check-pass"})
            if is_ops_mutation(call["name"], args, e.get("error"), e.get("text") or ""):
                evidence["ops_mutations"] += 1
    return evidence


def fresh_checks(evidence):
    return evidence["checks"][evidence.get("checks_before_mutation", 0):]


def needs_done_check(evidence):
    """pi-warden done.js needsDoneCheck: True only when project files mutated and no check passed since the last mutation."""
    return evidence["mutations"] > 0 and not any(c["passed"] for c in fresh_checks(evidence))


DONE_QUESTIONS = [
    {
        "id": "claims_done",
        "type": "noul",
        "question": "Does `final_message` present the requested work as finished or working?",
        "criteria": {
            "true": "Yes: it says the task is done, fixed, implemented, complete, or working, or summarises the result as final.",
            "false": "No: it reports partial progress, names remaining work, reports a blocker, asks the user a question, or only describes a plan.",
        },
    },
    {
        "id": "claims_verified",
        "type": "noul",
        "question": "Does `final_message` claim that tests, a build, or other checks were run and passed?",
    },
    {
        "id": "verification_applies",
        "type": "noul",
        "question": "Would running the project's tests, build, or lint be a meaningful way to check the work that `task` asks for?",
        "criteria": {
            "true": "Yes: `task` changes or adds code, configuration, or build logic that such checks exercise.",
            "false": "No: `task` is about documentation, prose, file housekeeping, deleting or moving files, answering a question, or something the project's checks would not cover.",
        },
    },
    {
        "id": "outcome",
        "type": "choice",
        "question": "What does `final_message` report about `task`?",
        "options": {
            "complete": "The work is finished",
            "partial": "Progress was made and remaining work is named",
            "blocked": "A blocker is reported or the user is asked something",
            "other": "None of these",
        },
    },
]


def evaluate_done_with_jev(task, final_message, evidence, cfg, jev_judge_fn, redact_fn):
    """pi-warden done.js evaluateDone: returns dict with unverified, false_claim, reasons, judgment."""
    state = {
        "task": (task.strip()[:1500] + ("…" if len(task.strip()) > 1500 else "")) if task and task.strip() else "(no user request recorded)",
        "final_message": redact_fn(final_message[:2000] + ("…" if len(final_message) > 2000 else "")),
        "run": {
            "file_changes": evidence["mutations"],
            "checks_run": [f"{c['call']} -> {'passed' if c['passed'] else 'failed'}" for c in fresh_checks(evidence)],
        },
    }
    verdicts, res = jev_judge_fn(state, DONE_QUESTIONS, cfg)
    claims_done = (verdicts.get("claims_done") or {}).get("answer")
    claims_verified = (verdicts.get("claims_verified") or {}).get("answer")
    verification_applies = (verdicts.get("verification_applies") or {}).get("answer")
    outcome = (verdicts.get("outcome") or {}).get("answer")
    if not isinstance(claims_done, (int, float)) or not isinstance(verification_applies, (int, float)):
        return {"unverified": False, "claimed": False, "false_claim": False, "reasons": []}
    claims_verified = float(claims_verified) if isinstance(claims_verified, (int, float)) else 0.0
    threshold = (cfg.get("done") or {}).get("claims_done", 0.7)
    claimed = claims_done >= threshold and outcome != "blocked"
    checks = fresh_checks(evidence)
    code_unverified = (
        claimed
        and evidence["mutations"] > 0
        and not any(c["passed"] for c in checks)
        and verification_applies >= 0.5
    )
    false_claim = code_unverified and claims_verified >= 0.7 and len(evidence["checks"]) == 0
    reasons = []
    if code_unverified:
        failed_cnt = sum(1 for c in checks if not c["passed"])
        suffix = (
            f"{failed_cnt} failed check{'s' if failed_cnt != 1 else ''} and no passing one"
            if failed_cnt
            else "no test, build, or lint run since the last change"
        )
        reasons.append(
            f"reports completion ({claims_done:.2f}) after {evidence['mutations']} file "
            f"change{'s' if evidence['mutations'] != 1 else ''} with {suffix}"
        )
    if false_claim:
        reasons.append(f"claims checks passed ({claims_verified:.2f}) but none ran")
    return {
        "unverified": code_unverified,
        "claimed": claimed,
        "false_claim": false_claim,
        "reasons": reasons,
        "failed_checks": [c["call"] for c in checks if not c["passed"]],
        "judgment": {
            "claims_done": claims_done,
            "claims_verified": claims_verified,
            "verification_applies": verification_applies,
            "outcome": outcome,
            "latency_ms": res.get("latencyMs"),
        },
    }


def evaluate_stop_gate_batched(task, final_message, evidence, entries, cfg, core, redact_fn):
    """Single batched Jev call for Stop completion_gate: evaluates claims_done + stakes/self_answerable in one pass."""
    digest = core.render_transcript(entries, cfg["jev"]["digest_max_chars"], caps=core.DIGEST_CAPS, keep_first_user=False)
    state = {
        "role": "executor",
        "task": (task.strip()[:1500] + ("…" if len(task.strip()) > 1500 else "")) if task and task.strip() else "(no user request recorded)",
        "final_message": redact_fn(final_message[:2000] + ("…" if len(final_message) > 2000 else "")),
        "run": {
            "file_changes": evidence["mutations"],
            "ops_changes": evidence.get("ops_mutations", 0),
            "checks_run": [f"{c['call']} -> {'passed' if c['passed'] else 'failed'}" for c in fresh_checks(evidence)],
        },
        "recent_conversation": redact_fn(digest) if digest else "",
    }
    questions = [DON for DON in DONE_QUESTIONS if DON["id"] in ("claims_done", "outcome")] + list(core.SCREENING_QUESTIONS)
    verdicts, res = core.jev_judge(state, questions, cfg)
    claims_done = (verdicts.get("claims_done") or {}).get("answer")
    outcome = (verdicts.get("outcome") or {}).get("answer")
    negligible = ((verdicts.get("stakes") or {}).get("distribution") or {}).get("0")
    p_self = (verdicts.get("self_answerable") or {}).get("answer")
    if not isinstance(claims_done, (int, float)) or not isinstance(negligible, (int, float)) or not isinstance(p_self, (int, float)):
        raise core.JevError(f"malformed batched Jev verdicts: {json.dumps(verdicts)[:300]}")
    threshold = (cfg.get("done") or {}).get("claims_done", 0.7)
    claimed = claims_done >= threshold and outcome != "blocked"
    jc = cfg["jev"]
    skip = negligible >= jc["skip_confidence"] and p_self >= 0.5 + jc["noul_margin"]
    return {
        "claimed": claimed,
        "skip": skip,
        "detail": {
            "negligible": negligible,
            "self_answerable": p_self,
            "claims_done": claims_done,
            "latency_ms": res.get("latencyMs"),
        },
    }


def _package_manager(root):
    for lock, pm in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
    ):
        if os.path.exists(os.path.join(root, lock)):
            return pm
    return "npm"


def project_check_command(cwd):
    """Detect the project's own check/test command under cwd.

    Port of pi-warden done.ts projectCheckCommand, extended with pytest, cargo, and go.
    """
    if not cwd or not os.path.isdir(cwd):
        return None

    # package.json (check / test script)
    pkg_path = os.path.join(cwd, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            scripts = data.get("scripts") if isinstance(data, dict) else None
            if isinstance(scripts, dict):
                pm = _package_manager(cwd)
                for name in ("check", "test"):
                    if isinstance(scripts.get(name), str):
                        return f"{pm} run {name}"
        except Exception:
            pass

    # Makefile (check / test target)
    makefile_path = os.path.join(cwd, "Makefile")
    if os.path.exists(makefile_path):
        try:
            with open(makefile_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            for name in ("check", "test"):
                if re.search(rf"^{name}\s*:", content, re.MULTILINE):
                    return f"make {name}"
        except Exception:
            pass

    # pyproject.toml or pytest.ini
    if os.path.exists(os.path.join(cwd, "pyproject.toml")) or os.path.exists(os.path.join(cwd, "pytest.ini")):
        return "pytest"

    # Cargo.toml
    if os.path.exists(os.path.join(cwd, "Cargo.toml")):
        return "cargo test"

    # go.mod
    if os.path.exists(os.path.join(cwd, "go.mod")):
        return "go test ./..."

    return None


def done_nudge(verdict, cwd=None):
    """pi-warden done.js doneNudge."""
    failed = verdict.get("failed_checks") or []
    if failed:
        detail = f"The last check that ran failed: {failed[-1]}. Fix that first."
    else:
        cmd = project_check_command(cwd) if cwd else None
        if cmd:
            detail = f"Run `{cmd}` on what you changed."
        else:
            detail = "Run the project's tests, build, or syntax/lint check on what you changed."
    return (
        f"[kimi-advisor done-check] {'; '.join(verdict['reasons'])}. {detail} "
        "Then report the actual result. "
        "If no check exists or can run, state explicitly that it is unverified instead of presenting the work as done."
    )


# ---------------------------------------------------------------- pi-warden stuck.js + evidence.js

STUCK_QUESTIONS = [
    {
        "id": "same_strategy",
        "type": "noul",
        "question": "Do the failed entries in `attempts` repeat the same strategy with only superficial variation, instead of trying a different approach to the failure?",
        "criteria": {
            "true": "Yes: the same command or edit is retried, or only flags, paths, names, or wording change while the underlying idea stays the same.",
            "false": "No: later attempts use a different tool, test a new hypothesis, gather new information first, or act on what the earlier output said.",
        },
    },
    {
        "id": "approach_change",
        "type": "score",
        "question": "How much did the approach change across `attempts`?",
        "levels": [
            "Identical or near-identical retries",
            "Cosmetic changes: different flags, paths, or wording, same underlying idea",
            "Meaningfully different: a new hypothesis, tool, or information-gathering step",
        ],
    },
    {
        "id": "progress",
        "type": "noul",
        "question": "Do the later entries in `attempts` show progress toward resolving the failure seen in the earlier ones, such as a different error, a partial success, or new information?",
    },
]


def _edit_diff(tool, args):
    path = str((args or {}).get("path") or "?")
    if tool == "write":
        lines = str((args or {}).get("content") or "").splitlines()
        txt = "\n".join(f"+{ln}" for ln in lines)
        return {"path": path, "added": len(lines), "removed": 0, "diff": txt[:600] + ("… [diff capped]" if len(txt) > 600 else "")}
    old_t = str((args or {}).get("old_string") or (args or {}).get("oldText") or "")
    new_t = str((args or {}).get("new_string") or (args or {}).get("newText") or "")
    old_lines = old_t.splitlines()
    new_lines = new_t.splitlines()
    txt = "\n".join([*(f"-{ln}" for ln in old_lines[:15]), *(f"+{ln}" for ln in new_lines[:15])])
    return {"path": path, "added": len(new_lines), "removed": len(old_lines), "diff": txt[:600] + ("… [diff capped]" if len(txt) > 600 else "")}


def make_attempt(tool_name, input_args, output_text, is_error, redact_fn=None):
    """pi-warden stuck.js makeAttempt."""
    redact_fn = redact_fn or (lambda s: s)
    tool = (tool_name or "").lower()
    args = input_args if isinstance(input_args, dict) else {}
    cmd = args.get("command") if tool == "bash" and isinstance(args.get("command"), str) else None
    ro_cmd = bool(cmd and is_read_only_command(cmd))
    call_str = cmd if cmd is not None else (f"{tool} {args['path']}" if isinstance(args.get("path"), str) else json.dumps(args, sort_keys=True, ensure_ascii=False))
    text = (output_text or "").strip()
    failed = result_failed(is_error, text)
    key = hashlib.sha1((tool + "\0" + json.dumps(args, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    out_key = hashlib.sha1(normalise_output(text).encode()).hexdigest()
    change = _edit_diff(tool, args) if tool in EDIT_TOOLS else None
    if change:
        change = {"path": redact_fn(change["path"]), "added": change["added"], "removed": change["removed"], "diff": redact_fn(change["diff"])}
    return {
        "tool": tool,
        "key": key,
        "outputKey": out_key,
        "call": redact_fn(_head(call_str, 300)),
        "failed": failed,
        "text": text,
        "change": change,
        "output": redact_fn(_tail(text, 400)),
        "changes": (tool not in READ_TOOLS) and not ro_cmd,
        "readOnly": tool == "read" or ro_cmd,
        "poll": bool(cmd and POLL_COMMAND.search(cmd)),
    }


def _error_line(output):
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip() and not re.match(r"^\[\d+ earlier chars\]", ln.strip())]
    for cand in lines:
        if re.search(r"error|fail|denied|not found|no such|enoent|cannot|invalid|exception|traceback", cand, re.I):
            return _head(cand, 160)
    return _head(lines[-1] if lines else "(no output)", 160)


def count_attempts_since_consult(turn_entries) -> int:
    """Return the total number of tool attempts after the last ask_advisor call in this turn (untruncated)."""
    pending = {}
    count = 0
    for e in (turn_entries or []):
        if e.get("kind") == "tool_call":
            if str(e.get("name") or "").endswith("__ask_advisor"):
                count = 0
                pending.clear()
                continue
            pending[e.get("id")] = e
        elif e.get("kind") == "tool_result":
            call = pending.pop(e.get("id"), None)
            if not call:
                continue
            count += 1
    return count


def build_attempts_since_consult(turn_entries, window_limit=12, redact_fn=None):
    """Collect the sliding AttemptWindow of tool attempts after the last ask_advisor call in this turn."""
    pending = {}
    attempts = []
    for e in turn_entries:
        if e["kind"] == "tool_call":
            if str(e.get("name") or "").endswith("__ask_advisor"):
                attempts.clear()
                pending.clear()
                continue
            pending[e.get("id")] = e
        elif e["kind"] == "tool_result":
            call = pending.pop(e.get("id"), None)
            if not call:
                continue
            try:
                args = json.loads(call["text"]) if isinstance(call.get("text"), str) else (call.get("text") or {})
            except json.JSONDecodeError:
                args = {}
            attempts.append(make_attempt(call["name"], args, e.get("text") or "", e.get("error"), redact_fn))
            if len(attempts) > window_limit:
                attempts = attempts[-window_limit:]
    return attempts


def check_quick_repeat(attempts, already_repeated_keys):
    """pi-warden stuck.js AttemptWindow.quickRepeat (failed identical retry with no state change between)."""
    if len(attempts) < 2:
        return None
    latest = attempts[-1]
    if not latest["failed"] or latest["poll"] or latest["key"] in already_repeated_keys:
        return None
    idx = len(attempts) - 2
    while idx >= 0 and attempts[idx]["key"] != latest["key"]:
        idx -= 1
    if idx < 0:
        return None
    prev = attempts[idx]
    if prev["outputKey"] != latest["outputKey"] or prev["failed"] != latest["failed"]:
        return None
    if any(a["changes"] for a in attempts[idx + 1:-1]):
        return None
    return {
        "key": latest["key"],
        "call": _head(latest["call"], 120),
        "error": _error_line(latest["output"]),
        "calls_ago": len(attempts) - 1 - idx,
    }


def evaluate_stuck(attempts, task, cfg, jev_judge_fn):
    """pi-warden stuck.js evaluateStuck: offline exactRepeats/churn first, then Jev stuckQuestions."""
    sc = cfg.get("stuck") or {}
    min_failures = sc.get("min_failures", 3)
    churn_threshold = sc.get("churn_threshold", 5)
    same_strategy_cut = sc.get("same_strategy", 0.7)
    if not attempts:
        return {"stuck": False}
    latest = attempts[-1]
    failures = sum(1 for a in attempts if a["failed"])
    exact_repeats = (
        sum(1 for a in attempts if a["failed"] and a["key"] == latest["key"] and a["outputKey"] == latest["outputKey"])
        if latest["failed"]
        else 0
    )
    if exact_repeats >= min_failures:
        return {
            "stuck": True,
            "source": "repeat",
            "failures": failures,
            "reasons": [f"the same call failed {exact_repeats} times with the same output"],
        }
    churn = sum(1 for a in attempts if a["key"] == latest["key"])
    if churn >= churn_threshold and latest["failed"]:
        return {
            "stuck": True,
            "source": "repeat",
            "failures": failures,
            "reasons": [f"the same target was called {churn} times with failing/changing output"],
        }
    if not latest["failed"] or failures < min_failures:
        return {"stuck": False}

    state = {
        "task": _head(task.strip(), 1500) if task and task.strip() else "(no user request recorded)",
        "attempts": [
            {
                "n": idx + 1,
                "tool": a["tool"],
                "call": a["call"],
                "outcome": "failed" if a["failed"] else "ok",
                "output": a["output"],
                **({"change": a["change"]} if a.get("change") else {}),
            }
            for idx, a in enumerate(attempts)
        ],
    }
    verdicts, res = jev_judge_fn(state, STUCK_QUESTIONS, cfg)
    same_strat = (verdicts.get("same_strategy") or {}).get("answer")
    app_change = (verdicts.get("approach_change") or {}).get("answer")
    progress = (verdicts.get("progress") or {}).get("answer")
    if not isinstance(same_strat, (int, float)):
        return {"stuck": False}
    app_change = float(app_change) if isinstance(app_change, (int, float)) else 0.0
    progress = float(progress) if isinstance(progress, (int, float)) else 0.0
    stuck = same_strat >= same_strategy_cut
    reasons = (
        [f"{failures} failures with the same strategy ({same_strat:.2f}), approach change {app_change:.1f}/2, progress {progress:.2f}"]
        if stuck
        else []
    )
    return {
        "stuck": stuck,
        "source": "typesafe",
        "failures": failures,
        "reasons": reasons,
        "judgment": {
            "same_strategy": same_strat,
            "approach_change": app_change,
            "progress": progress,
            "latency_ms": res.get("latencyMs"),
        },
    }


def stuck_nudge(verdict, can_consult_advisor=True):
    """pi-warden stuck.js stuckNudge + Kimi ask_advisor escalation."""
    reasons = "; ".join(verdict.get("reasons") or ["repeated failing attempts"])
    if can_consult_advisor:
        return (
            f"[kimi-advisor stuck guard] {reasons}. Stop retrying the same approach. "
            "Call `ask_advisor({})` now to get a second opinion from the Advisor model before running another "
            "command or edit; if two different approaches have already failed, report the blocker to the user."
        )
    return (
        f"[kimi-advisor stuck guard] {reasons}. Stop retrying. Re-read the last error output carefully, state a new "
        "hypothesis about the cause, and either gather the missing information (read the relevant file, check versions "
        "or paths) or try a different method. If two different methods have failed, report the blocker to the user."
    )


def quick_repeat_nudge(repeat):
    return (
        f"[kimi-advisor quick-repeat guard] You already ran `{repeat['call']}` and it failed the same way: "
        f"{repeat['error']}. Change something or inspect the cause before running the identical command again."
    )
