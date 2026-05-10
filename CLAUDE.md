# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context

The live deployment is on **AWS LightSail**. An earlier Azure backend (PR #2) was scrapped and the repo was reset
on 2026-05-09 to reverse-engineer a hand-built LightSail Minecraft deployment into Terraform (issue #4, merged in
PR #9). The repo was originally named `azure-hosted-minecraft` and renamed to `aws-hosted-minecraft` on 2026-05-09;
historical commits, PR titles, and issue bodies may still mention Azure or the old name.

The eventual goal is idempotent scripts that can rebuild or repair the live server, plus a Python nightly updater
for Paper / Geyser / Floodgate version bumps. The updater is deliberately not designed yet — it waits on issue #4
(closed — Terraform import) and issue #5 (open — server-state capture from the live host), so its requirements
are shaped by what those uncover.

## Architecture

The Terraform stack lives in two layers under `infra/`:

- **`infra/bootstrap/`** — provisions the S3 bucket + DynamoDB lock table that hold the main layer's remote state.
  Has **no `backend` block**; its own state lives on the operator's laptop. This solves the chicken-and-egg of
  bootstrapping its own state backend. Re-running this layer is rare (recovery scenarios only — see its README).
- **`infra/`** — the main layer. Manages the live LightSail Minecraft deployment via an S3 backend whose values
  come from `backend-configs/prod.hcl` (gitignored; the operator copies `prod.hcl.example` and fills in bucket /
  lock-table / profile from the bootstrap layer's outputs).

The main layer was authored by importing existing AWS resources, so `terraform plan` is zero changes for everything
it manages. See `infra/README.md` for the full import map and the deliberately-not-imported resources:
`aws_lightsail_instance_public_ports.mcserver` (provider gap — applied as a no-op to enter state), the
`ssh-mcserver` key pair (the AWS provider docs explicitly forbid importing), LightSail snapshots (no provider
resource exists), and the `bytehorizonforge.com` Route 53 zone — that one is a `data` source on purpose because it
pre-existed this project and hosts unrelated Azure M365 records (`auth.bytehorizonforge.com`).

### File-per-concern Terraform layout

No `main.tf` — ever. Resources are split by domain: `providers.tf`, `variables.tf`, `outputs.tf`, plus per-domain
files (`lightsail.tf`, `networking.tf`, `storage.tf`, `dns.tf`). `infra/bootstrap/` follows the same pattern
(`s3.tf`, `dynamodb.tf`). Modules under `infra/modules/<name>/` are acceptable only when a real reuse boundary
appears — do not pre-extract speculatively. If a future PR proposes a `main.tf` or speculative module, push back.

## Operator vs. agent split (Terraform)

The agent runs routine, low-risk Terraform commands. The operator runs anything that mutates AWS or initializes a
layer for the first time. **The agent must stop at every operator-owned step**, even if the plan looks safe.

| Command                           | Owner    |
|-----------------------------------|----------|
| First `terraform init` per layer  | Operator |
| Subsequent `terraform init`       | Agent    |
| `terraform validate` / `fmt`      | Agent    |
| `terraform plan`                  | Agent    |
| `terraform import`                | Agent    |
| `terraform state rm` / `state mv` | Agent    |
| `terraform apply` / `destroy`     | Operator |

## AWS authentication

- Account `444672861827`, region `us-east-1`, profile `mc-aws` (IAM Identity Center, `AdministratorAccess`
  permission set). Pass `--profile mc-aws` or set `AWS_PROFILE=mc-aws` for every `aws` / `terraform` invocation.
- The cached SSO token at `~/.aws/sso/cache/` typically expires every 8–12 hours. Recovery: `aws sso login
  --profile mc-aws`.
- `MC_AWS_PROFILE=mc-aws` lives in the gitignored `.env`. See `docs/aws-auth-setup.md` for the full IdC walkthrough
  (Checkpoints A–F); `.env.example` lists every required key (also `MC_SSH_HOST`, `MC_SSH_USER`, `MC_SSH_KEY` for
  reaching the live host).

## Common commands

```bash
# Verify AWS CLI auth (read-only; run before anything that touches AWS).
./scripts/aws/verify-credentials.sh

# Snapshot every AWS resource the deployment depends on (drift detection).
./scripts/aws/inventory.sh   # writes infra/aws-inventory.json (gitignored)

# Bootstrap layer (rare — only on first setup or laptop recovery).
cd infra/bootstrap && terraform fmt -recursive && terraform validate && terraform plan

# Main layer.
cd infra && terraform init -backend-config=backend-configs/prod.hcl
terraform fmt -recursive && terraform validate && terraform plan   # plan must be zero changes

# Lint shell scripts (project requires shellcheck-clean).
shellcheck scripts/aws/*.sh

# Python formatter (line-length 120 from pyproject.toml). Project requires Python 3.13+.
black .
```

There are no Python files yet — `server/` and `docs/` only contain `.gitkeep` placeholders plus the AWS auth doc.

## Public-repo hygiene

This is a public GitHub repo. Never commit, paste into PR descriptions, or echo to logs:

- The AWS account ID (12 digits) — redact to `<account-id>` or `<redacted>`. The inventory script already redacts.
- Real `.env` values: SSH host / user / PEM path, `MC_AWS_PROFILE` if it identifies the operator.
- `infra/backend-configs/*.hcl` (only `*.hcl.example` is committed).
- `infra/aws-inventory.json` (operator-specific data — cross-account bucket names, personal-domain DNS records,
  M365 verification tokens, IAM usernames — even with the account ID redacted).
- Anything from `~/.aws/`, including SSO start URLs.

`.env`, `*.pem`, `*.tfvars`, real `*.hcl` backend configs, and the inventory snapshot are already gitignored.

## Project rules

Topic-specific instructions live in `.claude/rules/` and load alongside this file. They cover Terraform conventions,
Python style, line length (120 char wrap in `.md` / `.py` / `.tf` / `.tfvars` / `.hcl` / `.yml` / `.yaml` / `.sh`),
dependency vetting via the `vet-dependency` skill, scripted infrastructure setup (no prose-recipe operator steps —
ship a script under `scripts/`), harness-format enforcement, external-API grounding, and skill matching on
redirected intent. Read the relevant rule before touching the matching file type or planning operator-facing work.
