---
name: advisor
description: Use before declaring done on multi-step or state-changing work, when stuck (errors recurring, approach not converging), or when the user asks for a second opinion / advisor review / to change advisor settings.
---

# Advisor second opinion

`mcp__plugin-kimi-advisor_advisor__ask_advisor` forwards your reconstructed session conversation (user requests, your messages, every tool call and result, and any compaction summary) to the configured reviewer model (`~/.kimi-code/advisor/config.json`, referencing `[models]` in `~/.kimi-code/config.toml`).

## When to call

1. **Before declaring done** on any task longer than a few steps, any task using `TodoList`, or any task that modifies files, configs, containers, processes, or services:
   - Write deliverables to disk and run verification commands **first**.
   - Call `ask_advisor({})` **before** marking the final `TodoList` item `done`, sending a completion `NotifyUser`, or outputting your final completion message. Do not excuse multi-step changes as "routine".
2. **When stuck**: errors recur, an approach does not converge, results contradict expectations, or you are about to change approach.
3. **When the user explicitly asks for an advisor review**: pass `{"force": true}` (and `question` only if the user gave a specific focus).

Skip `ask_advisor` on intermediate reactive steps where tool output dictates the immediate next step, and skip altogether on pure read-only lookups, single-step Q&A, or trivial typo fixes.

## Calling

- Default arguments: `{}`. Do not paste conversation context.
- Pre-checks run automatically unless `force: true`:
  1. **Same-model skip**: skipped if executor and advisor resolve to the same underlying model.
  2. **Jev screen** (~300 ms): skips low-stakes, self-answerable calls; calling `ask_advisor` a second time in the same turn overrides the skip.

## Acting on the reply (`AGENTS.md` takes precedence)

The advisor returns a **plan**, a **correction**, or a **stop** signal. When the proposed plan or deliverable has no issues, the advisor's first line returns `Verdict: sound` as a brief confirmation.

- **`AGENTS.md` and harness rules always win**: Never violate user instructions, `AGENTS.md` (scope/safety, confirming before external-service changes, no silent fallback/provider switching, preserving real errors/endpoints, marking unverified claims `未验证`), or active mode rules (e.g. do not call `AskUserQuestion` in Auto / Never Ask mode even if the advisor suggests it).
- **Verify claims**: Treat advisor claims about external facts or unseen files as hypotheses — verify with tools or mark `未验证`.
- **Reconcile conflicts**: If primary evidence you retrieved contradicts the advisor, make one focused `ask_advisor` call (`{"question": "I found X, you suggest Y; which constraint breaks the tie?"}`) instead of silently ignoring either side.
- **Relay to user**: Include a brief summary of the advisor's key guidance in your next visible reply when a consultation actually ran.

## Settings

`mcp__plugin-kimi-advisor_advisor__advisor_config` (change only when the user asks):
- `{}`: show current settings, curated model pool, supported efforts, and stats
- `{"list_models": true}`: include all usable aliases from `config.toml`
- Update keys: `model`, `effort` (`off`/`low`/`medium`/`high`/`xhigh`/`max`), `jev_enabled`, `jev_preset` (`strict`/`pi`/`loose`), `stuck_enabled` (pi-warden stuck guard), `done_enabled` (pi-warden done-check guard), `completion_gate` (require ask_advisor on file-mutating done claims), `disable_same_model`, `context_max_chars` (`0` = full), `max_calls_per_session` (`0` = unlimited), `max_output_tokens`, `redact_secrets`, `timeout_s`

No-model CLI (terminal or `!` shell mode):
- `python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py --help`
- CLI `set` subcommand uses kebab-case keys: `stuck` -> `stuck_enabled`, `done` -> `done_enabled`, `completion-gate` -> `completion_gate` (also `same-model-skip` -> `disable_same_model`, `context`, `budget`, `max-output`, `redact`, `timeout`).
