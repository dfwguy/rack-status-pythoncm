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
# cluster_facts.sh — cluster-wide software versions for the status report.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Runs ONCE on the head, not per node: BCM, Slurm, NCCL and HPC-X are properties of the cluster's
# shared software tree, so asking two thousand nodes the same question would be two thousand
# times the cost for one answer.
#
# Prints "key<TAB>value" lines, same contract as node_inventory.sh, so one parser serves both.
# Every probe is guarded — a site without HPC-X should yield a report missing that row, not a
# failure.
set -uo pipefail

emit() { [ -n "${2:-}" ] && printf '%s\t%s\n' "$1" "$2"; return 0; }

# ---- Base Command Manager. /etc/cm-release is the canonical string ("Cluster Manager v11.0").
if [ -r /etc/cm-release ]; then
  emit bcm.version "$(head -1 /etc/cm-release 2>/dev/null | tr -d '\r')"
fi
emit bcm.cmdaemon_version "$(dpkg-query -W -f='${Version}' cmdaemon 2>/dev/null | head -1)"

# ---- Slurm. sinfo needs SLURM_CONF; without it --version still runs but scontrol does not, and
# on this cluster the conf lives under a per-arch shared tree rather than /etc/slurm.
if [ -z "${SLURM_CONF:-}" ]; then
  cand=$(ls /cm/shared*/apps/slurm/etc/slurm/slurm.conf /etc/slurm/slurm.conf 2>/dev/null | head -1)
  [ -n "${cand:-}" ] && export SLURM_CONF="$cand"
fi
sinfo_bin=$(command -v sinfo 2>/dev/null || ls -d /cm/local/apps/slurm/*/bin/sinfo 2>/dev/null | sort -V | tail -1)
[ -n "${sinfo_bin:-}" ] && emit slurm.version "$("$sinfo_bin" --version 2>/dev/null | awk '{print $NF}')"
emit slurm.conf "${SLURM_CONF:-}"

# ---- NCCL, HPC-X and the CUDA toolkit are NOT on the host on a container-based site: the
# validation runs execute inside a pyxis/enroot image, so the only truthful answer is the
# version that actually produced the numbers, which the run logs record. Host paths are still
# probed first because other sites install them in a module tree.
nccl_h=$(ls /usr/include/nccl.h /cm/shared*/apps/nccl*/include/nccl.h 2>/dev/null | head -1)
if [ -n "${nccl_h:-}" ]; then
  emit nccl.version "$(awk '/define NCCL_MAJOR/{maj=$3} /define NCCL_MINOR/{min=$3} /define NCCL_PATCH/{pat=$3} END{if(maj!="") printf "%s.%s.%s", maj, min, pat}' "$nccl_h")"
  emit nccl.source host
else
  lib=$(ls -1 /usr/lib/*/libnccl.so.* /cm/shared*/apps/nccl*/lib/libnccl.so.* 2>/dev/null | sort -V | tail -1)
  if [ -n "${lib:-}" ]; then
    emit nccl.version "$(basename "$lib" | sed 's/^libnccl\.so\.//')"
    emit nccl.source host
  fi
fi

hpcx=$(ls -d /cm/shared*/apps/hpcx*/ /opt/hpcx* 2>/dev/null | sort -V | tail -1)
[ -n "${hpcx:-}" ] && emit hpcx.version "$(basename "${hpcx%/}")"

nvcc=$(command -v nvcc 2>/dev/null || ls -d /usr/local/cuda*/bin/nvcc 2>/dev/null | sort -V | tail -1)
[ -n "${nvcc:-}" ] && emit cuda.toolkit_version "$("$nvcc" --version 2>/dev/null | awk -F', ' '/release/{print $2}' | tr -d 'V')"

# ---- Log-derived: what the runs actually used. $1 is the log directory. Bounded to the newest
# handful of files so this stays a metadata probe and never a scan of the corpus.
LOGDIR="${1:-}"
if [ -n "$LOGDIR" ] && [ -d "$LOGDIR" ]; then
  # Only if a host install did not already answer. It is the NEMO logs that print the banner --
  # the nccl-test logs do not -- and the value differs between runs because it comes from the
  # container image of the day, so the newest run that recorded one is the honest answer.
  #
  # Reads are capped: these logs reach hundreds of MB, and NCCL initialises early, so the first
  # few MB either has the banner or the file does not have one. Without the cap, every file
  # lacking it would be read end to end.
  if [ -z "${nccl_h:-}${lib:-}" ]; then
    for f in $(ls -t "$LOGDIR"/*NEMO*.txt "$LOGDIR"/*nemo*.txt 2>/dev/null | head -25); do
      v=$(head -c 8000000 "$f" 2>/dev/null | grep -ohm1 -E "NCCL version [0-9]+\.[0-9]+\.[0-9]+\+cuda[0-9.]+" | head -1)
      if [ -n "$v" ]; then
        emit nccl.version "$(echo "$v" | awk '{print $3}')"
        emit nccl.source "run log (container image)"
        break
      fi
    done
  fi
  img=$(grep -ohm1 -E '\-\-container-image[= ][^ ]+' $(ls -t "$LOGDIR"/*.txt 2>/dev/null | head -20) 2>/dev/null | head -1 | awk '{print $NF}')
  [ -n "$img" ] && emit container.image "$(basename "$img")"
fi

# ---- UFM, if this head can see it. Absent on most sites, which is a real answer.
emit ufm.version "$(dpkg-query -W -f='${Version}' ufm 2>/dev/null | head -1)"

# ---- Head identity, so a report says which machine answered.
emit head.hostname "$(hostname -f 2>/dev/null || hostname)"
# shellcheck disable=SC1091
[ -r /etc/os-release ] && emit head.os "$(. /etc/os-release && echo "$PRETTY_NAME")"
emit head.kernel "$(uname -r)"

exit 0
