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
#### ROW J
# done:
# p17-r01-ct[01-18] - completed
# p17-r02-ct[01-18] - completed
# p17-r03-ct[01-18] - completed
# p17-r04-ct[01-18] - completed
# p18-r05-ct[01-18] - completed
# p18-r06-ct[01-18] - completed
# p18-r08-ct[01-18] - completed
# p19-r16-ct[01-18] - completed
# p19-r17-ct[01-18] - completed
# p19-r18-ct[01-18] - completed
# p20-r19-ct[01-18] - completed
# p20-r20-ct[01-18] - completed
# p20-r21-ct[01-18] - completed
# p20-r22-ct[01-18] - completed
##
# next:
# p19-r15-ct[01-18] - p19-r15-ct06 - installer stuck - reboot again aux cycle next - ongoing - @walid on it
# p18-r07-ct[01-18] - after the aux cycle the gpu on ct07 is back - dcgmi diag -r 4 PASS - retrying after nvlink cycle - failed after nccl - ct07 reseated - trying again - crashed ask replacement for ct07
                      # i still see problem here on ct08 (previously 07) with link 12 with high ber - after cc replacement we still see this problem
                      # waiting for ct07 raplacement - st07 replaced and ct replaced. Walid started validation

# test on going:
# p19-r15-ct[01-18]


#### ROW K
# done:
# p21-r02-ct[01-18] # maybe low nccl but nemo is ok? - completed
# p21-r03-ct[01-18] - completed
# p22-r08-ct[01-18] - completed
# p23-r15-ct[01-18] - completed
# p23-r16-ct[01-18] - completed
# p23-r18-ct[01-18] # nccl to be checked - completed
# p24-r20-ct[01-18] - completed
# p24-r21-ct[01-18] - completed
##
# next:
# p21-r01-ct[01-18] # nccl got stuck - nvlink cycle - ct01 cable issue?
# p21-r04-ct[01-18] # p21-r04-ct04 possible misscabling - check now @gio - testing
# p22-r05-ct[01-18] # ct10 with ECC errors - need replacement 

# p24-r19-ct[01-18] # nvsw04 is down/not reachable
# p24-r22-ct[01-18] # p24-r22-ct13 one bf3 not connected

# test on going 
# p21-r04-ct[01-18] # p21-r04-ct04 possible misscabling - check now @gio - testing - nccl stack - nvlink cycle
# p23-r17-ct[01-18] # p23-r17-ct15 - need fw upgrade - testing
# p22-r06-ct[01-18] # p22-r06-ct03 - possible misscabling pxe not starting - testing

#### ROW L

# next:
# p25-r01-ct[01-18] - nvsw01 is missing
# p25-r02-ct[01-18] - nvlink cycle



a=(
  p32-r22-ct[01-18]
)

[[ $# -gt 0 ]] && { a=( $1 ); } 

export single_rack=1

# Same directory as parser.sh / rack_status.sh. Manual runs use the same shared default.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/node_images.inc.sh
source "${_SCRIPT_DIR}/lib/node_images.inc.sh"
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"
mkdir -p "${JOB_LOG_DIR}" || { echo "run_singlerack: cannot create log directory: ${JOB_LOG_DIR}" >&2; exit 2; }
cd "${JOB_LOG_DIR}" || exit 2

## Per rack test
for r in ${a[*]}
do
 # Rack id for the log basename (…-NEMO-1Rack-<rack>-…): prefer the id the app passes as $2
 # (works for any naming convention, e.g. Boston bos01-b05); fall back to the Bright p<N>-r<N> regex.
 rack="${2:-}"
 [[ -z "$rack" ]] && rack=$(echo "$r" | sed -E 's/(p[0-9]+-r[0-9]+)-ct.*/\1/')
 hosts=($(scontrol show hostname $r))
 : "${NEMO_SQSH_SRC:?no NeMo image: add this tool (validation-singlerack) to the Required by of its image in Artifact Distribution -> Catalog, or set NEMO_SQSH_SRC}"
 _wl=$(echo "${hosts[*]}" | tr ' ' ',')
 # Into the cluster's node image cache (NVIS_NODE_CONTAINER_DIR; lib/node_images.inc.sh), never a
 # baked-in /raid/data: NC's nodes have no /raid. A warm cache is reused as-is; the helper exports
 # NVIS_NEMO_CONT and refuses to submit unless every node positively confirms the image.
 nvis_stage_node_image run_singlerack "$_wl" "$NEMO_SQSH_SRC" || exit 1

 n=${#hosts[@]}
 (( n > 0 )) || { echo "run_singlerack: target resolved to no hosts: $r" >&2; exit 2; }
 # The NeMo training recipe requires paired nodes, but a single node is a valid NCCL
 # preflight target. Preserve the historical even-node truncation for larger selections.
 (( n > 1 )) && n=$((n / 2 * 2))
 gpus_per_node="${GPUS_PER_NODE:-4}"
 cpus_per_task="${NVIS_NEMO_CPUS_PER_TASK:-36}"

# NVIS_NEMO_CONT MUST be exported: the batch script's own default names a fixed image, but staging
# above placed <node cache>/<basename of NEMO_SQSH_SRC>. Without it pyxis looks for the default
# name, fails with "No such file or directory", and the job dies in ~12s at task_init
# (spank_pyxis.so). NVIS_NODE_CONTAINER_DIR rides along so the batch script's fallback agrees.
# NVIS_SCRIPTS_DIR likewise: without it the batch script cannot find lib/fabric.inc.sh and falls
# back to a hardcoded mlx5_0..3 list, which "may mix IB/RoCE".
# NOTE: these comments must stay ABOVE the sbatch line -- a comment between backslash-continued
# argument lines terminates the command ("sbatch: error: Batch script is empty!").
sbatch \
 -N ${n} \
 -w $(echo "${hosts[*]}"|tr ' ' ',') \
 --ntasks-per-node="${gpus_per_node}" \
 --cpus-per-task="${cpus_per_task}" \
 --gpus-per-node="${gpus_per_node}" \
 -t 03:00:00 \
 --job-name=NEMO-1R \
 --output="${JOB_LOG_DIR%/}/${USER}-NEMO-1Rack-${rack}-${n}N-%j.txt" \
 --export=ALL,single_rack,NVIS_SCRIPTS_DIR=${_SCRIPT_DIR},NVIS_VAR_DIR=${NVIS_VAR_DIR:-},NVIS_NEMO_CONT=${NVIS_NEMO_CONT},NVIS_NODE_CONTAINER_DIR=$(nvis_node_container_dir),NVIS_NCCL_DEBUG=${NVIS_NCCL_DEBUG:-} \
 "$_SCRIPT_DIR/nemo-nemotron5h_56b-mbridge-fp8.sh"

done
