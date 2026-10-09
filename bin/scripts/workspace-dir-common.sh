# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# NVIS Rack Status — source-only: the cluster's workspace root, and the paths that live under it.
# Source from bin/scripts. Safe to source multiple times.
#
# The workspace holds what is built or installed ON the cluster for its tests: HPC-X, CUDA, host
# NCCL + nccl-tests, nvloom, and the dgxc-benchmarking / LLMB install. It is a project setting --
# the cluster's connection.workspace_dir, which Rack Status exports as NVIS_WORKSPACE_DIR (and
# forwards over SSH by its NVIS_ prefix) -- never a path baked into a script. The installers
# write under it and the launchers read under it, so the two cannot disagree.
#
# Resolution order:
#   1) WORKSPACE_DIR (already set, e.g. an install script's explicit argument)
#   2) NVIS_WORKSPACE_DIR   (the cluster's workspace_dir)
#   3) RACK_WORKSPACE_DIR   (legacy alias)
#   4) TOPDIR               (legacy install-script name)
# There is no default. This used to fall through to /home/cmsupport/workspace, a directory that
# exists on no current cluster: the installers then wrote to one place and the launchers read an
# empty other, and a launch failed far from the cause ("llmb-run not found", "libnccl.so.2 not
# found"). Unset is now an error that names the setting.
#
# Paths under the workspace are derived by the resolver for their kind -- LLMB's is
# lib/llmb_paths.inc.sh -- never re-spelled in a script.
#
# Exports: WORKSPACE_DIR, TOPDIR and NVIS_WORKSPACE_DIR (aliases).

if [[ -z "${WORKSPACE_DIR:-}" ]]; then
  WORKSPACE_DIR="${NVIS_WORKSPACE_DIR:-${RACK_WORKSPACE_DIR:-${TOPDIR:-}}}"
fi
if [[ -z "${WORKSPACE_DIR}" ]]; then
  echo "ERROR: no workspace configured for this cluster. Set it in Rack Status (Projects -> cluster ->" \
       "Workspace), or export NVIS_WORKSPACE_DIR for a manual run." >&2
  return 2 2>/dev/null || exit 2
fi
WORKSPACE_DIR="${WORKSPACE_DIR%/}"

export WORKSPACE_DIR
# Install scripts use TOPDIR; keep it aligned with the resolved workspace root.
export TOPDIR="${WORKSPACE_DIR}"
export NVIS_WORKSPACE_DIR="${WORKSPACE_DIR}"

