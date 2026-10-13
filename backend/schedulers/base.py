# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Scheduler abstraction: submit/track work across nodes, transport-agnostic.

Analogous to :class:`platforms.PlatformProvider` (cluster-manager strategy) and
:mod:`transport` (where/how a command runs): a :class:`Scheduler` owns *how work
is launched across nodes* -- ``sbatch``/``srun`` today, run:AI / slinky / MaaS
later -- while delegating the actual command execution to a ``transport.Connection``.

A :class:`JobSpec` is the backend-neutral request (N nodes x tasks, a script or
command or image, output path, env). Backends translate it to their native
submit and report a normalized :class:`JobResult`.
"""
from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Normalized terminal job states (superset across backends).
TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
    "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE", "PREEMPTED",
}

# Normalized node states: what the workload manager says a node can do *now*, in words every
# backend maps into, so a view never has to know whose vocabulary it is reading. Ordered from
# "can take work" to "cannot say"; a view rolling several nodes up reads severity off this order.
#   idle      free to take a job
#   running   holding work (allocated, mixed, or finishing it)
#   pending   set aside for a job still waiting to start (Slurm's planned node)
#   reserved  held out of the queue (an admin reservation, maintenance, a blocked node)
#   draining  running its last job before it is taken out
#   drained   out of service by an operator or a health check, otherwise healthy
#   down      the scheduler cannot use it (down, failed, invalid)
#   unknown   no answer, or a state outside this list
NODE_STATES = ("idle", "running", "pending", "reserved", "draining", "drained", "down", "unknown")


@dataclass
class JobSpec:
    """Backend-neutral description of work to run across nodes."""

    name: str = "nvis-job"
    nodes: int | None = None            # node COUNT
    nodelist: str | None = None         # explicit Slurm nodelist (pins placement)
    ntasks_per_node: int | None = None
    script: str | None = None           # a batch script path (sbatch <script>)
    command: str | None = None          # an inline command (sbatch --wrap / local exec)
    image: str | None = None            # container image (run:AI / pyxis, future)
    output: str | None = None           # stdout path (may contain %j)
    reservation: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    timeout: int = 1800                 # default wait/exec budget (seconds)
    # Where the command runs across the target. A batch scheduler has one answer and ignores
    # this; a native parallel runner has to be told. "fanout" runs it on every node,
    # "leader" on the first only (an MPI launcher that fans out itself), "head" on the
    # cluster head (an inventory query that must not touch compute).
    launch_mode: str = "fanout"
    # Account the work runs as. Slurm takes this from the submitting identity; a native
    # runner is told explicitly, and getting it wrong writes logs nobody can read.
    execution_user: str | None = None


@dataclass
class JobHandle:
    job_id: str
    kind: str
    submit_output: str = ""


@dataclass
class JobResult:
    returncode: int
    state: str
    stdout: str = ""
    stderr: str = ""
    job_id: str | None = None
    # Per-node outcome, when the backend can report one. A batch job has a single exit
    # status; a fan-out has one per node, and collapsing 18 of them into a single integer
    # loses the only thing worth knowing -- which node failed.
    node_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.state == "COMPLETED" or (self.state not in TERMINAL_STATES and self.returncode == 0)


class Scheduler(ABC):
    """Submit and track work across nodes; delegates execution to a Connection."""

    kind: str = "base"

    @abstractmethod
    def submit(self, spec: JobSpec) -> JobHandle:
        """Submit *spec* and return a handle (does not wait)."""

    @abstractmethod
    def status(self, handle: JobHandle) -> str:
        """Return the current normalized state for *handle*."""

    @abstractmethod
    def cancel(self, handle: JobHandle) -> None:
        """Cancel the job."""

    @abstractmethod
    def wait(self, handle: JobHandle, *, timeout: int = 1800, poll_interval: int = 5) -> JobResult:
        """Block until the job reaches a terminal state (or *timeout*)."""

    def run(self, spec: JobSpec, *, poll_interval: int = 5) -> JobResult:
        """Submit, wait, and cancel anything still running when the budget expires.

        The cancel is the part worth stating. Giving up on *waiting* is not the same as the
        work stopping: a native runner's command outlives this process, and the next job's
        BCM prolog then finds it and flags a rogue process, draining the rack
        (`bcm-prolog-rogueprocess.mdc`). So a timeout has to end the work, not just the wait.
        """
        handle = self.submit(spec)
        result = self.wait(handle, timeout=spec.timeout, poll_interval=poll_interval)
        if result.state == "TIMEOUT":
            with contextlib.suppress(Exception):
                # Best effort: the wait already failed, and raising here would replace a
                # useful TIMEOUT result with an unrelated error from the cancel path.
                self.cancel(handle)
        return result
