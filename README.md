# aws-hosted-minecraft

An AWS LightSail-hosted Minecraft server — Paper + Geyser + Floodgate + ViaVersion for Java/Bedrock crossplay — with
infrastructure modeled in Terraform and on-host operations captured in scripts. The configuration reflects the
original deployment; restoring an archived server and reconciling its Terraform state are not yet automated.

## What you get

- **Compute** — AWS LightSail Ubuntu 22.04 instance (`large_3_0` bundle) in `us-east-1a`, fronted by a static IPv4
  address.
- **Storage** — 128 GB LightSail data disk attached at `/dev/xvdf` and mounted at `/srv/minecraft` for the world,
  plugin state, and the Floodgate key.
- **DNS** — Route 53 A-record pointing `<mc_subdomain>.<domain_zone>` (both Terraform variables) at the static IP.
  The hosted zone must already exist in the account; the stack looks it up via a data source rather than creating it.
- **Firewall** — Java Edition on TCP 25565, Bedrock Edition on UDP 19132 (IPv4 + IPv6), HTTP on TCP 80, and SSH on
  TCP 22 restricted to LightSail Connect's range plus the operator's current public IPv4 (auto-detected via
  `api.ipify.org` at plan time, or pinned explicitly via `ssh_allowed_cidrs`). IPv6 SSH is intentionally closed.
- **Backups** — Terraform enables daily LightSail automatic snapshots while the instance exists. AWS deletes an
  instance's automatic snapshots when the instance is deleted; retain a manual snapshot independently.
- **Server stack** — Paper (Java edition core), Geyser + Floodgate (Bedrock-client crossplay without separate
  accounts), and ViaVersion (protocol bridging so older clients stay compatible across Mojang version bumps).
- **Worlds** — the observed deployment has the standard dimension worlds plus hub and creative worlds, managed by
  Multiverse Core, Inventories, and Portals. World data, plugin configuration, and player data live on the data disk.
- **Nightly auto-update** — an on-host systemd timer updates Paper / Geyser / Floodgate / ViaVersion with
  sha256-based skip-when-unchanged behavior. Paper stays on the installed Minecraft version, and the Floodgate key is
  never overwritten. Multiverse is excluded and must be maintained manually.

## Repo layout

| Path                | Purpose                                                                            |
|---------------------|------------------------------------------------------------------------------------|
| `infra/bootstrap/`  | Terraform layer that provisions the S3 + DynamoDB remote-state backend.            |
| `infra/`            | Main Terraform layer — manages the live LightSail deployment.                      |
| `scripts/aws/`      | Operator-facing AWS scripts: credential verification, drift inventory.             |
| `scripts/server/`   | On-host scripts: provisioner, updater, `deploy-updater.sh`, tests.                 |
| `docs/`             | Long-form operator walkthroughs (currently: AWS CLI auth via IAM Identity Center). |
| `server/`           | Gitignored operator-specific server state captures.                                |
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
4. **Provision a new host** — once Terraform has the LightSail instance up and the data volume is attached and mounted
   at `/srv/minecraft`, use a checkout on the host to run the provisioner. It refuses to proceed without the
   Floodgate key unless `--allow-fresh-floodgate-key` is passed for a new environment. Paper defaults to the version
   recorded on the volume; Geyser, Floodgate, and ViaVersion default to their latest releases unless pinned:

   ```bash
   sudo python3 scripts/server/install.py --help
   ```

   Multiverse installation is opt-in via `--with-multiverse`, with captured defaults of Core `5.4.0`, Inventories
   `5.3.0`, and Portals `5.1.1`. The individual `--multiverse-*-version` flags override these historical pins; check
   compatibility before using them. Existing different Multiverse jars are not replaced.

   **Do not run the provisioner blindly on a restored server.** Its defaults can replace existing plugins, and it
   starts services. Restore and verify the snapshot's existing server data instead.

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
  Minecraft versions; never touches `plugins/floodgate/key.pem`. The repository version recognizes both older and
  newer Paper version-history formats; an existing host keeps its previously deployed updater until redeployed.
- `scripts/server/deploy-updater.sh --help` — operator-side script for redeploying the updater payload + systemd
  units to an existing host without re-running `install.py`.
- `sudo python3 scripts/server/capture_state.py` — on a host with the data disk mounted, print a read-only inventory
  of recognized plugin versions and fingerprints, world names, selected server properties, and configuration/key
  presence. Raw configs, player lists, and key contents are omitted. The output is still operator-specific; if saved
  in a local checkout, use a private `server/state*.json` file, which is gitignored.

## Archiving and restoring

A manual instance snapshot can preserve the attached data disk as well as the instance, but check the snapshot's
disk list and availability before relying on it. A snapshot taken while Minecraft is running is not proof of
application-level consistency. Keep an independent copy, such as the original data disk, until a separate restore
has been verified, including the worlds, player data, Multiverse configuration, and the existing Floodgate key.
Restoring an instance may automatically start Minecraft and its persistent updater timer; this repository has no
tested mechanism to hold them before first boot. Retained disks and snapshots continue to incur AWS charges.

This repository does not yet automate restoring an instance snapshot or reconciling Terraform after manual deletion.
**Do not run a normal `terraform apply` after deleting the instance:** the current configuration describes a new
instance, not a snapshot restore, and may recreate an empty server. Review and reconcile the infrastructure and state
before any future apply. Do not generate or replace `plugins/floodgate/key.pem`.

If a future host has an existing ext4 data filesystem that needs mounting, identify its filesystem UUID on that host
using a read-only inspection such as `lsblk -f` or `sudo blkid`. From the host's checkout, preview and then run the
mount helper with that UUID:

```bash
sudo python3 scripts/server/mount_data.py --uuid YOUR-FILESYSTEM-UUID --dry-run
sudo python3 scripts/server/mount_data.py --uuid YOUR-FILESYSTEM-UUID
```

The helper does not format disks; it refuses ambiguous UUIDs, conflicting mounts, conflicting target or UUID fstab
entries, and a nonempty unmounted target. Snapshot clones can share a filesystem UUID, so do not attach multiple copies
with the same UUID and expect the helper to choose one. The generated Minecraft service checks that the data
directory is a mountpoint before starting; an existing host receives that guard only if its service unit is updated
separately.

## License

This project is licensed under the [MIT License](LICENSE).
