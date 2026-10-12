# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Composable Scheduler layer for launching work across nodes.

Public surface::

    from schedulers import build_scheduler, JobSpec, Scheduler

Backends: SlurmScheduler + LocalScheduler (implemented); run:AI / slinky / MaaS
(author-ahead stubs). See :mod:`schedulers.base` for the design.
"""
from __future__ import annotations

from schedulers.base import (
    NODE_STATES,
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
    "NODE_STATES",
    "TERMINAL_STATES",
    "JobHandle",
    "JobResult",
    "JobSpec",
    "LocalScheduler",
    "MaaSScheduler",
    "RunAIScheduler",
    "Scheduler",
    "SlinkyScheduler",
    "SlurmScheduler",
    "build_scheduler",
]
