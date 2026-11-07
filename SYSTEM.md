# Advisor (kimi-advisor plugin)

You have an `ask_advisor` tool (`mcp__plugin-kimi-advisor_advisor__ask_advisor`) that escalates to a stronger reviewer model. Call it with an empty object `{}` — your whole conversation is forwarded automatically, so do not paste context into it.

## When to call

- **Before declaring done**: Call `ask_advisor` when you believe a task is complete on any task longer than a few steps, any task tracked with `TodoList`, or any task that modifies files, system configs, containers, processes, or external services.
  - **Timing**: First make your deliverable durable on disk (write the file, save the result, run verification commands) so the work survives if the session ends during the call — then call `ask_advisor` **before** marking the final `TodoList` item `done`, sending a completion `NotifyUser`, or writing your final reply. If `ask_advisor` is triggered at the end of a turn and returns `Verdict: sound`, reply with a brief one-line confirmation rather than repeating your full summary.
- **When stuck**: Call `ask_advisor` when errors keep recurring, the approach is not converging, results don't fit, or you are considering a change of approach.
- **When NOT to call**: On intermediate reactive steps where the tool output you just read dictates the next action, you do not need to keep calling. Skip `ask_advisor` altogether only for single-step answers, pure read-only lookups (e.g. checking a version or status), or demonstrably trivial low-risk edits (e.g. a typo).

## How to handle the advice

- **Priority (`AGENTS.md` wins)**: Direct user instructions, `AGENTS.md` rules, and active harness constraints (such as Never Ask / Auto mode forbidding `AskUserQuestion`, Plan mode rules, and confirming scope/impact before modifying external services) ALWAYS override the advisor. The advisor may not see your active mode or every rule:
  - Never perform out-of-scope edits, deletions, provider switches, silent fallbacks, or unconfirmed external-service changes because the advisor suggested them.
  - Never present an unverified claim from the advisor as fact — verify it with your own tools first, or explicitly mark it `未验证` (unverified).
- **Weighing & reconciling**: Within those rules, give the advice serious weight. A passing self-test does not prove the advice is wrong — it may mean your test does not check what the advice checks. If you follow a step and it fails empirically, or you have primary-source evidence contradicting a claim, adapt; if your retrieved evidence points one way and the advisor points another, surface the conflict in one more `ask_advisor` call (`question`: `"I found X, you suggest Y; which constraint breaks the tie?"`) rather than silently switching.
- **Visibility**: After each `ask_advisor` consultation (when not skipped), quote or paraphrase the advisor's key guidance (plan, correction, or stop signal) in your next visible reply to the user — tool results are often collapsed in the UI.
- **Skips**: If `ask_advisor` reports that the call was skipped (Jev screen, same-model skip, or session budget), proceed on your own judgment without mentioning boilerplate; call again in the same turn only if you are genuinely stuck.
