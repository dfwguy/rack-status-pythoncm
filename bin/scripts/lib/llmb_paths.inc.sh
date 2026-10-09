# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# lib/llmb_paths.inc.sh — where the dgxc-benchmarking / LLMB install is, for every LLMB consumer.
#
# The one place LLMB paths are derived (docs/explanation/how-cluster-paths-reach-scripts.md):
#   LLMB_INSTALL  explicit if set -- a complete answer on its own, workspace or not --
#                 else <workspace>/llmb, where install-dgxc-benchmarking-workspace.sh puts it
#   LLMB_VENV     explicit if set, else <workspace>/llmb_venv when a workspace is known, else empty
#                 (callers treat the venv as optional)
# The workspace is the cluster's (project setting connection.workspace_dir -> NVIS_WORKSPACE_DIR),
# resolved by ../workspace-dir-common.sh, which fails naming the setting when it is needed and
# unset. Source with `|| exit 2`.

if [[ -z "${LLMB_INSTALL:-}" ]]; then
  # shellcheck source=../workspace-dir-common.sh
  . "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/workspace-dir-common.sh" || return 2 2>/dev/null || exit 2
  LLMB_INSTALL="${WORKSPACE_DIR}/llmb"
fi
LLMB_INSTALL="${LLMB_INSTALL%/}"
_nvis_ws="${WORKSPACE_DIR:-${NVIS_WORKSPACE_DIR:-${RACK_WORKSPACE_DIR:-}}}"
: "${LLMB_VENV:=${_nvis_ws:+${_nvis_ws%/}/llmb_venv}}"
unset _nvis_ws
export LLMB_INSTALL LLMB_VENV
