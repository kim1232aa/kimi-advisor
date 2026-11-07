---
description: Ask the Advisor model for a second opinion on the current work right now
---

Call `ask_advisor` now with `force: true`. The user explicitly asked for this review.

If the user wrote a focus below, pass it as `question`. Otherwise pass `{"force": true}` with no `question`: the advisor already receives the whole conversation.

Relay the advisor's plan, correction, or stop signal to the user, then act on what holds up:
- Check the advisor's claims against the code/environment first; mark anything not verified with tools as `未验证`.
- User instructions, `AGENTS.md`, and active permission-mode rules (e.g. no `AskUserQuestion` in Never Ask / Auto mode; confirm before modifying external services) always override the advisor.

User focus: $ARGUMENTS
