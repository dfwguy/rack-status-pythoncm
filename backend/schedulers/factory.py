# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Assemble a Scheduler backend from a kind + a transport Connection."""
from __future__ import annotations

import os

from schedulers.base import Scheduler
from schedulers.local import LocalScheduler
from schedulers.pythoncm import PythonCMScheduler
from schedulers.slurm import SlurmScheduler
from schedulers.stubs import MaaSScheduler, RunAIScheduler, SlinkyScheduler

_BUILDERS = {
    "slurm": SlurmScheduler,
    "pythoncm": PythonCMScheduler,
    "local": LocalScheduler,
    "runai": RunAIScheduler,
    "slinky": SlinkyScheduler,
    "maas": MaaSScheduler,
}


def build_scheduler(
    kind: str | None = None,
    *,
    connection=None,
    platform=None,
    profile: str = "control",
    env: dict[str, str] | None = None,
) -> Scheduler:
    """Build a scheduler backend.

    ``kind`` defaults to ``RACK_SCHEDULER`` env, then ``slurm``. When *connection*
    is None, the app's active transport connection is built from the environment.
    """
    e = env if env is not None else os.environ
    kind = (kind or e.get("RACK_SCHEDULER") or "slurm").strip().lower()
    if kind not in _BUILDERS:
        raise ValueError(f"unknown scheduler kind {kind!r} (expected one of {tuple(_BUILDERS)})")
    if connection is None:
        from transport import TransportConfig, build_connection

        connection = build_connection(TransportConfig.from_env())
    cls = _BUILDERS[kind]
    # SlurmScheduler takes (connection, platform, profile); LocalScheduler (connection, profile);
    # stubs accept (connection, platform, profile).
    if cls is LocalScheduler:
        return cls(connection, profile)
    return cls(connection, platform, profile)
