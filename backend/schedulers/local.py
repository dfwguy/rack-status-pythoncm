# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Local scheduler backend: run a command/script directly (no batch system).

Useful for single-host runs, dev, and the local-dev/demo path. Execution is
synchronous at ``submit`` time; ``wait`` returns the cached result.
"""
from __future__ import annotations

import shlex
import uuid

from schedulers.base import JobHandle, JobResult, JobSpec, Scheduler


class LocalScheduler(Scheduler):
    kind = "local"

    def __init__(self, connection, profile: str = "control"):
        self.conn = connection
        self.profile = profile
        self._results: dict[str, JobResult] = {}

    def _run(self, cmd: str, timeout: int):
        try:
            return self.conn.run(cmd, timeout=timeout, profile=self.profile)
        except TypeError:
            return self.conn.run(cmd, timeout=timeout)

    def submit(self, spec: JobSpec) -> JobHandle:
        if spec.command:
            cmd = spec.command
        elif spec.script:
            cmd = f"bash {shlex.quote(spec.script)}"
        else:
            raise ValueError("LocalScheduler.submit needs a script or command")
        proc = self._run(cmd, timeout=spec.timeout)
        state = "COMPLETED" if proc.returncode == 0 else "FAILED"
        job_id = uuid.uuid4().hex[:12]
        self._results[job_id] = JobResult(
            returncode=proc.returncode,
            state=state,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            job_id=job_id,
        )
        return JobHandle(job_id=job_id, kind=self.kind)

    def status(self, handle: JobHandle) -> str:
        r = self._results.get(handle.job_id)
        return r.state if r else "PENDING"

    def cancel(self, handle: JobHandle) -> None:
        return None  # synchronous; nothing to cancel

    def wait(self, handle: JobHandle, *, timeout: int = 1800, poll_interval: int = 5) -> JobResult:
        return self._results.get(handle.job_id) or JobResult(returncode=1, state="FAILED", job_id=handle.job_id)
