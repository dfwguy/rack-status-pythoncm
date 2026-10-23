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

from importlib import import_module
from typing import TYPE_CHECKING

from schedulers.base import (
    NODE_STATES,
    TERMINAL_STATES,
    JobHandle,
    JobResult,
    JobSpec,
    Scheduler,
)

if TYPE_CHECKING:
    from schedulers.factory import build_scheduler
    from schedulers.local import LocalScheduler
    from schedulers.slurm import SlurmScheduler
    from schedulers.stubs import MaaSScheduler, RunAIScheduler, SlinkyScheduler

# Importing the data model on compute nodes must not load host-only providers.
# Preserve the public exports, resolving a backend only when a caller requests it.
_LAZY_EXPORTS = {
    "build_scheduler": "schedulers.factory",
    "LocalScheduler": "schedulers.local",
    "SlurmScheduler": "schedulers.slurm",
    "MaaSScheduler": "schedulers.stubs",
    "RunAIScheduler": "schedulers.stubs",
    "SlinkyScheduler": "schedulers.stubs",
}


def __getattr__(name):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


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
