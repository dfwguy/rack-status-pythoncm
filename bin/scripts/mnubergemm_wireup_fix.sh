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
# mnubergemm_wireup_fix.sh — pin OpenMPI + NCCL + DCGM multi-node wireup to a routable
# interface (default bond0) across a nodelist, so `dcgmi mndiag -r mnubergemm` stops dialing
# a stray non-routable /31 NIC (or docker0) and hanging. Applies the pin in THREE places that
# matter, because each covers a different launch path:
#   (1) /etc/openmpi/openmpi-mca-params.conf  — orted OOB + TCP BTL wireup (read locally by
#       every OMPI process; mpirun also auto-forwards OMPI_MCA_* to remote orteds).
#   (2) 99- nvidia-dcgm systemd drop-in        — env nv-hostengine hands to the LOCAL ranks
#       (99- sorts AFTER the auto-regenerated /run/.../50-mndiag.conf, so it wins).
#   (3) /etc/environment  — NCCL_SOCKET_IFNAME for the REMOTE ranks: mpirun forwards
#       OMPI_MCA_* but NOT NCCL_*, so ssh-spawned orted ranks miss it and NCCL bootstrap
#       hangs on a stray NIC. PAM applies /etc/environment to non-interactive ssh sessions,
#       so this reaches the remote ranks. Then restarts nvidia-dcgm.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Run ON the BCM head / slogin as root (needs pdsh + pdcp + passwordless ssh to the nodes).
# Usage: mnubergemm_wireup_fix.sh <comma-separated-nodelist> [iface]
#   e.g. mnubergemm_wireup_fix.sh bos01-b05-11-s1-dgx-05-c01,bos01-b05-12-s1-dgx-05-c02 bond0
set -uo pipefail

NODES="${1:?usage: mnubergemm_wireup_fix.sh <comma-nodelist> [iface]}"
IFACE="${2:-bond0}"
TAG="NVIS-${IFACE}-pin"
DROP="/tmp/99-nvis-${IFACE}.conf"

# Rack-scale launch fixes for the DCGM-spawned mpirun:
#  - plm_rsh_no_tree_spawn=1 : launch every orted DIRECTLY from the HNP instead of cascading
#    node->node. The tree-spawn's peer/leaf ssh hops hit host-key prompts and fail for the last
#    nodes ("ORTE does not know how to route a message to daemon on <cNN>"); HNP->all works.
#  - plm_rsh_agent : force a non-interactive ssh (no host-key prompt) for orted launch, exactly
#    like the known-good NCCL launcher. UserKnownHostsFile=/dev/null avoids first-contact fails.
#  - routed=direct : each orted dials the HNP directly (robust routing at rack scale).
cat > "${DROP}" <<EOF
[Service]
Environment="NCCL_SOCKET_IFNAME=${IFACE}"
Environment="OMPI_MCA_oob_tcp_if_include=${IFACE}"
Environment="OMPI_MCA_btl_tcp_if_include=${IFACE}"
Environment="OMPI_MCA_routed=direct"
Environment="OMPI_MCA_plm_rsh_no_tree_spawn=1"
Environment="OMPI_MCA_plm_rsh_agent=ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
EOF

echo "== 1) openmpi-mca-params.conf pin + drop-in dir + /etc/environment NCCL pin (idempotent) =="
pdsh -R ssh -w "${NODES}" "mkdir -p /etc/systemd/system/nvidia-dcgm.service.d; \
  grep -q '${TAG}' /etc/openmpi/openmpi-mca-params.conf 2>/dev/null || \
  printf '\n# ${TAG}: pin MPI wireup to ${IFACE}\noob_tcp_if_include = ${IFACE}\nbtl_tcp_if_include = ${IFACE}\nrouted = direct\n' >> /etc/openmpi/openmpi-mca-params.conf; \
  grep -q '^NCCL_SOCKET_IFNAME=' /etc/environment 2>/dev/null || echo 'NCCL_SOCKET_IFNAME=${IFACE}' >> /etc/environment"

echo "== 2) copy nvidia-dcgm 99- drop-in =="
pdcp -R ssh -w "${NODES}" "${DROP}" "/etc/systemd/system/nvidia-dcgm.service.d/99-nvis-${IFACE}.conf"

echo "== 3) daemon-reload + restart nvidia-dcgm =="
pdsh -R ssh -w "${NODES}" "systemctl daemon-reload && systemctl restart nvidia-dcgm && systemctl is-active nvidia-dcgm" 2>&1 | sort

echo "== 4) verify env applied (dcgm service + /etc/environment) =="
pdsh -R ssh -w "${NODES}" "systemctl show nvidia-dcgm -p Environment | tr ' ' '\n' | grep -E 'IFNAME|if_include|routed' | tr '\n' ' '; grep '^NCCL_SOCKET_IFNAME=' /etc/environment; echo" 2>&1 | sort
echo "DONE (${IFACE} pinned on: ${NODES})"
