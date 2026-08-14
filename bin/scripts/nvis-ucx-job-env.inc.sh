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
# Shared UCX / CUDA defaults for NVIS Slurm batch drivers (nvloom, ClusterKit, …).
# Source after ``module load`` and workspace helpers (and typically after ``hpcx_load`` when using HPC-X).
# Override any variable in the parent environment before sourcing this file.
#
# Motivation (Spectrum-X / RoCE + large srun fan-in):
#   - UCX VFS DIAG spam when /run/user/<uid>/ucx is missing (common for root Slurm jobs).
#   - rdmacm_cm ``rdma_create_event_channel`` ENOMEM / EAGAIN when many ranks open CM channels at once;
#     defaulting UCX_SOCKADDR_TLS to tcp uses TCP for sockaddr wireup while keeping RC for data path.
#   - Stable CUDA enumeration across hosts for nvloom / MNNVL checks.

export UCX_VFS_ENABLE="${UCX_VFS_ENABLE:-n}"
export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"
export UCX_IB_REG_METHODS="${UCX_IB_REG_METHODS:-rcache,direct}"

if [[ -z ${UCX_SOCKADDR_TLS+x} ]]; then
  export UCX_SOCKADDR_TLS=tcp
fi

# Soft nofile bump (best-effort; Slurm / PAM may cap below this).
if [[ "$(id -u)" -eq 0 ]]; then
  ulimit -n 1048576 2>/dev/null || ulimit -n 524288 2>/dev/null || ulimit -n 262144 2>/dev/null || true
else
  ulimit -n 1048576 2>/dev/null || ulimit -n 524288 2>/dev/null || true
fi
