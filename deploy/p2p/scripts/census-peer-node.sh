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
# census-peer-node.sh NODE -- one node's NVIS-P2P peer health, probed the way the peer deploy sees it
# (over ssh as the service account). Read-only.
# Author: Rafael M. Koike
#
# Prints:  NODE <TAB> STATE <TAB> key=value detail
#   healthy      everything below holds
#   needs-prep   the node itself is not ready for a peer: the service account's primary group, its
#                passwordless sudo, a writable container directory, or (when configured) the
#                node-healthcheck allowlist. A root-level fix (prepare-peer-node.sh) comes first.
#   needs-peer   the node is ready, but the peer is not: unit not active, no socket, a daemon that
#                is not the pinned build, or the catalog refresh timer not active. Re-deploy it.
#   unreachable  ssh as the service account failed.
#
# Env:
#   NVIS_SERVICE_GID                  (required) primary gid the service account must have
#   NVIS_P2P_EXPECTED_DAEMON_SHA      (required) sha256 of the pinned dfdaemon -- census-peer-fleet.sh
#                                     reads it from artifacts.lock
#   NVIS_P2P_BASE                     peer directory on the node (default /opt/containers/.nvis-p2p)
#   NVIS_P2P_CONTAINER_DIR            image directory on the node (default /opt/containers)
#   NVIS_P2P_HEALTHCHECK_ALLOWLIST    optional file on the node that must name the service account,
#                                     e.g. BCM's rogueprocess config, which otherwise drains a node
#                                     running the peer. Unset: not checked (allow=n/a).
set -uo pipefail

node=${1:?node is required}
gid=${NVIS_SERVICE_GID:?NVIS_SERVICE_GID is required}
daemon_sha=${NVIS_P2P_EXPECTED_DAEMON_SHA:?NVIS_P2P_EXPECTED_DAEMON_SHA is required}
base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
containers=${NVIS_P2P_CONTAINER_DIR:-/opt/containers}
allowlist=${NVIS_P2P_HEALTHCHECK_ALLOWLIST:-}
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=4 -o ServerAliveCountMax=1)

# One round trip; every value is a single token so the reply splits cleanly. An empty allowlist path
# is sent as "-" because ssh drops empty arguments.
r=$(ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" "$containers" "${allowlist:--}" 2>/dev/null <<'REMOTE'
base=$1 containers=$2 allowlist=$3
g=$(id -g)
s=no; sudo -n true 2>/dev/null && s=yes
w=no; test -w "$containers" && w=yes
if [ "$allowlist" = - ]; then a=n/a
elif sudo -n grep -qw -- "$(id -un)" "$allowlist" 2>/dev/null || grep -qw -- "$(id -un)" "$allowlist" 2>/dev/null; then a=yes
else a=no; fi
u=$(systemctl is-active nvis-p2p-peer 2>/dev/null); u=${u:-unknown}
k=no; test -S "$base/run/dfdaemon.sock" && k=yes
d=$(sha256sum "$base/bin/dfdaemon" 2>/dev/null | cut -d' ' -f1); d=${d:--}
t=$(systemctl is-active nvis-p2p-catalog-refresh.timer 2>/dev/null); t=${t:-unknown}
echo "$g $s $w $a $u $k $d $t"
REMOTE
)
[ -n "$r" ] || { printf '%s\tunreachable\t-\n' "$node"; exit 0; }
read -r g s w a u k d t <<< "$r"

if [ "$g" != "$gid" ] || [ "$s" != yes ] || [ "$w" != yes ] || [ "$a" = no ]; then
  state=needs-prep
elif [ "$u" = active ] && [ "$k" = yes ] && [ "$d" = "$daemon_sha" ] && [ "$t" = active ]; then
  state=healthy
else
  state=needs-peer
fi
[ "$d" = "$daemon_sha" ] && build=pinned || build=${d:0:12}
printf '%s\t%s\tgid=%s sudo=%s containers_writable=%s allowlist=%s unit=%s socket=%s daemon=%s refresh_timer=%s\n' \
  "$node" "$state" "$g" "$s" "$w" "$a" "$u" "$k" "$build" "$t"
