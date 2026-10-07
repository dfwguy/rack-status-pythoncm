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
# deploy-catalog-refresh-timer-node.sh NODE -- install (or update) the 15-minute catalog refresh on
# one node, without touching its peer daemon, and prove it works by refreshing once immediately.
# Author: Rafael M. Koike
#
# The origin URL comes from NVIS_P2P_CATALOG_URL, or -- when unset -- from the env file a previous
# install left on the node's data partition (it survives a reimage; /etc/systemd does not), so the
# peer deploy can re-install the timer on a reimaged node without being told the URL again.
#
# Prints REFRESH_TIMER_READY node=... status=<last refresh outcome>, or exits non-zero.
set -euo pipefail

node=${1:?node is required}
root=${NVIS_P2P_ROOT:-/opt/nvis-p2p}
# The node's P2P directory. Overridable like catalog-refresh.sh's, but the units hard-code the
# default, so anything else is only for exercising this installer against a stand-in node.
base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
url=${NVIS_P2P_CATALOG_URL:-}
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

# The URL lands in an EnvironmentFile and a curl argument: accept only a plain http(s) images.tsv URL.
if [[ -n $url && ! $url =~ ^https?://[A-Za-z0-9.:-]+(/[A-Za-z0-9._/-]*)?/images\.tsv$ ]]; then
  echo "REFRESH_TIMER_REJECTED node=$node reason=bad-url url=$url" >&2
  exit 64
fi

scp -q "${ssh_opts[@]}" "$root/scripts/catalog-refresh.sh" "$node:$base/bin/catalog-refresh.sh.next"
scp -q "${ssh_opts[@]}" "$root/deploy/systemd/nvis-p2p-catalog-refresh.service" "$node:$base/nvis-p2p-catalog-refresh.service.next"
scp -q "${ssh_opts[@]}" "$root/deploy/systemd/nvis-p2p-catalog-refresh.timer" "$node:$base/nvis-p2p-catalog-refresh.timer.next"

status=$(ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" "$url" <<'REMOTE'
set -euo pipefail
# ssh joins its arguments into one command line, so an empty URL arrives as no argument at all.
base=$1 url=${2:-}
env=$base/catalog-refresh.env
if [[ -n $url ]]; then
  printf 'NVIS_P2P_CATALOG_URL=%s\n' "$url" > "$env.next"
  mv -f "$env.next" "$env"
elif [[ ! -s $env ]]; then
  echo "no NVIS_P2P_CATALOG_URL given and no $env on the node" >&2
  exit 64
fi
chmod 0644 "$env"
mv -f "$base/bin/catalog-refresh.sh.next" "$base/bin/catalog-refresh.sh"
chmod 0755 "$base/bin/catalog-refresh.sh"
sudo -n install -m 0644 "$base/nvis-p2p-catalog-refresh.service.next" /etc/systemd/system/nvis-p2p-catalog-refresh.service
sudo -n install -m 0644 "$base/nvis-p2p-catalog-refresh.timer.next" /etc/systemd/system/nvis-p2p-catalog-refresh.timer
rm -f "$base/nvis-p2p-catalog-refresh.service.next" "$base/nvis-p2p-catalog-refresh.timer.next"
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now nvis-p2p-catalog-refresh.timer >/dev/null 2>&1
# A oneshot start blocks until the refresh finishes, so a failure here fails the install.
sudo -n systemctl start nvis-p2p-catalog-refresh.service
sudo -n systemctl is-active --quiet nvis-p2p-catalog-refresh.timer
cut -d' ' -f2 "$base/run/catalog-refresh.status"
REMOTE
)

echo "REFRESH_TIMER_READY node=$node status=$status"
