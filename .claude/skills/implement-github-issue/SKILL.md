---
name: implement-github-issue
description: Invoke this skill when the user asks to implement, take, pick up, work on, tackle, start, or grab a GitHub issue. Triggers on phrases like "implement issue #N", "take issue #N", "work on #N", "do #N", or "pick up issue #N". Handles the full workflow from reading the issue through opening a pull request.
argument-hint: [ issue-number-or-url ]
allowed-tools: Bash(git *), Bash(gh *), mcp__github__issue_read
---

# Implement GitHub Issue

Follow this workflow exactly, in order. Do not skip steps.

## Step 1 — Read the Issue

Detect the available tooling, then fetch the issue details on the matching path. The `gh` CLI is the default; the MCP
server is the fallback for environments (e.g. Claude Cloud) where `gh` is not installed.

```bash
if command -v gh >/dev/null 2>&1; then
  # gh path (default — preferred for local)
else
  # MCP path (Cloud sandbox fallback)
fi
```

### `gh` path (default)

```
gh issue view $ARGUMENTS --json number,title,body,labels,comments
```

### MCP path (fallback)

Make two calls to `mcp__github__issue_read`, passing the repository `owner`, `repo`, and `issue_number` (parsed from
`$ARGUMENTS`, which may be a bare number or a full issue URL):

1. `method: "get"` → returns number, title, body, labels.
2. `method: "get_comments"` → returns the comments array.

### Parse the response

You need:

- **number**: The issue number
- **title**: The issue title
- **body**: The full issue body — these are your requirements
- **labels**: Any labels (used to infer the branch prefix in Step 4)
- **comments**: All comments on the issue, in chronological order

If the issue cannot be found, stop and tell the user.

### Comment precedence

Parse comments in chronological order. A later comment supersedes the issue body and any earlier comment when it
addresses the same decision. Use the resolved values as the requirements for implementation. No multi-author conflict
handling is needed — the repo owner is the sole author of spec decisions.

## Step 2 — Ensure Clean Working Tree

Run `git status --porcelain`. If there is any output, **stop** and tell the user:

> Your working tree has uncommitted changes. Please commit or stash them before implementing a new issue.

Do not proceed until the working tree is clean.

## Step 3 — Checkout and Pull Main

Determine the default branch name, then checkout and pull:

```
git checkout main && git pull
```

If this fails (e.g., merge conflicts), stop and tell the user.

## Step 4 — Cut a New Branch

Derive a branch name using this convention:

1. **Prefix** — infer from issue labels or title keywords:
    - `bug` or `fix` label, or title contains "fix"/"bug" → `fix/`
    - `enhancement` or `feature` label, or title suggests new capability → `feat/`
    - `docs` or `documentation` label → `docs/`
    - `refactor` label → `refactor/`
    - `audit` label or title contains "audit" → `audit/`
    - Fallback → `feat/`

2. **Slug** — lowercase the issue title, replace non-alphanumeric characters with hyphens, collapse consecutive hyphens,
   trim leading/trailing hyphens, truncate to 50 characters.

3. **Suffix** — append the issue number.

Format: `<prefix><slug>-<number>`

Example: Issue #42 titled "Add widget component" with label `enhancement` → `feat/add-widget-component-42`

```
git checkout -b <branch-name>
```

## Step 5 — Implement the Issue

Read the issue body in full and implement the requested changes directly. Use the standard tools (Read, Edit, Write,
Bash). For research-heavy or independent sub-tasks, you may dispatch the `Explore` or `general-purpose` Agent. Apply
all `.claude/rules/` guidance (line length, Python style, external API grounding, scripted infra setup, etc.).

Then proceed to Step 6 (commit, push, open PR via `create-github-pr` skill).

## Step 6 — Commit, Push, and Open a PR

Once the implementation is complete:

1. **Stage** the relevant changed files by name (prefer explicit paths over `git add -A`).

2. **Commit** with a message referencing the issue. Use this format:

   ```
   <type>: <short description> (closes #<number>)
   ```

3. **Push** the branch:

   ```
   git push -u origin <branch-name>
   ```

4. **Create a PR** using the `create-github-pr` skill (`.claude/skills/create-github-pr/SKILL.md`). Follow that skill's
   full workflow (write body to temp file, create with `--body-file`, validate, clean up). The PR title should match the
   commit message (without the `closes` suffix). The body must include:

   ```
   ## Summary
   <1-3 bullet points summarizing the changes>

   Closes #<number>

   ## Test plan
   <Bulleted checklist of how to verify the changes>

   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   ```

5. **Report** the PR URL to the user.
