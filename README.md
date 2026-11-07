# kimi-advisor

Advisor second-opinion plugin for **Kimi Code CLI**, combining:

- **Full-context / bounded-transcript Advisor consultation** ([Anthropic Advisor Strategy](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/advisor-tool) / `rpiv-advisor` / `pi-advisor-flow`): escalates to a stronger reviewer model configured in `~/.kimi-code/config.toml` without using subagents.
- **Jev pre-screening** (`pi-advisor-flow`): ~300 ms fast judgment via `jev-use` skips low-stakes, self-answerable calls (`P(negligible) >= 0.85` and `P(self_answerable) >= 0.85` on the `pi` preset).
- **Stuck & Done-Check guards** (`pi-warden`):
  - **Stuck guard** (`PostToolUseFailure` + `PreToolUse`): tracks an `AttemptWindow` (12 calls) with offline `quickRepeat` detection and Jev `same_strategy >= 0.7` judgment to break failing retry loops and steer the executor to consult `ask_advisor`.
  - **Done-check guard** (`Stop`): checks file mutations against `freshChecks` and Jev `doneQuestions`, auto-discovers project check commands (`package.json`, `Makefile`, `pytest`, `cargo test`, `go test`), and optionally gates completion with Jev-screened `ask_advisor` review (`completion_gate`).

## Installation

Copy or clone into Kimi Code's managed plugins directory:

```bash
git clone https://github.com/kim1232aa/kimi-advisor.git ~/.kimi-code/plugins/managed/kimi-advisor
```

Then run `/plugins reload` (or `/reload`) inside Kimi Code.

## Requirements

- Python 3.11+
- At least one reviewer model configured under `[models]` and `[providers]` in `~/.kimi-code/config.toml` (`anthropic`, `openai`, or `openai_responses` provider types).
- Optional (for Jev pre-screening and Jev-judged guards): `jev-use` CLI on `PATH` and `TYPESAFE_API_KEY` in environment or `~/.kimi-code/mcp.json`.

## Configuration

Settings are stored in `~/.kimi-code/advisor/config.json`.

### Slash commands (in Kimi Code)

- `/kimi-advisor:config` — view or interactively configure model, reasoning effort, Jev preset, and guards.
- `/kimi-advisor:review [focus]` — force an immediate Advisor review of the current session.

### Shell CLI (no LLM call)

```bash
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py show
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py models
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set model <alias>
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set effort high
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set jev-preset pi
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set stuck on
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set done on
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set completion-gate on
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set context 60000
python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set max-output 4096
```

## License

MIT
