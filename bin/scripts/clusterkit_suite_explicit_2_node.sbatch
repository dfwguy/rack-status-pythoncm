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
#
# One explicitly selected pair, one RoCE rail, CPU bandwidth/latency only.
#SBATCH --job-name=CK-NC-2-node
#SBATCH --nodes=2
#SBATCH --ntasks=2
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:05:00
#SBATCH --no-requeue

set -euo pipefail
ulimit -c 0
: "${NVIS_CLUSTERKIT_HPCX_INIT:?}"
: "${RACK_CLUSTERKIT_SUITE_OUTDIR:?}"
: "${RACK_CLUSTERKIT_SUITE_HCA:?}"
: "${RACK_CLUSTERKIT_SUITE_EXPECTED_NODES:?}"
: "${NVIS_BIN_SCRIPTS:?}"
[[ "${SLURM_JOB_NUM_NODES}" == 2 ]] || { echo 'ERROR: allocation must have two nodes'; exit 1; }
actual=$(scontrol show hostnames "$SLURM_JOB_NODELIST" | sort)
expected=$(printf '%s\n' "$RACK_CLUSTERKIT_SUITE_EXPECTED_NODES" | tr ':' '\n' | sort)
[[ "$actual" == "$expected" ]] || { echo 'ERROR: allocation differs from reviewed node list'; exit 1; }
cd "$RACK_CLUSTERKIT_SUITE_OUTDIR"
echo "JOB=$SLURM_JOB_ID HOSTS=$SLURM_JOB_NODELIST HCA=$RACK_CLUSTERKIT_SUITE_HCA"
export UCX_VFS_ENABLE=n

# Preflight runs once on each allocated node, not through independent SSH sessions.
# Failure of either host prevents the benchmark. These checks do not change nodes.
timeout 60s srun --mpi=none --nodes=2 --ntasks=2 --ntasks-per-node=1 \
  --cpus-per-task=1 --cpu-bind=cores --kill-on-bad-exit=1 --label bash -c '
    set -eo pipefail
    echo "HOST=$(hostname) ARCH=$(uname -m)"
    test "$(uname -m)" = aarch64
    dev=${RACK_CLUSTERKIT_SUITE_HCA%:*}; port=${RACK_CLUSTERKIT_SUITE_HCA##*:}
    rdma=/sys/class/infiniband/$dev
    test -d "$rdma/ports/$port"
    grep -q "ACTIVE" "$rdma/ports/$port/state"
    grep -q "Ethernet" "$rdma/ports/$port/link_layer"
    cat "$rdma/ports/$port/state" "$rdma/ports/$port/phys_state" "$rdma/ports/$port/rate"
    if test -r "$rdma/device/numa_node"; then printf "NIC_NUMA="; cat "$rdma/device/numa_node"; fi
    grep -E "Cpus_allowed_list|Mems_allowed_list" /proc/self/status
    python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" snapshot --phase before
    test -r "$NVIS_CLUSTERKIT_HPCX_INIT"
    export HPCX_ENABLE_NCCL_MRC_PLUGIN=0 HPCX_ENABLE_NCCL_SPCX_PLUGIN=0
    source "$NVIS_CLUSTERKIT_HPCX_INIT"
    hpcx_load
    test -x "$HPCX_DIR/clusterkit/bin/clusterkit"
    test -x "$HPCX_MPI_DIR/bin/mpirun"
    "$HPCX_MPI_DIR/bin/mpirun" --version
    "$HPCX_UCX_DIR/bin/ucx_info" -c | grep -E "UCX_IB_(GID_INDEX|IS_GLOBAL|HOP_LIMIT|ROCE_|TRAFFIC_CLASS)" || true
    "$HPCX_UCX_DIR/bin/ucx_info" -d -t rc_mlx5 | grep -A 10 -F "Device: $dev:$port" || {
      echo "ERROR: selected HCA absent from HPC-X UCX inventory"; exit 1;
    }
  '

# MPI ranks are Slurm tasks, with verified process CPU binding; no hardcoded Nscale mapper
# and no SSH process launcher. Pin one known-active RoCE device/port. GPU tests,
# stress modes, CPU-governor changes, retests, and suite fanout are not used.
export UCX_NET_DEVICES="$RACK_CLUSTERKIT_SUITE_HCA"
export UCX_TLS=rc,sm,self UCX_LOG_LEVEL="${RACK_CLUSTERKIT_SUITE_LOG_LEVEL:-info}"
export OMPI_MCA_pml=ucx OMPI_MCA_coll_hcoll_enable=0 OMPI_MCA_coll_ucc_enable=0
export CUDA_VISIBLE_DEVICES=""
# ClusterKit concatenates CK_OUTPUT_SUBDIR with --outputdir. Use only the
# explicit outputdir so an absolute path cannot be duplicated in that join.
unset CK_OUTPUT_SUBDIR
run_stage() {
  local seconds="$1" stage="$2" rc
  export RACK_CLUSTERKIT_SUITE_STAGE="$stage"
  export RACK_CLUSTERKIT_SUITE_CK_OUTDIR="${PWD}/$stage"
  echo "Running stage=$stage (one task per node, limit=${seconds}s)"
  set +e
  timeout --kill-after=10s "${seconds}s" srun --mpi=pmix_v3 --nodes=2 --ntasks=2 --ntasks-per-node=1 \
    --cpus-per-task=1 --cpu-bind=cores --kill-on-bad-exit=1 bash -c \
    'exec bash "$NVIS_BIN_SCRIPTS/clusterkit_suite_explicit_rank.sh"'
  rc=$?
  set -e
  printf '%s_EXIT=%s\n' "$stage" "$rc" | tee -a status.txt
  return "$rc"
}

primary_rc=0; global_rc=not-run; tcp_rc=not-run; path_rc=not-run
run_stage 90 baseline || primary_rc=$?
if ((primary_rc != 0)); then
  echo 'Baseline failed: testing global GID selection, then a TCP latency control.'
  global_rc=0; run_stage 60 global-gid || global_rc=$?
  tcp_rc=0; run_stage 45 tcp-control || tcp_rc=$?
fi
printf 'PRIMARY_EXIT=%s GLOBAL_GID_EXIT=%s TCP_CONTROL_EXIT=%s\n' "$primary_rc" "$global_rc" "$tcp_rc" | tee -a status.txt
if ((primary_rc == 0)) && [[ "${RACK_CLUSTERKIT_SUITE_PATH_CHECK:-0}" == 1 ]]; then
  # Same pair and allocation; changing a process setting does not reconfigure the NIC.
  timeout 30s srun --mpi=none --nodes=2 --ntasks=2 --ntasks-per-node=1 \
    --cpus-per-task=1 --label python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" snapshot --phase between
  path_rc=0; run_stage 60 single-path || path_rc=$?
  printf 'SINGLE_PATH_EXIT=%s\n' "$path_rc" | tee -a status.txt
fi
diag_args=(snapshot --phase after)
((primary_rc == 0)) || diag_args+=(--check-peer)
timeout 30s srun --mpi=none --nodes=2 --ntasks=2 --ntasks-per-node=1 \
  --cpus-per-task=1 --label python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" "${diag_args[@]}" || true
python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" summarize --outdir "$PWD" || {
  echo 'WARNING: summary unavailable; preserve and inspect raw results.';
}
# Preserve baseline failure even if a diagnostic configuration succeeds.
if ((primary_rc == 0)) && [[ "$path_rc" != not-run ]]; then exit "$path_rc"; fi
exit "$primary_rc"
