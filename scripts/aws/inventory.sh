#!/usr/bin/env bash
#
# Inventory every AWS resource the LightSail Minecraft deployment depends on.
#
# Used during the issue #4 reverse-engineering to drive `terraform import`
# decisions, and re-run later as a drift-detection cross-check against
# `terraform plan`. Output is a structured JSON snapshot ordered for stable
# diff results across runs.
#
# Output is gitignored. Even with the AWS account ID redacted, the snapshot
# pulls in cross-account bucket names, DNS records for unrelated domains,
# IAM usernames, and other operator-specific data — too much to safely
# commit to a public repo. Re-runs overwrite the same file; for drift
# detection keep a local baseline copy and `diff` against it manually.
#
# Usage:
#   scripts/aws/inventory.sh [output-path]
#
# Env / .env:
#   MC_AWS_PROFILE  — AWS CLI profile from `aws configure sso`. Required.

set -euo pipefail

readonly DEFAULT_OUTPUT="infra/aws-inventory.json"
readonly REDACTED_PLACEHOLDER="<account-id>"

err() {
  printf 'inventory: %s\n' "$*" >&2
}

script_dir() {
  cd "$(dirname "$0")" && pwd
}

repo_root() {
  cd "$(script_dir)/../.." && pwd
}

require_cmd() {
  local cmd=$1
  if ! command -v "$cmd" >/dev/null 2>&1; then
    err "$cmd not found on PATH."
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
  exit 1
}

resolve_region() {
  local profile=$1 region
  region=$(aws configure get region --profile "$profile" 2>/dev/null || true)
  if [ -z "$region" ]; then
    err "Profile '$profile' has no region set in ~/.aws/config."
    err "Run: aws configure sso  (and re-enter '$profile' as the profile name)."
    exit 1
  fi
  printf '%s' "$region"
}

resolve_account_id() {
  local profile=$1 account
  account=$(aws sts get-caller-identity --profile "$profile" --query 'Account' --output text 2>/dev/null || true)
  if [ -z "$account" ] || [ "$account" = "None" ]; then
    err "aws sts get-caller-identity failed for profile '$profile'."
    err "If the SSO token is expired, recover with:"
    err "  aws sso login --profile $profile"
    exit 1
  fi
  printf '%s' "$account"
}

# Run an aws command and return its JSON output, defaulting to {} on a permission error
# so a missing-service or no-permission case doesn't kill the whole inventory.
fetch_json() {
  local description=$1
  shift
  local out
  if ! out=$(aws "$@" --output json 2>&1); then
    err "warning: '$description' failed; recording empty object. Detail:"
    err "  $(printf '%s' "$out" | head -n1)"
    printf '%s' '{}'
    return 0
  fi
  printf '%s' "$out"
}

main() {
  require_cmd aws
  require_cmd jq

  local profile region account_id
  profile=$(resolve_profile)
  region=$(resolve_region "$profile")
  account_id=$(resolve_account_id "$profile")

  local output_path
  output_path=${1:-$DEFAULT_OUTPUT}
  if [ "${output_path#/}" = "$output_path" ]; then
    output_path="$(repo_root)/$output_path"
  fi

  err "profile=$profile region=$region account=${account_id:0:4}…"
  err "output=$output_path"
  err "running AWS API queries (this can take ~10–20s)..."

  local ls_instances ls_disks ls_disk_snapshots ls_instance_snapshots
  local ls_static_ips ls_key_pairs ls_domains ls_load_balancers ls_relational_databases
  ls_instances=$(fetch_json "lightsail get-instances" --profile "$profile" lightsail get-instances)
  ls_disks=$(fetch_json "lightsail get-disks" --profile "$profile" lightsail get-disks)
  ls_disk_snapshots=$(fetch_json "lightsail get-disk-snapshots" --profile "$profile" lightsail get-disk-snapshots)
  ls_instance_snapshots=$(fetch_json "lightsail get-instance-snapshots" --profile "$profile" lightsail get-instance-snapshots)
  ls_static_ips=$(fetch_json "lightsail get-static-ips" --profile "$profile" lightsail get-static-ips)
  ls_key_pairs=$(fetch_json "lightsail get-key-pairs" --profile "$profile" lightsail get-key-pairs)
  ls_domains=$(fetch_json "lightsail get-domains" --profile "$profile" lightsail get-domains)
  ls_load_balancers=$(fetch_json "lightsail get-load-balancers" --profile "$profile" lightsail get-load-balancers)
  ls_relational_databases=$(fetch_json "lightsail get-relational-databases" \
                                       --profile "$profile" lightsail get-relational-databases)

  # Per-instance port states (firewall rules) keyed by instance name.
  local instance_names ls_port_states
  instance_names=$(printf '%s' "$ls_instances" | jq -r '(.instances // [])[].name')
  ls_port_states='{}'
  if [ -n "$instance_names" ]; then
    while IFS= read -r name; do
      [ -z "$name" ] && continue
      local ports
      ports=$(fetch_json "lightsail get-instance-port-states ($name)" \
                         --profile "$profile" lightsail get-instance-port-states --instance-name "$name")
      ls_port_states=$(printf '%s' "$ls_port_states" | \
                       jq --arg n "$name" --argjson p "$ports" '. + {($n): $p}')
    done <<<"$instance_names"
  fi

  # Route 53 (global service — no region argument).
  local r53_zones
  r53_zones=$(fetch_json "route53 list-hosted-zones" --profile "$profile" route53 list-hosted-zones)

  local zone_ids r53_records
  zone_ids=$(printf '%s' "$r53_zones" | jq -r '(.HostedZones // [])[].Id')
  r53_records='{}'
  if [ -n "$zone_ids" ]; then
    while IFS= read -r zid; do
      [ -z "$zid" ] && continue
      local short_id recs
      short_id=${zid##*/}
      recs=$(fetch_json "route53 list-resource-record-sets ($short_id)" \
                       --profile "$profile" route53 list-resource-record-sets --hosted-zone-id "$zid")
      r53_records=$(printf '%s' "$r53_records" | \
                    jq --arg id "$short_id" --argjson r "$recs" '. + {($id): $r}')
    done <<<"$zone_ids"
  fi

  # IAM is global — list metadata only, never policy documents (those can leak resource ARNs).
  local iam_users iam_roles iam_policies
  iam_users=$(fetch_json "iam list-users" --profile "$profile" iam list-users)
  iam_roles=$(fetch_json "iam list-roles" --profile "$profile" iam list-roles)
  iam_policies=$(fetch_json "iam list-policies (Local scope)" \
                            --profile "$profile" iam list-policies --scope Local)

  local s3_buckets
  s3_buckets=$(fetch_json "s3api list-buckets" --profile "$profile" s3api list-buckets)

  local snapshot
  snapshot=$(jq -n \
    --arg profile "$profile" \
    --arg region "$region" \
    --argjson ls_instances "$ls_instances" \
    --argjson ls_disks "$ls_disks" \
    --argjson ls_disk_snapshots "$ls_disk_snapshots" \
    --argjson ls_instance_snapshots "$ls_instance_snapshots" \
    --argjson ls_static_ips "$ls_static_ips" \
    --argjson ls_key_pairs "$ls_key_pairs" \
    --argjson ls_domains "$ls_domains" \
    --argjson ls_load_balancers "$ls_load_balancers" \
    --argjson ls_relational_databases "$ls_relational_databases" \
    --argjson ls_port_states "$ls_port_states" \
    --argjson r53_zones "$r53_zones" \
    --argjson r53_records "$r53_records" \
    --argjson iam_users "$iam_users" \
    --argjson iam_roles "$iam_roles" \
    --argjson iam_policies "$iam_policies" \
    --argjson s3_buckets "$s3_buckets" \
    '{
       profile: $profile,
       region: $region,
       lightsail: {
         instances: $ls_instances,
         disks: $ls_disks,
         disk_snapshots: $ls_disk_snapshots,
         instance_snapshots: $ls_instance_snapshots,
         static_ips: $ls_static_ips,
         key_pairs: $ls_key_pairs,
         domains: $ls_domains,
         load_balancers: $ls_load_balancers,
         relational_databases: $ls_relational_databases,
         instance_port_states: $ls_port_states
       },
       route53: {
         hosted_zones: $r53_zones,
         records: $r53_records
       },
       iam: {
         users: $iam_users,
         roles: $iam_roles,
         customer_managed_policies: $iam_policies
       },
       s3: {
         buckets: $s3_buckets
       }
     }')

  # Redact the AWS account ID from every ARN / UserId before writing.
  # The account ID is a 12-digit string; sed's literal-string replacement is safe
  # against false positives at this width.
  printf '%s' "$snapshot" | jq -S '.' | sed "s/${account_id}/${REDACTED_PLACEHOLDER}/g" > "$output_path"

  err "wrote $(wc -c <"$output_path" | tr -d ' ') bytes to $output_path"
  err "snapshot is gitignored — keep a baseline copy locally if you want drift detection"
}

main "$@"
