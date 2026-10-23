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

import inspect
import re
import shlex
import time
from datetime import datetime, timedelta

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
        # Pure argv rendering is also used by compute-side plan validation;
        # importing it must not pull in the application's host transport/store.
        if platform is None:
            from platforms import get_platform

            platform = get_platform()
        self.platform = platform
        self.profile = profile

    def _run(self, cmd: str, timeout: int = 120):
        # Decide the calling convention before execution. A TypeError raised
        # after sbatch was accepted must never cause another submission.
        parameters = inspect.signature(self.conn.run).parameters
        kwargs = {"timeout": timeout}
        if "profile" in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            kwargs["profile"] = self.profile
        return self.conn.run(cmd, **kwargs)

    @staticmethod
    def render_argv(spec: JobSpec) -> list[str]:
        args = ["--parsable", f"--job-name={spec.name}"]
        if spec.nodes:
            args.append(f"--nodes={spec.nodes}")
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
            args += list(spec.script_args)
        elif spec.command:
            args += ["--wrap", spec.command]
        return args

    _sbatch_argv = render_argv  # Backward-compatible pure renderer.

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
        if not re.fullmatch(r"[1-9][0-9]*", job_id):
            raise RuntimeError("sbatch returned an invalid job reference; acceptance is unknown")
        return JobHandle(job_id=job_id, kind=self.kind, submit_output=out)

    def _accounting(self, handle: JobHandle, timeout: int):
        if not re.fullmatch(r"[1-9][0-9]*", handle.job_id):
            raise ValueError("Invalid Slurm job reference")
        return self._run(self.platform.sacct_cmd(
            f"-j {shlex.quote(handle.job_id)} -X -n -o State%30"), timeout=timeout)

    def status(self, handle: JobHandle) -> str:
        """Legacy scheduler contract: a new job not yet in accounting is pending."""
        proc = self._accounting(handle, 60)
        rows = (proc.stdout or "").strip().splitlines()
        return rows[0].strip().split()[0].rstrip("+").upper() if rows else "PENDING"

    def observed_status(self, handle: JobHandle) -> str:
        """Evidence-backed state for reconciliation; absent records are not pending."""
        proc = self._accounting(handle, 15)
        rows = (proc.stdout or "").strip().splitlines()
        if proc.returncode == 0 and rows:
            return rows[0].strip().split()[0].rstrip("+").upper()
        queue = self._run(self.platform.squeue_cmd(
            f"-j {shlex.quote(handle.job_id)} -h -o %T"), timeout=15)
        rows = (queue.stdout or "").strip().splitlines()
        if queue.returncode == 0 and rows:
            return rows[0].strip().split()[0].rstrip("+").upper()
        if proc.returncode or queue.returncode:
            raise RuntimeError("Slurm accounting/queue state is unavailable; job state is unknown")
        return "UNKNOWN"

    def find_by_tag(self, name: str, comment: str, submitted_at: str, user: str):
        """Bounded read-only recovery of an ambiguous submission; never resubmit."""
        if (not re.fullmatch(r"ck-[0-9a-f]{32}", name)
                or comment != "rack-clusterkit:" + name[3:]
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user)
                or not re.fullmatch(r"[0-9T:+.Z-]{19,40}", submitted_at)):
            raise ValueError("Invalid submission recovery identity")
        start = (datetime.fromisoformat(submitted_at.replace("Z", "+00:00")).astimezone()
                 - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        common = f"--name={shlex.quote(name)} --user={shlex.quote(user)}"
        commands = [self.platform.squeue_cmd(common + " -h -o '%i|%j|%k'"),
                    self.platform.sacct_cmd(common + " --starttime=" + shlex.quote(start)
                                            + " -X -n -P -o JobIDRaw,JobName%80,Comment%80")]
        matches = set()
        failures = 0
        for command in commands:
            proc = self._run(command, timeout=15)
            if proc.returncode:
                failures += 1
                continue
            text = proc.stdout or ""
            if len(text) > 256 * 1024 or len(text.splitlines()) > 64:
                raise RuntimeError("Submission recovery response exceeds its bound")
            for line in text.splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 3 and parts[1:3] == [name, comment]:
                    if not re.fullmatch(r"[1-9][0-9]*", parts[0]):
                        raise RuntimeError("Invalid recovered job reference")
                    matches.add(parts[0])
        if len(matches) > 1:
            raise RuntimeError("Multiple jobs match this attempt; manual reconciliation required")
        if matches:
            return JobHandle(matches.pop(), "slurm")
        if failures:
            raise RuntimeError("Submission recovery is incomplete; acceptance remains unknown")
        return None

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
