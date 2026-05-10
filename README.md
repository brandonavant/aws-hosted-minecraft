# aws-hosted-minecraft

A personal-scale Minecraft server hosted on AWS LightSail, with infrastructure managed by Terraform and operator
workflows captured in committed scripts and docs. Designed for reproducibility — a forked clone with a fresh AWS
account should be able to stand up an equivalent stack from this repo plus its own secrets.

## Status

The repo was reset on 2026-05-09 to reverse-engineer a hand-built deployment that pre-dated source-controlled
infrastructure. Two foundational issues drive the work:

- **#4** (closed,
  [PR #9](https://github.com/brandonavant/aws-hosted-minecraft/pull/9)) — AWS LightSail infrastructure imported
  into Terraform. `terraform plan` is zero-changes against the live deployment.
- **#5** (closed) — initial state-capture effort. Superseded by #11 once it became clear the deliverable should be
  an executable provisioner, not a captured runbook.
- **#11** (closed) — idempotent Python provisioner under `scripts/server/install.py` that takes a fresh LightSail
  Ubuntu 22.04 instance with the data volume re-attached and produces a running Paper + Geyser + Floodgate +
  ViaVersion crossplay server.
- **#14** (in flight) — idempotent Python updater under `scripts/server/update.py`, run on a systemd timer, that
  keeps Geyser / Floodgate / Paper / ViaVersion current so Bedrock clients stay connectable when Mojang ships a
  protocol bump.

## What's deployed

- **Compute** — AWS LightSail Ubuntu 22.04 instance (`large_3_0` bundle) in `us-east-1a`.
- **Storage** — 128 GB LightSail data disk attached at `/dev/xvdf` for the world directory.
- **Networking** — a static LightSail IPv4 address bound to the instance.
- **DNS** — `mc.bytehorizonforge.com` A-record points at the static IP (Route 53).
- **Firewall** — Java Edition (TCP 25565), Bedrock Edition (UDP 19132), HTTP (TCP 80), and SSH (TCP 22, IPv4
  restricted to LightSail Connect's range). Open issue
  [#8](https://github.com/brandonavant/aws-hosted-minecraft/issues/8) tracks tightening rules that the inventory
  script flagged.
- **Backups** — LightSail AutoSnapshot daily at 11:00 UTC.

The Terraform stack covers everything in this list except the `ssh-mcserver` key pair (the AWS provider docs
forbid importing it) and LightSail snapshots (no Terraform resource exists for this provider). See
`infra/README.md` for the full list of deliberately-unmanaged resources and the rationale.

## Repo layout

| Path                | Purpose                                                                            |
|---------------------|------------------------------------------------------------------------------------|
| `infra/bootstrap/`  | Terraform layer that provisions the S3 + DynamoDB remote-state backend.            |
| `infra/`            | Main Terraform layer — imports and manages the live LightSail deployment.          |
| `scripts/aws/`      | Operator-facing AWS scripts: credential verification, drift inventory.             |
| `scripts/server/`   | On-host scripts: provisioner, updater, `deploy-updater.sh`, tests.                 |
| `docs/`             | Long-form operator walkthroughs (currently: AWS CLI auth via IAM Identity Center). |
| `server/`           | Reserved for on-host server state captures (placeholder).                          |
| `.claude/`          | Project rules and skills used by Claude Code.                                      |

Per-directory READMEs in `infra/` and `infra/bootstrap/`, plus `docs/aws-auth-setup.md`, carry the detail.

## Setup from scratch (forking operators)

1. **AWS authentication** — follow `docs/aws-auth-setup.md` end-to-end (Checkpoints A–F). Ends with
   `./scripts/aws/verify-credentials.sh` exiting clean.
2. **Bootstrap layer** — `cd infra/bootstrap`, copy `terraform.auto.tfvars.example` to `terraform.auto.tfvars`,
   pick a globally-unique S3 bucket name, then `terraform init` and `terraform apply`. See
   `infra/bootstrap/README.md` for the full first-run workflow and a recovery path if the local state is lost.
3. **Main layer** — `cd infra`, copy `backend-configs/prod.hcl.example` to `prod.hcl` with the bootstrap layer's
   outputs, then `terraform init -backend-config=backend-configs/prod.hcl`. The committed `.tf` files match the
   shape of the existing live deployment, so the import path documented in `infra/README.md` is the supported one
   today; a clean greenfield apply has not been exercised and may surface differences that need to be reconciled.
4. **Provision the host** — once Terraform has stood up the LightSail instance and the data volume is attached
   and mounted at `/srv/minecraft`, SSH in and run the provisioner:

   ```bash
   sudo python3 scripts/server/install.py --help
   ```

   The script is idempotent (re-runs are no-ops when state matches), pins Paper / Geyser / Floodgate / ViaVersion
   versions via flags (defaulting to the volume's existing state), and refuses to regenerate the Floodgate key
   unless `--allow-fresh-floodgate-key` is passed. See the script's docstring and `--dry-run` for details.

## Operating the live server

- `./scripts/aws/verify-credentials.sh` — read-only AWS auth check. Run this whenever an AWS call starts failing;
  it surfaces expired SSO tokens, missing region config, and wrong-permission-set ARNs with a copy-paste recovery
  command.
- `./scripts/aws/inventory.sh` — snapshot every relevant AWS resource into `infra/aws-inventory.json` (gitignored,
  account ID redacted). Keep a local baseline copy and `diff` against future runs for drift detection on
  resources Terraform doesn't manage (key pair, instance snapshots, unrelated records in the shared
  `bytehorizonforge.com` zone).
- `terraform fmt -recursive` / `terraform validate` / `terraform plan` — routine Terraform hygiene. `apply` and
  `destroy` are operator-only.
- `scripts/server/update.py --help` — the on-host updater (lands at `/usr/local/sbin/minecraft-update.py`). Runs
  daily under `minecraft-update.timer` (fires at `*-*-* 10:00:00 UTC` with a `RandomizedDelaySec=3600` jitter —
  ~5 AM CDT, genuine off-peak). Sha256-driven: skips when on-disk jars match the API. Refuses to bump Paper
  across MC versions, never touches `plugins/floodgate/key.pem`.
- `scripts/server/deploy-updater.sh --help` — operator-side script for the existing hand-built live host. Reads
  `.env` for SSH details, rsyncs `update.py` + `_common.py` to `/usr/local/sbin/`, then invokes the updater's
  `--install-systemd-units` mode to drop the timer + service units. Idempotent end-to-end. Use this rather than
  re-running `install.py` against a host that was never tested with the provisioner.

## License

This project is licensed under the [MIT License](LICENSE).
