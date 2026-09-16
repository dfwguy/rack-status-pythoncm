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
#SBATCH --exclusive
#SBATCH --mem=0
#SBATCH --ntasks-per-node=4
#SBATCH --gpus-per-node=4
#SBATCH --time=12:00:00
#SBATCH --no-requeue

# Execute the exact NVFTP five-collective recipe with the reviewed production contract:
# dynamic exact-request labeling, 500 iterations, and detailed NCCL INFO subsystems.
set -euo pipefail

# Keep the allocation shape explicit while allowing NCCL to discover MNNVL for
# the default single-rack NVLS path. A true multi-rack run matches the proven 8K
# recipe: MNNVL is disabled, NVLS is enabled, and rack boundaries are crossed
# on the four IB rails. RACK_MNNVL remains available only as a legacy opt-in.
contract="${NVIS_NCCL_TRANSPORT_CONTRACT:-}"
mapfile -t allocated_nodes < <(scontrol show hostnames "${SLURM_JOB_NODELIST:?}")
node_count=${#allocated_nodes[@]}
mapfile -t allocated_racks < <(
  printf '%s\n' "${allocated_nodes[@]}" | sed -E 's/-[0-9]+$//' | sort -u
)
rack_count=${#allocated_racks[@]}
case "$contract" in
  TRAY_NVLS)
    [[ $node_count == 1 && $rack_count == 1 ]] || {
      echo "ERROR: TRAY_NVLS requires exactly one node; got nodes=$node_count racks=$rack_count" >&2
      exit 18
    }
    export NVIS_NCCL_MNNVL_ENABLE=0
    export NVIS_NCCL_NVLS_ENABLE=1
    ;;
  RACK_NVLS)
    [[ $node_count -ge 2 && $rack_count == 1 ]] || {
      echo "ERROR: RACK_NVLS requires multiple nodes in exactly one physical rack; got nodes=$node_count racks=$rack_count" >&2
      exit 18
    }
    export NVIS_NCCL_MNNVL_ENABLE=auto
    export NVIS_NCCL_NVLS_ENABLE=1
    ;;
  RACK_MNNVL)
    [[ $node_count -ge 2 && $rack_count == 1 ]] || {
      echo "ERROR: RACK_MNNVL requires multiple nodes in exactly one physical rack; got nodes=$node_count racks=$rack_count" >&2
      echo "ERROR: fan out multi-rack selections into one RACK_MNNVL allocation per rack" >&2
      exit 18
    }
    export NVIS_NCCL_MNNVL_ENABLE=1
    export NVIS_NCCL_NVLS_ENABLE=1
    ;;
  MULTIRACK_HYBRID)
    [[ $node_count -ge 2 && $rack_count -ge 2 ]] || {
      echo "ERROR: MULTIRACK_HYBRID requires nodes from at least two physical racks; got nodes=$node_count racks=$rack_count" >&2
      exit 18
    }
    export NVIS_NCCL_MNNVL_ENABLE=0
    export NVIS_NCCL_NVLS_ENABLE=1
    ;;
  *)
    echo "ERROR: NVIS_NCCL_TRANSPORT_CONTRACT must be TRAY_NVLS, RACK_NVLS, RACK_MNNVL, or MULTIRACK_HYBRID" >&2
    exit 18
    ;;
esac
printf 'NCCL_TRANSPORT_CONTRACT=%s nodes=%s racks=%s mnnvl=%s nvls=%s hcoll=0 rails=mlx5_0,mlx5_1,mlx5_2,mlx5_3\n' \
  "$contract" "$node_count" "$rack_count" "$NVIS_NCCL_MNNVL_ENABLE" "$NVIS_NCCL_NVLS_ENABLE"

provenance_root="${RACK_VALIDATION_PROVENANCE_ROOT:-/cluster/OVA/scripts/nscaletx-16k-validation}"
snapshot="${provenance_root}/working_HLT_NEMO_NCCL_LLAMA/20260824/NCCL/recipes/single-node/nccl_five_collective.sbatch"
snapshot_sha=b923a98e913230f1a0d0d208aa411b72a373147fdfba05fd6e0fe0915a5138d1
portable_sha=7dbcc584e26f7fece556aebbc5bdf58fddba6400435370eeaf535fc73a5fc767

[[ -r "$snapshot" ]] || { echo "ERROR: reviewed NCCL recipe missing: $snapshot" >&2; exit 2; }
[[ "$(sha256sum "$snapshot" | awk '{print $1}')" == "$snapshot_sha" ]] || {
  echo "ERROR: reviewed NCCL recipe provenance mismatch: $snapshot" >&2
  exit 2
}

portable_recipe="$(mktemp /tmp/nvis-nccl-five-collective.XXXXXX.sbatch)"
trap 'rm -f "$portable_recipe"' EXIT
sed -e 's/racks=112 layout=111x17+j19x16/racks=dynamic layout=exact-request/' \
  -e 's/iterations=100 /iterations=500 /' \
  -e 's/ -n100 >/ -n500 >/' \
  -e 's/export NCCL_NVLS_ENABLE=1/export NCCL_NVLS_ENABLE="${NVIS_NCCL_NVLS_ENABLE:?}"/' \
  -e 's|--output=/dev/null --error=/dev/null|--output=/dev/null --error="${NVIS_REPORT_DIR}/test-${number}-${collective}-srun-%t.stderr"|' \
  -e 's|--container-mounts="/dev/infiniband:|--container-mounts="/dev:/dev,/dev/infiniband:|' \
  -e '/export NCCL_DEBUG=INFO/a export NCCL_DEBUG_SUBSYS=ALL\
if [[ "${NVIS_NCCL_MNNVL_ENABLE:?}" == auto ]]; then unset NCCL_MNNVL_ENABLE; else export NCCL_MNNVL_ENABLE="${NVIS_NCCL_MNNVL_ENABLE}"; fi\
export NVIS_NCCL_MNNVL_MODE="${NVIS_NCCL_MNNVL_ENABLE}"\
export NCCL_IB_HCA="=mlx5_0,mlx5_1,mlx5_2,mlx5_3"\
export OMPI_MCA_coll_hcoll_enable=0' \
  -e 's/--container-env=UCX_TLS,NCCL_DEBUG,NCCL_NVLS_ENABLE,/--container-env=UCX_TLS,NCCL_DEBUG,NCCL_DEBUG_SUBSYS,NCCL_NVLS_ENABLE,NCCL_MNNVL_ENABLE,NVIS_NCCL_MNNVL_MODE,NCCL_IB_HCA,OMPI_MCA_coll_hcoll_enable,/' \
  -e '/      "$binary" -b8K/i\
      if [ "$NVIS_NCCL_MNNVL_MODE" = auto ]; then unset NCCL_MNNVL_ENABLE; else export NCCL_MNNVL_ENABLE="$NVIS_NCCL_MNNVL_MODE"; fi' \
  -e '/export NVIS_REPORT_DIR NVIS_IMAGE/a\
ulimit -l unlimited\
if ! srun -N "$SLURM_JOB_NUM_NODES" --ntasks="$SLURM_JOB_NUM_NODES" --ntasks-per-node=1 --mpi=pmi2 --kill-on-bad-exit=1 --wait=0 bash -c '\''set -e; uid=$(id -u); gid=$(id -g); sudo -n install -d -m 0775 -o "$uid" -g "$gid" /run/enroot /run/enroot/runtime; sudo -n install -d -m 0755 -o root -g root /var/lib/enroot /var/lib/enroot/data; sudo -n install -d -m 0700 -o "$uid" -g "$gid" "/var/lib/enroot/data/$uid"; mkdir -p "$HOME/.config/enroot"; chmod 700 "$HOME/.config/enroot"'\''; then echo "ERROR: enroot runtime setup failed; no NCCL collective was started" >&2; exit 17; fi' \
  "$snapshot" > "$portable_recipe"
portable_actual="$(sha256sum "$portable_recipe" | awk '{print $1}')"
[[ "$portable_actual" == "$portable_sha" ]] || {
  echo "ERROR: reviewed NCCL portability transform mismatch: got $portable_actual expected $portable_sha" >&2
  exit 2
}

# A degraded tray must not make the entire rack look defective.  NCCL can fall
# back from a missing IB rail to the RoCE bond, after which peers abort with the
# misleading cluster-wide error "Remote RoCE/IB device is incompatible".  Check
# the four GB300 compute rails on every allocated node before communicator init
# and name the exact tray/HCA that needs service.
if ! srun -N "$SLURM_JOB_NUM_NODES" --ntasks="$SLURM_JOB_NUM_NODES" \
    --ntasks-per-node=1 --mpi=pmi2 --cpu-bind=none --kill-on-bad-exit=0 --wait=0 \
    bash -c '
      bad=0
      for hca in mlx5_0 mlx5_1 mlx5_2 mlx5_3; do
        state=$(cat "/sys/class/infiniband/$hca/ports/1/state" 2>/dev/null || true)
        if [[ "$state" != "4: ACTIVE" ]]; then
          printf "NCCL_IB_PREFLIGHT_FAIL node=%s hca=%s state=%s\n" \
            "$(hostname)" "$hca" "${state:-missing}" >&2
          bad=1
        fi
      done
      (( bad == 0 )) || exit 17
      printf "NCCL_IB_PREFLIGHT_READY node=%s rails=4\n" "$(hostname)"
    '; then
  echo "ERROR: NCCL IB preflight failed; no collective was started" >&2
  exit 17
fi

bash "$portable_recipe"

if [[ $contract == RACK_MNNVL || $contract == RACK_NVLS ]]; then
  report_dir="${AUG21_REPORT_DIR:?}/job-${SLURM_JOB_ID}"
  all_reduce_raw="$report_dir/test-1-all_reduce-8k-32g.raw"
  min_busbw="${NVIS_NCCL_MIN_RACK_BUSBW_GBS:-600}"
  [[ $min_busbw =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "ERROR: invalid NVIS_NCCL_MIN_RACK_BUSBW_GBS=$min_busbw" >&2
    exit 19
  }
  for bytes in 17179869184 34359738368; do
    busbw=$(awk -v bytes="$bytes" '$1 == bytes && $9 == 0 {value=$8} END {if (value != "") print value}' "$all_reduce_raw")
    [[ -n $busbw ]] || {
      echo "ERROR: $contract missing correct all-reduce result bytes=$bytes" >&2
      exit 19
    }
    awk -v value="$busbw" -v floor="$min_busbw" 'BEGIN {exit !(value + 0 >= floor + 0)}' || {
      echo "ERROR: $contract below acceptance floor bytes=$bytes busbw_gbs=$busbw required_gbs=$min_busbw" >&2
      exit 19
    }
    echo "${contract}_ACCEPT bytes=$bytes busbw_gbs=$busbw required_gbs=$min_busbw"
  done
fi
