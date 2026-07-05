#!/usr/bin/env bash
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

  echo "==> NCCL + nccl-tests (installation/03-build-nccl.sh)"
  cd "$TOPDIR"
  if [[ ! -d "${TOPDIR}/nccl/.git" ]]; then
    git clone https://github.com/NVIDIA/nccl.git "${TOPDIR}/nccl"
  fi
  cd "${TOPDIR}/nccl"
  git fetch --tags 2>/dev/null || true
  git checkout v2.28.9-1
  make -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)" src.build
  if [[ ! -d nccl-tests/.git ]]; then
    git clone https://github.com/NVIDIA/nccl-tests.git
  fi
  cd nccl-tests
  make -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)" MPI=1 MPI_HOME="${OMPI_HOME}" NCCL_HOME="${PWD}/../build"
  mkdir -p ../bins
  cp -arv build/ ../bins/
  cd ../bins
  rm -fr *.o verifiable 2>/dev/null || true
  for f in ../build/lib/libnccl*so*; do
    [[ -e "$f" ]] || continue
    cp -av "$f" .
  done
  echo "OK: NCCL binaries under ${TOPDIR}/nccl/bins"
fi

echo "OK: CUDA / NCCL remote install finished (CUDA ${CUDA_MAJ}, ${ARCH})."
