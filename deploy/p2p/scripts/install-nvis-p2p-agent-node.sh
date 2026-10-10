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
#
# install-nvis-p2p-agent-node.sh NODES_FILE [AGENT_PY] -- bootstrap the pull agent on compute nodes.
#
# This is the one job the pull design keeps SSH for: putting the agent on a node. Everything after
# that -- what to download, cancel, retry, status -- goes through the desired-state documents and
# the metrics pipeline (docs/DESIGN_p2p-pull-agent.md).
#
# Per node: copy the agent onto persistent /opt/containers, hand the agent its one metrics file in the
# root-owned node_exporter textfile directory, and (re)start it as a transient systemd unit. Transient
# on purpose for the P1 rack test: it is gone after a reboot, so nothing is left behind by accident.
# Idempotent; one line per node (AGENT_INSTALLED / AGENT_INSTALL_FAILED); a failed node never aborts
# the others (xargs would stop the whole batch on an exit 255).
#
# Env: NVIS_P2P_AGENT_PARALLELISM (default 8), NVIS_P2P_AGENT_INTERVAL (poll seconds, default 30),
#      NVIS_P2P_AGENT_UNIT (default nvis-p2p-agent).
set -uo pipefail

nodes_file=${1:?nodes file (one hostname per line) is required}
agent_py=${2:-$(dirname "$(readlink -f "$0")")/nvis-p2p-agent.py}
parallelism=${NVIS_P2P_AGENT_PARALLELISM:-8}
interval=${NVIS_P2P_AGENT_INTERVAL:-30}
unit=${NVIS_P2P_AGENT_UNIT:-nvis-p2p-agent}
[[ -r $agent_py ]] || { echo "AGENT_SOURCE_MISSING $agent_py" >&2; exit 64; }
[[ $parallelism =~ ^[1-9][0-9]?$ ]] || { echo "AGENT_BAD_PARALLELISM $parallelism" >&2; exit 64; }
[[ $interval =~ ^[1-9][0-9]*$ ]] || { echo "AGENT_BAD_INTERVAL $interval" >&2; exit 64; }
[[ $unit =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "AGENT_BAD_UNIT $unit" >&2; exit 64; }

agent_b64=$(base64 -w0 < "$agent_py")
remote=$(cat <<REMOTE
set -euo pipefail
base=/opt/containers/.nvis-p2p
textfile=/var/lib/node_exporter/textfile_collector
tmp=\$(mktemp)
echo '$agent_b64' | base64 -d > "\$tmp"
sudo -n install -d -o nvis-p2p -g cmods "\$base/agent"
sudo -n install -o nvis-p2p -g cmods -m 755 "\$tmp" "\$base/agent/nvis-p2p-agent.py"
rm -f "\$tmp"
if [[ -d \$textfile ]]; then
  [[ -f \$textfile/nvis_p2p_agent.prom ]] || sudo -n install -o nvis-p2p -g cmods -m 644 /dev/null "\$textfile/nvis_p2p_agent.prom"
fi
sudo -n systemctl stop '$unit' 2>/dev/null || true
sudo -n systemctl reset-failed '$unit' 2>/dev/null || true
sudo -n systemd-run --quiet --unit='$unit' --uid=nvis-p2p --gid=cmods -p Restart=always -p RestartSec=5 \
  --description='NVIS-P2P pull agent' /usr/bin/python3 "\$base/agent/nvis-p2p-agent.py" --interval $interval
sleep 2
systemctl is-active --quiet '$unit'
REMOTE
)

install_one() {
  local node=$1 out
  if out=$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$node" "$remote" 2>&1); then
    echo "AGENT_INSTALLED node=$node unit=$unit"
  else
    echo "AGENT_INSTALL_FAILED node=$node detail=$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
  fi
  return 0
}
export -f install_one
export remote unit

grep -vE '^\s*(#|$)' "$nodes_file" | xargs -r -P "$parallelism" -I{} bash -c 'install_one "$1"' _ {}
