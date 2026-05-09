# Rule Scoping: `paths` Frontmatter vs. Unconditional

When creating or modifying a `.claude/rules/` file, you MUST decide whether to use `paths` frontmatter or leave the rule
unconditional. This decision has a direct impact on whether the rule will be visible during file creation.

## How `paths` Scoping Works

Path-scoped rules trigger when Claude **reads** a file matching the pattern. They do **not** load at session start and
they do **not** fire when creating net-new files if no matching file has been read yet.

## Decision Criteria

**Use `paths` frontmatter** when ALL of these are true:

- The project already has a substantial set of files matching the pattern.
- The rule only matters when editing existing files, not when creating new ones.
- Context savings justify the scoping (e.g., a large rule that applies to a narrow set of files).

**Omit `paths` frontmatter (unconditional)** when ANY of these are true:

- The project is greenfield or the relevant file type has few or no existing files yet.
- The rule must apply during creation of net-new files (e.g., coding standards, naming conventions).
- The rule is small enough that always-loading it has negligible context cost.

## Why This Matters

The official Claude Code docs present `paths` scoping as a best practice for file-type-specific rules but do not warn
about the net-new file gap. A rule scoped to `**/*.py` will be invisible when creating the project's first Python file —
exactly when coding standards matter most.

When in doubt, leave the rule unconditional. The context cost of a concise, always-loaded rule is far smaller than the
cost of generating code that violates project standards and needs to be rewritten.
