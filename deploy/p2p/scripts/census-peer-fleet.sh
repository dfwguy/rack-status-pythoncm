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
# census-peer-fleet.sh ROSTER -- NVIS-P2P peer health for every node in ROSTER, in parallel. Read-only.
# Author: Rafael M. Koike
#
# One TSV line per node on stdout, sorted (see census-peer-node.sh for the states), and a
# CENSUS_SUMMARY line with the counts on stderr. Answers "which nodes need a root-level fix, which
# need a peer re-deploy, which are unreachable" before a campaign rather than from its deferrals.
#
# Env:
#   NVIS_SERVICE_GID              (required) the service account's primary gid
#   NVIS_P2P_ROOT                 install root holding artifacts.lock (default /opt/nvis-p2p)
#   NVIS_P2P_PEER_CLIENT          dist/ build the peers run (default client-arm64-64k-v1.4.9); its
#                                 dfdaemon pin is read from artifacts.lock
#   NVIS_P2P_EXPECTED_DAEMON_SHA  overrides the pin read from artifacts.lock
#   NVIS_P2P_CENSUS_PARALLELISM   concurrent probes (default 48)
#   ...and the optional node settings census-peer-node.sh documents.
set -euo pipefail

roster=${1:?roster file is required}
[[ -r $roster ]] || { echo "roster not readable: $roster" >&2; exit 66; }
: "${NVIS_SERVICE_GID:?NVIS_SERVICE_GID is required}"
root=${NVIS_P2P_ROOT:-/opt/nvis-p2p}
client=${NVIS_P2P_PEER_CLIENT:-client-arm64-64k-v1.4.9}
lock=${NVIS_P2P_ARTIFACTS_LOCK:-$root/artifacts.lock}

if [[ -z ${NVIS_P2P_EXPECTED_DAEMON_SHA:-} ]]; then
  [[ -r $lock ]] || { echo "no pinned dfdaemon: $lock is not readable (set NVIS_P2P_EXPECTED_DAEMON_SHA)" >&2; exit 66; }
  NVIS_P2P_EXPECTED_DAEMON_SHA=$(awk -v want="$client/dfdaemon" '$2 == want { print $1; exit }' "$lock")
  [[ $NVIS_P2P_EXPECTED_DAEMON_SHA =~ ^[0-9a-f]{64}$ ]] \
    || { echo "no pin for $client/dfdaemon in $lock (set NVIS_P2P_PEER_CLIENT)" >&2; exit 65; }
fi
export NVIS_SERVICE_GID NVIS_P2P_EXPECTED_DAEMON_SHA

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=$(grep -v '^[[:space:]]*\(#\|$\)' "$roster" \
  | xargs -r -P "${NVIS_P2P_CENSUS_PARALLELISM:-48}" -n 1 "$here/census-peer-node.sh" | sort)
[[ -z $out ]] || printf '%s\n' "$out"

printf '%s\n' "$out" | awk -F'\t' 'NF { n[$2]++; total++ }
  END { printf "CENSUS_SUMMARY healthy=%d needs-peer=%d needs-prep=%d unreachable=%d total=%d\n",
        n["healthy"], n["needs-peer"], n["needs-prep"], n["unreachable"], total }' >&2
