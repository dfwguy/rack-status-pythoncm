# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
pdsh -w $1 "systemctl stop cmd; systemctl stop slurmd; systemctl stop nvidia-imex; systemctl stop cuda-dcgm; sleep 10; nvidia-smi -r; sleep 10; systemctl start cmd; systemctl start slurmd; systemctl start nvidia-imex; systemctl start cuda-dcgm"
