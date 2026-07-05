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
from transport.local import LocalConnection
from transport.ssh import SSHConfig, SSHConnection

__all__ = [
    "Connection",
    "Process",
    "LocalConnection",
    "SSHConnection",
    "SSHConfig",
    "EnvForwardingConnection",
    "RoutingConnection",
    "SudoConnection",
    "make_env_resolver",
    "build_connection",
    "TransportConfig",
    "FORWARD_ENV_PREFIXES",
    "FORWARD_ENV_NAMES",
    "as_shell_str",
    "env_export_prefix",
    "merge_env",
]
