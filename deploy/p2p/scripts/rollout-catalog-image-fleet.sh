#!/usr/bin/env bash
set -euo pipefail

filename=${1:?canonical image filename is required}
roster=${2:?eligible roster file is required}
label=${3:-fleet}
root=${NVIS_P2P_ROOT:-/opt/nvis-p2p}
parallelism=${NVIS_P2P_ROLLOUT_PARALLELISM:-48}
poll_seconds=${NVIS_P2P_ROLLOUT_POLL_SECONDS:-60}
max_launches=${NVIS_P2P_ROLLOUT_MAX_LAUNCHES:-3}
max_unreachable=${NVIS_P2P_ROLLOUT_MAX_UNREACHABLE:-3}
timeout_seconds=${NVIS_P2P_ROLLOUT_TIMEOUT_SECONDS:-28800}
# A download whose written bytes have not moved for this long is stopped and relaunched (or deferred
# once it has used its launches), instead of holding the wave open until the timeout. 0 disables.
stall_seconds=${NVIS_P2P_ROLLOUT_STALL_SECONDS:-600}

[[ $parallelism =~ ^[1-9][0-9]*$ && $parallelism -le 128 ]] || {
  echo "ROLLOUT_BAD_PARALLELISM value=$parallelism expected=1..128" >&2; exit 64;
}
for value in "$poll_seconds" "$max_launches" "$max_unreachable" "$timeout_seconds"; do
  [[ $value =~ ^[1-9][0-9]*$ ]] || { echo "ROLLOUT_BAD_LIMIT value=$value" >&2; exit 64; }
done
[[ $stall_seconds =~ ^[0-9]+$ ]] || { echo "ROLLOUT_BAD_LIMIT value=$stall_seconds" >&2; exit 64; }
[[ -r $roster ]] || { echo "ROLLOUT_ROSTER_UNREADABLE roster=$roster" >&2; exit 66; }

row=$(awk -F '\t' -v image="$filename" '$3 == image {print; exit}' "$root/catalog/images.tsv")
[[ -n $row ]] || { echo "ROLLOUT_UNKNOWN filename=$filename" >&2; exit 64; }
IFS=$'\t' read -r order display_name filename expected_bytes expected_sha <<< "$row"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
run_dir=$root/state/rollout-$label-$filename-$stamp
mkdir -p "$run_dir"
nodes=$run_dir/nodes
awk 'NF && $1 !~ /^#/ {print $1}' "$roster" | sort -u > "$nodes"
node_count=$(wc -l < "$nodes")
[[ $node_count -gt 0 ]] || { echo "ROLLOUT_EMPTY_ROSTER roster=$roster" >&2; exit 64; }
if awk '$0 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/ {bad=1} END {exit bad ? 0 : 1}' "$nodes"; then
  echo "ROLLOUT_INVALID_NODE roster=$roster" >&2
  exit 64
fi

printf 'node\tlaunches\tunreachable\tlast_state\tlast_detail\n' > "$run_dir/tracking.tsv"
awk '{printf "%s\t0\t0\tNEW\t-\n", $0}' "$nodes" >> "$run_dir/tracking.tsv"
printf 'node\treason\ttimestamp_utc\n' > "$run_dir/deferred.tsv"
printf 'node\ttimestamp_utc\n' > "$run_dir/ready.tsv"
printf 'node\twritten\tsince_epoch\n' > "$run_dir/progress.tsv"

log=$run_dir/rollout.log
exec > >(tee -a "$log") 2>&1
echo "ROLLOUT_BEGIN label=$label filename=$filename nodes=$node_count run_dir=$run_dir"
"$root/scripts/preheat-catalog-image.sh" "$filename"
start_epoch=$(date +%s)
iteration=0

check_nodes() {
  local list=$1 output=$2
  xargs -r -P "$parallelism" -I{} "$root/scripts/check-catalog-image-node.sh" \
    "{}" "$filename" "$expected_bytes" "$expected_sha" < "$list" > "$output"
}

while :; do
  iteration=$((iteration + 1))
  now_epoch=$(date +%s)
  elapsed=$((now_epoch - start_epoch))
  { awk 'NR>1 {print $1}' "$run_dir/ready.tsv"; awk 'NR>1 {print $1}' "$run_dir/deferred.tsv"; } \
    > "$run_dir/done.nodes"
  awk 'FILENAME==ARGV[1] {done[$1]=1; next} !($1 in done) {print $1}' \
    "$run_dir/done.nodes" "$nodes" > "$run_dir/pending.nodes"
  pending=$(wc -l < "$run_dir/pending.nodes")
  if [[ $pending -eq 0 ]]; then
    break
  fi

  check_nodes "$run_dir/pending.nodes" "$run_dir/check-$iteration.tsv"

  # Update counters/state in one local pass. No remote sessions are retained.
  awk -F '\t' -v OFS='\t' \
    'NR==FNR {if (FNR>1) {launch[$1]=$2; unreach[$1]=$3}; next}
     BEGIN {print "node","launches","unreachable","last_state","last_detail"}
     {u=unreach[$1]+($2=="UNREACHABLE"); print $1,launch[$1],u,$2,$3}' \
    "$run_dir/tracking.tsv" "$run_dir/check-$iteration.tsv" > "$run_dir/tracking.next"
  mv "$run_dir/tracking.next" "$run_dir/tracking.tsv"

  # Progress per ACTIVE download: when its written bytes last changed. A node is stalled once that is
  # stall_seconds ago -- unless nothing can be measured yet (written=-1) or every byte is already
  # there (it is checksumming, which takes minutes on a large image and is not a stall).
  now_epoch=$(date +%s)
  : > "$run_dir/stalled.nodes"
  awk -F '\t' -v OFS='\t' -v now="$now_epoch" -v stall="$stall_seconds" -v full="$expected_bytes" \
      -v stalled="$run_dir/stalled.nodes" \
    'BEGIN {print "node","written","since_epoch"}
     NR==FNR {if (FNR>1) {prev[$1]=$2; since[$1]=$3}; next}
     $2=="ACTIVE" {
       w=-1; if (match($3, /written=-?[0-9]+/)) w=substr($3, RSTART+8, RLENGTH-8)+0
       s=(($1 in prev) && prev[$1]==w) ? since[$1] : now
       print $1, w, s
       if (stall>0 && w>=0 && w<full+0 && now-s>=stall) print $1 > stalled
     }' "$run_dir/progress.tsv" "$run_dir/check-$iteration.tsv" > "$run_dir/progress.next"
  mv "$run_dir/progress.next" "$run_dir/progress.tsv"

  while IFS=$'\t' read -r node state detail; do
    [[ $state == READY ]] || continue
    grep -Fqx "$node" <(awk 'NR>1 {print $1}' "$run_dir/ready.tsv") || \
      printf '%s\t%s\n' "$node" "$(date -u +%FT%TZ)" >> "$run_dir/ready.tsv"
  done < "$run_dir/check-$iteration.tsv"

  : > "$run_dir/relaunch.nodes"
  : > "$run_dir/stop.nodes"
  while IFS=$'\t' read -r node launches unreachable state detail; do
    [[ $node == node ]] && continue
    case $state in
      READY) ;;
      ACTIVE)
        if grep -Fqx "$node" "$run_dir/stalled.nodes"; then
          printf '%s\n' "$node" >> "$run_dir/stop.nodes"
          if (( launches < max_launches )); then
            printf '%s\n' "$node" >> "$run_dir/relaunch.nodes"
          else
            printf '%s\t%s\t%s\n' "$node" "stalled_${stall_seconds}s_after_${launches}_launches" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
          fi
        fi
        ;;
      UNREACHABLE)
        if (( unreachable >= max_unreachable )); then
          printf '%s\t%s\t%s\n' "$node" "unreachable_after_${unreachable}_checks" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
        fi
        ;;
      FAILED|BAD_SIZE|UNVERIFIED|PENDING)
        if (( launches < max_launches )); then
          printf '%s\n' "$node" >> "$run_dir/relaunch.nodes"
        else
          safe_detail=$(printf '%s' "$detail" | tr '\t\r\n' '   ' | cut -c1-160)
          printf '%s\t%s:%s\t%s\n' "$node" "launch_limit_${launches}" "$safe_detail" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
        fi
        ;;
      *)
        printf '%s\tunknown_state:%s\t%s\n' "$node" "$state" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
        ;;
    esac
  done < "$run_dir/tracking.tsv"

  stops=$(wc -l < "$run_dir/stop.nodes")
  if (( stops > 0 )); then
    # Stop first, so the relaunch below starts a fresh transfer rather than finding the old one alive.
    set +e
    xargs -r -P "$parallelism" -I{} "$root/scripts/stop-catalog-image-node.sh" \
      "{}" "$filename" < "$run_dir/stop.nodes" > "$run_dir/stop-$iteration.log" 2>&1
    stop_rc=$?
    set -e
    # A stopped node starts its stall clock again from its next measurement.
    awk -F '\t' 'NR==FNR {stopped[$1]=1; next} !($1 in stopped)' \
      "$run_dir/stop.nodes" "$run_dir/progress.tsv" > "$run_dir/progress.next"
    mv "$run_dir/progress.next" "$run_dir/progress.tsv"
    echo "ROLLOUT_STALLED iteration=$iteration nodes=$stops stall_seconds=$stall_seconds rc=$stop_rc"
  fi

  sort -u -o "$run_dir/relaunch.nodes" "$run_dir/relaunch.nodes"
  relaunches=$(wc -l < "$run_dir/relaunch.nodes")
  if (( relaunches > 0 )); then
    set +e
    xargs -r -P "$parallelism" -I{} "$root/scripts/launch-catalog-image-node.sh" \
      "{}" "$filename" < "$run_dir/relaunch.nodes" > "$run_dir/launch-$iteration.log" 2>&1
    launch_rc=$?
    set -e
    awk -F '\t' -v OFS='\t' 'NR==FNR {retry[$1]=1; next}
      FNR==1 {print; next} {if ($1 in retry) $2++; print}' \
      "$run_dir/relaunch.nodes" "$run_dir/tracking.tsv" > "$run_dir/tracking.next"
    mv "$run_dir/tracking.next" "$run_dir/tracking.tsv"
    echo "ROLLOUT_LAUNCH iteration=$iteration nodes=$relaunches rc=$launch_rc"
  fi

  ready=$(($(wc -l < "$run_dir/ready.tsv") - 1))
  deferred=$(($(wc -l < "$run_dir/deferred.tsv") - 1))
  echo "ROLLOUT_PROGRESS iteration=$iteration ready=$ready deferred=$deferred pending=$((node_count-ready-deferred)) elapsed_seconds=$elapsed"

  if (( ready + deferred == node_count )); then
    break
  fi

  if (( elapsed >= timeout_seconds )); then
    { awk 'NR>1 {print $1}' "$run_dir/ready.tsv"; awk 'NR>1 {print $1}' "$run_dir/deferred.tsv"; } \
      > "$run_dir/done.nodes"
    awk 'FILENAME==ARGV[1] {done[$1]=1; next} !($1 in done) {print $1}' \
      "$run_dir/done.nodes" "$nodes" | while read -r node; do
        printf '%s\ttimeout_after_%ss\t%s\n' "$node" "$elapsed" "$(date -u +%FT%TZ)" >> "$run_dir/deferred.tsv"
      done
    break
  fi
  sleep "$poll_seconds"
done

ready=$(($(wc -l < "$run_dir/ready.tsv") - 1))
deferred=$(($(wc -l < "$run_dir/deferred.tsv") - 1))
printf 'label\tfilename\ttotal\tready\tdeferred\tfinished_utc\n%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$label" "$filename" "$node_count" "$ready" "$deferred" "$(date -u +%FT%TZ)" > "$run_dir/summary.tsv"
if (( ready + deferred != node_count )); then
  echo "ROLLOUT_INCOMPLETE total=$node_count ready=$ready deferred=$deferred run_dir=$run_dir" >&2
  exit 1
fi
if (( deferred > 0 )); then
  echo "ROLLOUT_COMPLETE_WITH_DEFERRALS total=$node_count ready=$ready deferred=$deferred run_dir=$run_dir"
else
  echo "ROLLOUT_COMPLETE total=$node_count ready=$ready deferred=0 run_dir=$run_dir"
fi
