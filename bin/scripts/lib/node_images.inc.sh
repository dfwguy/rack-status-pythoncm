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
# NVIS_NEMO_CONT to that path.
#
# On a P2P cluster (nvis_node_images_p2p) nothing is copied: the image must already be there, and
# a node without it fails the launch with a pointer to a P2P campaign. The proof is P2P's own
# receipt (<cache>/.nvis-p2p/receipts/<basename>.sha256, written only after a verified download),
# not a size compared against the launch host's copy -- on NC the launch host is the BCM head,
# which mounts only /home and cannot see the shared store at all. Every node's receipt must name
# this path and all of them must carry the same SHA256; if the source is readable here its size
# is checked too.
#
# Elsewhere a node whose copy already has the source's byte size is left alone (rsync -a would
# re-read 40+ GiB per node because the mtimes differ), and only a missing or short copy is rsync'd
# from the head. The copy runs as the launching user first and falls back to sudo where the cache
# is root-owned (as /raid was): RACK_SUDO_PASSWORD -> sudo -S, otherwise sudo -n.
#
# Success is counted, not assumed: every node must answer STAGE_OK. A node pdsh could not reach
# never runs the remote command, so "no node reported a failure" would pass a run where ssh failed
# everywhere (run-nemo-xrack.sh learned this the hard way). Returns non-zero before anything is
# submitted when any node falls short.
nvis_stage_node_image() {
  local tag="$1" hostcsv="$2" src="$3"
  local dir base dest size nhosts out ok q_dir q_dest q_src
  dir="$(nvis_node_container_dir)"
  base="$(basename "$src")"
  dest="${dir}/${base}"
  size=$(stat -c%s "$src" 2>/dev/null || echo 0)
  [[ "$size" =~ ^[1-9][0-9]*$ ]] || size=0
  nhosts=$(tr ',' '\n' <<<"$hostcsv" | grep -c . || true)
  if [[ "${nhosts:-0}" -eq 0 ]]; then
    echo "${tag}: ERROR: no nodes to stage ${base} onto" >&2
    return 1
  fi
  # Quoted for the remote shell: dir/dest come from a validated cluster setting and src from the
  # launch environment, and none of them may split into extra words over there.
  q_dir=$(printf %q "$dir"); q_dest=$(printf %q "$dest"); q_src=$(printf %q "$src")

  if nvis_node_images_p2p; then
    local q_receipt
    q_receipt=$(printf %q "${dir}/.nvis-p2p/receipts/${base}.sha256")
    echo "${tag}: verifying P2P receipts for ${base} in ${dir} on ${nhosts} node(s); NVIS-P2P owns this cache, so no head-node copy"
    out=$(pdsh -R ssh -w "$hostcsv" \
      "if [ ! -r ${q_dest} ]; then echo STAGE_FAIL_missing; \
elif [ ! -r ${q_receipt} ]; then echo STAGE_FAIL_no_p2p_receipt; \
else read -r sha path < ${q_receipt}; s=\$(stat -c%s ${q_dest} 2>/dev/null || echo 0); \
if [ \"\$path\" != ${q_dest} ]; then echo STAGE_FAIL_receipt_names=\$path; \
elif [ '${size}' != 0 ] && [ \"\$s\" != '${size}' ]; then echo STAGE_FAIL_size=\$s; \
else echo STAGE_OK sha=\$sha; fi; fi" 2>&1)
  else
    if [[ "$size" == 0 ]]; then
      echo "${tag}: ERROR: image is missing or empty on the launch host: ${src}" >&2
      return 1
    fi
    local sudo_pfx
    if [[ -n "${RACK_SUDO_PASSWORD:-}" ]]; then
      sudo_pfx="printf '%s\n' $(printf %q "$RACK_SUDO_PASSWORD") | sudo -S -p '' "
    else
      sudo_pfx="sudo -n "
    fi
    echo "${tag}: staging ${base} (${size} bytes) into ${dir} on ${nhosts} node(s)"
    out=$(pdsh -R ssh -w "$hostcsv" \
      "s=\$(stat -c%s ${q_dest} 2>/dev/null || echo 0); \
if [ \"\$s\" != '${size}' ]; then { mkdir -p ${q_dir} && rsync -ah ${q_src} ${q_dest}; } 2>/dev/null \
|| { ${sudo_pfx}mkdir -p ${q_dir} && ${sudo_pfx}rsync -ah ${q_src} ${q_dest}; }; \
s=\$(stat -c%s ${q_dest} 2>/dev/null || echo 0); fi; \
if [ \"\$s\" = '${size}' ] && [ -r ${q_dest} ]; then echo STAGE_OK; else echo STAGE_FAIL_size=\$s; fi" 2>&1)
  fi

  ok=$(printf '%s\n' "$out" | grep -c 'STAGE_OK' || true)
  if [[ "${ok:-0}" -ne "$nhosts" ]]; then
    echo "${tag}: ERROR: ${base} ready on only ${ok}/${nhosts} node(s) at ${dest}" >&2
    printf '%s\n' "$out" | grep -v 'STAGE_OK' | head -10 | sed 's/^/  /' >&2
    if nvis_node_images_p2p; then
      echo "${tag}: distribute ${base} to these nodes with a P2P campaign (Rack Status -> P2P), then relaunch" >&2
    fi
    return 1
  fi
  if nvis_node_images_p2p; then
    # One image, one digest: nodes that each hold a *verified* but different build of the same
    # filename (a re-staged catalog entry mid-rollout) must not run one job together.
    local shas
    shas=$(printf '%s\n' "$out" | grep -o 'STAGE_OK sha=[0-9a-f]*' | sort -u | grep -c . || true)
    if [[ "$shas" -ne 1 ]]; then
      echo "${tag}: ERROR: P2P receipts for ${base} disagree across nodes (${shas} distinct SHA256s)" >&2
      printf '%s\n' "$out" | grep 'STAGE_OK' | sort -k3 | head -10 | sed 's/^/  /' >&2
      return 1
    fi
  fi
  echo "${tag}: ${base} ready on all ${nhosts} node(s) at ${dest}"
  export NVIS_NEMO_CONT="$dest"
}
