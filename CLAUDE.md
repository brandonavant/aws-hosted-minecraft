# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Python tests

Run all unit tests from the repo root (pytest discovers `scripts/` via `pyproject.toml`):

```bash
pytest
```

Run a single test file or test:

```bash
pytest scripts/server/test_install.py
pytest scripts/server/test_update.py::test_name
```

Run e2e tests that hit real external APIs (PaperMC Fill v3, Geyser/Floodgate v2, Hangar):

```bash
pytest --run-e2e
```

Format Python code (line length is 120, configured in `pyproject.toml`):

```bash
black scripts/server/
```

Lint shell scripts:

```bash
shellcheck scripts/server/deploy-updater.sh scripts/aws/verify-credentials.sh scripts/aws/inventory.sh
```

### Terraform

Run from `infra/` (main layer) or `infra/bootstrap/` (bootstrap layer):

```bash
terraform fmt -recursive
terraform validate
terraform plan
```

`terraform apply` and `terraform destroy` are **operator-only** — never run them as an agent.

## Architecture

### Two Terraform layers

`infra/bootstrap/` creates the S3 bucket and DynamoDB table for remote state. It has no backend block (state lives on
the operator's machine). `infra/` is the main layer; it uses the bootstrap layer's outputs as its S3 backend. Run
bootstrap first on a fresh AWS account; subsequent infra work goes entirely in `infra/`.

### Three on-host Python scripts

All three scripts are stdlib-only (no pip-installable dependencies). They run as root on the LightSail host.

| Script                      | Role                                                                                                                                                                                                                                                 |
|-----------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `scripts/server/_common.py` | Shared artifact fetchers (PaperMC Fill v3, Geyser/Floodgate v2, Hangar v1), HTTP helpers, sha256 utils, and on-disk version discovery. No side effects at import time.                                                                               |
| `scripts/server/install.py` | One-time host provisioner. Installs Corretto JDK via apt, creates the `minecraft` user/group, downloads Paper + Geyser + Floodgate + ViaVersion, writes systemd units, and deploys the updater. Requires root and a mounted data volume.             |
| `scripts/server/update.py`  | Nightly updater. Compares on-disk jar sha256 against the API's latest; downloads, atomically swaps, restarts the service, and rolls back on restart failure. Installed to `/usr/local/sbin/minecraft-update.py`; driven by `minecraft-update.timer`. |

`install.py` and `update.py` import `_common.py` as a sibling module — both scripts must live in the same directory at
runtime. `deploy-updater.sh` renames `update.py` → `minecraft-update.py` when deploying to the live host, then places
`_common.py` beside it.

### Data volume layout (`/srv/minecraft`)

```
/srv/minecraft/
└── server/
    ├── paper.jar
    ├── start.sh
    ├── eula.txt
    ├── version_history.json     # Paper writes this on first start; both scripts read it for MC version
    └── plugins/
        ├── Geyser-Spigot.jar
        ├── Floodgate-Spigot.jar
        ├── ViaVersion.jar
        └── floodgate/
            └── key.pem          # NEVER touched by install.py or update.py
```

### Deploy workflow

For an existing host (where re-running `install.py` is undesired), push updater changes via:

```bash
./scripts/server/deploy-updater.sh   # reads SSH details from .env
```

The script rsyncs `update.py` + `_common.py`, renames `update.py` → `minecraft-update.py` with `sudo install`, then
invokes `--install-systemd-units` on the host.

## Hard constraints

- **`plugins/floodgate/key.pem` is never written.** Both `install.py` and `update.py` check this as an invariant.
  Regenerating the key breaks Bedrock auth for every existing player.
- **Paper is never bumped across Minecraft versions.** `update.py` clamps Paper to the running MC version's build
  stream. Moving MC versions requires re-running `install.py` with `--mc-version`.
- **All downloads are sha256-verified** before being placed. The API's reported hash is compared against the downloaded
  bytes; mismatches delete the download and raise `ChecksumMismatchError`.
- **Bash scripts target macOS bash 3.2**: no `mapfile`, no `${var,,}`, no associative arrays.

## `.env` setup

Copy `.env.example` to `.env` and fill in:

```
MC_SSH_HOST   # LightSail static IP or DNS name
MC_SSH_USER   # SSH user (default: ubuntu)
MC_SSH_KEY    # Path to PEM private key
MC_AWS_PROFILE # AWS SSO profile name (see docs/aws-auth-setup.md)
```

`scripts/aws/verify-credentials.sh` validates the AWS profile; run it whenever AWS calls fail.
