# Skill Matching on Redirected Intent

When user intent arrives through any channel -- including tool-rejection feedback, follow-up corrections, or task
redirects -- treat it as a new user request subject to all skill-matching rules.

## Why this rule exists

The Skill tool's matching instruction ("When a skill matches the user's request, this is a BLOCKING REQUIREMENT")
applies only to direct user messages by default. When intent is expressed indirectly (e.g., rejecting a tool call with
"turn this into an issue"), the model may enter recovery mode and skip skill matching entirely. This rule closes that
gap.

## What to do

1. **Parse the redirect for intent.** Extract the user's actual goal from the rejection feedback or follow-up text.
2. **Match against available skills.** Check whether the extracted intent matches any skill description, exactly as you
   would for a direct user prompt.
3. **Invoke the matching skill.** If a skill matches and is model-invocable, use the Skill tool. If it has
   `disable-model-invocation: true`, read the skill's SKILL.md and follow its workflow instructions directly.
4. **Do not fall back to raw commands.** If a skill exists for the task, you must use it (either via the Skill tool or
   by following its instructions). Running the equivalent CLI command directly is a bypass.
