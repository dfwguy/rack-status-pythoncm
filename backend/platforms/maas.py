# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Canonical MAAS (Metal as a Service) platform provider.

Translates the rack-status action vocabulary into ``maas`` CLI equivalents.
Slurm commands are identical to BCM (the scheduler is the same), but cluster
management uses ``maas`` / ``juju`` instead of ``cmsh``.

Environment variables
---------------------
``RACK_MAAS_PROFILE``   MAAS CLI profile name (default ``admin``)
``RACK_MAAS_CLI``       path to the ``maas`` binary (default ``maas``)
"""
from __future__ import annotations

import json
import os

import host_exec

from platforms.base import PlatformProvider, _compute_node_records


class MAASProvider(PlatformProvider):
    kind = "maas"

    def _maas_cli(self) -> str:
        return (os.environ.get("RACK_MAAS_CLI") or "maas").strip() or "maas"

    def _maas_profile(self) -> str:
        return (os.environ.get("RACK_MAAS_PROFILE") or "admin").strip() or "admin"

    # -- Cluster manager (maas CLI) -----------------------------------------

    def compute_node_inventory(self) -> list[dict[str, str | None]]:
        proc = host_exec.run_on_host(self.device_status_cmd(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"MAAS node inventory failed (exit {proc.returncode})")
        try:
            records = json.loads(proc.stdout or "")
        except (TypeError, ValueError) as exc:
            raise RuntimeError("MAAS node inventory returned invalid JSON") from exc
        # Machine lifecycle status is not scheduler availability. MAAS ip_addresses are
        # host addresses, not the BMC/rf0 address represented by the inventory's ip field.
        return _compute_node_records(records, provider="MAAS")

    def list_racks_cmd(self) -> list[str]:
        cli = self._maas_cli()
        profile = self._maas_profile()
        return [cli, profile, "tags", "read"]

    def device_status_cmd(self) -> list[str]:
        cli = self._maas_cli()
        profile = self._maas_profile()
        return [cli, profile, "machines", "read"]

    def physical_rf0_cmd(self) -> list[str]:
        cli = self._maas_cli()
        profile = self._maas_profile()
        return [cli, profile, "machines", "read"]

    def device_power_cmd(self, inner: str) -> list[str]:
        cli = self._maas_cli()
        profile = self._maas_profile()
        parts = inner.split()
        action = "off" if "off" in parts else "on"
        system_id = parts[-1] if parts else ""
        return [cli, profile, "machine", f"power-{action}", system_id]

    # -- pdsh service management --------------------------------------------

    def services_restart_remote_cmd(self) -> str:
        return (
            "mount -a; "
            "systemctl restart munge; "
            "systemctl restart slurmd; "
            "systemctl restart nvidia-imex; "
            "systemctl restart nvidia-dcgm 2>/dev/null || systemctl restart cuda-dcgm"
        )
