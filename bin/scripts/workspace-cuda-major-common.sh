# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# NVIS Rack Status — source-only: install-hpcx-workspace.sh and install-cuda-nccl-workspace.sh
# Prerequisites: CUDA_MAJ is 12 or 13 (and TOPDIR set by caller before download paths are used).
# Sets: TBZ, URL, UNPACK (HPC-X); RUN_FILE, RUN_URL (NVIDIA CUDA .run installers for optional toolkit).
case "${CUDA_MAJ:-}" in
  12)
    TBZ="hpcx-v2.22.1-gcc-doca_ofed-ubuntu24.04-cuda12-aarch64.tbz"
    URL="https://content.mellanox.com/hpc/hpc-x/v2.22.1rc4/${TBZ}"
    UNPACK="hpcx-v2.22.1-gcc-doca_ofed-ubuntu24.04-cuda12-aarch64"
    RUN_FILE="cuda_12.9.1_575.57.08_linux_sbsa.run"
    RUN_URL="https://developer.download.nvidia.com/compute/cuda/12.9.1/local_installers/${RUN_FILE}"
    ;;
  13)
    TBZ="hpcx-v2.25.1-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64.tbz"
    URL="https://content.mellanox.com/hpc/hpc-x/v2.25.1_cuda13/${TBZ}"
    UNPACK="hpcx-v2.25.1-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64"
    RUN_FILE="cuda_13.0.2_580.95.05_linux_sbsa.run"
    RUN_URL="https://developer.download.nvidia.com/compute/cuda/13.0.2/local_installers/${RUN_FILE}"
    ;;
  *)
    echo "CUDA_MAJOR must be 12 or 13" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

# The directory holding the CUDA runtime (libcudart.so.<CUDA_MAJ>) that tools built against this CUDA
# major load at run time -- ClusterKit dlopens it for its GPU tests and, without it, skips them while
# reporting success. NVIS_CUDA_LIB (a site override) wins; otherwise the workspace's toolkit for this
# major, then any workspace toolkit, then the system ones -- the first that actually has the library.
# Prints nothing when none does.
nvis_cuda_runtime_libdir() {
  local maj="${CUDA_MAJ:-}" d
  if [[ -n "${NVIS_CUDA_LIB:-}" ]]; then printf '%s\n' "${NVIS_CUDA_LIB}"; return 0; fi
  shopt -s nullglob
  for d in "${WORKSPACE_DIR:-/nonexistent}/cuda${maj}"/targets/*/lib "${WORKSPACE_DIR:-/nonexistent}"/cuda*/targets/*/lib \
           /usr/local/cuda-"${maj}"*/targets/*/lib /usr/local/cuda/targets/*/lib; do
    if [[ -n "$maj" ]]; then
      compgen -G "$d/libcudart.so.${maj}" >/dev/null || continue
    else
      compgen -G "$d/libcudart.so*" >/dev/null || continue
    fi
    shopt -u nullglob
    printf '%s\n' "$d"
    return 0
  done
  shopt -u nullglob
  return 0
}
