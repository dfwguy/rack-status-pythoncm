# shellcheck shell=bash
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
# lib/rank_layout.inc.sh — lay a job's ranks out rack by rack, and name the host rank 0 runs on.
#
# Source (do not execute). Used by the launchers whose ranks run llmb's generated job script
# (run_llmb_nemo.sh, run-exemplar-llama.sh), which we cannot edit -- only its environment.
#
# Slurm numbers a job's tasks in its own node order, and on a cluster whose hostnames do not group by
# rack that order interleaves racks. Nscale NC names nodes gpuN-suM, so the racks at the same position
# in different SUs (Q7 and S7: gpu91-108) alternate node by node. Megatron builds its tensor- and
# expert-parallel groups from consecutive ranks, so a 64-rank expert group spanned two or three NVLink
# domains and every MoE Exemplar recipe died on its first step: DeepEP shares GPU memory through
# fabric handles, which do not cross an NVLink domain ("cuMemImportFromShareableHandle ... invalid
# resource handle", Slurm 216666, 2026-10-20). The same hostnames do not compress into a Slurm range,
# so SLURM_NODELIST reaches the ranks as a plain comma list, and the Megatron-Bridge in the NeMo
# 26.04.01 image took that whole list as its rendezvous address and hung for 20 minutes (216664) --
# the variables the enroot PyTorch hook would set are not set on NC.
#
# So the launcher writes the rank order itself: one line per task (Slurm's arbitrary distribution),
# the hosts of each rack together, and exports SLURM_HOSTFILE + SLURM_DISTRIBUTION=arbitrary (read by
# the srun inside the job) and MASTER_ADDR = the first host, where rank 0 now runs. Which rack a host
# is in comes from the cluster's inventory: Rack Status projects it as NVIS_NODE_PLACEMENT,
# "rack=hostlist;rack=hostlist", any Slurm hostlist per rack. Hosts it does not place keep their
# natural order after the placed ones. A key stated for this launch (NVIS_EXTRA_ENV_KEYS) wins:
# a stated SLURM_HOSTFILE leaves the layout to the operator entirely, a stated MASTER_ADDR is kept.
#
# Public:
#   nvis_rank_layout WHO NODES TASKS_PER_NODE DIR
#       NODES: the job's hosts (comma list or Slurm hostlist). Writes DIR/rank-layout-<stamp>.hosts,
#       exports the three variables and adds them to NVIS_FORCE_EXPORT_VARS. Returns 1 (and changes
#       nothing) if the hosts cannot be expanded or the file cannot be written.

_nvis_rank_layout_expand() {  # hostlist -> one host per line
  local list=$1
  if command -v scontrol >/dev/null 2>&1; then
    scontrol show hostnames "$list" 2>/dev/null && return 0
  fi
  [[ $list == *"["* ]] && return 1  # a range needs Slurm to expand it
  tr ',' '\n' <<< "$list" | sed '/^$/d'
}

nvis_rank_layout() {
  local who=${1:-launcher} nodes=${2:-} per_node=${3:-4} dir=${4:-.} entry rack hostlist host
  local IFS=$' \t\n'
  if [[ " ${NVIS_EXTRA_ENV_KEYS:-} " == *" SLURM_HOSTFILE "* ]]; then
    echo "${who}: rank layout: SLURM_HOSTFILE stated for this launch; left as given" >&2
    return 0
  fi
  [[ $per_node =~ ^[1-9][0-9]*$ ]] || { echo "${who}: rank layout: bad tasks per node '${per_node}'" >&2; return 1; }
  local -a job=()
  mapfile -t job < <(_nvis_rank_layout_expand "$nodes")
  ((${#job[@]})) || { echo "${who}: rank layout: cannot expand '${nodes}'" >&2; return 1; }

  # host -> rack, from the placement the cluster's inventory gave each rack's hosts.
  local -A rack_of=()
  while IFS= read -r -d ';' entry; do
    rack=${entry%%=*}; hostlist=${entry#*=}
    [[ $entry == *=* && -n $rack && -n $hostlist ]] || continue
    while IFS= read -r host; do
      [[ -n $host ]] && rack_of[$host]=$rack
    done < <(_nvis_rank_layout_expand "$hostlist")
  done <<< "${NVIS_NODE_PLACEMENT:-};"

  # Racks in natural order, hosts naturally ordered within each; unplaced hosts last.
  local ordered unplaced=0
  ordered=$(for host in "${job[@]}"; do
              if [[ -n ${rack_of[$host]:-} ]]; then printf '0\t%s\t%s\n' "${rack_of[$host]}" "$host"
              else printf '1\t~\t%s\n' "$host"; fi
            done | sort -t $'\t' -k1,1n -k2,2V -k3,3V | cut -f3)
  for host in "${job[@]}"; do [[ -n ${rack_of[$host]:-} ]] || unplaced=$((unplaced + 1)); done

  mkdir -p "$dir" 2>/dev/null
  local file
  file="${dir%/}/rank-layout-$(date +%Y%m%dT%H%M%S)-$$.hosts"
  awk -v n="$per_node" '{for (i = 0; i < n; i++) print}' <<< "$ordered" > "$file" 2>/dev/null \
    || { echo "${who}: rank layout: cannot write ${file}" >&2; return 1; }

  export SLURM_HOSTFILE=$file SLURM_DISTRIBUTION=arbitrary
  local keys="SLURM_HOSTFILE SLURM_DISTRIBUTION"
  if [[ " ${NVIS_EXTRA_ENV_KEYS:-} " != *" MASTER_ADDR "* ]]; then
    MASTER_ADDR=$(head -n 1 "$file"); export MASTER_ADDR
    keys="$keys MASTER_ADDR"
  fi
  local k
  for k in $keys; do
    [[ " ${NVIS_FORCE_EXPORT_VARS:-} " == *" ${k} "* ]] || NVIS_FORCE_EXPORT_VARS="${NVIS_FORCE_EXPORT_VARS:+${NVIS_FORCE_EXPORT_VARS} }${k}"
  done
  export NVIS_FORCE_EXPORT_VARS
  local racks
  racks=$(for host in "${job[@]}"; do [[ -n ${rack_of[$host]:-} ]] && echo "${rack_of[$host]}"; done | sort -u | wc -l)
  local note=""
  ((unplaced)) && note=", ${unplaced} not placed by the inventory (kept last, in natural order)"
  echo "${who}: rank layout: ${#job[@]} hosts in ${racks} rack(s)${note} -> ${file} (rank 0 on ${MASTER_ADDR:-<stated>})" >&2
  return 0
}
