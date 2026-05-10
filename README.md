# aws-hosted-minecraft

An AWS LightSail-hosted Minecraft server — Paper + Geyser + Floodgate + ViaVersion for Java/Bedrock crossplay — with
infrastructure managed by Terraform and on-host operations captured in idempotent scripts. Fork this repo, point it
at a fresh AWS account, fill in a few `.env` values, and bring up an equivalent server end to end without re-deriving
any of the steps.

## What you get

- **Compute** — AWS LightSail Ubuntu 22.04 instance (`large_3_0` bundle) in `us-east-1a`, fronted by a static IPv4
  address.
- **Storage** — 128 GB LightSail data disk attached at `/dev/xvdf` and mounted at `/srv/minecraft` for the world,
  plugin state, and the Floodgate key.
- **DNS** — Route 53 A-record pointing a subdomain at the static IP (the live deployment uses
  `mc.bytehorizonforge.com`; forks override via Terraform variables).
- **Firewall** — Java Edition on TCP 25565, Bedrock Edition on UDP 19132 (IPv4 + IPv6), HTTP on TCP 80, and SSH on
  TCP 22 with the IPv4 source restricted to LightSail Connect's range.
- **Backups** — LightSail AutoSnapshot scheduled daily.
- **Server stack** — Paper (Java edition core), Geyser + Floodgate (Bedrock-client crossplay without separate
  accounts), and ViaVersion (protocol bridging so older clients stay compatible across Mojang version bumps).
- **Nightly auto-update** — an on-host systemd timer that bumps Paper / Geyser / Floodgate / ViaVersion when upstream
  ships new builds, with sha256-based skip-when-unchanged behavior and hard refusal to bump Paper across Minecraft
  versions or to overwrite the Floodgate key.

## Repo layout

| Path                | Purpose                                                                            |
|---------------------|------------------------------------------------------------------------------------|
| `infra/bootstrap/`  | Terraform layer that provisions the S3 + DynamoDB remote-state backend.            |
| `infra/`            | Main Terraform layer — manages the live LightSail deployment.                      |
| `scripts/aws/`      | Operator-facing AWS scripts: credential verification, drift inventory.             |
| `scripts/server/`   | On-host scripts: provisioner, updater, `deploy-updater.sh`, tests.                 |
| `docs/`             | Long-form operator walkthroughs (currently: AWS CLI auth via IAM Identity Center). |
| `server/`           | Reserved for on-host server state captures (placeholder).                          |
| `.claude/`          | Project rules and skills used by Claude Code.                                      |

Per-directory READMEs in `infra/` and `infra/bootstrap/`, plus `docs/aws-auth-setup.md`, carry the depth.

## Setup from scratch (forking operators)

1. **AWS authentication** — follow `docs/aws-auth-setup.md` end-to-end (Checkpoints A–F). It ends with
   `./scripts/aws/verify-credentials.sh` exiting clean.
2. **Bootstrap layer** — `cd infra/bootstrap`, copy `terraform.auto.tfvars.example` to `terraform.auto.tfvars`, pick
   a globally-unique S3 bucket name, then `terraform init` and `terraform apply`. See `infra/bootstrap/README.md` for
   the first-run workflow and the recovery path if the local state file is lost.
3. **Main layer** — `cd infra`, copy `backend-configs/prod.hcl.example` to `prod.hcl` with the bootstrap layer's
   outputs, then `terraform init -backend-config=backend-configs/prod.hcl`. The committed `.tf` files describe the
   live deployment shape, so the supported path today is the import workflow in `infra/README.md`; a clean greenfield
   apply has not been exercised end to end and may surface differences that need reconciling.
4. **Provision the host** — once Terraform has the LightSail instance up and the data volume is attached and mounted
   at `/srv/minecraft`, SSH in and run the provisioner. It is idempotent (re-runs are no-ops when state matches),
   pins Paper / Geyser / Floodgate / ViaVersion versions via flags (defaulting to the volume's existing state), and
   refuses to regenerate the Floodgate key unless `--allow-fresh-floodgate-key` is passed:

   ```bash
   sudo python3 scripts/server/install.py --help
   ```

5. **Deploy the nightly updater** — from the operator workstation, run `scripts/server/deploy-updater.sh` (reads
   `.env` for SSH details, rsyncs `update.py` + `_common.py` to `/usr/local/sbin/`, then installs the systemd timer
   and service via the updater's `--install-systemd-units` mode). Idempotent end to end. Prefer this over re-running
   `install.py` against a host that was not originally provisioned with it.

## Operating the live server

- `./scripts/aws/verify-credentials.sh` — read-only AWS auth check. Run this whenever an AWS call starts failing; it
  surfaces expired SSO tokens, missing region config, and wrong-permission-set ARNs with a copy-paste recovery
  command.
- `./scripts/aws/inventory.sh` — snapshot every relevant AWS resource into `infra/aws-inventory.json` (gitignored,
  account ID redacted). Keep a local baseline and `diff` against future runs for drift detection on resources
  Terraform does not manage (key pair, instance snapshots, unrelated records in a shared Route 53 zone).
- `terraform fmt -recursive` / `terraform validate` / `terraform plan` — routine Terraform hygiene. `apply` and
  `destroy` stay operator-driven.
- `scripts/server/update.py --help` — the on-host updater (lands at `/usr/local/sbin/minecraft-update.py`). Runs
  daily under `minecraft-update.timer` (fires at `*-*-* 10:00:00 UTC` with a `RandomizedDelaySec=3600` jitter —
  ~5 AM CDT, genuine off-peak). Sha256-driven: skips when on-disk jars match the API. Refuses to bump Paper across
  Minecraft versions; never touches `plugins/floodgate/key.pem`.
- `scripts/server/deploy-updater.sh --help` — operator-side script for redeploying the updater payload + systemd
  units to an existing host without re-running `install.py`.

## License

This project is licensed under the [MIT License](LICENSE).
