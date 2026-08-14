# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Composable transport layer for NVIS host command execution.

Public surface::

    from transport import build_connection, TransportConfig, Connection

See :mod:`transport.base` for the design (composition over inheritance: a plain
``Connection`` delegating to a composed ``Executor``).
"""
from __future__ import annotations

from transport.base import (
    DEFAULT_HEAD_BYTES,
    DEFAULT_TAIL_BYTES,
    DEFAULT_WHOLE_BYTES,
    Connection,
    Executor,
    FileStat,
    Process,
    as_shell_str,
    env_export_prefix,
    merge_env,
)
from transport.chain import HopSpec, apply_hop, build_chain
from transport.compose import (
    EnvForwardingExecutor,
    ExecutorDecorator,
    RoutingConnection,
    SudoExecutor,
    make_env_resolver,
)
from transport.factory import (
    DEFAULT_EXEC_MODE,
    FORWARD_ENV_NAMES,
    FORWARD_ENV_PREFIXES,
    TransportConfig,
    build_connection,
)
from transport.fanout import NodeResult, fanout, fanout_errors, group_outputs
from transport.hop import HopConnection, HopExecutor
from transport.local import LocalConnection, LocalExecutor
from transport.shellfs import ShellFilesystem
from transport.ssh import SSHConfig, SSHConnection, SSHExecutor

__all__ = [
    "DEFAULT_EXEC_MODE",
    "DEFAULT_HEAD_BYTES",
    "DEFAULT_TAIL_BYTES",
    "DEFAULT_WHOLE_BYTES",
    "FORWARD_ENV_NAMES",
    "FORWARD_ENV_PREFIXES",
    "Connection",
    "EnvForwardingExecutor",
    "Executor",
    "ExecutorDecorator",
    "FileStat",
    "HopConnection",
    "HopExecutor",
    "HopSpec",
    "LocalConnection",
    "LocalExecutor",
    "NodeResult",
    "Process",
    "RoutingConnection",
    "SSHConfig",
    "SSHConnection",
    "SSHExecutor",
    "ShellFilesystem",
    "SudoExecutor",
    "TransportConfig",
    "apply_hop",
    "as_shell_str",
    "build_chain",
    "build_connection",
    "env_export_prefix",
    "fanout",
    "fanout_errors",
    "group_outputs",
    "make_env_resolver",
    "merge_env",
]
