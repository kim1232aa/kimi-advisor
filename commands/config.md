---
description: Interactive advisor settings panel via AskUserQuestion, or apply arguments directly
---

You are handling the `/kimi-advisor:config` command. Your goal is to give the user a clear way to see and change their kimi-advisor settings.

## Fast path (explicit arguments)
If `$ARGUMENTS` contains setting assignments (such as `model ...`, `effort ...`, `jev on|off`, `reset`), do NOT ask questions. Translate the arguments to `advisor_config` parameters, call the tool once, and print the resulting settings.

## Interactive panel path (empty or general `$ARGUMENTS`)

Follow these steps in order:

### Step 1: Read current state
Call `advisor_config` with no arguments. It returns:
- `settings`: current active values for all 13 settings
- `model_pool`: curated aliases from `config.toml` (`[secondary_model.models]`) with their description and supported reasoning efforts
- `current_model_efforts`: valid effort levels for the currently-configured model

### Step 2: Check whether interactive selection is possible
Check whether you are permitted to use `AskUserQuestion`.
- In **Never Ask / Auto mode**, `AskUserQuestion` is disallowed by system instructions. Do NOT call it. Instead, reply with a clean Markdown table of the current settings, list the model pool aliases with their notes, and show the user how to change any setting: either by saying it in chat (e.g. "advisor 换成 gemini"), using `/kimi-advisor:config model <alias>`, or running `python3 ~/.kimi-code/plugins/managed/kimi-advisor/advisor_cli.py set <key> <val>`.
- In **Always Ask** or **Ask When Needed** mode, proceed to Step 3.

### Step 3: Present the interactive selection panel
Call `AskUserQuestion` with 2 to 3 related questions so the user gets a multi-step form:

**Question 1 (header: "Model")**:
"Choose the Advisor model (must be configured in config.toml):"
Build the options from `model_pool`. The option matching `settings.model` must come first and carry the label suffix `(Recommended)` or `(Current)`. Labels must be concise (the model alias, max 5 words); put the model's description/note from `model_pool` into the option `description`.

**Question 2 (header: "Effort")**:
"Reasoning effort for the advisor model:"
Build the options from `current_model_efforts`. Always include `off (provider default)` (Recommended if currently off), plus each supported effort level (`low`, `medium`, `high`, `xhigh`, `max`) with a short description of the latency/depth trade-off.

**Question 3 (header: "Tuning", optional/multi_select=false)**:
"Jev pre-screening & behavior preset:"
- `pi default (Recommended)`: Jev skips low-stakes self-answerable calls (0.85/0.35 thresholds); same-model skip enabled
- `strict`: Jev skips very few calls (higher review coverage, more advisor calls)
- `loose`: Jev skips more aggressively (lowest cost)
- `disabled`: turn Jev screening off (every ask_advisor call goes to the advisor model)

**Guard switches (`stuck_enabled`, `done_enabled`, `completion_gate`)**:
If the user asks to configure guards or when presenting guard switches in the interactive panel / CLI:
- `stuck_enabled` (`stuck on|off` in CLI): pi-warden stuck guard to detect loop/failure patterns and trigger early advisor intervention.
- `done_enabled` (`done on|off` in CLI): pi-warden done-check guard to verify state changes against fresh checks before finishing.
- `completion_gate` (`completion-gate on|off` in CLI): require `ask_advisor` verification at Stop on file-mutating turns even when self-checks passed (default off).
These can be toggled directly via command arguments (e.g. `/kimi-advisor:config stuck on`, `/kimi-advisor:config completion-gate on`), interactive selections, or shell CLI (`advisor_cli.py set stuck on|off`, `set done on|off`, `set completion-gate on|off`).

### Step 4: Apply choices
Once `AskUserQuestion` returns with the user's answers:
1. Translate the labels back to `advisor_config` parameters:
   - Model: the chosen alias
   - Effort: `off` -> `off`, or the chosen level
   - Tuning: apply `jev_enabled` and `jev_preset`
2. Call `advisor_config` with those parameters.
3. Show the user the updated settings summary and confirm the changes are active.

User request: $ARGUMENTS
