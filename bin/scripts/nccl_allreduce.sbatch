#!/bin/bash
# nccl_allreduce.sbatch — direct multi-node NCCL all_reduce (host nccl-tests + mpirun).
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# The simple, proven cross-rack NCCL test: run the HOST nccl-tests all_reduce_perf
# across the allocated nodes via mpirun, with the MPI *bootstrap* on the routable
# management net (ob1/tcp over the NS iface) and the NCCL *data path* on the IB
# compute rails (NCCL_IB_HCA). This deliberately avoids llmb-run's containerized
# UCX harness (the container exposes only tcp/IPoIB netdevs, no mlx5 verbs, so
# UCX_NET_DEVICES=mlx5_* fails). Validated on Boston at ~322 GB/s busbw (b05<->b07).
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
# key off the catalog CUDA_MAJ: it can lag the actually-staged toolchain (e.g. Boston
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
NCCL_TESTS_BIN="${NVIS_NCCL_TESTS_BIN:-$(ls -t /home/*/workspace/nccl/nccl-tests/build/all_reduce_perf 2>/dev/null | head -1)}"
NCCL_LIB="${NVIS_NCCL_LIB:-$(dirname "$(ls -t /home/*/workspace/nccl/build/lib/libnccl.so.2 2>/dev/null | head -1)" 2>/dev/null)}"
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

# --- Fabric: NCCL on the IB rails, bootstrap on the routable NS iface -----------
# shellcheck disable=SC1091
source "${_D}/lib/fabric.inc.sh"
nvis_fabric_export || true            # sets NCCL_IB_HCA + NCCL_SOCKET_IFNAME (on-node)
NS_IF="${NCCL_SOCKET_IFNAME:-bond0}"  # routable mgmt iface for MPI + NCCL bootstrap

# --- Build hostfile from the allocation -----------------------------------------
NODES="$(scontrol show hostnames "${SLURM_JOB_NODELIST}")"
NNODES="$(printf '%s\n' "${NODES}" | grep -c .)"
NP=$(( NNODES * GPUS_PER_NODE ))
HF="$(mktemp "${TMPDIR:-/tmp}/nccl_hf.XXXXXX")"
printf '%s\n' "${NODES}" | sed "s/\$/ slots=${GPUS_PER_NODE}/" > "${HF}"
trap 'rm -f "${HF}"' EXIT

echo "nccl_allreduce: nodes=${NNODES} np=${NP} gpus/node=${GPUS_PER_NODE} sizes=${SIZE_MIN}..${SIZE_MAX}"
echo "nccl_allreduce: bin=${NCCL_TESTS_BIN}"
echo "nccl_allreduce: NCCL_IB_HCA=${NCCL_IB_HCA:-<auto>} NCCL_SOCKET_IFNAME=${NS_IF} (MPI bootstrap: ob1/tcp over ${NS_IF})"

# MPI bootstrap over tcp/${NS_IF} (routable cross-rack); NCCL data over IB rails.
# plm=rsh launches orted via ssh across the allocated nodes (works in-allocation).
mpirun -np "${NP}" --hostfile "${HF}" --map-by "ppr:${GPUS_PER_NODE}:node" --bind-to none \
  --mca plm rsh --mca plm_rsh_agent ssh \
  --mca pml ob1 --mca btl tcp,self,vader --mca btl_tcp_if_include "${NS_IF}" \
  --mca oob_tcp_if_include "${NS_IF}" \
  -x LD_LIBRARY_PATH -x PATH \
  -x NCCL_SOCKET_IFNAME="${NS_IF}" \
  ${NCCL_IB_HCA:+-x NCCL_IB_HCA="${NCCL_IB_HCA}"} \
  -x NCCL_DEBUG="${NCCL_DEBUG:-WARN}" \
  "${NCCL_TESTS_BIN}" -b "${SIZE_MIN}" -e "${SIZE_MAX}" -f 2 -g 1
rc=$?
echo "nccl_allreduce: exit ${rc}"
exit ${rc}
