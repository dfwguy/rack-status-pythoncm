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
# Compatibility entry point for the Rack Status single-rack NeMo action.
#
# Legacy CLI contract:
#   run_singlerack.sh NODELIST RACK_ID [LOG_DIR]
#
# The rack id is retained for callers and attribution, but execution is owned by
# the reviewed nemo-26.04.01 preset. That preset pins the image, exact nodes,
# 30-step recipe, submit identity, fail-closed IMEX/NVLink transport, and compute-visible
# shared runtime scripts.
set -euo pipefail
umask 0002

usage() {
  echo "Usage: $0 NODELIST RACK_ID [LOG_DIR]" >&2
}

[[ $# -ge 2 && $# -le 3 ]] || { usage; exit 2; }
nodelist="$1"
rack_id="$2"
log_dir="${3:-${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/OVA/logs/nscaletx-16k-validation}}}"

[[ -n "$nodelist" ]] || { echo "run_singlerack: NODELIST is empty" >&2; exit 2; }
[[ "$rack_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "run_singlerack: invalid RACK_ID: $rack_id" >&2
  exit 2
}
[[ "$log_dir" == /* && "$log_dir" != *","* && "$log_dir" != *$'\n'* ]] || {
  echo "run_singlerack: LOG_DIR must be an absolute path without commas or newlines" >&2
  exit 2
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/slurm-init.inc.sh"
dispatcher="${script_dir}/run-validation-preset.sh"
[[ -x "$dispatcher" ]] || {
  echo "run_singlerack: reviewed preset dispatcher is missing or not executable: $dispatcher" >&2
  exit 2
}

mkdir -p "$log_dir" || {
  echo "run_singlerack: cannot create shared log directory: $log_dir" >&2
  exit 2
}
[[ -d "$log_dir" && -w "$log_dir" && -x "$log_dir" ]] || {
  echo "run_singlerack: shared log directory is not writable/searchable: $log_dir" >&2
  exit 2
}
chmod g+rws "$log_dir" 2>/dev/null || true

# Slurm children cannot see the Rack Status container's /app mount. Pin their
# helpers to the shared runtime tree that is mounted on every compute node.
runtime_root="${RACK_VALIDATION_PROVENANCE_ROOT:-/cluster/OVA/scripts/nscaletx-16k-validation}"
export NVIS_VALIDATION_NODE_SCRIPTS_DIR="${NVIS_VALIDATION_NODE_SCRIPTS_DIR:-${runtime_root%/}/runtime/bin/scripts}"
[[ "$NVIS_VALIDATION_NODE_SCRIPTS_DIR" == /* && \
   "$NVIS_VALIDATION_NODE_SCRIPTS_DIR" != /app && \
   "$NVIS_VALIDATION_NODE_SCRIPTS_DIR" != /app/* ]] || {
  echo "run_singlerack: compute runtime scripts must use a shared non-/app path" >&2
  exit 2
}

export RACK_JOB_LOG_DIR="$log_dir"
export NVIS_NCCL_MNNVL=1
export NVIS_NEMO_NVLS_ENABLE=1
export NVIS_NEMO_UB_SKIPMC=0

echo "NEMO_SINGLE_RACK_START rack=$rack_id nodes=$nodelist log_dir=$log_dir runtime=$NVIS_VALIDATION_NODE_SCRIPTS_DIR"
exec bash "$dispatcher" \
  --preset nemo-26.04.01 \
  --nodelist "$nodelist" \
  --storage local \
  --no-rsync \
  --local-container-dir "${RACK_VALIDATION_NODE_CONTAINER_DIR:-/opt/containers}" \
  --log-dir "$log_dir"
