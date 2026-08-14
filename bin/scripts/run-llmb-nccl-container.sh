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
# Audited allocation-scoped entry point for the containerized LLMB NCCL 24-test sweep.
set -euo pipefail

usage() {
  echo "Usage: $0 --nodelist HOSTLIST [--storage local|nfs] [--rsync] [--log-dir PATH] [--dry-run]" >&2
}
nodelist=""; storage=local; rsync_enabled=1
local_dir=/tmp/containers; log_dir="${RACK_STATUS_LOG_DIR:-$PWD}"; dry_run=0
while (($#)); do
  case "$1" in
    --nodelist) nodelist="${2:?}"; shift 2 ;;
    --nodelist=*) nodelist="${1#*=}"; shift ;;
    --storage) storage="${2:?}"; shift 2 ;;
    --storage=*) storage="${1#*=}"; shift ;;
    --rsync) rsync_enabled=1; shift ;;
    --no-rsync) rsync_enabled=0; shift ;;
    --local-container-dir) local_dir="${2:?}"; shift 2 ;;
    --local-container-dir=*) local_dir="${1#*=}"; shift ;;
    --log-dir) log_dir="${2:?}"; shift 2 ;;
    --log-dir=*) log_dir="${1#*=}"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$nodelist" ]] || { echo "ERROR: --nodelist is required" >&2; exit 2; }
[[ "$storage" == local || "$storage" == nfs ]] || { echo "ERROR: storage must be local or nfs" >&2; exit 2; }
[[ "$local_dir" == /tmp/containers ]] || { echo "ERROR: local container dir is fixed to /tmp/containers" >&2; exit 2; }
[[ "$storage" == local || "$rsync_enabled" == 0 ]] || { echo "ERROR: rsync requires local storage" >&2; exit 2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_image=/cluster/containers/nvidia+nemo+26.04.01-arm.sqsh
local_image=/tmp/containers/nvidia+nemo+26.04.01-arm.sqsh
run_image="$source_image"; [[ "$storage" == local ]] && run_image="$local_image"
expected_size=42245001216
expected_sha=55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc
expected_nccl=22903
expected_cuda=13.1

print_cmd() { printf 'PLAN:'; printf ' %q' "$@"; printf '\n'; }
if (( dry_run )); then
  node_count="${NVIS_PREPARED_NODE_COUNT:?NVIS_PREPARED_NODE_COUNT is required for offline dry-run}"
  print_cmd sbatch --wait --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4 \
    --export="ALL,NVIS_STAGE_SOURCE=${source_image},NVIS_STAGE_DEST=${run_image},NVIS_STAGE_RSYNC=${rsync_enabled},NVIS_STAGE_CONTAINER_PREFLIGHT=1,NVIS_STAGE_EXPECTED_GPUS=4,NVIS_STAGE_EXPECTED_NCCL=${expected_nccl},NVIS_STAGE_EXPECTED_CUDA=${expected_cuda},NVIS_STAGE_EXPECTED_SIZE=${expected_size},NVIS_STAGE_EXPECTED_SHA256=${expected_sha}" \
    "${script_dir}/validation_container_stage.sbatch"
  print_cmd env RUN_CONF_IMAGE="$run_image" PMIX_MCA_gds=hash MELLANOX_VISIBLE_DEVICES=all \
    NVIS_LLMB_AUDITED_CONTAINER=1 NVIS_FABRIC_SKIP=1 NVIS_LLMB_TESTSET_APPLY=0 \
    bash "${script_dir}/run_llmb_nccl.sh" --nodelist "$nodelist"
  echo "LLMB_NCCL_CONTAINER_DRY_RUN nodes=$node_count image=$run_image tests=24 ranks_per_node=4 recipe=job147392-shape nccl=2.29.3 cuda=13.1 pmix_gds=hash-requested-scale-workaround image_provenance=fleet-staged-digest-embedded-runtime-match"
  exit 0
fi

source "${script_dir}/lib/nodes.inc.sh"
nvis_nodes_resolve --nodelist "$nodelist"
nodelist="$NVIS_NODELIST"; node_count="$NVIS_NNODES"
mkdir -p "$log_dir"

stage_export="ALL,NVIS_STAGE_SOURCE=${source_image},NVIS_STAGE_DEST=${run_image},NVIS_STAGE_RSYNC=${rsync_enabled},NVIS_STAGE_CONTAINER_PREFLIGHT=1,NVIS_STAGE_EXPECTED_GPUS=4,NVIS_STAGE_EXPECTED_NCCL=${expected_nccl},NVIS_STAGE_EXPECTED_CUDA=${expected_cuda},NVIS_STAGE_EXPECTED_SIZE=${expected_size},NVIS_STAGE_EXPECTED_SHA256=${expected_sha}"
stage_out="${log_dir%/}/llmb-nccl-container-stage-%j.out"
if [[ "$storage" == local ]]; then
  stage_text="$(sbatch --parsable --wait --no-requeue --nodes="$node_count" --nodelist="$nodelist" \
    --ntasks-per-node=1 --gpus-per-node=4 --output="$stage_out" --export="$stage_export" \
    "${script_dir}/validation_container_stage.sbatch")"
  stage_id="${stage_text%%;*}"
  [[ "$stage_id" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid stage job id: $stage_text" >&2; exit 3; }
  echo "SUBMITTED jobid=$stage_id kind=container-stage output=${stage_out//%j/$stage_id}"
else
  [[ "$(stat -c %s "$source_image" 2>/dev/null || true)" == "$expected_size" ]] || { echo "ERROR: NFS image size mismatch" >&2; exit 3; }
  [[ "$(sha256sum "$source_image" | awk '{print $1}')" == "$expected_sha" ]] || { echo "ERROR: NFS image SHA mismatch" >&2; exit 3; }
  preflight_text="$(sbatch --parsable --wait --no-requeue --nodes="$node_count" --nodelist="$nodelist" \
    --ntasks-per-node=1 --gpus-per-node=4 --output="$stage_out" \
    --export="ALL,NVIS_PREFLIGHT_IMAGE=${source_image},NVIS_PREFLIGHT_EXPECTED_NCCL=${expected_nccl},NVIS_PREFLIGHT_EXPECTED_CUDA=${expected_cuda}" "${script_dir}/validation_container_preflight.sbatch")"
  stage_id="${preflight_text%%;*}"
  [[ "$stage_id" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid preflight job id: $preflight_text" >&2; exit 3; }
  echo "SUBMITTED jobid=$stage_id kind=container-preflight output=${stage_out//%j/$stage_id}"
fi

export RUN_CONF_IMAGE="$run_image" PMIX_MCA_gds=hash MELLANOX_VISIBLE_DEVICES=all
export NVIS_LLMB_AUDITED_CONTAINER=1 NVIS_FORCE_EXPORT_VARS="RUN_CONF_IMAGE PMIX_MCA_gds MELLANOX_VISIBLE_DEVICES"
# Exact-node access on this cluster is allocation-only.  The stage/preflight
# allocation already checked the runtime and IB devices, so never perform the
# legacy head-node SSH fabric probe or rewrite the shared LLMB recipe config.
export NVIS_FABRIC_SKIP=1 NVIS_LLMB_TESTSET_APPLY=0 NVIS_LLMB_KEEP_UCX_ENV=0
export LLMB_INSTALL="${LLMB_INSTALL:-/home/cmods/workspace/llmb}"
export LLMB_REPO="${LLMB_REPO:-${LLMB_INSTALL}/llmb_repo}"
export LLMB_VENV="${LLMB_VENV:-/home/cmods/workspace/llmb_venv}"
export RACK_JOB_LOG_DIR="$log_dir"

set +e
llmb_output="$(bash "${script_dir}/run_llmb_nccl.sh" --nodelist "$nodelist" 2>&1)"
llmb_rc=$?
set -e
printf '%s\n' "$llmb_output"
job_id="$(printf '%s\n' "$llmb_output" | sed -n 's/^\([0-9][0-9]*\)\(;.*\)\{0,1\}$/\1/p' | tail -1)"
result_log="${log_dir%/}/llmb-nccl_nodes_${job_id}.out"
[[ -n "$job_id" ]] && echo "SUBMITTED jobid=$job_id kind=llmb-nccl-24test output=$result_log"
(( llmb_rc == 0 )) || { echo "LLMB_NCCL_CONTAINER_FAIL rc=$llmb_rc" >&2; exit "$llmb_rc"; }
[[ -n "$job_id" && -r "$result_log" ]] || { echo "LLMB_NCCL_CONTAINER_FAIL missing child job/output" >&2; exit 44; }
grep -q 'LLMB_NCCL_24TEST_PASS completed=24 failed=0' "$result_log" || {
  echo "LLMB_NCCL_CONTAINER_FAIL missing truthful 24-test pass marker" >&2; exit 44;
}
echo "LLMB_NCCL_CONTAINER_PASS nodes=$node_count ranks=$((node_count * 4)) tests=24 recipe=job147392-shape image=$run_image nccl=2.29.3 cuda=13.1 pmix_gds=hash-requested-scale-workaround image_provenance=fleet-staged-digest-embedded-runtime-match output=$result_log"
