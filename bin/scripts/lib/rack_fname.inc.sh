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
# lib/rack_fname.inc.sh — the ONE mapping from a log basename to its rack id and job id.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). Shared by rack_status.sh and parser.sh.
#
# Why this file exists: both scripts carried their own rack_from_fname/jobid_from_fname and the
# copies had drifted in three ways, each losing real data on some site:
#   * rack_status.sh knew ClusterKit (root-CK-…) names; parser.sh did not.
#   * parser.sh had the generic NVIS_RACK_FNAME_RE fallback; rack_status.sh did not, so on a
#     site like Nscale TX (ink50-dh01-h22) EVERY log fell through to "?" in the shell table
#     while the Parser Results tables resolved the rack correctly.
#   * parser.sh's generic job-id branch matched plain "…-<jid>.txt" (MNUBERGEMM, nvbandwidth);
#     rack_status.sh returned "?" for those.
# This file is the union. Where the two callers genuinely need DIFFERENT answers -- Llama
# stdout, where the parser tabulates per NODE and the rack table needs the rack -- the two
# answers are separate named functions instead of an accidental difference.
#
# Public:
#   rack_from_fname FILE                 -> rack id, or "?"
#   llama_host_from_fname FILE           -> CT hostname for Llama stdout, else "" (rc 1)
#   rack_or_llama_host_from_fname FILE   -> Llama host when Llama, else rack id (parser tables)
#   jobid_from_fname FILE                -> Slurm job id, or "?"
#
# Env:
#   NVIS_RACK_FNAME_RE   per-site override for the generic rack pattern; must contain exactly
#                        one capture group. Default matches <word>-<word>-<letter><digits>.

# Lowercase without bash 4 ``${v,,}`` so this include stays usable under bash 3.2.
_rf_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

_rf_is_llama() {
  case "$(_rf_lower "$1")" in *nemo-llama3_8b-1n-*) return 0 ;; *) return 1 ;; esac
}

# CT hostname from Llama Slurm stdout: …-NEMO-LLAMA3_8B-1N-<hostname>-<jid>.txt
llama_host_from_fname() {
  local b _save
  b=$(basename "$1")
  _rf_is_llama "$b" || return 1
  _save=$(shopt -p nocasematch 2>/dev/null || printf 'shopt -u nocasematch\n')
  shopt -s nocasematch 2>/dev/null || true
  if [[ "$b" =~ NEMO-LLAMA3_8B-1N-(.+)-([0-9]+)\.txt$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    eval "$_save"
    return 0
  fi
  eval "$_save"
  return 1
}

rack_from_fname() {
  local b low
  b=$(basename "$1")
  low=$(_rf_lower "$b")

  # Bright / cmsh: …1Rack-p05-r01-… or …1Rack-p05-r01[…]
  if [[ "$b" =~ 1Rack-(p[0-9]+-r[0-9]+)(\[|-) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  # CBR / Firmus: …1Rack-CBR01-DH01-SVR-CN0102[11-18]-…
  if [[ "$b" =~ 1Rack-(CBR[0-9]{2}-DH[0-9]{2}-SVR-CN[0-9]{4})(\[|-) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  # ClusterKit: root-CK-<rack>-… (rack token must look like a CBR rack)
  if [[ "$low" == *root-ck* ]] && [[ "$b" =~ (CBR[0-9]{2}-DH[0-9]{2}-SVR-CN[0-9]{4}) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  # Llama stdout names a NODE; derive its rack.
  if _rf_is_llama "$b"; then
    if [[ "$b" =~ NEMO-LLAMA3_8B-1N-(p[0-9]+-r[0-9]+)-ct[0-9]+- ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"; return 0
    fi
    # CBR01-DH01-SVR-CN010211 -> CBR01-DH01-SVR-CN0102 (drop the 2 tray digits)
    if [[ "$b" =~ NEMO-LLAMA3_8B-1N-(CBR[0-9]{2}-DH[0-9]{2}-SVR-CN[0-9]{6,})-([0-9]+)\.txt$ ]]; then
      local host prefix tail
      host="${BASH_REMATCH[1]}"
      if [[ "$host" =~ ^(CBR[0-9]{2}-DH[0-9]{2}-SVR-CN)([0-9]+)$ ]]; then
        prefix="${BASH_REMATCH[1]}"; tail="${BASH_REMATCH[2]}"
        if ((${#tail} >= 3)); then
          printf '%s\n' "${prefix}${tail:0:${#tail}-2}"; return 0
        fi
      fi
    fi
  fi
  # Generic site fallback: <word>-<word>-<letter><digits>, e.g. ink50-dh01-h22. Deliberately
  # case-sensitive (site rack ids are lowercase) and last, so the specific shapes win.
  if [[ "$b" =~ ${NVIS_RACK_FNAME_RE:-([a-z0-9]+-[a-z0-9]+-[a-z][0-9]+)} ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  printf '?\n'
}

# What the parser's per-file tables want: a Llama log is tabulated by NODE, everything else
# by rack. Kept as its own name so the difference is a decision, not a drifted copy.
rack_or_llama_host_from_fname() {
  local host
  if host=$(llama_host_from_fname "$1"); then
    printf '%s\n' "$host"; return 0
  fi
  rack_from_fname "$1"
}

jobid_from_fname() {
  local b
  b=$(basename "$1")
  # Llama first: …-1N-<host>-<jid>.txt has no "N-<jid>" tail unless the host ends in N.
  if [[ "$b" =~ NEMO-LLAMA3_8B-1N-.+-([0-9]+)\.txt$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  # NEMO/NCCL: …-18N-212.txt
  if [[ "$b" =~ N-([0-9]+)\.txt$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  # Other tools (MNUBERGEMM, nvbandwidth, …): plain trailing -<jid>.txt
  if [[ "$b" =~ -([0-9]+)\.txt$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  # MPI launcher stdout: nccl-mpi_<rack>+<rack>_<jid>.out — an underscore separator and a .out
  # extension, so none of the .txt patterns above matched and the Parser Results table showed the
  # job as "?" even though the id is right there in the name.
  if [[ "$b" =~ [_-]([0-9]+)\.out$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"; return 0
  fi
  printf '?\n'
}
