# Scripted Infrastructure Setup

Every AWS, GitHub, or other operator-facing configuration mutation MUST ship as a committed, idempotent script under
`scripts/` — never as a prose recipe in a README or issue body. The next operator (a fork, a new environment, you in
six months) must re-execute the script, not re-derive the steps from documentation.

Loads unconditionally — the rule fires at planning time (issue / PR description), which has no associated file path.

## When this rule fires

A planned operator step that:

- Mutates GitHub config: `gh variable set`, `gh secret set`, `gh api`, etc.
- Calls `aws` to create / update / configure an AWS resource (e.g., LightSail instance bootstrap, IAM role grants,
  Route 53 records, S3 bucket policies).
- Configures a third-party SaaS the project depends on (DNS records, monitoring API keys, registrar tokens).
- Bootstraps the Minecraft server host itself (package installs, systemd unit deployment, file-system layout, fstab
  edits, cron / timer registration).

Does NOT fire on: one-time human-only setup with no machine-executable form ("create an AWS account"), or read-only
diagnostics (`gh variable list`, `aws sts get-caller-identity`, `systemctl status`).

## Script conventions

- **Location**: `scripts/` at the repo root. Sub-group (`scripts/aws/`, `scripts/github/`, `scripts/server/`) when it
  grows.
- **Language**: bash for thin imperative wrappers (sequence of `gh`/`aws` calls + prereq checks);
  Python (real `.py` with `argparse`, type hints, Google-style docstrings, sibling `test_*.py`) when the work
  involves non-trivial JSON parsing, branching on enriched inputs, or `jq` pipelines / nested `case` you'd
  otherwise reach for in bash. Pick by shape of work, not line count.
- **Bash specifics**: `set -euo pipefail`; targets macOS bash 3.2 (no `mapfile`, no `${var,,}`, no associative
  arrays); passes `shellcheck` clean; `chmod +x`.
- **Idempotent by default**: re-runs produce the same end state without error. Rely on `gh`/`aws` upsert behavior;
  do not add custom "exists?" checks unless the tool refuses to overwrite.
- **Fail loudly**: missing prereqs print the missing piece by name and exit non-zero. No silent no-ops, no blank
  overwrites when an upstream value is empty.

## Issue / PR requirements

When a planned change triggers this rule, the issue body MUST:

- Name the script path in the Scope section (e.g., `scripts/server/install-paper-updater.sh`).
- Add an Acceptance Criterion that the script exists, is executable, passes `shellcheck` (bash) or its tests
  (Python), and is idempotent.
- Point the relevant onboarding doc (e.g., `README.md`) at the script rather than inlining the commands —
  prose-vs-script drift is the failure mode this rule prevents.
