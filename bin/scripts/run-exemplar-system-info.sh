#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Submit NVIDIA DGXC v26.06.01 system_info once per exact node, wait, and classify
# markers because the upstream recipe deliberately treats some failures as nonfatal.
set -euo pipefail
nodelist=""; image=""; log_dir=""; dependency=""
while (($#)); do
  case "$1" in
    --nodelist) nodelist="${2:?}"; shift 2 ;;
    --image) image="${2:?}"; shift 2 ;;
    --log-dir) log_dir="${2:?}"; shift 2 ;;
    --dependency=*) dependency="$1"; shift ;;
    *) echo "ERROR: unknown system_info argument: $1" >&2; exit 2 ;;
  esac
done
: "${nodelist:?}"; : "${image:?}"; : "${log_dir:?}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "${script_dir}/lib/nodes.inc.sh" ]] || { echo "ERROR: nodes library missing" >&2; exit 2; }
# shellcheck disable=SC1091
source "${script_dir}/lib/nodes.inc.sh"
nvis_nodes_resolve --nodelist "$nodelist"
mapfile -t nodes < <(scontrol show hostnames "$NVIS_NODELIST")
launch="${LLMB_REPO:-/home/cmods/workspace/llmb/llmb_repo}/microbenchmarks/system_info/launch.sh"
[[ -r "$launch" ]] || { echo "ERROR: official v26.06.01 system_info launch missing" >&2; exit 2; }
mkdir -p "$log_dir"
declare -a job_ids=() job_nodes=() job_outputs=()
submit_failed=0
for node in "${nodes[@]}"; do
  output="${log_dir%/}/exemplar-system-info-${node}-%j.out"
  cmd=(sbatch --parsable --no-requeue --nodes=1 --nodelist="$node" --output="$output")
  [[ -n "$dependency" ]] && cmd+=("$dependency")
  cmd+=(--export="ALL,RUN_CONF_IMAGE=${image},MELLANOX_VISIBLE_DEVICES=all,GPU_TYPE=gb300,LLMB_INSTALL=${LLMB_INSTALL:-/home/cmods/workspace/llmb},LLMB_REPO=${LLMB_REPO:-/home/cmods/workspace/llmb/llmb_repo}" "$launch")
  set +e; submitted="$("${cmd[@]}" 2>&1)"; rc=$?; set -e
  job_id="${submitted%%;*}"
  if (( rc != 0 )) || [[ ! "$job_id" =~ ^[0-9]+$ ]]; then
    echo "EXEMPLAR_SYSTEM_INFO_SUBMIT_FAIL node=$node detail=$submitted" >&2
    ((submit_failed+=1)); continue
  fi
  job_ids+=("$job_id"); job_nodes+=("$node"); job_outputs+=("${output//%j/$job_id}")
  echo "EXEMPLAR_SYSTEM_INFO_SUBMITTED node=$node jobid=$job_id output=${job_outputs[-1]}"
done

deadline=$((SECONDS + ${NVIS_SYSINFO_WAIT_SEC:-10800}))
joined="$(IFS=,; echo "${job_ids[*]}")"
while [[ -n "$joined" ]] && squeue -h -j "$joined" 2>/dev/null | grep -q .; do
  (( SECONDS < deadline )) || { echo "EXEMPLAR_SYSTEM_INFO_TIMEOUT jobs=$joined" >&2; exit 6; }
  sleep 5
done

failed=$submit_failed; warned=0; passed=0
for index in "${!job_ids[@]}"; do
  job_id="${job_ids[$index]}"; node="${job_nodes[$index]}"; resolved="${job_outputs[$index]}"
  state="$(sacct -X -n -j "$job_id" -o State 2>/dev/null | awk 'NF {print $1; exit}' | cut -d+ -f1)"
  result=pass
  if [[ "$state" != COMPLETED ]] || [[ ! -r "$resolved" ]] || ! grep -q 'System Info Collection Summary' "$resolved"; then result=fail
  elif grep -Eq 'Failed non-fatal steps: [1-9][0-9]*' "$resolved"; then result=findings
  elif grep -Eq '\[WARNING\]|WARNING' "$resolved"; then result=warning
  fi
  case "$result" in pass) ((passed+=1));; warning) ((warned+=1));; findings) ((warned+=1));; fail) ((failed+=1));; esac
  printf 'EXEMPLAR_SYSTEM_INFO_RESULT node=%s jobid=%s result=%s output=%s\n' "$node" "${job_id:-unknown}" "$result" "$resolved"
done
echo "EXEMPLAR_SYSTEM_INFO_SUMMARY passed=$passed warning=$warned failed=$failed total=${#nodes[@]}"
(( failed == 0 ))
