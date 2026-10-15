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

# shellcheck source=lib/node_config.inc.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/node_config.inc.sh"

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

# nvis_catalog_image <basename> — print "<bytes> <sha256>" for an image in the published catalog
# (config/image-catalog.tsv); non-zero when the catalog is absent or does not list the image.
nvis_catalog_image() {
  local tsv
  tsv="$(nvis_node_config_file image-catalog.tsv)" || return 2
  awk -F'\t' -v f="$1" 'NR > 1 && $3 == f && $4 ~ /^[0-9]+$/ && $5 ~ /^[0-9a-f]{64}$/ {print $4, $5; found=1; exit}
                        END {exit !found}' "$tsv"
}

# nvis_launch_image <consumer> — the image a launch stages, as the cluster's image catalog states it.
# Sets NVIS_IMAGE_FILE (bare *.sqsh name), NVIS_IMAGE_BYTES and NVIS_IMAGE_SHA256; non-zero, with the
# fix on stderr, when there is none.
#
# Rack Status resolves the catalog entry whose Required by names <consumer> (a preset or tool id) and
# hands it over as NVIS_LAUNCH_IMAGE / _BYTES / _SHA256, bound to the reviewed plan. A manual run may
# set NVIS_LAUNCH_IMAGE alone; its size and digest then come from the published catalog. There is no
# built-in image: the filename and digest used to be literals in each launcher, and went stale the day
# a site re-imported the image -- every gate then failed a healthy file.
nvis_launch_image() {
  local consumer="$1" entry
  NVIS_IMAGE_FILE="${NVIS_LAUNCH_IMAGE:-}"
  NVIS_IMAGE_BYTES="${NVIS_LAUNCH_IMAGE_BYTES:-}"
  NVIS_IMAGE_SHA256="${NVIS_LAUNCH_IMAGE_SHA256:-}"
  if [[ -z "$NVIS_IMAGE_FILE" ]]; then
    echo "ERROR: no image for ${consumer}: add ${consumer} to the Required by of its image in Rack Status (Artifact Distribution -> Catalog), or set NVIS_LAUNCH_IMAGE" >&2
    return 1
  fi
  if [[ ! "$NVIS_IMAGE_FILE" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*\.sqsh$ ]]; then
    echo "ERROR: NVIS_LAUNCH_IMAGE must be a bare *.sqsh name, not: ${NVIS_IMAGE_FILE}" >&2
    return 1
  fi
  if [[ -z "$NVIS_IMAGE_BYTES" || -z "$NVIS_IMAGE_SHA256" ]]; then
    if ! entry="$(nvis_catalog_image "$NVIS_IMAGE_FILE")"; then
      echo "ERROR: ${NVIS_IMAGE_FILE} is not pinned in the image catalog ($(nvis_node_config_file image-catalog.tsv || true)); set its bytes and sha256 in Rack Status (Artifact Distribution -> Catalog)" >&2
      return 1
    fi
    read -r NVIS_IMAGE_BYTES NVIS_IMAGE_SHA256 <<<"$entry"
  fi
  if [[ ! "$NVIS_IMAGE_BYTES" =~ ^[1-9][0-9]*$ || ! "$NVIS_IMAGE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "ERROR: ${NVIS_IMAGE_FILE}: image size must be a positive byte count and sha256 64 hex digits (got '${NVIS_IMAGE_BYTES}' / '${NVIS_IMAGE_SHA256}')" >&2
    return 1
  fi
}

# nvis_stage_node_image <tag> <host,csv> <source .sqsh>
#
# Make <source> present on every listed node at $(nvis_node_container_dir)/<basename>, then export
# NVIS_NEMO_CONT to that path.
#
# On a P2P cluster (nvis_node_images_p2p) nothing is copied -- P2P distributes the images -- and the
# node's copy is checked against the image catalog Rack Status publishes beside these scripts
# (config/image-catalog.tsv, lib/node_config.inc.sh): the catalog names the image's byte size and
# SHA256, and every node must hold a file of that size whose SHA256 matches. Only the basename of
# <source> is used, so the launch host need not see the shared store (on NC it is the BCM head,
# which mounts only /home). NVIS_NODE_IMAGE_VERIFY=size skips the hash for a quick re-launch; the
# default hashes the file on every node, in parallel. This is script execution's own check, kept
# apart from the P2P control plane's manifests and receipts, which tell P2P whether *it* is in sync.
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
  nhosts=$(tr ',' '\n' <<<"$hostcsv" | grep -c . || true)
  if [[ "${nhosts:-0}" -eq 0 ]]; then
    echo "${tag}: ERROR: no nodes to stage ${base} onto" >&2
    return 1
  fi
  # Quoted for the remote shell: dir/dest come from a validated cluster setting and src from the
  # launch environment, and none of them may split into extra words over there.
  q_dir=$(printf %q "$dir"); q_dest=$(printf %q "$dest"); q_src=$(printf %q "$src")

  if nvis_node_images_p2p; then
    local entry sha mode hash_cmd
    if ! entry="$(nvis_catalog_image "$base")"; then
      if [[ ! -r "$(nvis_node_config_file image-catalog.tsv)" ]]; then
        echo "${tag}: ERROR: no image catalog at $(nvis_node_config_file image-catalog.tsv); publish it from Rack Status (P2P -> Catalog -> Publish to scripts)" >&2
      else
        echo "${tag}: ERROR: ${base} is not pinned in the image catalog ($(nvis_node_config_file image-catalog.tsv)); add it under P2P -> Catalog and distribute it" >&2
      fi
      return 1
    fi
    read -r size sha <<<"$entry"
    mode="${NVIS_NODE_IMAGE_VERIFY:-sha256}"
    if [[ "$mode" == size ]]; then
      hash_cmd="echo STAGE_OK"
    else
      hash_cmd="h=\$(sha256sum ${q_dest} 2>/dev/null | cut -d' ' -f1); \
if [ \"\$h\" = '${sha}' ]; then echo STAGE_OK; else echo STAGE_FAIL_sha256=\${h:-unreadable}; fi"
    fi
    echo "${tag}: checking ${base} in ${dir} on ${nhosts} node(s) against the image catalog (${size} bytes, sha256 ${sha:0:12}…, verify=${mode}); P2P distributes images here, so nothing is copied"
    out=$(pdsh -R ssh -w "$hostcsv" \
      "s=\$(stat -c%s ${q_dest} 2>/dev/null || echo 0); \
if [ ! -r ${q_dest} ]; then echo STAGE_FAIL_missing; \
elif [ \"\$s\" != '${size}' ]; then echo STAGE_FAIL_size=\$s; \
else ${hash_cmd}; fi" 2>&1)
  else
    size=$(stat -c%s "$src" 2>/dev/null || echo 0)
    if [[ ! "$size" =~ ^[1-9][0-9]*$ ]]; then
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
  echo "${tag}: ${base} ready on all ${nhosts} node(s) at ${dest}"
  export NVIS_NEMO_CONT="$dest"
}
