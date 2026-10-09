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
# nccl_alltoall_ab.sbatch — A/B sweep of NCCL env knobs for the cross-rack alltoall bug.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY: multi-domain NVL72 (NCCL 2.28.x) alltoall trips "bootstrap.cc Message truncated
# (512 vs 256)" at the NVSwitch<->IB boundary. This isolates WHICH single knob fixes it
# (avoid changing several at once): baseline / PXN / PROTO / NCHANNELS / combos. Each config
# runs the containerized alltoall_perf_mpi once (fails fast at init if broken) and we record
# PASS/FAIL. all-reduce is run first as a fabric sanity check.
#
# RUN: sbatch -N36 -w <valid-topology pair, e.g. a07+a08 nodes> --ntasks-per-node=4 \
#        --export=ALL,NVIS_SCRIPTS_DIR=<scripts dir> \
#        --output=/home/cmods/workspace/nccl_alltoall_ab-%j.log nccl_alltoall_ab.sbatch
#SBATCH --job-name=nccl-a2a-ab
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=00:40:00
set -uo pipefail

CONT="${NVIS_NEMO_CONT:-${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}/nemo-25.11.01.sqsh}"
SCR="${NVIS_SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# shellcheck source=workspace-dir-common.sh
. "${SCR}/workspace-dir-common.sh" || exit 2
PRELOAD_SRC="${NVIS_NCCL_PRELOAD_SRC:-${WORKSPACE_DIR}/nccl/bins}"
PRELOAD_LIB="$(ls "${PRELOAD_SRC}"/libnccl.so.2.* 2>/dev/null | sort -V | tail -1 | xargs -r basename)"
SIZES="-b 8 -e 512M -f 2 -g 1 --iters 5"     # bug hits at init; small sweep = fast

echo "== nccl_alltoall_ab: NNODES=${SLURM_NNODES} CONT=${CONT} preload=${PRELOAD_LIB:-none} =="
echo "== nodelist: ${SLURM_JOB_NODELIST} =="

# --- Fabric env (mirror the multi-rack NeMo path): rails + bootstrap NIC via lib/fabric.inc.sh ---
# shellcheck disable=SC1090,SC1091
[[ -f "${SCR}/lib/fabric.inc.sh" ]] && . "${SCR}/lib/fabric.inc.sh"
# Rails (pinned exactly, =list), RoCE address family, the bootstrap NIC and the MPI transport
# defaults all come from the shared detector. This used to hard-code ns_nic=bond0 and fall back to
# mlx5_0,1,4,5 -- GB300 NscaleTX-NC has no bond0 and its rails are roce_vf_r1..4.
if declare -F nvis_fabric_export_pinned >/dev/null 2>&1; then nvis_fabric_export_pinned || true; fi
export MELLANOX_VISIBLE_DEVICES=all PMIX_MCA_gds=hash
export UCX_TLS=rc
export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 OMPI_MCA_coll_hcoll_enable=0 SLURM_CPU_BIND=none
export NCCL_NVLS_ENABLE=1 NCCL_MIN_CTAS=16 NCCL_RUNTIME_CONNECT=0 NCCL_IB_QPS_PER_CONNECTION=2 CUDA_MODULE_LOADING=EAGER
unset NCCL_MNNVL_ENABLE   # multi-rack: let NCCL auto-detect per-rack MNNVL domains
export NCCL_DEBUG=WARN
echo "== fabric: NCCL_IB_HCA=${NCCL_IB_HCA} NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME} =="

CMNT="/dev:/dev"
BASE_EXPORT="ALL"
if [[ -n "${PRELOAD_LIB}" && -f "${PRELOAD_SRC}/${PRELOAD_LIB}" && "${NVIS_NCCL_PRELOAD_DISABLE:-0}" != 1 ]]; then
  CMNT="${CMNT},${PRELOAD_SRC}:/host-nccl"
  BASE_EXPORT="ALL,LD_PRELOAD=/host-nccl/${PRELOAD_LIB}"
fi
# CRITICAL (learned 2026-07-07, jobs 205 vs 206): under pyxis, `srun --export=ALL` does NOT reliably
# forward batch-env NCCL_* extras to the container ranks — only vars listed EXPLICITLY in --export
# land (verified: NCCL_IGNORE_COLLNET_MISMATCH set via `sbatch --export` was "NOT seen" by NCCL in
# job 206, so the sweep silently tested the wrong env). So bake any set carry-through knobs into the
# explicit --export list, exactly like the per-config knobs.
for v in NCCL_IGNORE_COLLNET_MISMATCH NCCL_NET_PLUGIN NCCL_COLLNET_ENABLE NCCL_ALGO NCCL_ENV_PLUGIN NCCL_IGNORE_NET_MISMATCH; do
  [[ -n "${!v:-}" ]] && BASE_EXPORT="${BASE_EXPORT},${v}=${!v}"
done
echo "== BASE_EXPORT=${BASE_EXPORT} =="

run_one() {  # $1=name  $2=binary  $3=extra-nccl-env(csv, may be empty)
  local name="$1" bin="$2" extra="$3" exp out rc res avg cname
  exp="${BASE_EXPORT}${extra:+,${extra}}"
  out="/tmp/ab_${name}.$$"
  # UNIQUE per-config container name. CRITICAL (learned 2026-07-07): pyxis reuses an existing
  # --container-name and then IGNORES --container-image AND leaves stale writable state. The old code
  # used a fixed name "nccl_ab" but removed the WRONG name (`enroot remove -f nccl_ab` vs the actual
  # pyxis-prefixed "pyxis_nccl_ab"), so every config after the first ran a stale container from a
  # prior job/image -> bogus, contradictory results. A per-config unique name guarantees a fresh
  # import of ${CONT} every time.
  cname="nccl_ab_${SLURM_JOB_ID}_${name}"
  echo "############################################################"
  echo "### CONFIG=${name}  bin=${bin}  extra=[${extra:-none}]  container=${cname}"
  echo "### export=${exp}"
  srun -N"${SLURM_NNODES}" --ntasks-per-node=1 enroot remove -f "pyxis_${cname}" &>/dev/null || true
  timeout 200 srun --export="${exp}" --cpu-bind=none --mpi=pmix \
    --container-name="${cname}" --container-writable --container-image="${CONT}" --container-mounts="${CMNT}" \
    "${bin}" ${SIZES} >"${out}" 2>&1
  rc=$?
  if grep -q "Out of bounds values : 0 OK" "${out}"; then
    avg="$(grep -m1 "Avg bus bandwidth" "${out}" | sed 's/.*: *//')"
    res="PASS (avg busbw ${avg} GB/s)"
  elif grep -q "Message truncated" "${out}"; then
    res="FAIL: bootstrap Message truncated"
  else
    res="FAIL: rc=${rc} ($(grep -m1 -iE 'error|failure|truncated' "${out}" | head -c 120))"
  fi
  echo "### RESULT ${name}: ${res}"
  printf '%s\t%s\n' "${name}" "${res}" >> /tmp/ab_summary.$$
  tail -30 "${out}"
  srun -N"${SLURM_NNODES}" --ntasks-per-node=1 enroot remove -f "pyxis_${cname}" &>/dev/null || true
}

: > /tmp/ab_summary.$$

# Fabric sanity: all-reduce (should PASS on a healthy pair).
run_one allreduce_sanity all_reduce_perf_mpi ""

# A/B: isolate the alltoall fix.
run_one a2a_baseline  alltoall_perf_mpi ""
run_one a2a_pxn       alltoall_perf_mpi "NCCL_PXN_DISABLE=1"
run_one a2a_proto     alltoall_perf_mpi "NCCL_PROTO=SIMPLE"
run_one a2a_nch16     alltoall_perf_mpi "NCCL_MIN_NCHANNELS=16,NCCL_MAX_NCHANNELS=16"
run_one a2a_pxn_proto alltoall_perf_mpi "NCCL_PXN_DISABLE=1,NCCL_PROTO=SIMPLE"
run_one a2a_all       alltoall_perf_mpi "NCCL_PXN_DISABLE=1,NCCL_PROTO=SIMPLE,NCCL_MIN_NCHANNELS=16,NCCL_MAX_NCHANNELS=16"

echo
echo "=================== A/B SUMMARY ==================="
cat /tmp/ab_summary.$$
echo "==================================================="
rm -f /tmp/ab_summary.$$ /tmp/ab_*.$$
