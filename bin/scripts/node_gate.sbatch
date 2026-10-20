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
# node_gate.sbatch -- the node gate's Slurm job: holds every target node exclusively (submitted with
# --exclusive by run-validation-preset.sh) and runs node_gate.sh once per node. Writes one verdict line per
# node to NVIS_NODE_GATE_RESULTS; a node that does not answer gets a fail line of its own. Exit 0 when every
# node passes (warnings included), 3 when any fails.
# Environment: NVIS_NODE_GATE_RESULTS (results file), NVIS_NODE_GATE_STATE (shared Xid baselines),
# NVIS_BIN_SCRIPTS (where node_gate.sh lives).
#SBATCH --job-name=node-gate
set -u
results=${NVIS_NODE_GATE_RESULTS:?}
state=${NVIS_NODE_GATE_STATE:?}
bin=${NVIS_BIN_SCRIPTS:?}
mkdir -p "$state"
srun --nodes="$SLURM_JOB_NUM_NODES" --ntasks-per-node=1 --kill-on-bad-exit=0 \
  bash "$bin/node_gate.sh" "$state" 2>/dev/null | grep '^NVIS_NODE_GATE_JSON=' > "$results"
for h in $(scontrol show hostnames "$SLURM_JOB_NODELIST"); do
  grep -q "\"node\":\"${h%%.*}\"" "$results" || \
    printf 'NVIS_NODE_GATE_JSON={"node":"%s","status":"fail","codes":["no-answer"],"reason":"the node check did not report","warnings":[],"actions":[]}\n' \
      "${h%%.*}" >> "$results"
done
grep -q '"status":"fail"' "$results" && exit 3
exit 0
