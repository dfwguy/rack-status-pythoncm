# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Slurm scheduler backend (sbatch submit + sacct/squeue polling)."""
from __future__ import annotations

import shlex
import time

from platforms import get_platform

from schedulers.base import TERMINAL_STATES, JobHandle, JobResult, JobSpec, Scheduler

# Slurm's node states, compact (``sinfo %t``) and long (``%T`` / ``scontrol``) spellings, onto the
# normalized vocabulary in ``schedulers.base.NODE_STATES``. Anything absent (boot, cloud, the
# power-save states, ``unk``, ``?``) says nothing about whether the node can work: ``unknown``.
_SLURM_NODE_STATE = {
    "idle": "idle",
    "alloc": "running", "allocated": "running", "mix": "running", "mixed": "running",
    "comp": "running", "completing": "running",
    # PLANNED: backfill has set the node aside for a job still pending on it.
    "plnd": "pending", "planned": "pending",
    "resv": "reserved", "reserved": "reserved",
    "maint": "reserved", "maintenance": "reserved", "block": "reserved", "blocked": "reserved",
    "drng": "draining", "draining": "draining",
    "drain": "drained", "drained": "drained",
    "down": "down", "fail": "down", "failg": "down", "failing": "down",
    "inval": "down", "invalid": "down",
}


def slurm_node_status(raw: str | None) -> str:
    """One of ``NODE_STATES`` for a Slurm node state such as ``drain``, ``mix-`` or ``IDLE+DRAIN``.

    Suffix flags (``*`` not responding, ``~`` powered off, ...) are dropped: the view reads the
    state, and a non-responding node is already ``down`` or ``unk`` in it. A combined state takes
    its most telling part -- ``ALLOCATED+DRAIN`` is a node draining, not one running.
    """
    parts = {
        _SLURM_NODE_STATE.get(p.strip().rstrip("*~$#:!%@^-"), "unknown")
        for p in str(raw or "").lower().split("+") if p.strip()
    }
    if "down" in parts:
        return "down"
    if "draining" in parts or ("drained" in parts and "running" in parts):
        return "draining"
    for status in ("drained", "running", "reserved", "pending", "idle"):
        if status in parts:
            return status
    return "unknown"


class SlurmScheduler(Scheduler):
    kind = "slurm"

    def __init__(self, connection, platform=None, profile: str = "control"):
        self.conn = connection
        self.platform = platform or get_platform()
        self.profile = profile

    def _run(self, cmd: str, timeout: int = 120):
        # RoutingConnection accepts profile=; plain Connection does not.
        try:
            return self.conn.run(cmd, timeout=timeout, profile=self.profile)
        except TypeError:
            return self.conn.run(cmd, timeout=timeout)

    def _sbatch_argv(self, spec: JobSpec) -> list[str]:
        args = ["--parsable", f"--job-name={spec.name}"]
        if spec.nodes:
            args += ["-N", str(spec.nodes)]
        if spec.nodelist:
            args.append(f"--nodelist={spec.nodelist}")
        if spec.ntasks_per_node:
            args.append(f"--ntasks-per-node={spec.ntasks_per_node}")
        if spec.output:
            args.append(f"--output={spec.output}")
        if spec.reservation:
            args.append(f"--reservation={spec.reservation}")
        args += list(spec.extra_args)
        if spec.script:
            args.append(spec.script)
        elif spec.command:
            args += ["--wrap", spec.command]
        return args

    def submit(self, spec: JobSpec) -> JobHandle:
        if not (spec.script or spec.command):
            raise ValueError("SlurmScheduler.submit needs a script or command")
        cmd = self.platform.sbatch_cmd(shlex.join(self._sbatch_argv(spec)))
        proc = self._run(cmd, timeout=120)
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            raise RuntimeError(f"sbatch failed (rc={proc.returncode}): {(proc.stderr or out).strip()[:500]}")
        # --parsable => "<jobid>[;cluster]" on the first line.
        job_id = out.splitlines()[0].split(";")[0].strip()
        return JobHandle(job_id=job_id, kind=self.kind, submit_output=out)

    def status(self, handle: JobHandle) -> str:
        proc = self._run(
            self.platform.sacct_cmd(
                f"-j {shlex.quote(handle.job_id)} -X -n -o State%30"
            ),
            timeout=60,
        )
        line = (proc.stdout or "").strip().splitlines()
        if not line:
            return "PENDING"  # too new for sacct
        # Normalize "CANCELLED+" / "CANCELLED by ..." -> "CANCELLED".
        return line[0].strip().split()[0].rstrip("+").upper()

    def cancel(self, handle: JobHandle) -> None:
        self._run(self.platform.scancel_cmd(shlex.quote(handle.job_id)), timeout=60)

    def wait(self, handle: JobHandle, *, timeout: int = 1800, poll_interval: int = 5) -> JobResult:
        deadline = time.monotonic() + max(1, timeout)
        state = "PENDING"
        while time.monotonic() < deadline:
            state = self.status(handle)
            if state in TERMINAL_STATES:
                break
            time.sleep(max(1, poll_interval))
        stdout = self._read_output(handle) if state in TERMINAL_STATES else ""
        rc = 0 if state == "COMPLETED" else 1
        return JobResult(returncode=rc, state=state, stdout=stdout, job_id=handle.job_id)

    def _read_output(self, handle: JobHandle) -> str:
        """Best-effort: resolve the job's StdOut via scontrol and cat it."""
        proc = self._run(
            self.platform.scontrol_cmd(
                f"show job {shlex.quote(handle.job_id)} 2>/dev/null | "
                "grep -oE 'StdOut=[^ ]+' | head -1 | cut -d= -f2-"
            ),
            timeout=60,
        )
        path = (proc.stdout or "").strip().splitlines()
        if not path or not path[0]:
            return ""
        cat = self._run(f"cat {shlex.quote(path[0])} 2>/dev/null", timeout=120)
        return cat.stdout or ""
