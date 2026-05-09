---
name: update-github-issue
description: Invoke this skill when updating, editing, modifying, appending to, or amending an existing GitHub issue body. Triggers on phrases like "update issue", "edit issue", "modify issue body", "append to issue", "tweak the issue", or "fix the issue body".
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Update GitHub Issue

Update an existing GitHub issue's body reliably by fetching the current body to a file, editing the file, applying the
edit, and validating against the on-disk source-of-truth. This workflow avoids the silent-content-loss pitfalls of
inline subshells AND the false-mismatch pitfalls of raw `diff` against a fetched body.

## When to use

- Adding or amending sections in an existing issue body (e.g., "Status — what's already done" notes, narrowing scope,
  cross-linking a freshly merged PR).
- Fixing typos, broken links, or stale references in an existing issue.
- Replacing the entire body wholesale.

For *creating* a new issue, use the `create-github-issue` skill instead.

## Workflow

Follow these steps for **every** update. Do not skip or combine steps.

### Step 1: Fetch the current body to a temp file

```bash
gh issue view <number> --json body --jq .body > /tmp/gh-issue-<number>-current.md
```

This file is now the **source of truth** for what the issue body should contain after your edit.

**Validate the fetch succeeded.** Check the file is non-empty and starts with content you expect (e.g., `## Goal`,
`## Problem`, the original first heading). If the file is empty or clearly truncated, retry the fetch up to 2 times
with a 5-second pause between attempts before proceeding. A silent empty fetch followed by an `--body-file` apply will
wipe the issue body.

Do NOT use inline `--body "$(gh issue view ... --jq .body)"` patterns. Subshells can silently return empty on transient
network failures, wiping the entire body on apply.

### Step 2: Edit the file

Use the `Edit` tool (preferred) or `Write` (only for full-body replacements). Make the precise edits you intend; do not
reformat or re-indent unrelated sections. Preserve the existing line-wrap conventions of the issue body.

If the user's intent is to *append* a section, add it where it fits semantically — typically right after the existing
`## Goal` / `## Context` block — not necessarily at the end of the file.

### Step 3: Apply the edit

```bash
gh issue edit <number> --body-file /tmp/gh-issue-<number>-current.md
```

If this command fails entirely (non-zero exit), retry up to 2 times with a 5-second pause between attempts.

### Step 4: Validate using the validator script

The validator from the sister `create-github-issue` skill is the **only** sanctioned way to verify the apply
succeeded. It normalizes trailing whitespace and trailing blank lines, so it will not raise false mismatches on
GitHub's body-storage idiosyncrasies.

```bash
python3 .claude/skills/create-github-issue/scripts/validate_issue.py <number> /tmp/gh-issue-<number>-current.md
```

The script exits 0 on match, 1 with a unified diff on real mismatch.

**Do NOT use raw shell `diff` against the fetched body.** GitHub's API may return a body with trailing newlines that
your local file does not have (or vice versa). A raw `diff` will surface those as a "MISMATCH" even when the
substantive content is identical, leading you to chase a phantom divergence. The validator script already normalizes
this; nothing else should.

### Step 5: If validation fails, repair and re-validate

If the validator reports a real mismatch:

1. **Repair**: re-apply with `gh issue edit <number> --body-file /tmp/gh-issue-<number>-current.md`.
2. **Re-validate**: run the validator script again.
3. If it still fails after 2 repair attempts, stop and report the failure to the user with the validator's diff
   output. Do not loop further — a persistent mismatch likely indicates a problem upstream (e.g., a hook rewriting the
   body, a permissions issue, or a parallel edit) that the user needs to investigate.

### Step 6: Clean up

After the issue is updated and validated, delete the temp file:

```bash
rm /tmp/gh-issue-<number>-current.md
```

## Batch updates

When updating multiple issues, process them sequentially — fetch, edit, apply, validate, then move to the next. Do NOT
update issues in parallel; transient failures are harder to diagnose and recover from when interleaved.

## Anti-patterns to avoid

- ❌ **Composing a new body from scratch** instead of fetching first. You will inevitably drop sections that exist in
  the live body but were not in your context.
- ❌ **Using inline `--body "$(...)"`**. Subshell failures wipe the body silently.
- ❌ **Raw shell `diff` for verification**. Trailing-newline differences cause false mismatches; use the validator.
- ❌ **Skipping the post-apply validation**. The apply may succeed but the result may not match what you intended
  (e.g., if your edit-tool call ran on a stale file).
- ❌ **Manually trimming the file with `perl -i -pe 'chomp if eof'` to "make `diff` happy"**. That defeats the purpose
  of normalization and risks stripping content you actually wanted preserved. Use the validator instead.
