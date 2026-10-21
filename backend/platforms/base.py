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

Command methods return a **shell command string** (or argv list) that
``host_exec.run_on_host`` / ``popen_on_host`` will execute either locally
or over SSH. Inventory methods execute a read and return normalized records.
Platform-specific subclasses supply the concrete commands and readers.
"""
from __future__ import annotations

import os
import shlex
from abc import ABC, abstractmethod

from core import is_valid_node_hostname


def device_state(raw: object) -> str:
    """A cluster manager's device state as the node record's ``status`` word.

    ``up``; ``down`` for DOWN or UNREACHABLE; ``inst`` for any other state the manager reports
    (installing, INSTALLER_FAILED, booting, ...); ``unknown`` when it reports none. The same
    mapping serves BCM's structured status and the bracket word of ``cmsh ds``, so the two
    sources cannot disagree about what a state means.
    """
    word = "".join(str(raw or "").split()).lower()
    if not word or word in {"none", "undefined", "unknown"}:
        return "unknown"
    if word == "up":
        return "up"
    if word in {"down", "unreachable"}:
        return "down"
    return "inst"


def _compute_node_records(
    records: object, *, provider: str, rack_field: str = "", ip_field: str = "",
    rack_unit_field: str = "", status_field: str = "",
) -> list[dict[str, str | None]]:
    """Validate manager inventory without deriving hostnames or requiring rack membership.

    ``rack_unit_field`` names the provider's position-in-rack attribute (BCM's rack unit). It is
    carried as ``rack_unit`` only when the provider reports one: it is placement data the cluster
    manager owns, so nothing here derives it from the hostname or the tray template.
    ``status_field`` names the provider's device state; :func:`device_state` normalizes it, and a
    record without one stays ``unknown`` for the caller to fill from another source.
    """
    if not isinstance(records, list):
        # This is a failed external query, rather than a caller passing a wrong Python type.
        raise RuntimeError(f"{provider} node inventory must contain a list of devices")  # noqa: TRY004
    nodes: dict[str, dict[str, str | None]] = {}
    for record in records:
        hostname = record.get("hostname") if isinstance(record, dict) else None
        if not isinstance(hostname, str) or not is_valid_node_hostname(hostname):
            raise RuntimeError(f"{provider} node inventory contains an invalid hostname")
        if hostname in nodes:
            continue
        rack = record.get(rack_field) if rack_field else None
        ip = record.get(ip_field) if ip_field else None
        nodes[hostname] = {
            "id": hostname,
            "rack": rack.strip() or None if isinstance(rack, str) else None,
            "status": device_state(record.get(status_field)) if status_field else "unknown",
            "ip": ip.strip() if isinstance(ip, str) else "",
            "scheduler_state": "",
        }
        unit = record.get(rack_unit_field) if rack_unit_field else None
        if isinstance(unit, (str, int)) and str(unit).strip():
            nodes[hostname]["rack_unit"] = str(unit).strip()
    return list(nodes.values())


class PlatformProvider(ABC):
    """Defines the command vocabulary that the rack-status app requires."""

    #: Which cluster manager this is (``bcm``, ``maas``), as a view names the source of a node's
    #: power/provisioning state. Mirrors ``schedulers.base.Scheduler.kind``.
    kind = ""

    # -- Cluster manager (rack inventory / device status) -------------------

    def compute_node_inventory(self) -> list[dict[str, str | None]]:
        """Read actual compute nodes independently of rack naming and scheduler presence."""
        raise RuntimeError(f"Compute node inventory is unsupported by {type(self).__name__}")

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

    def sacct_job_cmd(self, job_id: str) -> str:
        return self.sacct_cmd(
            f"-j {shlex.quote(job_id)} -X -P -n -o NodeList%2000 --noheader"
        )

    # -- pdsh / remote execution --------------------------------------------

    @abstractmethod
    def services_restart_remote_cmd(self) -> str:
        """The remote command string that pdsh sends to each node."""

    def gpu_reset_remote_cmd(self) -> str:
        """Reset every GPU on a node: stop what holds them, ``nvidia-smi -r``, start it all again.

        Stops only the services that are up -- running, or starting/restarting -- and starts the
        same ones in reverse order, so one command fits sites that name DCGM ``nvidia-dcgm``
        (Nscale NC) or ``cuda-dcgm`` and sites without BCM's ``cmd``. "Starting" counts: on a node
        whose GPU needs a reset, DCGM sits in ``activating (auto-restart)``, and a stop list that
        only took ``active`` units skipped it, so its nv-hostengine came back and held the GPUs
        ("In use by another client", Nscale NC gpu105-su21). ``nvidia-persistenced`` and
        ``dcgm-exporter`` are stopped too: both hold GPUs open. Exits with the reset's status.
        """
        return (
            "act=; for s in cmd slurmd dcgm-exporter nvidia-dcgm cuda-dcgm nvidia-imex nvidia-persistenced; do "
            "case $(systemctl is-active $s) in active|activating|reloading) act=\"$s $act\";; esac; done; "
            "[ -n \"$act\" ] && systemctl stop $act; sleep 5; "
            "nvidia-smi -r; rc=$?; "
            "for s in $act; do systemctl start $s; done; "
            "exit $rc"
        )

    def pdsh_cmd(self, hosts: str, remote_cmd: str, *, fanout: int | None = None) -> list[str]:
        """Build a ``pdsh -R ssh -w <hosts> <remote_cmd>`` argv.

        ``-R ssh`` always: pdsh's compiled-in default is often rsh, which needs a privileged
        source port and so fails with "rcmd: socket: Permission denied" for any non-root account
        -- on every node, while pdsh still exits 0. Every node script here already passes it.
        """
        argv = ["pdsh", "-R", "ssh"]
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
