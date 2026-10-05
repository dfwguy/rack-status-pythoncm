#!/usr/bin/env bash
set -euo pipefail

roster=${1:?eligible roster file is required}
label=${2:-fleet}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
parallelism=${NVIS_P2P_SYNC_PARALLELISM:-48}
start_parallelism=${NVIS_P2P_SYNC_START_PARALLELISM:-12}
max_start_attempts=${NVIS_P2P_SYNC_MAX_START_ATTEMPTS:-3}
start_retry_seconds=${NVIS_P2P_SYNC_START_RETRY_SECONDS:-5}
poll_seconds=${NVIS_P2P_SYNC_POLL_SECONDS:-60}
max_unreachable=${NVIS_P2P_SYNC_MAX_UNREACHABLE:-3}
max_failed_checks=${NVIS_P2P_SYNC_MAX_FAILED_CHECKS:-2}
timeout_seconds=${NVIS_P2P_SYNC_TIMEOUT_SECONDS:-86400}
start_helper=${NVIS_P2P_SYNC_START_HELPER:-$root/scripts/start-catalog-sync-node.sh}
check_helper=${NVIS_P2P_SYNC_CHECK_HELPER:-$root/scripts/check-catalog-sync-node.sh}

[[ $parallelism =~ ^[1-9][0-9]*$ && $parallelism -ge 1 && $parallelism -le 64 ]] || {
  echo "SYNC_BAD_PARALLELISM value=$parallelism expected=1..64" >&2; exit 64;
}
[[ $start_parallelism =~ ^[1-9][0-9]*$ && $start_parallelism -le 16 ]] || {
  echo "SYNC_BAD_START_PARALLELISM value=$start_parallelism expected=1..16" >&2; exit 64;
}
for value in "$max_start_attempts" "$start_retry_seconds" "$poll_seconds" "$max_unreachable" "$max_failed_checks" "$timeout_seconds"; do
  [[ $value =~ ^[1-9][0-9]*$ ]] || { echo "SYNC_BAD_LIMIT value=$value" >&2; exit 64; }
done
[[ -r $roster ]] || { echo "SYNC_ROSTER_UNREADABLE roster=$roster" >&2; exit 66; }
[[ -x $start_helper && -x $check_helper ]] || { echo "SYNC_HELPER_NOT_EXECUTABLE" >&2; exit 69; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
wave=${NVIS_P2P_SYNC_WAVE:-$stamp-$$}
[[ $wave =~ ^[A-Za-z0-9._-]+$ ]] || { echo "SYNC_BAD_WAVE value=$wave" >&2; exit 64; }
run_dir=$root/state/catalog-sync-$label-$wave
mkdir -p "$run_dir"
nodes=$run_dir/nodes
awk 'NF && $1 !~ /^#/ {print $1}' "$roster" | sort -u > "$nodes"
node_count=$(wc -l < "$nodes")
[[ $node_count -gt 0 ]] || { echo "SYNC_EMPTY_ROSTER roster=$roster" >&2; exit 64; }
if awk '$0 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/ {bad=1} END {exit bad ? 0 : 1}' "$nodes"; then
  echo "SYNC_INVALID_NODE roster=$roster" >&2; exit 64
fi

printf 'node\tunreachable_checks\tfailed_checks\tlast_state\tlast_detail\tservice_state\n' > "$run_dir/tracking.tsv"
awk '{printf "%s\t0\t0\tNEW\t-\t-\n", $0}' "$nodes" >> "$run_dir/tracking.tsv"
printf 'node\ttimestamp_utc\tcompletion\n' > "$run_dir/complete.tsv"
printf 'node\treason\ttimestamp_utc\n' > "$run_dir/deferred.tsv"
log=$run_dir/sync-wave.log
exec > >(tee -a "$log") 2>&1

echo "SYNC_WAVE_BEGIN label=$label wave=$wave nodes=$node_count run_dir=$run_dir"
: > "$run_dir/started.nodes"
: > "$run_dir/start.tsv"
for ((attempt=1; attempt<=max_start_attempts; attempt++)); do
  awk 'FILENAME==ARGV[1] {done[$1]=1; next} !($1 in done) {print $1}' \
    "$run_dir/started.nodes" "$nodes" > "$run_dir/start-pending.nodes"
  start_pending=$(wc -l < "$run_dir/start-pending.nodes")
  (( start_pending > 0 )) || break
  xargs -r -P "$start_parallelism" -I{} "$start_helper" \
    "{}" "$wave" < "$run_dir/start-pending.nodes" > "$run_dir/start-attempt-$attempt.tsv"
  cat "$run_dir/start-attempt-$attempt.tsv" >> "$run_dir/start.tsv"
  while IFS=$'\t' read -r node state detail; do
    if [[ $state == STARTED ]]; then
      printf '%s\n' "$node" >> "$run_dir/started.nodes"
    elif (( attempt == max_start_attempts )); then
      safe_detail=$(printf '%s' "$detail" | tr '\t\r\n' '   ' | cut -c1-180)
      printf '%s\tstart_%s_after_%s_attempts:%s\t%s\n' \
        "$node" "${state,,}" "$attempt" "$safe_detail" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
    fi
  done < "$run_dir/start-attempt-$attempt.tsv"
  sort -u -o "$run_dir/started.nodes" "$run_dir/started.nodes"
  started=$(wc -l < "$run_dir/started.nodes")
  echo "SYNC_WAVE_START attempt=$attempt started=$started pending=$((node_count-started))"
  if (( attempt < max_start_attempts && started < node_count )); then
    sleep "$start_retry_seconds"
  fi
done

start_epoch=$(date +%s)
iteration=0
while :; do
  iteration=$((iteration + 1))
  now_epoch=$(date +%s)
  elapsed=$((now_epoch - start_epoch))
  { awk 'NR>1 {print $1}' "$run_dir/complete.tsv"; awk 'NR>1 {print $1}' "$run_dir/deferred.tsv"; } > "$run_dir/done.nodes"
  awk 'FILENAME==ARGV[1] {done[$1]=1; next} !($1 in done) {print $1}' \
    "$run_dir/done.nodes" "$nodes" > "$run_dir/pending.nodes"
  pending=$(wc -l < "$run_dir/pending.nodes")
  (( pending > 0 )) || break

  xargs -r -P "$parallelism" -I{} "$check_helper" \
    "{}" "$wave" < "$run_dir/pending.nodes" > "$run_dir/check-$iteration.tsv"
  awk -F '\t' -v OFS='\t' '
    BEGIN {print "node","unreachable_checks","failed_checks","last_state","last_detail","service_state"}
    NR==FNR {if (FNR>1) {u[$1]=$2; f[$1]=$3}; next}
    {print $1,u[$1]+($2=="UNREACHABLE"),f[$1]+($2=="FAILED"),$2,$3,$4}' \
    "$run_dir/tracking.tsv" "$run_dir/check-$iteration.tsv" > "$run_dir/tracking.next"
  mv "$run_dir/tracking.next" "$run_dir/tracking.tsv"

  while IFS=$'\t' read -r node unreachable failed state detail service_state; do
    [[ $node == node ]] && continue
    case $state in
      COMPLETE)
        printf '%s\t%s\t%s\n' "$node" "$(date -u +%FT%TZ)" "$detail" >> "$run_dir/complete.tsv"
        ;;
      UNREACHABLE)
        if (( unreachable >= max_unreachable )); then
          printf '%s\tunreachable_after_%s_checks\t%s\n' "$node" "$unreachable" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
        fi
        ;;
      FAILED)
        if (( failed >= max_failed_checks )); then
          safe_detail=$(printf '%s' "$detail" | tr '\t\r\n' '   ' | cut -c1-180)
          printf '%s\tservice_failed_after_%s_checks:%s\t%s\n' "$node" "$failed" "$safe_detail" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
        fi
        ;;
      PENDING) ;;
      *) printf '%s\tunknown_state:%s\t%s\n' "$node" "$state" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv" ;;
    esac
  done < "$run_dir/tracking.tsv"

  complete=$(($(wc -l < "$run_dir/complete.tsv") - 1))
  deferred=$(($(wc -l < "$run_dir/deferred.tsv") - 1))
  echo "SYNC_WAVE_PROGRESS iteration=$iteration complete=$complete deferred=$deferred pending=$((node_count-complete-deferred)) elapsed_seconds=$elapsed"
  (( complete + deferred == node_count )) && break
  if (( elapsed >= timeout_seconds )); then
    { awk 'NR>1 {print $1}' "$run_dir/complete.tsv"; awk 'NR>1 {print $1}' "$run_dir/deferred.tsv"; } > "$run_dir/done.nodes"
    awk 'FILENAME==ARGV[1] {done[$1]=1; next} !($1 in done) {print $1}' \
      "$run_dir/done.nodes" "$nodes" | while read -r node; do
        printf '%s\ttimeout_after_%ss\t%s\n' "$node" "$elapsed" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
      done
    break
  fi
  sleep "$poll_seconds"
done

complete=$(($(wc -l < "$run_dir/complete.tsv") - 1))
deferred=$(($(wc -l < "$run_dir/deferred.tsv") - 1))
printf 'label\twave\ttotal\tcomplete\tdeferred\tfinished_utc\n%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$label" "$wave" "$node_count" "$complete" "$deferred" "$(date -u +%FT%TZ)" > "$run_dir/summary.tsv"
if (( complete + deferred != node_count )); then
  echo "SYNC_WAVE_INCOMPLETE total=$node_count complete=$complete deferred=$deferred run_dir=$run_dir" >&2
  exit 1
elif (( deferred > 0 )); then
  echo "SYNC_WAVE_COMPLETE_WITH_DEFERRALS total=$node_count complete=$complete deferred=$deferred run_dir=$run_dir"
else
  echo "SYNC_WAVE_COMPLETE total=$node_count complete=$complete deferred=0 run_dir=$run_dir"
fi
