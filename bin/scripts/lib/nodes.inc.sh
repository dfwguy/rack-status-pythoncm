# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# lib/nodes.inc.sh — resolve NVIS rack ids to a Slurm nodelist + compute scale.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). Extracted from run_llmb_nccl.sh so any launcher can
# reuse the rack->node mapping. An Nscale rack id is not itself a node hostname,
# so it has to be mapped. In priority order:
#
#   1. RACK_TO_NODE_SED + RACK_NODE_MATCH_SUFFIX — explicit per-site override. sed
#      turns the rack id into a node-name prefix; the suffix regex is appended when
#      matching sinfo. Setting either pins the mapping to exactly that expression.
#   2. NVIS_RACK_ID + NVIS_CT_ID — the app's naming templates, which the backend
#      already forwards over SSH (NVIS_ is in transport FORWARD_ENV_PREFIXES). The
#      rack template is a literal prefix of the CT template (the backend enforces
#      this in validate_nvis_naming_triplet), so the node pattern is just the rack
#      id followed by the CT tail. This is the single source of truth — the site's
#      geometry lives in the catalog, not in these scripts.
#   3. Built-in conventions, tried tightest-first, so a head node with no app
#      environment at all still resolves.
#
# Templates only decide which NAMES belong to a rack; sinfo decides which of them
# exist. Never expand a template into hostnames — Nscale TX trays are 11-18 and
# 28-37, so an expanded [01-18]/[11-37] range invents nodes that do not resolve.
#
# Public:
#   nvis_nodes_resolve --nodelist LIST      -> sets NVIS_NODELIST, NVIS_NNODES
#   nvis_nodes_resolve RACK [RACK ...]      -> sets NVIS_NODELIST, NVIS_NNODES
#   nvis_nodes_scale [gpus_per_node]        -> echoes NNODES * GPUS_PER_NODE
#   nvis_nodes_rack_of HOST                 -> echoes the rack id owning HOST
# Requires sinfo/scontrol on PATH.

# Translate an NVIS naming template (or a fragment of one) into an ERE. Token set
# mirrors backend/nvis_id_templates.py: [\d+] -> [0-9]+, [NN-NN] -> [0-9]{1,2}
# (loose, matching the backend's scan mode, since cmsh spells p5-r01 as p5-r1),
# [a-z]/[A-Z] -> [A-Za-z]. Anything else is a literal, escaped where needed.
_nvis_template_to_ere() {
  local s="${1:-}" out="" c
  while [[ -n "${s}" ]]; do
    case "${s}" in
      '[\d+]'*)                       out+='[0-9]+';     s="${s:5}" ;;
      '[a-z]'*|'[A-Z]'*)              out+='[A-Za-z]';   s="${s:5}" ;;
      '['[0-9][0-9]'-'[0-9][0-9]']'*) out+='[0-9]{1,2}'; s="${s:7}" ;;
      *) c="${s:0:1}"; s="${s:1}"
         case "${c}" in
           [A-Za-z0-9_-]) out+="${c}" ;;
           *)             out+="\\${c}" ;;
         esac ;;
    esac
  done
  printf '%s' "${out}"
}

# Anchored node-name ERE for a rack, derived from the app's naming templates.
# Fails (no output) when the templates are absent, or when NVIS_RACK_ID is not a
# prefix of NVIS_CT_ID — in which case we cannot say what the tray part looks like
# and the caller falls back to the built-in conventions.
_nvis_nodes_template_re() {
  local rk="$1" rack_t="${NVIS_RACK_ID:-}" ct_t="${NVIS_CT_ID:-}" tail
  [[ -n "${rack_t}" && -n "${ct_t}" ]] || return 1
  [[ "${ct_t}" == "${rack_t}"* ]] || return 1
  tail="${ct_t#"${rack_t}"}"
  [[ -n "${tail}" ]] || return 1
  printf '^%s%s$' "$(_nvis_template_to_ere "${rk}")" "$(_nvis_template_to_ere "${tail}")"
}

# Rack id owning a node hostname, per NVIS_RACK_ID (the rack template matches the
# head of every CT hostname). Silent failure when the template is unset or the host
# does not start with a rack id, so callers can fall back.
nvis_nodes_rack_of() {
  local h="${1:-}" out
  [[ -n "${h}" && -n "${NVIS_RACK_ID:-}" ]] || return 1
  out="$(printf '%s' "${h}" | grep -oE "^$(_nvis_template_to_ere "${NVIS_RACK_ID}")" | head -1)"
  [[ -n "${out}" ]] || return 1
  printf '%s' "${out}"
}

# Candidate anchored regexes matching a rack id's Slurm nodes, most specific first.
# See the header for the precedence rules. The built-in conventions cover:
#   NscaleTX   ink50-dh01-h18             -> ink50-dh01-h18-11
# The hyphen in candidates 2/3 is what the old bare default lacked: '[0-9]+$' glued
# straight onto the rack id can never match a '<rack>-<tray>' hostname.
_nvis_nodes_candidates() {
  local rk="$1" mel tpl
  if [[ -n "${RACK_TO_NODE_SED:-}" || -n "${RACK_NODE_MATCH_SUFFIX:-}" ]]; then
    printf '^%s%s\n' \
      "$(printf '%s' "${rk}" | sed -E "${RACK_TO_NODE_SED:-s/-RK/-SVR-CN/}")" \
      "${RACK_NODE_MATCH_SUFFIX:-[0-9]+$}"
    return 0
  fi
  tpl="$(_nvis_nodes_template_re "${rk}")" && [[ -n "${tpl}" ]] && printf '%s\n' "${tpl}"
  mel="$(printf '%s' "${rk}" | sed -E 's/-RK/-SVR-CN/')"
  [[ "${mel}" != "${rk}" ]] && printf '^%s[0-9]+$\n' "${mel}"
  printf '^%s-[0-9]+$\n' "${rk}"
  printf '^%s-.*$\n' "${rk}"
  printf '^%s[0-9]+$\n' "${rk}"
}

nvis_nodes_resolve() {
  NVIS_NODELIST=""
  NVIS_NNODES=0

  if [[ "${1:-}" == "--nodelist" ]]; then
    NVIS_NODELIST="${2:?nvis_nodes_resolve --nodelist needs a value}"
    NVIS_NNODES="$(scontrol show hostnames "${NVIS_NODELIST}" 2>/dev/null | grep -c .)"
    [[ "${NVIS_NNODES}" -gt 0 ]] || { echo "nvis_nodes: --nodelist '${NVIS_NODELIST}' expands to 0 nodes" >&2; return 2; }
    return 0
  fi

  [[ $# -gt 0 ]] || { echo "nvis_nodes: no racks given (pass rack ids or --nodelist LIST)" >&2; return 2; }
  # One sinfo for the whole call: the candidate sweep below would otherwise re-run it
  # per rack per candidate.
  local sinfo_nodes; sinfo_nodes="$(sinfo -N -h -o '%N' 2>/dev/null | sort -u)"
  [[ -n "${sinfo_nodes}" ]] || { echo "nvis_nodes: sinfo returned no nodes — is Slurm reachable (SLURM_CONF)?" >&2; return 2; }

  local -a all_nodes=() rk_nodes=() cands=()
  local rk re matched
  for rk in "$@"; do
    mapfile -t cands < <(_nvis_nodes_candidates "${rk}")
    matched=""
    for re in "${cands[@]}"; do
      mapfile -t rk_nodes < <(printf '%s\n' "${sinfo_nodes}" | grep -E "${re}")
      [[ ${#rk_nodes[@]} -gt 0 ]] && { matched="${re}"; break; }
    done
    if [[ -z "${matched}" ]]; then
      echo "nvis_nodes: rack '${rk}' matched 0 Slurm nodes with any of: ${cands[*]}" >&2
      echo "nvis_nodes: check the rack id against 'sinfo -N -h -o %N', or set RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX for this site" >&2
      return 2
    fi
    getent hosts "${rk_nodes[0]}" >/dev/null 2>&1 || \
      echo "nvis_nodes: WARNING: ${rk_nodes[0]} does not resolve via getent hosts" >&2
    echo "nvis_nodes: ${rk} -> ${#rk_nodes[@]} nodes (${rk_nodes[0]} ... ${rk_nodes[-1]}) [${matched}]" >&2
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
