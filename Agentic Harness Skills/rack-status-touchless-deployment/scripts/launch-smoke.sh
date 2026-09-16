#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
NVIS_PHASE=smoke
nvis_load_env
[[ "$NVIS_SMOKE_APPROVED" == YES ]] || nvis_die 'NVIS_SMOKE_APPROVED=YES is required'
for var in NVIS_VERIFY_URL RACK_WEB_AUTH_TOKEN_FILE NVIS_SMOKE_PRESET NVIS_SMOKE_SCOPE NVIS_SMOKE_NODES; do nvis_need_var "$var"; done
[[ "$NVIS_SMOKE_NODES" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]] || nvis_die 'invalid exact node list'
[[ "${NVIS_STAGE_ONLY:-false}" == true || "${NVIS_STAGE_ONLY:-false}" == false ]] || nvis_die 'NVIS_STAGE_ONLY must be true or false'
if [[ "${NVIS_STAGE_ONLY:-false}" == true ]]; then
  [[ "${NVIS_STAGE_APPROVED:-NO}" == YES && "$NVIS_SMOKE_PRESET" == mnubergemm-26.03-v1.17 ]] || nvis_die 'unapproved or unsupported stage-only request'
fi

cookie_file=$(mktemp); login_file=$(mktemp); request_file=$(mktemp); plan_file=$(mktemp); launch_file=$(mktemp)
trap 'rm -f "$cookie_file" "$login_file" "$request_file" "$plan_file" "$launch_file"' EXIT
nvis_cookie_login "$cookie_file" "$login_file"
IFS=, read -r -a nodes <<<"$NVIS_SMOKE_NODES"
jq -cn --arg preset "$NVIS_SMOKE_PRESET" --arg scope "$NVIS_SMOKE_SCOPE" \
  --argjson stage_only "${NVIS_STAGE_ONLY:-false}" --args \
  '$ARGS.positional as $nodes | {preset_id:$preset,scope:$scope,nodes:$nodes} + (if $stage_only then {stage_only:true,storage:"local",rsync_to_local:true} else {} end)' \
  "${nodes[@]}" >"$request_file"

curl --fail --silent --show-error --connect-timeout 5 --max-time 120 -X POST \
  "${NVIS_VERIFY_URL%/}/api/validation-presets/prepare" -b "$cookie_file" \
  -H 'Content-Type: application/json' --data-binary @"$request_file" >"$plan_file" || nvis_die 'prepare request failed'
[[ $(jq -r '.ok // false' "$plan_file") == true ]] || nvis_die "prepare rejected: $(jq -cr '.error // .message // "unknown"' "$plan_file")"
hash=$(jq -r '.plan.plan_hash // .plan_hash // empty' "$plan_file")
[[ -n "$hash" ]] || nvis_die 'prepare returned no plan hash'
requested=$(printf '%s\n' "${nodes[@]}" | LC_ALL=C sort -u | jq -Rsc 'split("\n")[:-1]')
resolved=$(jq -c '[(.plan.nodes // .nodes // [])[]] | unique | sort' "$plan_file")
[[ "$requested" == "$resolved" ]] || nvis_die "resolved target set differs: requested=$requested resolved=$resolved"
[[ $(jq -r '[(.plan.checks // .checks // [])[]? | select(type == "object" and (.ok // false) != true)] | length' "$plan_file") == 0 ]] || nvis_die 'one or more structured preparation checks failed'

jq --arg hash "$hash" '. + {plan_hash:$hash}' "$request_file" >"$launch_file.request"
trap 'rm -f "$cookie_file" "$login_file" "$request_file" "$plan_file" "$launch_file" "$launch_file.request"' EXIT
curl --fail --silent --show-error --connect-timeout 5 --max-time 120 -X POST \
  "${NVIS_VERIFY_URL%/}/api/run-validation-preset-async" -b "$cookie_file" \
  -H 'Content-Type: application/json' --data-binary @"$launch_file.request" >"$launch_file" || nvis_die 'launch request failed'
job_id=$(jq -r '.job_id // empty' "$launch_file"); activity_id=$(jq -r '.activity_id // .job_id // empty' "$launch_file")
[[ -n "$job_id" || -n "$activity_id" ]] || nvis_die "launch returned no job/activity ID: $(jq -c . "$launch_file")"
printf 'SMOKE_LAUNCHED preset=%s nodes=%s plan_hash=%s job_id=%s activity_id=%s\n' "$NVIS_SMOKE_PRESET" "$NVIS_SMOKE_NODES" "$hash" "${job_id:-none}" "${activity_id:-none}"

deadline=$((SECONDS + ${NVIS_SMOKE_TIMEOUT_SEC:-7200})); interval=${NVIS_POLL_INTERVAL_SEC:-15}
while (( SECONDS < deadline )); do
  status=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 30 -b "$cookie_file" \
    "${NVIS_VERIFY_URL%/}/api/run-validation-preset-status?job_id=$(printf %s "${job_id:-$activity_id}" | jq -sRr @uri)") || nvis_die 'terminal status request failed'
  phase=$(jq -r '(.work_phase // .phase // .state // "unknown") | ascii_downcase' <<<"$status")
  rc=$(jq -r '.returncode // empty' <<<"$status")
  case "$phase" in
    failed|error|timeout|cancelled|canceled) nvis_die "validation reached terminal failure: $phase" ;;
    done|complete|completed|passed|success|succeeded)
      [[ -z "$rc" || "$rc" == 0 ]] || nvis_die "validation completed with returncode=$rc"
      jq -e '((.evidence // .result // .results_log_path // .log_dir // "") | tostring | length > 0) or ((.tasks // []) | length > 0)' <<<"$status" >/dev/null || nvis_die 'terminal response has no durable evidence reference'
      printf 'SMOKE_TERMINAL status=%s job_id=%s activity_id=%s\n' "$phase" "${job_id:-none}" "${activity_id:-none}"
      exit 0 ;;
  esac
  sleep "$interval"
done
nvis_die "terminal state not reached within ${NVIS_SMOKE_TIMEOUT_SEC:-7200}s"
