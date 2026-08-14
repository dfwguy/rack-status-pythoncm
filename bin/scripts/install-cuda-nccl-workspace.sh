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
# NVIS Rack Status — orchestrator: download CUDA .run on **localhost** (01), then install on **compute** via pdsh (02).
# Environment (set by Flask from the dashboard JSON):
#   NVIS_INSTALL_CUDA_TOOLKIT=1|0   NVIS_BUILD_NCCL=1|0
#   NVIS_CUDA_NCCL_BUILD_HOST       single hostname for pdsh -w (same TOPDIR on shared home)
#
# Usage: install-cuda-nccl-workspace.sh [TOPDIR] [CUDA_MAJOR] [ARCH]
set -euo pipefail

# Force ssh transport: this cluster's pdsh defaults to rsh (refused on compute nodes).
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh

TOPDIR="${1:-/home/cmsupport/workspace}"
CUDA_MAJ="${2:-13}"
ARCH="${3:-aarch64}"
INSTALL_CUDA="${NVIS_INSTALL_CUDA_TOOLKIT:-0}"
BUILD_NCCL="${NVIS_BUILD_NCCL:-0}"
HOST="${NVIS_CUDA_NCCL_BUILD_HOST:-}"

if [[ "$ARCH" != "aarch64" ]]; then
  echo "Only aarch64 is supported (matches installation/03-build-nccl.sh)." >&2
  exit 1
fi

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$INSTALL_CUDA" != "1" && "$BUILD_NCCL" != "1" ]]; then
  echo "Nothing to do: enable install_cuda_toolkit and/or build_nccl (NVIS_INSTALL_CUDA_TOOLKIT / NVIS_BUILD_NCCL)." >&2
  exit 0
fi

if [[ -z "$HOST" ]]; then
  echo "ERROR: NVIS_CUDA_NCCL_BUILD_HOST must be set to one compute node hostname (pdsh target)." >&2
  exit 1
fi
if [[ "$HOST" == *","* ]] || [[ "$HOST" == *" "* ]]; then
  echo "ERROR: NVIS_CUDA_NCCL_BUILD_HOST must be a single hostname (no commas or spaces)." >&2
  exit 1
fi

if ! command -v pdsh >/dev/null 2>&1; then
  echo "ERROR: pdsh not found in PATH on the dashboard host." >&2
  exit 1
fi

echo "==> Phase 1: download on localhost (${HOST} unchanged)"
bash "${_SCRIPT_DIR}/01-download-cuda-nccl-workspace.sh" "$TOPDIR" "$CUDA_MAJ" "$ARCH"

# pdsh often invokes /bin/sh, which mishandles "export A=a B=b" or loses env before bash.
# Use explicit "export VAR=val;" per variable, then exec bash (non-login). Wrap in bash -c $(printf %q …)
# so the whole remote program is one argv to bash on the target (reliable pdsh/ssh fanout).
_ic="${NVIS_INSTALL_CUDA_TOOLKIT:-0}"
_ib="${NVIS_BUILD_NCCL:-0}"
[[ "$_ic" == "1" ]] || _ic=0
[[ "$_ib" == "1" ]] || _ib=0

_script_q=$(printf '%q' "${_SCRIPT_DIR}/02-install-cuda-nccl-workspace.sh")
_top_q=$(printf '%q' "$TOPDIR")
_maj_q=$(printf '%q' "$CUDA_MAJ")
_arch_q=$(printf '%q' "$ARCH")
# NVIS_NCCL_VERSION selects BOTH the git tag and the on-disk layout in 02-install. It must be
# forwarded explicitly: pdsh runs a non-login shell and only the vars named below survive, so
# omitting it silently built 2.28.9 no matter what the dashboard asked for.
_nver_q=$(printf '%q' "${NVIS_NCCL_VERSION:-2.28.9}")

_pds_inner=$(printf \
  'export NVIS_INSTALL_CUDA_TOOLKIT=%s; export NVIS_BUILD_NCCL=%s; export NVIS_NCCL_VERSION=%s; export TOPDIR=%s; export CUDA_MAJ=%s; export ARCH=%s; exec bash %s %s %s %s' \
  "$_ic" "$_ib" "$_nver_q" "$_top_q" "$_maj_q" "$_arch_q" "$_script_q" "$_top_q" "$_maj_q" "$_arch_q")

_remote_quoted=$(printf '%q' "$_pds_inner")

echo "==> Phase 2: pdsh -w ${HOST} (NVIS_INSTALL_CUDA_TOOLKIT=${_ic} NVIS_BUILD_NCCL=${_ib} NVIS_NCCL_VERSION=${NVIS_NCCL_VERSION:-2.28.9})"
echo "==> remote: ${_pds_inner}"
# shellcheck disable=SC2029
pdsh -R ssh -w "$HOST" bash -c "$_remote_quoted"
