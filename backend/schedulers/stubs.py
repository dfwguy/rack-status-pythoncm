# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Documented-but-unimplemented scheduler backends (run:AI / slinky / MaaS).

These let a workflow be *authored* against a target scheduler before support
lands; they raise a clear ``NotImplementedError`` at run time. Implement each by
subclassing :class:`schedulers.base.Scheduler` (submit/status/cancel/wait) using
the native CLI/API (e.g. ``runai submit``) driven through a ``transport.Connection``.
"""
from __future__ import annotations

from schedulers.base import JobHandle, JobResult, JobSpec, Scheduler


class _UnsupportedScheduler(Scheduler):
    kind = "unsupported"
    _msg = "scheduler not implemented yet"

    def __init__(self, connection=None, platform=None, profile: str = "control"):
        self.conn = connection

    def submit(self, spec: JobSpec) -> JobHandle:
        raise NotImplementedError(self._msg)

    def status(self, handle: JobHandle) -> str:
        raise NotImplementedError(self._msg)

    def cancel(self, handle: JobHandle) -> None:
        raise NotImplementedError(self._msg)

    def wait(self, handle: JobHandle, *, timeout: int = 1800, poll_interval: int = 5) -> JobResult:
        raise NotImplementedError(self._msg)


class RunAIScheduler(_UnsupportedScheduler):
    kind = "runai"
    _msg = "run:AI scheduler backend is not implemented yet (author-ahead stub). Implement submit via `runai submit`."


class SlinkyScheduler(_UnsupportedScheduler):
    kind = "slinky"
    _msg = "slinky (Slurm-on-Kubernetes) scheduler backend is not implemented yet (author-ahead stub)."


class MaaSScheduler(_UnsupportedScheduler):
    kind = "maas"
    _msg = "MaaS scheduler backend is not implemented yet (author-ahead stub)."
