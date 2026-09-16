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
# One sbatch per host under the shared rack-status log root's sn-llama/ directory.
# Usage: pass the same Slurm nodelist style as run_singlerack.sh / validation, e.g.
#   ./run-llama.sh 'p6-r06-ct[01-03,15]'
# Multiple arguments are joined with commas (same merged hostlist as the UI).
#
# Requires: scontrol, Slurm, NEMO_SQSH_SRC, and nemo-llma3_8b.sh next to this script.

set -uo pipefail

# Force ssh transport: this cluster's pdsh defaults to rsh (refused on compute nodes).
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Non-login control-plane shells do not have a reliable Slurm PATH. Use the same
# pinned site version and config source as every other rack-status launcher.
source "${SCRIPT_DIR}/slurm-init.inc.sh"
# The API starts this launcher with cwd set to the requested validation log
# directory. Honour that cwd unless the caller explicitly supplies a job-log
# override; RACK_STATUS_LOG_DIR is the service-wide default and must not replace
# a per-request destination.
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-$PWD}"
LLAMA_LOG_DIR="${JOB_LOG_DIR%/}/sn-llama"
umask 0002
mkdir -p "${LLAMA_LOG_DIR}" || { echo "run-llama: cannot create log directory: ${LLAMA_LOG_DIR}" >&2; exit 2; }
chmod 2775 "${LLAMA_LOG_DIR}" || { echo "run-llama: cannot make log directory group-writable: ${LLAMA_LOG_DIR}" >&2; exit 2; }
export RACK_JOB_LOG_DIR="${JOB_LOG_DIR}"
cd "${JOB_LOG_DIR}" || exit 2

if [[ $# -lt 1 ]]; then
  echo "run-llama: need a Slurm hostlist (e.g. p6-r06-ct[01-03,15]) — same style as single-rack validation" >&2
  exit 1
fi

hostspec=$(printf '%s,' "$@" | sed 's/,$//')

# Same host expansion as run_singlerack.sh (no nodeset).
hosts=()
# shellcheck disable=SC2207
hosts=($(scontrol show hostname "$hostspec"))
if ((${#hosts[@]} == 0)); then
  echo "run-llama: scontrol show hostname produced no hosts for: $hostspec" >&2
  exit 1
fi

: "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the head-node path of the nemo .sqsh image (same as validation)}"
_sqsh_b=$(basename "$NEMO_SQSH_SRC")
source_size=$(stat -c %s "$NEMO_SQSH_SRC") || {
  echo "run-llama: cannot stat source image: $NEMO_SQSH_SRC" >&2
  exit 2
}
pre_staged_image="/opt/containers/${_sqsh_b}"
# Rack Status runs in a deliberately minimal container without pdsh or compute-node
# SSH credentials.  Do not probe or copy from this control process: the allocation
# checks the exact path and size before Pyxis starts. Campaign launchers gate nodes on
# File Copy Service SUCCESS receipts, while an interactive launch fails in its Slurm
# log with LLAMA_VALIDATION_FAIL stage=image when a receipt/staged image is absent.
llama_run_image="$pre_staged_image"
log_owner="${USER:-rack-status}"
echo "LLAMA_IMAGE_REQUIRED run_image=${llama_run_image} size=${source_size} nodes=${#hosts[@]} verification=allocation"
export NVIS_LLAMA_CONT="$llama_run_image"
export NVIS_LLAMA_EXPECTED_SIZE="$source_size"
echo "LLAMA_IMAGE_STAGED source=${NEMO_SQSH_SRC} run_image=${NVIS_LLAMA_CONT} nodes=${#hosts[@]}"

# Rack Status launches as root inside its minimal control container. Submit the
# allocation as the configured cluster operator so root-squashed shared logs and
# container caches have the same ownership semantics as every validation preset.
submit_uid="${NVIS_SLURM_SUBMIT_UID:-${RACK_SLURM_SUBMIT_UID:-}}"
submit_gid="${NVIS_SLURM_SUBMIT_GID:-${RACK_SLURM_SUBMIT_GID:-}}"
submit_home="${NVIS_SLURM_SUBMIT_HOME:-${RACK_SLURM_SUBMIT_HOME:-}}"
slurm_submit_as=()
if [[ -n "$submit_uid" || -n "$submit_gid" ]]; then
  if [[ ! "$submit_uid" =~ ^[0-9]+$ || ! "$submit_gid" =~ ^[0-9]+$ ]]; then
    echo "run-llama: submit uid/gid must both be numeric" >&2
    exit 2
  fi
  slurm_submit_as=(--uid="$submit_uid" --gid="$submit_gid")
fi
if [[ -n "$submit_home" ]]; then
  if [[ "$submit_home" != /* || "$submit_home" == *","* || "$submit_home" == *$'\n'* ]]; then
    echo "run-llama: submit home must be an absolute path without commas/newlines" >&2
    exit 2
  fi
  export HOME="$submit_home"
fi

for i in "${hosts[@]}"; do
  [[ -z "$i" ]] && continue
  # Use the real hostname in -o so Slurm StdOut= and dashboard tail paths match (avoid literal %N).
  sbatch "${slurm_submit_as[@]}" --export=ALL,NVIS_LLAMA_CONT="$NVIS_LLAMA_CONT",NVIS_LLAMA_EXPECTED_SIZE="$NVIS_LLAMA_EXPECTED_SIZE" -N 1 -w "$i" \
    -o "${LLAMA_LOG_DIR}/${log_owner}-NEMO-56B-30STEPS-1N-${i}-%j.txt" \
    "$SCRIPT_DIR/nemo-single-56b-30steps.sh"
done
