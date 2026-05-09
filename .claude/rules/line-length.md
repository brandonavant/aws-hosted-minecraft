# Line Length

Wrap lines at 120 characters in `.md`, `.py`, `.tf`, `.tfvars`, `.hcl`, `.yml`, `.yaml`, and `.sh` files. Includes
prose, docstrings, and inline comments — not just code. Do not wrap shorter than 120 unless a natural break (heading,
list item, blank line, code fence, end of statement) ends the line.

Do **not** wrap at ~80 or ~88 columns because of formatter defaults or training-data habits. Extend the same width to
docstrings, comments, and Markdown prose where formatters do not enforce it.

Loads unconditionally because path-scoped rules do not fire on net-new files (see `rule-scoping-decisions.md`).
Excludes generated and lock files (e.g., `uv.lock`, `poetry.lock`, `package-lock.json`, `pnpm-lock.yaml`,
`Pipfile.lock`, `*.tfstate`, `*.tfstate.backup`).
