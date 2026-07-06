# shellcheck shell=bash
# lib/nodes.inc.sh — resolve NVIS rack ids to a Slurm nodelist + compute scale.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). Extracted from run_llmb_nccl.sh so any launcher can
# reuse the rack->node mapping. A rack id (e.g. MEL01-HC07-RK0105 or a Boston
# bos01-b06) is NOT a Slurm hostname; sites transform it via:
#   RACK_TO_NODE_SED         sed to turn a rack id into a node-name prefix
#                            (MEL01 default 's/-RK/-SVR-CN/'; Boston 's/$/-/')
#   RACK_NODE_MATCH_SUFFIX   regex appended to the prefix when matching sinfo
#                            (default '[0-9]+$'; Boston '.*$')
#
# Public:
#   nvis_nodes_resolve --nodelist LIST      -> sets NVIS_NODELIST, NVIS_NNODES
#   nvis_nodes_resolve RACK [RACK ...]      -> sets NVIS_NODELIST, NVIS_NNODES
#   nvis_nodes_scale [gpus_per_node]        -> echoes NNODES * GPUS_PER_NODE
# Requires sinfo/scontrol on PATH.

nvis_nodes_resolve() {
  local sed_expr="${RACK_TO_NODE_SED:-s/-RK/-SVR-CN/}"
  local suffix="${RACK_NODE_MATCH_SUFFIX:-[0-9]+$}"
  NVIS_NODELIST=""
  NVIS_NNODES=0

  if [[ "$1" == "--nodelist" ]]; then
    NVIS_NODELIST="${2:?nvis_nodes_resolve --nodelist needs a value}"
    NVIS_NNODES="$(scontrol show hostnames "${NVIS_NODELIST}" 2>/dev/null | grep -c .)"
    [[ "${NVIS_NNODES}" -gt 0 ]] || { echo "nvis_nodes: --nodelist '${NVIS_NODELIST}' expands to 0 nodes" >&2; return 2; }
    return 0
  fi

  [[ $# -gt 0 ]] || { echo "nvis_nodes: no racks given (pass rack ids or --nodelist LIST)" >&2; return 2; }
  local -a all_nodes=() rk_nodes=()
  local rk prefix
  for rk in "$@"; do
    prefix="$(echo "${rk}" | sed -E "${sed_expr}")"
    mapfile -t rk_nodes < <(sinfo -N -h -o '%N' 2>/dev/null | grep -E "^${prefix}${suffix}" | sort -u)
    if [[ ${#rk_nodes[@]} -eq 0 ]]; then
      echo "nvis_nodes: rack '${rk}' (prefix '${prefix}') matched 0 Slurm nodes — check the rack id / RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX" >&2
      return 2
    fi
    getent hosts "${rk_nodes[0]}" >/dev/null 2>&1 || \
      echo "nvis_nodes: WARNING: ${rk_nodes[0]} does not resolve via getent hosts" >&2
    echo "nvis_nodes: ${rk} -> ${#rk_nodes[@]} nodes (${rk_nodes[0]} ... ${rk_nodes[-1]})" >&2
    all_nodes+=("${rk_nodes[@]}")
  done
  NVIS_NNODES=${#all_nodes[@]}
  local csv; csv="$(IFS=,; echo "${all_nodes[*]}")"
  NVIS_NODELIST="$(scontrol show hostlist "${csv}" 2>/dev/null || echo "${csv}")"
}

nvis_nodes_scale() {
  local gpus="${1:-${GPUS_PER_NODE:-4}}"
  echo $(( ${NVIS_NNODES:-0} * gpus ))
}

# First hostname in NVIS_NODELIST (used for a representative-node fabric probe).
nvis_nodes_first() {
  scontrol show hostnames "${NVIS_NODELIST}" 2>/dev/null | head -1
}

# GPUs per node from Slurm GRES (head-side, no GPUs needed). Handles Gres=gpu:4
# and Gres=gpu:<type>:4[(S:...)]. Echoes the count, or nothing if undetectable.
# $1 = a node hostname.
nvis_nodes_gpus_per_node() {
  local n="${1:-}" g
  [[ -n "$n" ]] || return 1
  g="$(scontrol -o show node "$n" 2>/dev/null \
        | grep -oE 'Gres=[^ ]+' \
        | grep -oE 'gpu(:[A-Za-z0-9_]+)?:[0-9]+' \
        | grep -oE '[0-9]+$' | head -1)"
  [[ -n "$g" ]] && printf '%s' "$g"
}
