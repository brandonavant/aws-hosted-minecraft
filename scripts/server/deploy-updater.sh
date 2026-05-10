#!/usr/bin/env bash
# Deploy the Minecraft updater to the live host. Reads SSH details from the repo-root .env, rsyncs
# update.py + _common.py to /usr/local/sbin/ on the host, then SSHs in to invoke the updater's
# --install-systemd-units mode so the systemd timer + service units land idempotently.
#
# Designed to be re-runnable: rsync ships only changed bytes, and the self-install mode is a no-op when
# the units already match the desired state. The operator runs this whenever update.py or _common.py
# changes locally; the live host catches up without any manual SSH step.
#
# Why not bake this into install.py? install.py is the fresh-host provisioner — it runs on a brand-new
# LightSail instance during initial bring-up. This script is the lower-friction path for the existing
# hand-built live host, where running install.py is undesired (it would touch apt, users, the systemd
# minecraft.service, etc.). deploy-updater.sh ships ONLY the updater bits.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
REMOTE_INSTALL_DIR="/usr/local/sbin"
REMOTE_UPDATER_PATH="${REMOTE_INSTALL_DIR}/minecraft-update.py"
SOURCE_FILES=("update.py" "_common.py")

usage() {
  cat <<'EOF'
Usage: scripts/server/deploy-updater.sh [--dry-run] [-h|--help]

Deploys update.py + _common.py to the live host's /usr/local/sbin/ and runs the updater's
--install-systemd-units mode to install the systemd timer and service units.

Reads .env at the repo root for SSH details:
  MC_SSH_HOST   IPv4 or DNS name of the live host.
  MC_SSH_USER   SSH login user (e.g. ubuntu on LightSail Ubuntu).
  MC_SSH_KEY    Path to the SSH private key (.pem).

Idempotent end-to-end:
  - rsync ships zero bytes when the source files match the live host's copies.
  - --install-systemd-units rewrites unit files only on content drift, runs daemon-reload only when
    units change, and enable --now is a no-op when the timer is already active.

Options:
  --dry-run     Print the rsync + ssh commands without executing them.
  -h, --help    Print this message and exit.
EOF
}

DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "error: unknown argument $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ ! -f "${ENV_FILE}" ]; then
  echo "error: ${ENV_FILE} not found. Copy .env.example and fill in MC_SSH_*." >&2
  exit 2
fi

# Load .env without exporting unrelated variables. Only MC_SSH_* matters here. The path is runtime-
# determined so SC1090/SC1091 are suppressed at the source line.
set -a
# shellcheck disable=SC1090,SC1091
. "${ENV_FILE}"
set +a

MISSING=""
[ -z "${MC_SSH_HOST:-}" ] && MISSING="${MISSING} MC_SSH_HOST"
[ -z "${MC_SSH_USER:-}" ] && MISSING="${MISSING} MC_SSH_USER"
[ -z "${MC_SSH_KEY:-}" ] && MISSING="${MISSING} MC_SSH_KEY"
if [ -n "${MISSING}" ]; then
  echo "error: missing required .env vars:${MISSING}" >&2
  echo "       Update ${ENV_FILE} from .env.example before re-running." >&2
  exit 2
fi
if [ ! -f "${MC_SSH_KEY}" ]; then
  echo "error: MC_SSH_KEY=${MC_SSH_KEY} does not exist" >&2
  exit 2
fi

for file in "${SOURCE_FILES[@]}"; do
  if [ ! -f "${SCRIPT_DIR}/${file}" ]; then
    echo "error: source file ${SCRIPT_DIR}/${file} not found" >&2
    exit 2
  fi
done

SSH_OPTS=(-i "${MC_SSH_KEY}" -o StrictHostKeyChecking=accept-new)

run_cmd() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] $*"
  else
    echo "[run] $*"
    "$@"
  fi
}

echo "==> rsync update.py + _common.py → ${MC_SSH_USER}@${MC_SSH_HOST}:${REMOTE_INSTALL_DIR}/"
# --rsync-path="sudo rsync" runs the remote-side rsync as root so it can write to /usr/local/sbin.
# LightSail's default ubuntu user has NOPASSWD sudo, so this stays non-interactive.
RSYNC_SOURCES=()
for file in "${SOURCE_FILES[@]}"; do
  RSYNC_SOURCES+=("${SCRIPT_DIR}/${file}")
done
run_cmd rsync \
  -e "ssh ${SSH_OPTS[*]}" \
  --rsync-path="sudo rsync" \
  --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
  "${RSYNC_SOURCES[@]}" \
  "${MC_SSH_USER}@${MC_SSH_HOST}:${REMOTE_INSTALL_DIR}/"

# The local source is update.py; the remote install path is minecraft-update.py. rsync ships under the
# original name first; rename + chmod via a single remote sudo invocation so the destination has the
# expected basename and is executable. install -m 0755 is idempotent: a re-run with the same content is
# a content-preserving mode/owner reset.
echo "==> rename update.py → minecraft-update.py and ensure executable"
RENAME_CMD="sudo install -m 0755 -o root -g root"
RENAME_CMD="${RENAME_CMD} ${REMOTE_INSTALL_DIR}/update.py ${REMOTE_UPDATER_PATH}"
RENAME_CMD="${RENAME_CMD} && sudo rm -f ${REMOTE_INSTALL_DIR}/update.py"
run_cmd ssh "${SSH_OPTS[@]}" "${MC_SSH_USER}@${MC_SSH_HOST}" "${RENAME_CMD}"

echo "==> invoke ${REMOTE_UPDATER_PATH} --install-systemd-units"
run_cmd ssh "${SSH_OPTS[@]}" "${MC_SSH_USER}@${MC_SSH_HOST}" \
  "sudo ${REMOTE_UPDATER_PATH} --install-systemd-units"

echo "==> done. Verify with:"
echo "    ssh -i ${MC_SSH_KEY} ${MC_SSH_USER}@${MC_SSH_HOST} systemctl list-timers minecraft-update.timer"
