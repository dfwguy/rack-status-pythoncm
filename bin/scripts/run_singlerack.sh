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
# Slurm CLI on PATH (non-login SSH shells don't init Modules — see slurm-init.inc.sh).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/slurm-init.inc.sh"
# Force ssh transport: this cluster's pdsh defaults to rsh (refused on compute nodes).
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh
: "${1:?usage: run_singlerack.sh NODELIST RACK_ID}"
: "${2:?usage: run_singlerack.sh NODELIST RACK_ID}"
a=( "$1" )

export single_rack=1

# Same directory as parser.sh / rack_status.sh. Manual runs use the same shared default.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"
mkdir -p "${JOB_LOG_DIR}" || { echo "run_singlerack: cannot create log directory: ${JOB_LOG_DIR}" >&2; exit 2; }
cd "${JOB_LOG_DIR}" || exit 2

## Per rack test
for r in ${a[*]}
do
 # Rack id for the log basename (…-NEMO-1Rack-<rack>-…): prefer the id the app passes as $2
 # Nscale rack id is resolved from PythonCM inventory and passed explicitly by the app.
 rack="$2"
 hosts=($(scontrol show hostname $r))
 : "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the head-node path of the nemo .sqsh image to copy under /raid/data/ on compute nodes}"
 _sqsh_b=$(basename "$NEMO_SQSH_SRC")
 _wl=$(echo "${hosts[*]}" | tr ' ' ',')
 # /raid is root-owned on the compute trays, so the staging runs as root ON EACH COMPUTE NODE.
 # If RACK_SUDO_PASSWORD is set, elevate with sudo -S (cmods password, kept OUT of the repo — set via
 # env or the server secret store). Otherwise use sudo -n (works when passwordless sudo is baked into
 # the compute IMAGE by AIC, and also when the script already runs as root). NOTE: a passwordless
 # visudo edit only on the slogin does NOT help here — this sudo runs on the compute trays.
 if [[ -n "${RACK_SUDO_PASSWORD:-}" ]]; then
   _sudo="printf '%s\n' $(printf %q "$RACK_SUDO_PASSWORD") | sudo -S -p '' "
 else
   _sudo="sudo -n "
 fi
 pdsh -R ssh -w "$_wl" "${_sudo}mkdir -p /raid/data && ${_sudo}rsync -ah $NEMO_SQSH_SRC /raid/data/$_sqsh_b"

 n=$[${#hosts[*]}/2*2]

# NVIS_NEMO_CONT MUST be exported: the batch script defaults to
# CONT="${NVIS_NEMO_CONT:-/raid/data/nemo-25.11.01.sqsh}", but staging above copies
# /raid/data/$_sqsh_b -- the basename of NEMO_SQSH_SRC. Without this, pyxis looks for the
# hardcoded 25.11.01 name, fails with "No such file or directory: /raid/data/nemo-25.11.01.sqsh",
# and the job dies in ~12s at task_init (spank_pyxis.so). run-nemo-xrack.sh already exports it;
# this single-rack wrapper did not, so single-rack NEMO only ever worked if the image happened to
# be named exactly nemo-25.11.01.sqsh.
# NVIS_SCRIPTS_DIR likewise: without it the batch script cannot find lib/fabric.inc.sh and falls
# back to a hardcoded mlx5_0..3 list, which "may mix IB/RoCE".
# NOTE: these comments must stay ABOVE the sbatch line -- a comment between backslash-continued
# argument lines terminates the command ("sbatch: error: Batch script is empty!").
sbatch \
 -N ${n} \
 -w $(echo "${hosts[*]}"|tr ' ' ',') \
 -t 03:00:00 \
 --job-name=NEMO-1R \
 --output="${JOB_LOG_DIR%/}/${USER}-NEMO-1Rack-${rack}-${n}N-%j.txt" \
 --export=ALL,single_rack,NVIS_SCRIPTS_DIR=${_SCRIPT_DIR},NVIS_NEMO_CONT=/raid/data/${_sqsh_b},NVIS_NCCL_DEBUG=${NVIS_NCCL_DEBUG:-} \
 "$_SCRIPT_DIR/nemo-nemotron5h_56b-mbridge-fp8.sh"

done
