#!/usr/bin/env bash
#
# Verify AWS CLI credentials are configured for the operator's IAM Identity Center profile.
#
# Read-only. Run after Checkpoint E of docs/aws-auth-setup.md (and any time you suspect
# the SSO token has expired or the profile drifted). Exit 0 on success; non-zero with a
# copy-paste-able recovery command on failure.

set -euo pipefail

readonly REQUIRED_CLI_MAJOR=2
readonly REQUIRED_CLI_MINOR=22
readonly INSTALL_DOC_URL="https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
readonly SSO_DOC_URL="https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html"
readonly EXPECTED_ARN_PATTERN='^arn:aws:sts::[0-9]+:assumed-role/AWSReservedSSO_AdministratorAccess_[A-Za-z0-9]+/'

err() {
  printf 'verify-credentials: %s\n' "$*" >&2
}

script_dir() {
  cd "$(dirname "$0")" && pwd
}

repo_root() {
  cd "$(script_dir)/../.." && pwd
}

check_aws_cli() {
  if ! command -v aws >/dev/null 2>&1; then
    err "AWS CLI not found on PATH."
    err "Install AWS CLI v${REQUIRED_CLI_MAJOR}.${REQUIRED_CLI_MINOR}+ from:"
    err "  ${INSTALL_DOC_URL}"
    exit 1
  fi

  local version_line version major minor
  version_line=$(aws --version 2>&1)
  version=$(printf '%s\n' "$version_line" | sed -E 's|^aws-cli/([0-9]+\.[0-9]+).*|\1|')

  if ! printf '%s' "$version" | grep -Eq '^[0-9]+\.[0-9]+$'; then
    err "Could not parse AWS CLI version from: ${version_line}"
    err "Expected output starting with 'aws-cli/<major>.<minor>.<patch>'."
    exit 1
  fi

  major=$(printf '%s' "$version" | cut -d. -f1)
  minor=$(printf '%s' "$version" | cut -d. -f2)

  if [ "$major" -lt "$REQUIRED_CLI_MAJOR" ] \
    || { [ "$major" -eq "$REQUIRED_CLI_MAJOR" ] && [ "$minor" -lt "$REQUIRED_CLI_MINOR" ]; }; then
    err "AWS CLI v${major}.${minor} detected; need v${REQUIRED_CLI_MAJOR}.${REQUIRED_CLI_MINOR}+"
    err "(the modern sso-session block and PKCE auth are stable from v${REQUIRED_CLI_MAJOR}.${REQUIRED_CLI_MINOR})."
    err "Upgrade: ${INSTALL_DOC_URL}"
    exit 1
  fi
}

resolve_profile() {
  if [ -n "${MC_AWS_PROFILE:-}" ]; then
    printf '%s' "$MC_AWS_PROFILE"
    return 0
  fi

  local env_file
  env_file="$(repo_root)/.env"
  if [ -f "$env_file" ]; then
    local from_env
    from_env=$(grep -E '^MC_AWS_PROFILE=' "$env_file" | head -n1 | cut -d= -f2-)
    from_env=${from_env%\"}
    from_env=${from_env#\"}
    from_env=${from_env%\'}
    from_env=${from_env#\'}
    if [ -n "$from_env" ]; then
      printf '%s' "$from_env"
      return 0
    fi
  fi

  err "MC_AWS_PROFILE is not set in env or .env."
  err "Recover with:"
  err "  cp .env.example .env && \$EDITOR .env"
  err "and set MC_AWS_PROFILE to the profile name from 'aws configure sso'."
  exit 1
}

reject_placeholder() {
  local profile=$1
  if [[ "$profile" == \<*\> ]]; then
    err "MC_AWS_PROFILE looks like an unfilled placeholder ('${profile}')."
    err "Replace it with the real profile name from 'aws configure sso'."
    err "  ${SSO_DOC_URL}"
    exit 1
  fi
}

check_region() {
  local profile=$1 region
  region=$(aws configure get region --profile "$profile" 2>/dev/null || true)

  if [ -z "$region" ]; then
    err "Profile '${profile}' has no region set (or the profile does not exist) in ~/.aws/config."
    err "LightSail is region-scoped; downstream Terraform drift detection would silently target the wrong region."
    err "Recover by re-running:"
    err "  aws configure sso"
    err "and entering '${profile}' as the profile name (or edit ~/.aws/config to set 'region = <aws-region>')."
    exit 1
  fi

  printf '%s' "$region"
}

check_identity() {
  local profile=$1 out arn account
  if ! out=$(aws sts get-caller-identity \
              --profile "$profile" \
              --output text \
              --query '[Arn,Account]' 2>&1); then
    err "aws sts get-caller-identity failed for profile '${profile}':"
    printf '  %s\n' "$out" >&2
    err ""
    err "If the SSO token is expired (most common cause), recover with:"
    err "  aws sso login --profile ${profile}"
    err ""
    err "If the profile is missing or misconfigured, re-run:"
    err "  aws configure sso"
    err "  ${SSO_DOC_URL}"
    exit 1
  fi

  arn=$(printf '%s\n' "$out" | awk 'NR==1 {print $1}')
  account=$(printf '%s\n' "$out" | awk 'NR==1 {print $2}')

  if ! printf '%s' "$arn" | grep -Eq "$EXPECTED_ARN_PATTERN"; then
    err "Caller ARN does not match the expected IdC AdministratorAccess shape:"
    err "  Got:      ${arn}"
    err "  Expected: arn:aws:sts::<account-id>:assumed-role/AWSReservedSSO_AdministratorAccess_<hash>/<username>"
    err ""
    err "Common causes:"
    err "  - Logged in with an IAM user (long-lived access key) instead of IdC."
    err "  - Permission set is not 'AdministratorAccess' (e.g., 'PowerUserAccess', 'ViewOnlyAccess')."
    err "  - Different role assumed via 'aws sts assume-role' or an STS profile chain."
    err ""
    err "Re-run 'aws configure sso' with AdministratorAccess, then: aws sso login --profile ${profile}"
    exit 1
  fi

  printf '%s\t%s' "$arn" "$account"
}

main() {
  check_aws_cli

  local profile
  profile=$(resolve_profile)
  reject_placeholder "$profile"

  local region
  region=$(check_region "$profile")

  local identity arn account
  identity=$(check_identity "$profile")
  arn=$(printf '%s' "$identity" | cut -f1)
  account=$(printf '%s' "$identity" | cut -f2)

  printf '\n'
  printf 'verify-credentials: OK\n'
  printf '  profile : %s\n' "$profile"
  printf '  region  : %s\n' "$region"
  printf '  account : %s\n' "$account"
  printf '  arn     : %s\n' "$arn"
}

main "$@"
