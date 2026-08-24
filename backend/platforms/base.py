# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Abstract base for platform providers.

Each method returns a **shell command string** (or argv list) that
``host_exec.run_on_host`` / ``popen_on_host`` will execute either locally
or over SSH.  Platform-specific subclasses supply the concrete commands.
"""
from __future__ import annotations

import os
import shlex
from abc import ABC, abstractmethod


class PlatformProvider(ABC):
    """Defines the command vocabulary that the rack-status app requires."""

    # -- Cluster manager (rack inventory / device status) -------------------

    @abstractmethod
    def list_racks_cmd(self) -> list[str]:
        """Command to enumerate rack IDs (e.g. ``cmsh -c 'rack; list'``)."""

    @abstractmethod
    def device_status_cmd(self) -> list[str]:
        """Command to retrieve per-node device status (e.g. ``cmsh -c 'ds'``)."""

    @abstractmethod
    def physical_rf0_cmd(self) -> list[str]:
        """Command to map hostnames to rf0 IPs."""

    @abstractmethod
    def device_power_cmd(self, inner: str) -> list[str]:
        """Command for device power on/off operations."""

    # -- Slurm helpers (most are identical across platforms) -----------------

    def _slurm_module(self) -> str:
        return (os.environ.get("RACK_SLURM_MODULE") or "slurm").strip() or "slurm"

    def _skip_module_load(self) -> bool:
        return os.environ.get("RACK_SLURM_SKIP_MODULE_LOAD", "").strip().lower() in (
            "1", "true", "yes",
        )

    def slurm_prefix(self, cmd: str) -> str:
        """Wrap *cmd* with ``module load slurm && …`` unless skipped."""
        # NscaleTX: BCM 11 splits /cm/shared by arch and does NOT export SLURM_CONF for
        # non-login ssh, so Slurm CLIs die with "Could not establish a configuration source"
        # (DNS SRV fallback) even when given absolute paths. RACK_SLURM_CONF exports it in
        # the remote shell. Documented in RACKSTATUS_GO_LIVE.md.
        conf = (os.environ.get("RACK_SLURM_CONF") or "").strip()
        pre = f"export SLURM_CONF={shlex.quote(conf)}; " if conf else ""
        if self._skip_module_load():
            return pre + cmd
        mod = self._slurm_module()
        # Prefer silent module load; stderr from Slurm bash-completion is harmless but noisy.
        return (
            pre
            + f"module load -s {shlex.quote(mod)} 2>/dev/null || "
            f"module load {shlex.quote(mod)} 2>/dev/null; {cmd}"
        )

    def _slurm_cli_cmd(self, name: str, extra_args: str = "") -> str:
        """Build one Slurm CLI invocation using its configured executable.

        Keeping the executable lookup here is intentional: callers must never have to
        rediscover Slurm's non-login-shell PATH or guess a versioned BCM install path.
        """
        env_key = f"RACK_SLURM_{name.upper()}"
        executable = (os.environ.get(env_key) or name).strip() or name
        cmd = shlex.quote(executable)
        if extra_args:
            cmd = f"{cmd} {extra_args}"
        return self.slurm_prefix(cmd)

    def sbatch_cmd(self, extra_args: str = "") -> str:
        """``sbatch`` invocation through the configured platform environment."""
        return self._slurm_cli_cmd("sbatch", extra_args)

    def sinfo_cmd(self, extra_args: str = "") -> str:
        """``sinfo`` invocation (with optional extra arguments)."""
        return self._slurm_cli_cmd("sinfo", extra_args)

    def squeue_cmd(self, extra_args: str = "") -> str:
        return self._slurm_cli_cmd("squeue", extra_args)

    def srun_cmd(self, extra_args: str = "") -> str:
        """``srun`` invocation through the same platform Slurm environment."""
        return self._slurm_cli_cmd("srun", extra_args)

    def sacct_cmd(self, extra_args: str = "") -> str:
        return self._slurm_cli_cmd("sacct", extra_args)

    def scontrol_cmd(self, extra_args: str = "") -> str:
        return self._slurm_cli_cmd("scontrol", extra_args)

    def scancel_cmd(self, extra_args: str = "") -> str:
        return self._slurm_cli_cmd("scancel", extra_args)

    def scontrol_hostnames_cmd(self, nodespec: str) -> str:
        return self.scontrol_cmd(f"show hostnames {shlex.quote(nodespec)}")

    def node_drain_cmd(self, nodespec: str, reason: str = "") -> str | list[str]:
        """Prevent workload placement on *nodespec* through the active manager."""
        args = ["update", f"nodename={nodespec}", "state=drain"]
        if reason:
            args.append(f"reason={reason}")
        return self.scontrol_cmd(" ".join(shlex.quote(arg) for arg in args))

    def node_undrain_cmd(self, nodespec: str) -> str | list[str]:
        """Return *nodespec* to the workload manager's schedulable state."""
        args = ["update", f"nodename={nodespec}", "state=resume"]
        return self.scontrol_cmd(" ".join(shlex.quote(arg) for arg in args))

    def node_drain_status_cmd(self, nodespec: str) -> str | list[str]:
        """Retrieve workload-manager drain state for *nodespec*."""
        return self.sinfo_cmd(f"-h -N -o '%N %t' -n {shlex.quote(nodespec)}")

    def sacct_job_cmd(self, job_id: str) -> str:
        return self.sacct_cmd(
            f"-j {shlex.quote(job_id)} -X -P -n -o NodeList%2000 --noheader"
        )

    # -- pdsh / remote execution --------------------------------------------

    @abstractmethod
    def services_restart_remote_cmd(self) -> str:
        """The remote command string that pdsh sends to each node."""

    def pdsh_cmd(self, hosts: str, remote_cmd: str, *, fanout: int | None = None) -> list[str]:
        """Build a ``pdsh -w <hosts> <remote_cmd>`` argv."""
        argv = ["pdsh"]
        if fanout:
            argv.extend(["-f", str(fanout)])
        argv.extend(["-w", hosts, remote_cmd])
        return argv

    # -- Script wrappers (platform may prepend module load) -----------------

    def wrap_script_cmd(self, script_path: str, args: list[str] | None = None) -> str:
        """Wrap a shell script invocation with the Slurm module prefix."""
        parts = [f"bash {shlex.quote(script_path)}"]
        if args:
            parts.extend(shlex.quote(a) for a in args)
        return self.slurm_prefix(" ".join(parts))
