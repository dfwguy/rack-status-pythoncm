# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""NVIDIA Base Command Manager (BCM) 11.x platform provider.

Translates the rack-status action vocabulary into the CLI tools available on
a BCM head node: ``cmsh``, ``pdsh``, ``sinfo``/``squeue``/``sacct``/``scontrol``
(behind ``module load slurm``), and operational shell scripts.
"""
from __future__ import annotations

import os

import bcm_query

from platforms.base import PlatformProvider, _compute_node_records


class BCMProvider(PlatformProvider):

    # -- Cluster manager (cmsh) ---------------------------------------------

    def compute_node_inventory(self) -> list[dict[str, str | None]]:
        result = bcm_query.nodes()
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("error") if isinstance(result, dict) else "invalid response"
            raise RuntimeError(f"BCM node inventory failed: {reason or 'query unsuccessful'}")
        return _compute_node_records(
            result.get("data"), provider="BCM", rack_field="rack_id", ip_field="bmc_ip",
        )

    def list_racks_cmd(self) -> list[str]:
        cmsh = (os.environ.get("RACK_CMSH") or "cmsh").strip() or "cmsh"
        return [cmsh, "-c", "rack; list"]

    def device_status_cmd(self) -> list[str]:
        cmsh = (os.environ.get("RACK_CMSH") or "cmsh").strip() or "cmsh"
        return [cmsh, "-c", "ds"]

    def physical_rf0_cmd(self) -> list[str]:
        cmsh = (os.environ.get("RACK_CMSH") or "cmsh").strip() or "cmsh"
        return [
            cmsh, "-c",
            "device; foreach -t physicalnode (get hostname; interfaces; use rf0; get ip)",
        ]

    def device_power_cmd(self, inner: str) -> list[str]:
        cmsh = (os.environ.get("RACK_CMSH") or "cmsh").strip() or "cmsh"
        return [cmsh, "-c", inner]

    # -- pdsh service management --------------------------------------------

    def services_restart_remote_cmd(self) -> str:
        return (
            "mount -a; "
            "systemctl restart munge; "
            "systemctl restart slurmd; "
            "systemctl restart nvidia-imex; "
            "systemctl restart nvidia-dcgm 2>/dev/null || systemctl restart cuda-dcgm"
        )
