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
# nccl_allreduce.sbatch — direct multi-node NCCL all_reduce (host nccl-tests + mpirun).
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# The simple, proven cross-rack NCCL test: run the HOST nccl-tests all_reduce_perf
# across the allocated nodes via mpirun, with the MPI *bootstrap* on the routable
# management net (ob1/tcp over the NS iface) and the NCCL *data path* on the IB
# compute rails (NCCL_IB_HCA). This deliberately avoids llmb-run's containerized
# UCX harness (the container exposes only tcp/IPoIB netdevs, no mlx5 verbs, so
# UCX_NET_DEVICES=mlx5_* fails). Keep rail selection explicit on Nscale.
#
# Submit with: sbatch -N <n> --nodelist=<nodes> --ntasks-per-node=<gpus> \
#              --output=<logdir>/nccl-mpi_<label>_%j.out nccl_allreduce.sbatch
#SBATCH --job-name=nccl_allreduce
#SBATCH --exclusive
set -uo pipefail
# Slurm copies the batch script to a spool dir, so BASH_SOURCE points there, not at
# bin/scripts. The launcher exports NVIS_SCRIPTS_DIR so we can find lib/ reliably.
_D="${NVIS_SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# GPUs/node: explicit env (from launcher/platform geometry) wins; else auto-detect
# on this node via nvidia-smi (NVL=4, HGX=8, …); else 4. Never hardcode a generation.
GPUS_PER_NODE="${GPUS_PER_NODE:-$(nvidia-smi -L 2>/dev/null | grep -c '^GPU')}"
[ "${GPUS_PER_NODE:-0}" -gt 0 ] 2>/dev/null || GPUS_PER_NODE=4
SIZE_MIN="${NCCL_SIZE_MIN:-8}"
SIZE_MAX="${NCCL_SIZE_MAX:-1G}"

# --- Discover the host toolchain (env overrides win; else newest matching) ------
# Generation-flexible: pick the newest-version cuda*/hpcx-* DIRECTORY present (the
# .tbz tarballs and CPU arch are handled by _pick_first). We intentionally do NOT
# key off the catalog CUDA_MAJ: it can lag the actually-staged Nscale toolchain
# GB200 is catalogued cuda12 but validated on cuda13/driver-580). For deterministic
# per-cluster pinning set NVIS_HPCX_DIR / NVIS_NCCL_TESTS_BIN / NVIS_NCCL_LIB / NVIS_CUDA_LIB.
# Return the highest-version DIRECTORY matching the first glob group that hits.
# Directory-only (so we never pick a hpcx-*.tbz tarball) and version-sorted
# (so hpcx-...cuda13 wins over ...cuda12), for deterministic, generation-safe picks.
_pick_first() {
  local p d; local -a cand
  for p in "$@"; do
    cand=()
    for d in ${p}; do [ -d "$d" ] && cand+=("$d"); done
    if [ "${#cand[@]}" -gt 0 ]; then
      printf '%s\n' "${cand[@]}" | sort -V | tail -1
      return 0
    fi
  done
}
HPCX_DIR="${NVIS_HPCX_DIR:-$(_pick_first '/home/*/workspace/hpcx-*cuda*' '/home/*/workspace/hpcx-*' '/opt/hpcx*')}"
# --- NCCL version selection -----------------------------------------------------
# nccl-tests dynamically links libnccl.so.2, so the runtime NCCL version is chosen by
# which vetted lib dir is first on LD_LIBRARY_PATH (all NCCL 2.x share the soname
# libnccl.so.2 — a single test binary runs any 2.x lib). Vetted libs live on the
# NFS-shared workspace (visible on every compute node). Convention:
#   system | 2.28.9  ->  <ws>/nccl/build/lib              (system default host build)
#   <X.Y.Z>          ->  <ws>/nccl-<X.Y.Z>/nccl/build/lib  (vetted per-version build)
# Override the resolved dir with NVIS_NCCL_LIB, or the test binary with NVIS_NCCL_TESTS_BIN.
NVIS_NCCL_VERSION="${NVIS_NCCL_VERSION:-2.30.7}"
case "${NVIS_NCCL_VERSION}" in
  system|default|2.28.9) _nccl_lib_glob='/home/*/workspace/nccl/build/lib/libnccl.so.2' ;;
  *)                     _nccl_lib_glob="/home/*/workspace/nccl-${NVIS_NCCL_VERSION}/nccl/build/lib/libnccl.so.2" ;;
esac
if [ -n "${NVIS_NCCL_LIB:-}" ]; then
  NCCL_LIB="${NVIS_NCCL_LIB}"
else
  _nccl_lib_so="$(ls -t ${_nccl_lib_glob} 2>/dev/null | head -1)"
  [ -n "${_nccl_lib_so}" ] || { echo "nccl_allreduce: NCCL ${NVIS_NCCL_VERSION} libnccl.so.2 not found (looked: ${_nccl_lib_glob}). Build it on the NFS workspace or choose the 'system' version."; exit 2; }
  NCCL_LIB="$(dirname "${_nccl_lib_so}")"
fi
# Prefer a version-matched nccl-tests build when present; else the system build
# (ABI-compatible: a 2.28-built binary runs any 2.x libnccl loaded via LD_LIBRARY_PATH).
if [ -z "${NVIS_NCCL_TESTS_BIN:-}" ]; then
  case "${NVIS_NCCL_VERSION}" in
    system|default|2.28.9) : ;;
    *) _vt="$(ls -t /home/*/workspace/nccl-${NVIS_NCCL_VERSION}/nccl-tests/build/all_reduce_perf 2>/dev/null | head -1)"; [ -n "${_vt}" ] && NVIS_NCCL_TESTS_BIN="${_vt}" ;;
  esac
fi
NCCL_TESTS_BIN="${NVIS_NCCL_TESTS_BIN:-$(ls -t /home/*/workspace/nccl/nccl-tests/build/all_reduce_perf 2>/dev/null | head -1)}"
CUDA_LIB="${NVIS_CUDA_LIB:-$(_pick_first '/home/*/workspace/cuda*/targets/*/lib' '/usr/local/cuda*/targets/*/lib')}"

[ -n "${NCCL_TESTS_BIN}" ] && [ -x "${NCCL_TESTS_BIN}" ] || { echo "nccl_allreduce: all_reduce_perf not found (set NVIS_NCCL_TESTS_BIN)"; exit 2; }
[ -n "${HPCX_DIR}" ] && [ -d "${HPCX_DIR}" ] || { echo "nccl_allreduce: HPC-X not found (set NVIS_HPCX_DIR)"; exit 2; }

# --- MPI + CUDA/NCCL runtime ----------------------------------------------------
# hpcx-init.sh references unbound vars; do NOT run it under `set -u`.
set +u
# shellcheck disable=SC1090,SC1091
source "${HPCX_DIR}/hpcx-init.sh"; hpcx_load
set -u
export LD_LIBRARY_PATH="${NCCL_LIB}:${CUDA_LIB}:${LD_LIBRARY_PATH:-}"

# Optional fail-closed runtime gate used by the CUDA 13.3+/NCCL 2.31.2 preset.
# Inspect the actual linked library and selected CUDA runtime on every allocated
# node before MPI starts; setting version-looking env vars is not evidence.
if [ "${NVIS_NCCL_RUNTIME_PREFLIGHT:-0}" = "1" ]; then
  export NCCL_LIB CUDA_LIB NVIS_NCCL_VERSION
  if ! srun --kill-on-bad-exit=1 --nodes="${SLURM_JOB_NUM_NODES}" \
    --ntasks="${SLURM_JOB_NUM_NODES}" --ntasks-per-node=1 bash -euo pipefail -c '
      min_cuda="${NVIS_NCCL_MIN_CUDA_VERSION:-13.3}"
      cuda="$(printf "%s" "$CUDA_LIB" | sed -n "s#.*cuda-\{0,1\}\([0-9][0-9]*\.[0-9][0-9]*\).*#\1#p")"
      if [[ -z "$cuda" && -r "$CUDA_LIB/libcudart.so" ]]; then
        cuda="$(strings "$CUDA_LIB/libcudart.so" | sed -n "s/.*CUDA Runtime [vV]\{0,1\}\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p" | head -1)"
      fi
      [[ -n "$cuda" ]] || { echo "NCCL_RUNTIME_PREFLIGHT_FAIL $(hostname): selected CUDA_LIB version unavailable: $CUDA_LIB" >&2; exit 10; }
      awk -v got="$cuda" -v want="$min_cuda" "BEGIN {split(got,g,\".\"); split(want,w,\".\"); exit !((g[1]>w[1]) || (g[1]==w[1] && g[2]>=w[2]))}" || {
        echo "NCCL_RUNTIME_PREFLIGHT_FAIL $(hostname): CUDA=$cuda requires>=$min_cuda" >&2; exit 11;
      }
      version="$(LD_LIBRARY_PATH="$NCCL_LIB:$CUDA_LIB:${LD_LIBRARY_PATH:-}" python3 - <<"PY"
import ctypes
v = ctypes.c_int()
lib = ctypes.CDLL("libnccl.so.2")
assert lib.ncclGetVersion(ctypes.byref(v)) == 0
print(v.value)
PY
)"
      [[ "$version" == 23102 ]] || { echo "NCCL_RUNTIME_PREFLIGHT_FAIL $(hostname): ncclGetVersion=$version requires=23102" >&2; exit 12; }
      test -d /sys/class/infiniband || { echo "NCCL_RUNTIME_PREFLIGHT_FAIL $(hostname): no InfiniBand sysfs" >&2; exit 13; }
      test -e /dev/infiniband/rdma_cm || { echo "NCCL_RUNTIME_PREFLIGHT_FAIL $(hostname): no rdma_cm" >&2; exit 13; }
      uverbs="$(find /dev/infiniband -maxdepth 1 -name "uverbs*" -type c 2>/dev/null | wc -l)"
      (( uverbs > 0 )) || { echo "NCCL_RUNTIME_PREFLIGHT_FAIL $(hostname): no uverbs character devices" >&2; exit 13; }
      active="$(grep -l "4: ACTIVE" /sys/class/infiniband/*/ports/*/state 2>/dev/null | wc -l)"
      (( active > 0 )) || { echo "NCCL_RUNTIME_PREFLIGHT_FAIL $(hostname): no ACTIVE InfiniBand port" >&2; exit 14; }
      echo "NCCL_RUNTIME_PREFLIGHT_READY $(hostname): CUDA=$cuda NCCL=2.31.2 active_ib_ports=$active"
    '; then
    echo "NCCL_RUNTIME_PREFLIGHT_FAIL: one or more allocated nodes rejected the CUDA/NCCL/IB runtime" >&2
    exit 15
  fi
fi

# --- Fabric: NCCL on the IB rails, bootstrap on the routable NS iface -----------
# shellcheck disable=SC1091
source "${_D}/lib/fabric.inc.sh"
nvis_fabric_export || true            # sets NCCL_IB_HCA + NCCL_SOCKET_IFNAME (on-node)

# --- Per-node HCA auto-detect (NVIS_NCCL_HCA_AUTO=1) -----------------------------
# nvis_fabric_export probes the rails of the BATCH HOST ONLY, and the mpirun line below
# then broadcasts that single NCCL_IB_HCA to EVERY rank with -x. Rail state is a PER-NODE
# property: on 2026-07-28, h07 had 8 IB ports DOWN across 7 of 18 trays (h07-18 had only
# mlx5_1 up) while the batch host h07-11 had all four ACTIVE. Every rank was therefore told
# to use devices it could not open, and NCCL aborted with
#   "Detected mixed local CollNet device counts across ranks (min 1, max 4)"
#   -> "unhandled system error", rc=3, before measuring anything.
#
# With NVIS_NCCL_HCA_AUTO=1 we clear the pinned values so NCCL/UCX enumerate usable devices
# INDEPENDENTLY ON EACH NODE. This mirrors the bring-up harness's allreduce job, which never
# sets NCCL_IB_HCA at all and measures 683-698 GB/s on these same trays.
# NOTE: an EMPTY NCCL_IB_HCA means "any device" to NCCL, not "no devices" — but we must
# `unset` rather than set "", because fabric.inc.sh uses ${NCCL_IB_HCA:-default} which
# re-substitutes on empty.
if [ "${NVIS_NCCL_HCA_AUTO:-0}" = "1" ]; then
  unset NCCL_IB_HCA UCX_NET_DEVICES
  echo "nccl_allreduce: NVIS_NCCL_HCA_AUTO=1 — per-node HCA auto-detect (not pinning NCCL_IB_HCA/UCX_NET_DEVICES)"
fi
NS_IF="${NCCL_SOCKET_IFNAME:-bond0}"  # routable mgmt iface for MPI + NCCL bootstrap

# --- Build hostfile from the allocation -----------------------------------------
NODES="$(scontrol show hostnames "${SLURM_JOB_NODELIST}")"
NNODES="$(printf '%s\n' "${NODES}" | grep -c .)"
NP=$(( NNODES * GPUS_PER_NODE ))
HF="$(mktemp "${TMPDIR:-/tmp}/nccl_hf.XXXXXX")"
printf '%s\n' "${NODES}" | sed "s/\$/ slots=${GPUS_PER_NODE}/" > "${HF}"
trap 'rm -f "${HF}"' EXIT

# Forward custom launch env vars to the remote orted ranks. mpirun only propagates
# explicitly -x'd vars to nodes other than the one running mpirun, so without this a
# custom var (e.g. NCCL_NET_PLUGIN=none from the UI launch dialog) would silently apply
# to the local ranks only, leaving remote ranks on a different config.
#
# Two sources, UNIONED — the launch dialog's keys are ADDED to the scan, never a
# replacement for it:
#   1. scan of the batch env for NCCL_*/UCX_*/OMPI_MCA_* (what lib/fabric.inc.sh exported:
#      the plane selection, UCX_TLS, coll_ucc_enable, …).
#   2. NVIS_EXTRA_ENV_KEYS — the exact keys the app forwarded, ANY prefix, so a custom var
#      outside those three prefixes (HCOLL_*, NVIS_*, …) reaches remote ranks too.
# These used to be either/or, so supplying a single custom key from the UI silently stopped
# forwarding every scanned var: job 127306 sent only OMPI_MCA_coll_hcoll_enable and dropped
# UCX_TLS. OMPI_MCA_* are auto-propagated by mpirun, but we forward them explicitly so they
# are visible in the log line and land on every rank's env.
EXTRA_X=()
_seen=" NCCL_SOCKET_IFNAME NCCL_IB_HCA NCCL_DEBUG "   # forwarded explicitly below

# $1 = env var name. Skips vars that are unset or already queued (the two sources overlap:
# a custom OMPI_MCA_* key is also found by the scan, and -x FOO twice is not valid).
_extra_x_add() {
  _n="$1"
  case "${_seen}" in *" ${_n} "*) return 0 ;; esac
  [ -n "${!_n+x}" ] || return 0
  EXTRA_X+=( -x "${_n}" )
  _seen="${_seen}${_n} "
}

while IFS='=' read -r _k _; do
  case "${_k}" in NCCL_*|UCX_*|OMPI_MCA_*) _extra_x_add "${_k}" ;; esac
done < <(env)
for _k in ${NVIS_EXTRA_ENV_KEYS:-}; do
  _extra_x_add "${_k}"
done
if [ "${#EXTRA_X[@]}" -gt 0 ]; then
  echo "nccl_allreduce: forwarding extra env to all ranks:$(printf ' %s' "${EXTRA_X[@]}" | sed 's/ -x//g')"
fi

# Per-collective watchdog (nccl-tests -T/--timeout, seconds). nccl-tests' testStreamSynchronize
# polls with cudaStreamQuery and, if a single collective's sync exceeds this, calls ncclCommAbort
# (unblocking the stuck kernel) + prints "Test timeout" on the offending host and FAILS — instead
# of hanging forever on a degraded link/plane. 0 disables. Default 120s (a legit all_reduce at 16G
# is seconds, so this only trips on a real stall).
TEST_TIMEOUT="${NVIS_NCCL_TEST_TIMEOUT:-120}"

# Sweep rounds: run the whole all_reduce message-size sweep this many times. Stage-1
# acceptance requires N passing rounds (the app defaults single-rack Stage-1 to 5); the
# cross-rack callers leave it at 1 unless set. Any round failing fails the job (fail-fast).
ROUNDS="${NVIS_NCCL_ROUNDS:-1}"
case "${ROUNDS}" in ''|*[!0-9]*) ROUNDS=1 ;; esac
[ "${ROUNDS}" -ge 1 ] 2>/dev/null || ROUNDS=1

echo "nccl_allreduce: nodes=${NNODES} np=${NP} gpus/node=${GPUS_PER_NODE} sizes=${SIZE_MIN}..${SIZE_MAX} rounds=${ROUNDS}"
echo "nccl_allreduce: NCCL version=${NVIS_NCCL_VERSION} lib=${NCCL_LIB}"
echo "nccl_allreduce: bin=${NCCL_TESTS_BIN} per-collective timeout=${TEST_TIMEOUT}s (nccl-tests -T)"
echo "nccl_allreduce: NCCL_IB_HCA=${NCCL_IB_HCA:-<auto>} NCCL_SOCKET_IFNAME=${NS_IF} (MPI bootstrap: ob1/tcp over ${NS_IF})"

# MPI bootstrap over tcp/${NS_IF} (routable cross-rack); NCCL data over IB rails.
# plm=rsh launches orted via ssh across the allocated nodes (works in-allocation).
rc=0
for _round in $(seq 1 "${ROUNDS}"); do
  echo "===== nccl_allreduce round ${_round}/${ROUNDS} ====="
  mpirun -np "${NP}" --hostfile "${HF}" --map-by "ppr:${GPUS_PER_NODE}:node" --bind-to none \
    --mca plm rsh --mca plm_rsh_agent ssh \
    --mca pml ob1 --mca btl tcp,self --mca btl_tcp_if_include "${NS_IF}" \
    --mca oob_tcp_if_include "${NS_IF}" \
    -x LD_LIBRARY_PATH -x PATH \
    -x NCCL_SOCKET_IFNAME="${NS_IF}" \
    ${NCCL_IB_HCA:+-x NCCL_IB_HCA="${NCCL_IB_HCA}"} \
    -x NCCL_DEBUG="${NCCL_DEBUG:-WARN}" \
    ${EXTRA_X[@]+"${EXTRA_X[@]}"} \
    "${NCCL_TESTS_BIN}" -b "${SIZE_MIN}" -e "${SIZE_MAX}" -f 2 -g 1 -T "${TEST_TIMEOUT}"
  _rc=$?
  if [ "${_rc}" -ne 0 ]; then
    rc=${_rc}
    echo "nccl_allreduce: round ${_round}/${ROUNDS} FAILED rc=${_rc} (stopping)"
    break
  fi
  echo "nccl_allreduce: round ${_round}/${ROUNDS} OK"
done
echo "nccl_allreduce: exit ${rc}"
exit ${rc}
