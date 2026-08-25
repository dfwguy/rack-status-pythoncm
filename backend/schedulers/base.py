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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Normalized terminal job states (superset across backends).
TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
    "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE", "PREEMPTED",
}


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
    launch_mode: str = "fanout"          # fanout, first target (leader), or BCM head
    execution_user: str | None = None      # native remote account (PythonCM ProgramRunner)
    timeout: int = 1800                 # default wait/exec budget (seconds)


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
    node_results: list[dict[str, object]] = field(default_factory=list)

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
        """Convenience: submit + wait using the spec's timeout."""
        handle = self.submit(spec)
        result = self.wait(handle, timeout=spec.timeout, poll_interval=poll_interval)
        if result.state == "TIMEOUT":
            # Waiting expiring is not permission to leave native work running.
            # Providers implement idempotent/best-effort cancellation semantics.
            self.cancel(handle)
        return result
