# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# The same command Rack Status runs (platforms/base.py gpu_reset_remote_cmd): stop only what is
# up, starting or restarting included -- DCGM is nvidia-dcgm on some sites and cuda-dcgm on others,
# and nvidia-persistenced and dcgm-exporter hold GPUs open -- reset, then start the same services again. -R ssh: pdsh's rsh default
# fails for any non-root account. Prefix with "sudo -n" yourself when not root on the nodes.
pdsh -R ssh -w "$1" 'act=; for s in cmd slurmd dcgm-exporter nvidia-dcgm cuda-dcgm nvidia-imex nvidia-persistenced; do case $(systemctl is-active $s) in active|activating|reloading) act="$s $act";; esac; done; [ -n "$act" ] && systemctl stop $act; sleep 5; nvidia-smi -r; rc=$?; for s in $act; do systemctl start $s; done; exit $rc'

