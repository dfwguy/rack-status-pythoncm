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

    def sinfo_cmd(self, extra_args: str = "") -> str:
        """``sinfo`` invocation (with optional extra arguments)."""
        sinfo = (os.environ.get("RACK_SLURM_SINFO") or "sinfo").strip() or "sinfo"
        cmd = shlex.quote(sinfo)
        if extra_args:
            cmd = f"{cmd} {extra_args}"
        return self.slurm_prefix(cmd)

    def squeue_cmd(self, extra_args: str = "") -> str:
        squeue = (os.environ.get("RACK_SLURM_SQUEUE") or "squeue").strip() or "squeue"
        cmd = shlex.quote(squeue)
        if extra_args:
            cmd = f"{cmd} {extra_args}"
        return self.slurm_prefix(cmd)

    def sacct_cmd(self, extra_args: str = "") -> str:
        cmd = f"sacct {extra_args}" if extra_args else "sacct"
        return self.slurm_prefix(cmd)

    def scontrol_cmd(self, extra_args: str = "") -> str:
        scontrol = (os.environ.get("RACK_SLURM_SCONTROL") or "scontrol").strip() or "scontrol"
        cmd = shlex.quote(scontrol)
        if extra_args:
            cmd = f"{cmd} {extra_args}"
        return self.slurm_prefix(cmd)

    def scancel_cmd(self, extra_args: str = "") -> str:
        scancel = (os.environ.get("RACK_SLURM_SCANCEL") or "scancel").strip() or "scancel"
        cmd = shlex.quote(scancel)
        if extra_args:
            cmd = f"{cmd} {extra_args}"
        return self.slurm_prefix(cmd)

    def scontrol_hostnames_cmd(self, nodespec: str) -> str:
        return self.scontrol_cmd(f"show hostnames {shlex.quote(nodespec)}")

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
