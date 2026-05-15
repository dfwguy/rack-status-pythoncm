"""NVIDIA Base Command Manager (BCM) 11.x platform provider.

Translates the rack-status action vocabulary into the CLI tools available on
a BCM head node: ``cmsh``, ``pdsh``, ``sinfo``/``squeue``/``sacct``/``scontrol``
(behind ``module load slurm``), and operational shell scripts.
"""
from __future__ import annotations

import os

from platforms.base import PlatformProvider


class BCMProvider(PlatformProvider):

    # -- Cluster manager (cmsh) ---------------------------------------------

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
