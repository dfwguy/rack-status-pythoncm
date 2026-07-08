"""Composable transport layer for NVIS host command execution.

Public surface::

    from transport import build_connection, TransportConfig, Connection

See :mod:`transport.base` for the design (policy vs mechanism).
"""
from __future__ import annotations

from transport.base import Connection, Process, as_shell_str, env_export_prefix, merge_env
from transport.compose import (
    EnvForwardingConnection,
    RoutingConnection,
    SudoConnection,
    make_env_resolver,
)
from transport.factory import (
    FORWARD_ENV_NAMES,
    FORWARD_ENV_PREFIXES,
    TransportConfig,
    build_connection,
)
from transport.chain import HopSpec, apply_hop, build_chain
from transport.fanout import NodeResult, fanout, fanout_errors, group_outputs
from transport.hop import HopConnection
from transport.local import LocalConnection
from transport.ssh import SSHConfig, SSHConnection

__all__ = [
    "Connection",
    "Process",
    "LocalConnection",
    "SSHConnection",
    "SSHConfig",
    "HopConnection",
    "EnvForwardingConnection",
    "RoutingConnection",
    "SudoConnection",
    "make_env_resolver",
    "build_connection",
    "TransportConfig",
    "FORWARD_ENV_PREFIXES",
    "FORWARD_ENV_NAMES",
    "fanout",
    "group_outputs",
    "fanout_errors",
    "NodeResult",
    "HopSpec",
    "build_chain",
    "apply_hop",
    "as_shell_str",
    "env_export_prefix",
    "merge_env",
]
