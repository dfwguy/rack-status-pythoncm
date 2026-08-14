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
# NVIS Rack Status — part 2 of CUDA/NCCL workspace install: run on a **compute node** (same TOPDIR on shared $HOME).
# Expects NVIS_INSTALL_CUDA_TOOLKIT / NVIS_BUILD_NCCL from the environment. Optional .run must already exist in TOPDIR
# if installing CUDA toolkit (download via 01-download-cuda-nccl-workspace.sh on the dashboard host).
#
# Usage: 02-install-cuda-nccl-workspace.sh [TOPDIR] [CUDA_MAJOR] [ARCH]
set -euo pipefail


TOPDIR="${1:-/home/cmsupport/workspace}"
CUDA_MAJ="${2:-13}"
ARCH="${3:-aarch64}"
INSTALL_CUDA="${NVIS_INSTALL_CUDA_TOOLKIT:-0}"
BUILD_NCCL="${NVIS_BUILD_NCCL:-0}"

if [[ "$ARCH" != "aarch64" ]]; then
  echo "Only aarch64 is supported (matches installation/03-build-nccl.sh)." >&2
  exit 1
fi

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=workspace-cuda-major-common.sh
. "${_SCRIPT_DIR}/workspace-cuda-major-common.sh"

if [[ "$INSTALL_CUDA" != "1" && "$BUILD_NCCL" != "1" ]]; then
  echo "Nothing to do on remote (NVIS_INSTALL_CUDA_TOOLKIT / NVIS_BUILD_NCCL both off)." >&2
  exit 0
fi

write_cuda_env_sh() {
  # ${TOPDIR}/cuda is a symlink to cuda${CUDA_MAJ} (created by the caller); write env.sh
  # through it so CUDA_HOME=${TOPDIR}/cuda resolves to the real toolkit.
  cat > "${TOPDIR}/cuda/env.sh" <<EOF
#!/bin/bash
export CUDA_HOME=${TOPDIR}/cuda
export PATH=\${CUDA_HOME}/bin:\$PATH
export LD_LIBRARY_PATH=\${CUDA_HOME}/lib64:\${LD_LIBRARY_PATH}
EOF
  chmod +x "${TOPDIR}/cuda/env.sh"
}

install_cuda_toolkit_from_existing_run() {
  echo "==> CUDA toolkit (02 style), CUDA ${CUDA_MAJ}"
  mkdir -p "$TOPDIR"
  cd "$TOPDIR"
  if [[ ! -f "${TOPDIR}/${RUN_FILE}" ]]; then
    echo "ERROR: ${TOPDIR}/${RUN_FILE} missing — run 01-download-cuda-nccl-workspace.sh on the dashboard host first." >&2
    exit 1
  fi
  chmod +x "${TOPDIR}/${RUN_FILE}" 2>/dev/null || true
  bash "${TOPDIR}/${RUN_FILE}" --silent --toolkit --no-opengl-libs --no-drm --installpath="${TOPDIR}/cuda${CUDA_MAJ}"
  # ${TOPDIR}/cuda must be a *symlink* to cuda${CUDA_MAJ}. Do NOT pre-create it as a
  # directory: `ln -sfn` cannot replace an existing dir (it would drop the link inside
  # it, leaving ${TOPDIR}/cuda/bin/nvcc missing). Remove any stale path (dir or link) first.
  rm -rf "${TOPDIR}/cuda"
  ln -sfn "${TOPDIR}/cuda${CUDA_MAJ}" "${TOPDIR}/cuda"
  if [[ ! -x "${TOPDIR}/cuda/bin/nvcc" ]]; then
    echo "ERROR: ${TOPDIR}/cuda/bin/nvcc not found after install — symlink to cuda${CUDA_MAJ} failed." >&2
    exit 1
  fi
  write_cuda_env_sh
  echo "OK: CUDA toolkit at ${TOPDIR}/cuda (→ cuda${CUDA_MAJ}), nvcc=$(${TOPDIR}/cuda/bin/nvcc --version | sed -n 's/.*release //p')"
}

if [[ "$INSTALL_CUDA" == "1" ]]; then
  install_cuda_toolkit_from_existing_run
fi

if [[ "$BUILD_NCCL" == "1" ]] && [[ "$INSTALL_CUDA" != "1" ]]; then
  if [[ ! -x "${TOPDIR}/cuda/bin/nvcc" ]] && [[ ! -x "${TOPDIR}/cuda${CUDA_MAJ}/bin/nvcc" ]]; then
    echo "ERROR: NCCL build needs nvcc — enable Install CUDA toolkit or install CUDA under ${TOPDIR} first." >&2
    exit 1
  fi
fi

if [[ "$BUILD_NCCL" == "1" ]]; then
  mkdir -p "$TOPDIR"
  cd "$TOPDIR"
  INIT="${TOPDIR}/${UNPACK}/hpcx-mt-init-ompi.sh"
  if [[ ! -f "$INIT" ]]; then
    echo "ERROR: HPC-X not found at ${INIT} — run install-hpcx-workspace.sh for CUDA ${CUDA_MAJ} first (NCCL build needs OpenMPI from HPC-X)." >&2
    exit 1
  fi
  echo "==> Load HPC-X MPI for NCCL build"
  set +u
  # shellcheck disable=SC1090
  source "$INIT"
  hpcx_load
  # shellcheck disable=SC1091
  source /etc/profile || true

  if [[ -f "${TOPDIR}/cuda/env.sh" ]]; then
    # shellcheck disable=SC1091
    source "${TOPDIR}/cuda/env.sh"
  else
    echo "ERROR: ${TOPDIR}/cuda/env.sh missing — install CUDA toolkit first." >&2
    exit 1
  fi

  # Version + on-disk layout MUST match what nccl_allreduce.sbatch globs, or the build succeeds
  # and the run still reports "libnccl.so.2 not found":
  #   system|default|2.28.9 -> <TOPDIR>/nccl/build/lib/libnccl.so.2
  #   <X.Y.Z>               -> <TOPDIR>/nccl-<X.Y.Z>/nccl/build/lib/libnccl.so.2
  # Previously the tag AND the directory were both hardcoded to 2.28.9, so selecting any other
  # version in the dashboard could never resolve.
  NCCL_VER="${NVIS_NCCL_VERSION:-2.28.9}"
  case "${NCCL_VER}" in
    system|default|2.28.9) NCCL_ROOT="${TOPDIR}/nccl" ;;
    *)                     NCCL_ROOT="${TOPDIR}/nccl-${NCCL_VER}/nccl" ;;
  esac

  echo "==> NCCL ${NCCL_VER} + nccl-tests -> ${NCCL_ROOT}"
  mkdir -p "$(dirname "${NCCL_ROOT}")"
  if [[ ! -d "${NCCL_ROOT}/.git" ]]; then
    git clone https://github.com/NVIDIA/nccl.git "${NCCL_ROOT}"
  fi
  cd "${NCCL_ROOT}"
  git fetch --tags 2>/dev/null || true
  # Upstream tags are "v<X.Y.Z>-<n>"; try the common -1 suffix, then the bare tag. Fail LOUDLY with
  # the real candidates rather than silently building whatever HEAD happens to be.
  _tag=""
  for _c in "v${NCCL_VER}-1" "v${NCCL_VER}" "${NCCL_VER}"; do
    if git rev-parse --verify --quiet "refs/tags/${_c}" >/dev/null; then _tag="${_c}"; break; fi
  done
  if [[ -z "${_tag}" ]]; then
    echo "ERROR: no git tag for NCCL version '${NCCL_VER}'. Available near it:" >&2
    git tag --list "v${NCCL_VER%%.*}.*" | tail -15 >&2
    exit 1
  fi
  echo "==> checkout ${_tag}"
  git checkout "${_tag}"
  make -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)" src.build
  # nccl-tests MUST land where nccl_allreduce.sbatch looks, and the two globs are NOT symmetric:
  #   lib   -> <TOPDIR>/nccl-<VER>/nccl/build/lib/libnccl.so.2   (inside nccl/)
  #   tests -> <TOPDIR>/nccl-<VER>/nccl-tests/build/all_reduce_perf   (SIBLING of nccl/, no /nccl/)
  # Cloning nccl-tests inside nccl/ (as the system layout does) would build fine and then fail the
  # run with "all_reduce_perf not found". Put it beside nccl/ for versioned builds.
  case "${NCCL_VER}" in
    system|default|2.28.9) TESTS_ROOT="${NCCL_ROOT}/nccl-tests" ;;
    *)                     TESTS_ROOT="$(dirname "${NCCL_ROOT}")/nccl-tests" ;;
  esac
  echo "==> nccl-tests -> ${TESTS_ROOT}"
  mkdir -p "$(dirname "${TESTS_ROOT}")"
  if [[ ! -d "${TESTS_ROOT}/.git" ]]; then
    git clone https://github.com/NVIDIA/nccl-tests.git "${TESTS_ROOT}"
  fi
  cd "${TESTS_ROOT}"
  make -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)" MPI=1 MPI_HOME="${OMPI_HOME}" NCCL_HOME="${NCCL_ROOT}/build"
  mkdir -p "${NCCL_ROOT}/bins"
  cp -arv build/. "${NCCL_ROOT}/bins/"
  cd "${NCCL_ROOT}/bins"
  rm -fr *.o verifiable 2>/dev/null || true
  for f in "${NCCL_ROOT}"/build/lib/libnccl*so*; do
    [[ -e "$f" ]] || continue
    cp -av "$f" .
  done
  [[ -x "${TESTS_ROOT}/build/all_reduce_perf" ]] \
    || { echo "ERROR: all_reduce_perf not built at ${TESTS_ROOT}/build/" >&2; exit 1; }
  echo "OK: NCCL ${NCCL_VER}  lib=${NCCL_ROOT}/build/lib/libnccl.so.2  tests=${TESTS_ROOT}/build/all_reduce_perf"
fi

echo "OK: CUDA / NCCL remote install finished (CUDA ${CUDA_MAJ}, ${ARCH})."
