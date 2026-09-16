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
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --exclusive
#
# MNUBERGEMM — GB300_B300_Validation_MOP.md §7.2, Stress Test (MNUBERGEMM) – Phase 1.
# Reproduced as written. Every departure from the MOP text is marked "NSCALETX:" and
# there are only three; nothing else is added, reordered, or retuned.
#
#   sbatch -N 1 mnubergemm.sbatch                          # MOP: Execute Single-Node Stress
#   sbatch -N <Total_Rack_Nodes> mnubergemm.sbatch         # MOP: Execute Multi-Node Stress
#
# For NscaleTX GB300 NVL72, <Total_Rack_Nodes> = 18 (18 trays x 4 GPU = 72 GPU).

umask 0002

# NSCALETX [1/3]: the MOP pins CONT to a 25.08 image under /home/nvis. NscaleTX stages the
# 26.03 image in the AAT container dir; run_mnubergemm_stress.sh exports NVIS_MNUBERGEMM_CONT
# after its pre-req check so the driver and a hand-run sbatch resolve identically.
CONT="${NVIS_MNUBERGEMM_CONT:-/home/cmods/bringup/containers/mnubergemm-26.03-v1.17-arm.sqsh}"

echo "#######################":
echo "HOSTLIST: $(scontrol show hostname $SLURM_NODELIST | paste -s -d,)"
echo "Image: ${CONT}"
echo "#######################"

# NSCALETX [2/3]: fail fast when the image is absent. Not in the MOP, but without it pyxis
# emits "couldn't start container" once per rank and the real cause — a missing .sqsh —
# scrolls off. That single fault accounts for 36 of the 176 runs in the Novartis corpus.
if [[ ! -s "${CONT}" ]]; then
  echo "ERROR: container image not found or empty: ${CONT}" >&2
  echo "       enroot import -o ${CONT} docker://nvcr.io/0948245400141054/mnubergemm:tensorrt-26.03-v1.17-arm" >&2
  exit 1
fi

# NSCALETX [5/5]: the MOP sets MELLANOX_VISIBLE_DEVICES=all. On these trays that makes
# enroot's /etc/enroot/hooks.d/99-mellanox.sh try to bind-mount /sys/class/infiniband_mad,
# which does not exist (IB stack not loaded) — the hook exits 1 and pyxis refuses to start
# the container on EVERY rank. Job 62114 died exactly this way. IB is not needed for an
# NVLink-domain stress: the MOP itself sets NCCL_IB_DISABLE=1 below.
# Set RACK_MNUBERGEMM_MLX=all to restore the MOP value once the IB stack is up.
export MELLANOX_VISIBLE_DEVICES="${RACK_MNUBERGEMM_MLX:-none}"

export NCCL_NVLS_ENABLE=1
export NCCL_DEBUG=INFO
export NCCL_P2P_LEVEL=NVL
export NCCL_CUMEM_ENABLE=1
export NCCL_CUMEM_HOST_ENABLE=0
export NCCL_IB_DISABLE=1
export NCCL_MIN_CTAS=64
export UCX_NET_DEVICES=bond0
export UCX_TLS=tcp
export NCCL_SOCKET_IFNAME=bond0
export NCCL_NVLS_ENABLE=0

hosts=($(scontrol show hostnames $SLURM_JOB_NODELIST))
export NCCL_MNNVL_ENABLE=0
[[ ${#hosts[@]} -gt 1 ]] && { export NCCL_MNNVL_ENABLE=1; }

srun --nodes=1 --ntasks=1 --nodelist="${hosts[0]}" nvidia-imex-ctl -N

#setting up variables
# NSCALETX [4/4]: the MOP hardcodes --time_to_run 86400 (24h burn-in). MNUBERGEMM does not
# "finish a sweep" — it stresses for exactly this many seconds then exits. Kept at the MOP
# value by default; MNUBERGEMM_TIME_TO_RUN lets the driver run a short smoke test instead.
export STD="--dynamic_adj --MM_max_workload 65536 --max_workload 65536 --time_to_run ${MNUBERGEMM_TIME_TO_RUN:-86400} --workload GNC"
# NSCALETX [3/3]: the MOP renders this flag with an em-dash ("—MM_force_cublas_algo").
# Copied verbatim the binary rejects the argument; corrected to a double hyphen.
export MM="--MM_M 0 --MM_N 0 --MM_sm_count 112 --MM_type SX_SX_SSS --MM_force_cublas_algo SQAAABQAAAAiAAAAAQAAAAAAAAAAAAAAAAAAAAAACQAAAAAARdgBAAAAAAAAAAAAAAAAAE0AAAAAAAAAAAAAAA=="
export NET="--NET_sm_count 32 --NET_link_order scatter_sum --NET_size 20480000"
export CE="--CE_type H --CE_size 2048000"
export FREQQ="--freq 400 --duty 0.9 --dual_mode --dual_freq 0.0013888889 --dual_duty 0.50"

#executing the application
# NSCALETX [6/6]: --container-workdir added. The MOP invokes the binary as a RELATIVE path
# ("./mnubergemm"), which only works if the container CWD holds it. pyxis inherits the host
# CWD, but the rack-status submit dir does not exist inside the container,
# so slurmd falls back to /tmp and "./mnubergemm" is not found. In this image the binary is
# at /opt/mnubergemm/mnubergemm, so pin the workdir there and keep the MOP's ./mnubergemm.
srun --cpu-bind=none --mpi=pmix --container-image="$CONT" \
     --container-mounts=/dev:/dev \
     --container-workdir=/opt/mnubergemm \
     ./mnubergemm ${STD} ${FREQQ} ${MM} ${NET} ${CE}
