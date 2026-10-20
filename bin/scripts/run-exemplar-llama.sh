#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Submit the GB300 Llama 3.1 405B Exemplar pretraining gate through llmb-run.
#
# This is deliberately separate from run-llama.sh: that script is the single-rack
# inference validation, while this wrapper launches the 512-GPU training workload.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
job_log_dir="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"
mkdir -p "$job_log_dir" || { echo "ERROR: cannot create log directory: $job_log_dir" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: run-exemplar-llama.sh --dtype fp8|nvfp4 --nodelist HOSTLIST
       [--image nccl2.30.7|stock] [--tuning baseline|gb300] [--dry-run]
EOF
}

dtype=""
nodelist=""
image_profile="nccl2.30.7"
tuning="baseline"
dry_run=0
while (($#)); do
  case "$1" in
    --dtype) dtype="${2:?missing --dtype value}"; shift 2 ;;
    --dtype=*) dtype="${1#*=}"; shift ;;
    --nodelist) nodelist="${2:?missing --nodelist value}"; shift 2 ;;
    --nodelist=*) nodelist="${1#*=}"; shift ;;
    --image) image_profile="${2:?missing --image value}"; shift 2 ;;
    --image=*) image_profile="${1#*=}"; shift ;;
    --tuning) tuning="${2:?missing --tuning value}"; shift 2 ;;
    --tuning=*) tuning="${1#*=}"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$dtype" in
  fp8|nvfp4) ;;
  *) echo "ERROR: --dtype must be fp8 or nvfp4" >&2; exit 2 ;;
esac
[ -n "$nodelist" ] || { echo "ERROR: --nodelist is required" >&2; exit 2; }

case "$image_profile" in
  nccl2.30.7)
    run_image="${NVIS_EXEMPLAR_LLAMA_IMAGE_NCCL2307:-${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh}"
    ;;
  stock)
    run_image="${NVIS_EXEMPLAR_LLAMA_IMAGE_STOCK:-${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}/nvidia+nemo+26.04.01-arm.sqsh}"
    ;;
  *) echo "ERROR: --image must be nccl2.30.7 or stock" >&2; exit 2 ;;
esac

case "$tuning" in
  baseline|gb300) ;;
  *) echo "ERROR: --tuning must be baseline or gb300" >&2; exit 2 ;;
esac

export SLURM_CONF="${SLURM_CONF:-/cm/shared-ubuntu2404-aarch64/apps/slurm/etc/slurm/slurm.conf}"
export PATH="/cm/local/apps/slurm/25.05/bin:${PATH}"
command -v scontrol >/dev/null || { echo "ERROR: scontrol not found" >&2; exit 2; }
command -v sinfo >/dev/null || { echo "ERROR: sinfo not found" >&2; exit 2; }
command -v squeue >/dev/null || { echo "ERROR: squeue not found" >&2; exit 2; }

mapfile -t nodes < <(scontrol show hostnames "$nodelist")
if ((${#nodes[@]} != 128)); then
  echo "ERROR: the 512-GPU gate requires exactly 128 GB300 tray hosts; resolved ${#nodes[@]}" >&2
  exit 2
fi
unique_nodes=$(printf '%s\n' "${nodes[@]}" | sort -u | wc -l)
if ((unique_nodes != 128)); then
  echo "ERROR: nodelist contains duplicates (${unique_nodes} unique of 128)" >&2
  exit 2
fi

# Bring-up DCGM jobs are deliberately yieldable: once the Llama allocation is
# pending, /cluster/bringup's GET_OUT_OF_THE_WAY supervisor cancels only its own
# bringup-* jobs.  Permit that one allocation class here so llmb-run can create
# the pending higher-priority job.  Genuine workloads and every drain/down state
# remain a hard stop.
bad_state=""
while IFS='|' read -r node state gres; do
  if [[ "$gres" != gpu:b300:4* ]]; then
    bad_state+="${node}|${state}|${gres}|expected four B300 GRES entries"$'\n'
    continue
  fi
  case "$state" in
    idle)
      ;;
    allocated)
      mapfile -t running_jobs < <(squeue -h -t RUNNING -w "$node" -o '%j')
      if ((${#running_jobs[@]} == 0)); then
        bad_state+="${node}|${state}|${gres}|allocated without a visible running job"$'\n'
        continue
      fi
      for job_name in "${running_jobs[@]}"; do
        if [[ "$job_name" != bringup-* ]]; then
          bad_state+="${node}|${state}|${gres}|non-yieldable job ${job_name}"$'\n'
        fi
      done
      ;;
    *)
      bad_state+="${node}|${state}|${gres}|state is neither idle nor yieldable bring-up allocation"$'\n'
      ;;
  esac
done < <(sinfo -N -h -n "$nodelist" -o '%N|%T|%G')
if [ -n "$bad_state" ]; then
  echo "ERROR: targets must be idle or allocated only to yieldable bringup-* jobs, with four B300 GRES entries:" >&2
  printf '%s' "$bad_state" >&2
  exit 2
fi

# Under the cluster's workspace (project setting -> NVIS_WORKSPACE_DIR; workspace-dir-common.sh).
# shellcheck source=lib/llmb_paths.inc.sh
. "${script_dir}/lib/llmb_paths.inc.sh" || exit 2
llmb_install="${LLMB_INSTALL}"
llmb_run="${NVIS_LLMB_RUN:-${LLMB_VENV:+${LLMB_VENV}/bin/llmb-run}}"
llmb_run="${llmb_run:-${llmb_install}/llmb-run}"
[ -x "$llmb_run" ] || { echo "ERROR: llmb-run not executable: $llmb_run" >&2; exit 2; }
[ -f "$llmb_install/cluster_config.yaml" ] || {
  echo "ERROR: cluster_config.yaml missing under LLMB_INSTALL=$llmb_install" >&2
  exit 2
}
export LLMB_INSTALL="$llmb_install"

# Ranks rack by rack, and rank 0's host named, before the shim forces them into the job's --export:
# Megatron groups consecutive ranks, and Slurm's own node order interleaves racks whose hostnames do
# not group by rack (lib/rank_layout.inc.sh). Placement comes from Rack Status (NVIS_NODE_PLACEMENT).
# shellcheck source=lib/rank_layout.inc.sh
source "$script_dir/lib/rank_layout.inc.sh"
nvis_rank_layout run-exemplar-llama "$nodelist" 4 "$job_log_dir" \
  || echo "run-exemplar-llama: WARNING: no rank layout; ranks follow Slurm's node order" >&2

# llmb-run owns the inner sbatch call. Reuse the production shim so the outer
# Slurm stdout cannot fall back to the LLMB workspace during a manual launch.
# shellcheck disable=SC1091
source "$script_dir/lib/sbatch_pin.inc.sh"
if ((!dry_run)); then
  nvis_sbatch_pin_install "$nodelist" "${job_log_dir%/}/exemplar-llama-${dtype}-512GPU_%j.out"
  trap nvis_sbatch_pin_cleanup EXIT
fi

cmd=(
  "$llmb_run" submit
  --workload pretrain_llama3.1
  --model-size 405b
  --dtype "$dtype"
  --scale 512
  --nodelist "$nodelist"
  --dump-env
  --env "RUN_CONF_IMAGE=$run_image"
  # The v26.06.01 recipe consumes MAX_STEPS and appends --max_steps=$MAX_STEPS.
  # Keep this immutable: a 13-step "quick" interpretation is not validation.
  --env MAX_STEPS=50
  --env RUN_CONF_MOUNTS=/dev/infiniband
  --env MELLANOX_VISIBLE_DEVICES=all
  # Required on this cluster until NVLink-domain CliqueIds are unique.
  --env NCCL_NVLS_ENABLE=0
)

# Keep the clean NCCL A/B profile isolated from the broader tuning experiment.
if [ "$tuning" = gb300 ]; then
  cmd+=(
    --env NCCL_IB_QPS_PER_CONNECTION=4
    --env NVTE_NORM_FWD_USE_CUDNN=1
    --env NVTE_NORM_BWD_USE_CUDNN=1
  )
fi
((dry_run)) && cmd+=(--dry-run)

printf 'Exemplar Llama 3.1 405B: platform=GB300 dtype=%s GPUs=512 NCCL-image=%s tuning=%s dry_run=%s\n' \
  "$dtype" "$image_profile" "$tuning" "$dry_run"
printf 'Target: %s\n' "$nodelist"
printf 'Image: %s\n' "$run_image"
printf 'Command:'
printf ' %q' "${cmd[@]}"
printf '\n'

cd "$job_log_dir"
submit_log="${job_log_dir%/}/exemplar-llama-${dtype}-submit-$(date -u +%Y%m%dT%H%M%SZ).log"
set +e
"${cmd[@]}" 2>&1 | tee "$submit_log"
rc=${PIPESTATUS[0]}
exit "$rc"
