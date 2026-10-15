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
# Exact audited 256-node / 1024-GPU Llama 3.1 405B FP8 validation.
#SBATCH --account=default
#SBATCH --exclusive
#SBATCH --job-name=llama405b-fp8-1024
#SBATCH --mem=0
#SBATCH --gpus-per-node=4
#SBATCH --nodes=256
#SBATCH --ntasks-per-node=4
#SBATCH --partition=defq
#SBATCH --segment=16
#SBATCH --time=02:00:00

set -euo pipefail

: "${NVIS_LLAMA_SOURCE_IMAGE:?}"
: "${NVIS_LLAMA_RUN_IMAGE:?}"
: "${NVIS_LLAMA_STORAGE:?}"
: "${NVIS_LLAMA_RSYNC:=0}"
: "${NVIS_LLAMA_EXPECTED_NODE_SHA256:?}"
: "${NVIS_LLAMA_LOG_DIR:?}"
: "${NVIS_LLAMA_SCRIPTS_DIR:?}"
# The image's size and digest are the cluster's image catalog entry (Required by: llama31-405b-fp8),
# resolved by run-validation-preset.sh -- never literals here, which go stale on a re-import.
readonly IMAGE_BYTES="${NVIS_LAUNCH_IMAGE_BYTES:?image byte size from the image catalog (NVIS_LAUNCH_IMAGE_BYTES)}"
readonly IMAGE_SHA256="${NVIS_LAUNCH_IMAGE_SHA256:?image sha256 from the image catalog (NVIS_LAUNCH_IMAGE_SHA256)}"
# The LLMB install under the cluster's workspace (project setting connection.workspace_dir ->
# NVIS_WORKSPACE_DIR, carried here by --export=ALL); lib/llmb_paths.inc.sh derives LLMB_INSTALL.
# shellcheck source=lib/llmb_paths.inc.sh
. "${NVIS_LLAMA_SCRIPTS_DIR}/lib/llmb_paths.inc.sh" || exit 2
readonly PERF_ROOT=${LLMB_INSTALL}/workloads/pretrain_llama3.1/Megatron-Bridge/scripts/performance

mapfile -t assigned_nodes < <(scontrol show hostnames "${SLURM_JOB_NODELIST}" | sort)
[[ "${#assigned_nodes[@]}" -eq 256 ]] || { echo "ALLOCATION_GATE_FAIL nodes=${#assigned_nodes[@]} expected=256" >&2; exit 10; }
assigned_sha="$(printf '%s\n' "${assigned_nodes[@]}" | sha256sum | awk '{print $1}')"
[[ "$assigned_sha" == "$NVIS_LLAMA_EXPECTED_NODE_SHA256" ]] || {
  echo "ALLOCATION_GATE_FAIL sha=$assigned_sha expected=$NVIS_LLAMA_EXPECTED_NODE_SHA256" >&2; exit 11;
}
echo "ALLOCATION_GATE_READY nodes=256 gpus=1024 sha256=$assigned_sha"

source_size="$(stat -c %s "$NVIS_LLAMA_SOURCE_IMAGE" 2>/dev/null || true)"
[[ "$source_size" == "$IMAGE_BYTES" ]] || { echo "IMAGE_SOURCE_FAIL size=${source_size:-missing}" >&2; exit 20; }
source_sha="$(sha256sum "$NVIS_LLAMA_SOURCE_IMAGE" | awk '{print $1}')"
[[ "$source_sha" == "$IMAGE_SHA256" ]] || { echo "IMAGE_SOURCE_FAIL sha256=$source_sha" >&2; exit 21; }

export NVIS_LLAMA_SOURCE_IMAGE NVIS_LLAMA_RUN_IMAGE NVIS_LLAMA_RSYNC IMAGE_BYTES IMAGE_SHA256
if [[ "$NVIS_LLAMA_STORAGE" == local ]]; then
  srun --nodes=256 --ntasks=256 --ntasks-per-node=1 --kill-on-bad-exit=1 bash -euo pipefail -c '
    destination="$NVIS_LLAMA_RUN_IMAGE"; mkdir -p "$(dirname "$destination")"
    exec 9>"${destination}.nvis-stage.lock"; flock -x 9
    ready=0
    if [[ "$(stat -c %s "$destination" 2>/dev/null || true)" == "$IMAGE_BYTES" ]]; then
      [[ "$(sha256sum "$destination" | awk "{print \$1}")" == "$IMAGE_SHA256" ]] && ready=1
    fi
    if (( ! ready )); then
      [[ "$NVIS_LLAMA_RSYNC" == 1 ]] || { echo "IMAGE_STAGE_FAIL $(hostname): local image missing/corrupt and rsync disabled" >&2; exit 22; }
      partial="${destination}.partial"
      rsync -a --partial --append-verify -- "$NVIS_LLAMA_SOURCE_IMAGE" "$partial"
      [[ "$(stat -c %s "$partial")" == "$IMAGE_BYTES" ]] || exit 23
      [[ "$(sha256sum "$partial" | awk "{print \$1}")" == "$IMAGE_SHA256" ]] || exit 24
      mv -- "$partial" "$destination"
    fi
    echo "IMAGE_STAGE_READY $(hostname): path=$destination"
  '
else
  [[ "$NVIS_LLAMA_STORAGE" == nfs && "$NVIS_LLAMA_RUN_IMAGE" == "$NVIS_LLAMA_SOURCE_IMAGE" ]] || exit 25
fi

export RUN_CONF_IMAGE="$NVIS_LLAMA_RUN_IMAGE" MELLANOX_VISIBLE_DEVICES=all
readonly NODE_PREFLIGHT="${NVIS_LLAMA_SCRIPTS_DIR}/llama405b_node_preflight.sh"
[[ -x "$NODE_PREFLIGHT" ]] || { echo "ERROR: repo-owned Llama node preflight missing: $NODE_PREFLIGHT" >&2; exit 30; }
# Proven gate is intentionally two-pass: every host completes DCGM recovery, then
# the second pass after a settle interval catches diagnostics racing the allocation.
srun --nodes=256 --ntasks=256 --ntasks-per-node=1 --kill-on-bad-exit=1 --label "$NODE_PREFLIGHT"
sleep 20
srun --nodes=256 --ntasks=256 --ntasks-per-node=1 --kill-on-bad-exit=1 --label "$NODE_PREFLIGHT"

srun --nodes=256 --ntasks=256 --ntasks-per-node=1 --kill-on-bad-exit=1 --label bash -euo pipefail -c '
  test -c /dev/infiniband/rdma_cm
  [[ "$(find /dev/infiniband -maxdepth 1 -name "uverbs*" -type c | wc -l)" -ge 4 ]]
  . "${NVIS_LLAMA_SCRIPTS_DIR}/lib/fabric.inc.sh"
  eval "$(nvis_fabric_scan)"
  want="${NVIS_FABRIC_EXPECT_RAILS:-4}"
  [[ "${NVIS_FAB_RAILS:-0}" -ge "$want" ]]
  echo "HOST_IB_READY $(hostname): link=${NVIS_FAB_LINK} rails=${NVIS_FAB_RAILS} want>=${want} hcas=${NVIS_FAB_IB_HCA}"
'

srun --nodes=256 --ntasks=256 --ntasks-per-node=1 --kill-on-bad-exit=1 --label \
  --container-image="$RUN_CONF_IMAGE" --container-writable --no-container-mount-home \
  --container-mounts=/dev/infiniband:/dev/infiniband --container-env=MELLANOX_VISIBLE_DEVICES \
  bash -euo pipefail -c '
    [[ "$(nvidia-smi -L | grep -c "^GPU")" == 4 ]]; test -e /dev/infiniband/uverbs0
    python3 - <<"PY"
import ctypes
import torch

assert torch.cuda.device_count() == 4
lib = ctypes.CDLL("libnccl.so.2")
version = ctypes.c_int()
rc = lib.ncclGetVersion(ctypes.byref(version))
assert rc == 0, rc
code = version.value
parts = (code // 10000, (code % 10000) // 100, code % 100)
assert parts == (2, 30, 7), code
print("CONTAINER_PREFLIGHT_READY gpus=4 runtime_nccl=2.30.7 ib=visible")
PY
  '

mkdir -p "$NVIS_LLAMA_LOG_DIR"
export PYTHONUNBUFFERED=1 SLURM_UNBUFFEREDIO=1 TORCHX_MAX_RETRIES=0
export TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=False NCCL_NVLS_ENABLE=0 NCCL_DEBUG=WARN
export TORCH_NCCL_HIGH_PRIORITY=1 TORCH_NCCL_AVOID_RECORD_STREAMS=1 HF_HUB_OFFLINE=1
export HF_HOME=${LLMB_INSTALL}/.cache/huggingface HF_HUB_CACHE=${LLMB_INSTALL}/.cache/huggingface/hub
export CUDA_DEVICE_MAX_CONNECTIONS=32 NVTE_FWD_LAYERNORM_SM_MARGIN=16 NVTE_BWD_LAYERNORM_SM_MARGIN=16
export NVTE_NORM_FWD_USE_CUDNN=1 NVTE_NORM_BWD_USE_CUDNN=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True NCCL_GRAPH_REGISTER=0
# The training ranks' compute plane (lib/fabric.inc.sh). On a RoCE cluster NCCL must be kept off
# the management NIC (NCCL_IB_HCA pin) and told the address family (NscaleTX-NC is IPv6-only); on
# InfiniBand this exports nothing, so the validated IB profile is unchanged. Only the compute plane:
# this profile's MPI/bootstrap env is validated as-is and is deliberately not touched.
# shellcheck disable=SC1091
. "${NVIS_LLAMA_SCRIPTS_DIR}/lib/fabric.inc.sh"
nvis_fabric_export_compute || true
common_env=HF_HOME,HF_HUB_CACHE,HF_HUB_OFFLINE,NCCL_NVLS_ENABLE,NCCL_DEBUG,MELLANOX_VISIBLE_DEVICES,RUN_CONF_IMAGE,CUDA_DEVICE_MAX_CONNECTIONS,NVTE_FWD_LAYERNORM_SM_MARGIN,NVTE_BWD_LAYERNORM_SM_MARGIN,NVTE_NORM_FWD_USE_CUDNN,NVTE_NORM_BWD_USE_CUDNN,PYTORCH_CUDA_ALLOC_CONF,NCCL_GRAPH_REGISTER,TOKENIZERS_PARALLELISM,TORCH_NCCL_AVOID_RECORD_STREAMS,TORCH_NCCL_HIGH_PRIORITY,TRANSFORMERS_OFFLINE
for v in NCCL_IB_HCA NCCL_IB_ADDR_FAMILY; do
  if [[ -n "${!v:-}" ]]; then common_env="${common_env},${v}"; fi
done
srun --output="${NVIS_LLAMA_LOG_DIR%/}/llama405b-train-%j.out" --error="${NVIS_LLAMA_LOG_DIR%/}/llama405b-train-%j.err" \
  --kill-on-bad-exit=1 --mpi=pmix --container-image="$RUN_CONF_IMAGE" --container-writable --no-container-mount-home \
  --container-mounts="/dev/infiniband:/dev/infiniband,${LLMB_INSTALL}/.cache/huggingface,${PERF_ROOT}:${PERF_ROOT}" \
  --container-env="$common_env" bash -euo pipefail -c \
  'numactl --cpunodebind=$((SLURM_LOCALID/2)) --membind=$((SLURM_LOCALID/2)) python '"$PERF_ROOT"'/run_script.py --model_family_name llama --model_recipe_name llama31_405b --gpu gb300 --container_image "$RUN_CONF_IMAGE" --num_gpus 1024 --gpus_per_node 4 --offline --dump_env -E RUN_CONF_IMAGE="$RUN_CONF_IMAGE" -E NCCL_NVLS_ENABLE=0 --max_steps=50 --custom_mounts='"${LLMB_INSTALL}"'/.cache/huggingface --compute_dtype fp8_cs --enable_pct_binding false --config_variant v2 --account default --partition defq --log_dir '"${LLMB_INSTALL}"'/workloads/pretrain_llama3.1 --time_limit 02:00:00 --packager none --micro_batch_size 1 --global_batch_size 6144 --tensor_model_parallel_size 4 --pipeline_model_parallel_size 8 --context_parallel_size 1 --virtual_pipeline_model_parallel_size 4 train.manual_gc=true train.manual_gc_interval=100'
