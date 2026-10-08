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
# lib/node_images.inc.sh — where a compute node keeps its container images, and how a launcher
# gets one there.
#
# The node cache is a site fact. Launchers used to hard-code /raid/data (Boston/Mayo DGX) or
# /tmp/containers (Nscale TX); Nscale NC has neither — its nodes cache images under
# /opt/containers and have no /raid at all, so every staging step died at mkdir. The directory
# is now the cluster's connection.node_container_dir, which Rack Status exports as
# NVIS_NODE_CONTAINER_DIR (forwarded over SSH by its NVIS_ prefix). A manual run sets that
# variable; NVIS_NODE_CONTAINER_DIR_DEFAULT below is the one fallback, matching the backend's
# validation_presets.LOCAL_CONTAINER_DIR.

NVIS_NODE_CONTAINER_DIR_DEFAULT=/tmp/containers

# Print the node image cache directory, without a trailing slash.
nvis_node_container_dir() {
  local d="${NVIS_NODE_CONTAINER_DIR:-${NVIS_NODE_CONTAINER_DIR_DEFAULT}}"
  [[ "$d" == / ]] || d="${d%/}"
  printf '%s\n' "$d"
}

# Is the node image cache owned by NVIS-P2P? Rack Status sets NVIS_NODE_IMAGES_P2P=1 for a cluster
# with P2P enabled; images then reach nodes only through P2P campaigns.
nvis_node_images_p2p() {
  [[ "${NVIS_NODE_IMAGES_P2P:-0}" == 1 ]]
}

# nvis_stage_node_image <tag> <host,csv> <source .sqsh>
#
# Make <source> present on every listed node at $(nvis_node_container_dir)/<basename>, then export
# NVIS_NEMO_CONT to that path. A node whose copy already has the source's byte size is left alone:
# the cache is usually warm (bringup's file_copy filled it), and rsync -a would otherwise re-read
# 40+ GiB per node because the mtimes differ. Only a missing or short copy is rsync'd from the head
# -- and on a P2P cluster (nvis_node_images_p2p) not even that: the image must already be there,
# and a node without it fails the launch with a pointer to a P2P campaign.
#
# Success is counted, not assumed: every node must answer STAGE_OK. A node pdsh could not reach
# never runs the remote command, so "no node reported a failure" would pass a run where ssh failed
# everywhere (run-nemo-xrack.sh learned this the hard way). Returns non-zero before anything is
# submitted when any node falls short.
#
# The copy runs as the launching user first (NC's /opt/containers is group-writable by cmods) and
# falls back to sudo where the cache is root-owned (as /raid was): RACK_SUDO_PASSWORD -> sudo -S,
# otherwise sudo -n (passwordless sudo in the node image, or already root). A warm cache needs no
# write at all.
nvis_stage_node_image() {
  local tag="$1" hostcsv="$2" src="$3"
  local dir base dest size nhosts sudo_pfx out ok
  dir="$(nvis_node_container_dir)"
  base="$(basename "$src")"
  dest="${dir}/${base}"
  size=$(stat -c%s "$src" 2>/dev/null || echo 0)
  if [[ ! "$size" =~ ^[1-9][0-9]*$ ]]; then
    echo "${tag}: ERROR: image is missing or empty on the launch host: ${src}" >&2
    return 1
  fi
  nhosts=$(tr ',' '\n' <<<"$hostcsv" | grep -c . || true)
  if [[ "${nhosts:-0}" -eq 0 ]]; then
    echo "${tag}: ERROR: no nodes to stage ${base} onto" >&2
    return 1
  fi
  if [[ -n "${RACK_SUDO_PASSWORD:-}" ]]; then
    sudo_pfx="printf '%s\n' $(printf %q "$RACK_SUDO_PASSWORD") | sudo -S -p '' "
  else
    sudo_pfx="sudo -n "
  fi
  # Quoted for the remote shell: dir/dest come from a validated cluster setting and src from the
  # launch environment, and none of them may split into extra words over there.
  local q_dir q_dest q_src
  q_dir=$(printf %q "$dir"); q_dest=$(printf %q "$dest"); q_src=$(printf %q "$src")
  local copy_cmd
  if nvis_node_images_p2p; then
    # P2P owns this cache: verify only. Never write into it, never fan out from the head.
    echo "${tag}: verifying ${base} (${size} bytes) in ${dir} on ${nhosts} node(s); NVIS-P2P owns this cache, so no head-node copy"
    copy_cmd=":"
  else
    echo "${tag}: staging ${base} (${size} bytes) into ${dir} on ${nhosts} node(s)"
    copy_cmd="{ mkdir -p ${q_dir} && rsync -ah ${q_src} ${q_dest}; } 2>/dev/null \
|| { ${sudo_pfx}mkdir -p ${q_dir} && ${sudo_pfx}rsync -ah ${q_src} ${q_dest}; }"
  fi
  out=$(pdsh -R ssh -w "$hostcsv" \
    "s=\$(stat -c%s ${q_dest} 2>/dev/null || echo 0); \
if [ \"\$s\" != '${size}' ]; then ${copy_cmd}; \
s=\$(stat -c%s ${q_dest} 2>/dev/null || echo 0); fi; \
if [ \"\$s\" = '${size}' ] && [ -r ${q_dest} ]; then echo STAGE_OK; else echo STAGE_FAIL_size=\$s; fi" 2>&1)
  ok=$(printf '%s\n' "$out" | grep -c 'STAGE_OK' || true)
  if [[ "${ok:-0}" -ne "$nhosts" ]]; then
    echo "${tag}: ERROR: ${base} staged on only ${ok}/${nhosts} node(s) at ${dest}" >&2
    printf '%s\n' "$out" | grep -v 'STAGE_OK' | head -10 | sed 's/^/  /' >&2
    if nvis_node_images_p2p; then
      echo "${tag}: distribute ${base} to these nodes with a P2P campaign (Rack Status -> P2P), then relaunch" >&2
    fi
    return 1
  fi
  echo "${tag}: ${base} ready on all ${nhosts} node(s) at ${dest}"
  export NVIS_NEMO_CONT="$dest"
}
