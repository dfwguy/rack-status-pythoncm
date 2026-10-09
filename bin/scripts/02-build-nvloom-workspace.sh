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
# NVIS Rack Status — build nvloom (typically on an aarch64 compute node via pdsh).
# Expects Kitware CMake under TOPDIR (00-download on dashboard). Loads HPC-X + CUDA like NCCL build when available.
#
# Usage: 02-build-nvloom-workspace.sh [TOPDIR] [CUDA_MAJOR]
set -euo pipefail

# Workspace = the explicit argument, else the cluster's workspace (project setting
# connection.workspace_dir -> NVIS_WORKSPACE_DIR); workspace-dir-common.sh fails naming the setting.
WORKSPACE_DIR="${1:-}"
# shellcheck source=workspace-dir-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/workspace-dir-common.sh" || exit 2
CUDA_MAJ="${2:-13}"
NVLOOM="${TOPDIR}/nvloom"
CUDA_ENV_SH="${TOPDIR}/cuda/env.sh"

CMAKE_VER="3.28.1"
CMAKE_ROOT="${TOPDIR}/cmake-${CMAKE_VER}-linux-aarch64"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=workspace-cuda-major-common.sh
. "${_SCRIPT_DIR}/workspace-cuda-major-common.sh"

if [[ ! -d "${NVLOOM}/.git" ]]; then
  echo "ERROR: ${NVLOOM} missing — run 01-clone-nvloom-workspace.sh or install-nvloom-workspace.sh first." >&2
  exit 1
fi

if [[ ! -x "${CMAKE_ROOT}/bin/cmake" ]]; then
  echo "ERROR: ${CMAKE_ROOT}/bin/cmake missing — download CMake on the dashboard host first (00-download-cmake-nvloom-workspace.sh via install-nvloom-workspace.sh)." >&2
  exit 1
fi

_arch="$(uname -m 2>/dev/null || echo unknown)"
if [[ "$_arch" != "aarch64" ]]; then
  echo "ERROR: nvloom build expects aarch64 (nvcc + CMake bundle are aarch64). This host: ${_arch}." >&2
  exit 1
fi

# --- Toolchain: HPC-X (g++, MPI) then CUDA env ---
INIT="${TOPDIR}/${UNPACK}/hpcx-init.sh"
set +u
# shellcheck disable=SC1091
source /etc/profile 2>/dev/null || true
if [[ -f "$INIT" ]]; then
  echo "==> nvloom: load HPC-X (${INIT})"
  # shellcheck disable=SC1090
  source "$INIT"
  hpcx_load
else
  echo "NOTE: HPC-X init not found at ${INIT} — continuing if system g++/MPI suffice." >&2
fi
if type module &>/dev/null; then
  module load gcc 2>/dev/null || true
  echo "==> nvloom: try Boost environment modules (nvloom needs Boost.program_options)"
  _boost_mod_loaded=""
  for m in boost Boost boost/gcc14 boost/gcc13 boost/gcc12 boost/gcc boost/1.86 boost/1.85 boost/1.84 boost/1.83 boost/1.82; do
    if module load "$m" 2>/dev/null; then
      _boost_mod_loaded="$m"
      echo "==> nvloom: module load ${m}"
      break
    fi
  done
  if [[ -z "${_boost_mod_loaded}" ]]; then
    echo "NOTE: no Boost module matched known names — will use BOOST_ROOT / CM /usr paths if found." >&2
  fi
fi
# CMake FindBoost: set Boost_ROOT / CMAKE_PREFIX_PATH (modules often set BOOST_ROOT or BOOSTDIR).
set +u
if [[ -n "${Boost_ROOT:-}" && -r "${Boost_ROOT}/include/boost/program_options.hpp" ]]; then
  export CMAKE_PREFIX_PATH="${Boost_ROOT}${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"
  echo "==> nvloom: Boost_ROOT=${Boost_ROOT} (already set / module)"
elif [[ -n "${BOOST_ROOT:-}" && -r "${BOOST_ROOT}/include/boost/program_options.hpp" ]]; then
  export Boost_ROOT="${BOOST_ROOT}"
  export CMAKE_PREFIX_PATH="${BOOST_ROOT}${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"
  echo "==> nvloom: Boost_ROOT=${BOOST_ROOT} (BOOST_ROOT)"
elif [[ -n "${BOOSTDIR:-}" && -r "${BOOSTDIR}/include/boost/program_options.hpp" ]]; then
  export Boost_ROOT="${BOOSTDIR}"
  export CMAKE_PREFIX_PATH="${BOOSTDIR}${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"
  echo "==> nvloom: Boost_ROOT=${BOOSTDIR} (BOOSTDIR)"
elif [[ -n "${BOOST_DIR:-}" && -r "${BOOST_DIR}/include/boost/program_options.hpp" ]]; then
  export Boost_ROOT="${BOOST_DIR}"
  export CMAKE_PREFIX_PATH="${BOOST_DIR}${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"
  echo "==> nvloom: Boost_ROOT=${BOOST_DIR} (BOOST_DIR)"
elif [[ -z "${Boost_ROOT:-}" ]]; then
  for _br in /cm/local/apps/boost/current /cm/local/apps/boost /usr /usr/local; do
    if [[ -r "${_br}/include/boost/program_options.hpp" ]]; then
      export Boost_ROOT="${_br}"
      export CMAKE_PREFIX_PATH="${_br}${CMAKE_PREFIX_PATH:+:${CMAKE_PREFIX_PATH}}"
      echo "==> nvloom: Boost_ROOT=${_br} (layout scan)"
      break
    fi
  done
fi
set -u

if [[ -f "$CUDA_ENV_SH" ]]; then
  echo "==> nvloom: source workspace CUDA env (${CUDA_ENV_SH})"
  set +u
  # shellcheck disable=SC1091
  source "$CUDA_ENV_SH"
  set -u
else
  echo "NOTE: ${CUDA_ENV_SH} not found; using ${TOPDIR}/cuda${CUDA_MAJ} for CUDA_HOME." >&2
  export CUDA_HOME="${TOPDIR}/cuda${CUDA_MAJ}"
  export PATH="${CUDA_HOME}/bin:${PATH}"
  export LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH:-}"
fi

# env.sh sets CUDA_HOME to ${TOPDIR}/cuda (symlink). Always prefer ${TOPDIR}/cuda${CUDA_MAJ} when nvcc exists
# there so CUDA_HOME matches the install script's CUDA major and compute nodes are not stuck on a broken symlink.
_cuda_explicit="${TOPDIR}/cuda${CUDA_MAJ}"
if [[ -x "${_cuda_explicit}/bin/nvcc" ]]; then
  export CUDA_HOME="${_cuda_explicit}"
  export PATH="${CUDA_HOME}/bin:${PATH}"
  export LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH:-}"
  echo "==> nvloom: CUDA_HOME=${CUDA_HOME} (cuda${CUDA_MAJ}; env.sh sourced above for shared defaults)"
elif [[ -x "${CUDA_HOME}/bin/nvcc" ]]; then
  echo "==> nvloom: CUDA_HOME=${CUDA_HOME} (env.sh / symlink; cuda${CUDA_MAJ} toolkit has no nvcc)"
else
  echo "ERROR: nvcc not found under ${CUDA_HOME} or ${_cuda_explicit} — install workspace CUDA or fix ${CUDA_ENV_SH}." >&2
  exit 1
fi

export PATH="${CMAKE_ROOT}/bin:${PATH}"

echo "==> nvloom: cmake binary: $(command -v cmake)"

if ! command -v g++ >/dev/null 2>&1; then
  echo "ERROR: g++ not on PATH after HPC-X / modules — run install-hpcx-workspace.sh for CUDA ${CUDA_MAJ} or load a GCC module on this node." >&2
  exit 1
fi

cd "$NVLOOM"

# nvloom sets Boost_USE_STATIC_LIBS ON; Bright / CM trees (e.g. /cm/local/apps/boost/current) usually ship shared
# libboost_program_options.so only — FindBoost then reports program_options missing. Prefer shared linkage here.
_cl="${NVLOOM}/CMakeLists.txt"
if [[ -f "${_cl}" ]] && grep -q 'set(Boost_USE_STATIC_LIBS ON)' "${_cl}"; then
  echo "==> nvloom: patch ${_cl} — Boost_USE_STATIC_LIBS OFF (shared Boost for find_package program_options)"
  sed -i.bak-nvis-boostlink 's/set(Boost_USE_STATIC_LIBS ON)/set(Boost_USE_STATIC_LIBS OFF)/' "${_cl}"
fi

set +u
if [[ -n "${Boost_ROOT:-}" ]]; then
  for _libdir in "${Boost_ROOT}/lib64" "${Boost_ROOT}/lib"; do
    if [[ -d "${_libdir}" ]] && ls "${_libdir}"/libboost_program_options.so* >/dev/null 2>&1; then
      export Boost_LIBRARY_DIR="${_libdir}"
      export LD_LIBRARY_PATH="${_libdir}:${LD_LIBRARY_PATH:-}"
      echo "==> nvloom: Boost_LIBRARY_DIR=${_libdir} (libboost_program_options.so)"
      break
    fi
  done
fi
set -u

# Schedulers/modules may export CMAKE_CUDA_ARCHITECTURES=""; CMake 3.28+ rejects empty. Force native for nvcc.
unset CMAKE_CUDA_ARCHITECTURES 2>/dev/null || true
export CMAKE_CUDA_ARCHITECTURES=native

# Fresh configure: stale cache can keep a broken Boost detection; CM trees often ship BoostConfig.cmake that
# does not expose program_options — force legacy FindBoost.cmake via Boost_NO_BOOST_CMAKE=ON.
echo "==> nvloom: clean prior CMake configure (CMakeCache.txt / CMakeFiles/)"
rm -rf CMakeCache.txt CMakeFiles 2>/dev/null || true

_nv_cmake_args=(-Wno-dev -DBoost_NO_BOOST_CMAKE=ON)
set +u
if [[ -n "${Boost_ROOT:-}" ]]; then
  _nv_cmake_args+=("-DBoost_ROOT=${Boost_ROOT}")
fi
if [[ -n "${Boost_LIBRARY_DIR:-}" ]]; then
  _nv_cmake_args+=("-DBoost_LIBRARY_DIR=${Boost_LIBRARY_DIR}")
fi
set -u

echo "==> nvloom: cmake + make (CUDA_HOME=${CUDA_HOME}, CXX=$(command -v g++), CMAKE_CUDA_ARCHITECTURES=${CMAKE_CUDA_ARCHITECTURES})"
cmake "${_nv_cmake_args[@]}" .
make -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 8)"

echo "OK: nvloom built under ${NVLOOM}"
