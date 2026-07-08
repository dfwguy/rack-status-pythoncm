"""Composable Scheduler layer for launching work across nodes.

Public surface::

    from schedulers import build_scheduler, JobSpec, Scheduler

Backends: SlurmScheduler + LocalScheduler (implemented); run:AI / slinky / MaaS
(author-ahead stubs). See :mod:`schedulers.base` for the design.
"""
from __future__ import annotations

from schedulers.base import (
    TERMINAL_STATES,
    JobHandle,
    JobResult,
    JobSpec,
    Scheduler,
)
from schedulers.factory import build_scheduler
from schedulers.local import LocalScheduler
from schedulers.slurm import SlurmScheduler
from schedulers.stubs import MaaSScheduler, RunAIScheduler, SlinkyScheduler

__all__ = [
    "Scheduler",
    "JobSpec",
    "JobHandle",
    "JobResult",
    "TERMINAL_STATES",
    "SlurmScheduler",
    "LocalScheduler",
    "RunAIScheduler",
    "SlinkyScheduler",
    "MaaSScheduler",
    "build_scheduler",
]
