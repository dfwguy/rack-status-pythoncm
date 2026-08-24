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

BCM inventory and control operations run through the head node's matching
``pythoncm`` package.  Keeping that client out-of-process is deliberate: the
library is generated/version-locked to CMDaemon and cannot safely be shared by
one long-lived Rack Status process serving clusters on different BCM releases.
"""
from __future__ import annotations

import re
import shlex

import bcm_query

from platforms.base import PlatformProvider


class BCMProvider(PlatformProvider):

    # -- Cluster manager (pythoncm on the control host) ---------------------

    def list_racks_cmd(self) -> list[str]:
        return bcm_query.build_command("racks", extra_args=["--format", "cmsh"])

    def device_status_cmd(self) -> list[str]:
        return bcm_query.build_command(
            "status", entity_type="PhysicalNode", extra_args=["--format", "cmsh"]
        )

    def physical_rf0_cmd(self) -> list[str]:
        return bcm_query.build_command("rf0", extra_args=["--format", "cmsh"])

    def device_power_cmd(self, inner: str) -> list[str]:
        match = re.fullmatch(
            r"\s*device\s*;\s*power\s+-n\s+(\S+)\s+(on|off|reset|status)\s*",
            inner,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError(f"unsupported BCM power expression: {inner!r}")
        target, action = match.groups()
        return bcm_query.build_command(
            "power", extra_args=["--target", target, "--action", action.lower()]
        )

    def node_drain_cmd(self, nodespec: str, reason: str = "") -> list[str]:
        args = ["--target", nodespec]
        if reason:
            args += ["--reason", reason]
        return bcm_query.build_command("drain", extra_args=args)

    def node_undrain_cmd(self, nodespec: str) -> list[str]:
        return bcm_query.build_command("undrain", extra_args=["--target", nodespec])

    def node_drain_status_cmd(self, nodespec: str) -> list[str]:
        return bcm_query.build_command("drain-status", extra_args=["--target", nodespec])

    def scontrol_cmd(self, extra_args: str = "") -> str:
        """Translate node drain/resume updates; retain Slurm for unrelated legacy verbs.

        Existing routes build ``scontrol update nodename=...`` strings. Recognising only that
        narrow grammar lets them use PythonCM immediately without pretending PythonCM provides
        Slurm's unrelated ``show hostnames`` and job-step commands.
        """
        try:
            words = shlex.split(extra_args)
        except ValueError:
            words = []
        if words and words[0].lower() == "update":
            fields = {
                key.lower(): value
                for word in words[1:]
                if "=" in word
                for key, value in [word.split("=", 1)]
            }
            nodespec = fields.get("nodename", "")
            state = fields.get("state", "").lower()
            if nodespec and state == "drain":
                return shlex.join(self.node_drain_cmd(nodespec, fields.get("reason", "")))
            if nodespec and state in {"resume", "undrain"}:
                return shlex.join(self.node_undrain_cmd(nodespec))
        return super().scontrol_cmd(extra_args)

    # -- pdsh service management --------------------------------------------

    def services_restart_remote_cmd(self) -> str:
        return (
            "mount -a; "
            "systemctl restart munge; "
            "systemctl restart slurmd; "
            "systemctl restart nvidia-imex; "
            "systemctl restart nvidia-dcgm 2>/dev/null || systemctl restart cuda-dcgm"
        )
