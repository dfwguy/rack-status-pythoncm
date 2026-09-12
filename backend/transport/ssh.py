# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""SSH execution mechanism (single target)."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field

from transport.base import (
    Connection,
    Executor,
    NotConfigured,
    Process,
    as_shell_str,
    env_export_prefix,
    interactive_timeout_opts,
)
from transport.shellfs import ShellFilesystem


@dataclass
class SSHConfig:
    host: str
    user: str = "root"
    key: str = ""
    port: str = "22"
    extra_opts: list[str] = field(default_factory=list)
    connect_timeout: str = "10"
    alive_interval: str = "5"
    alive_count: str = "2"
    # SSH connection multiplexing (ControlMaster). When on, repeated/concurrent ssh
    # to this host reuse one TCP + handshake — essential for fan-out, where N node
    # hops otherwise open N simultaneous app->head sessions and trip sshd MaxStartups
    # ("kex_exchange_identification: Connection reset by peer").
    multiplex: bool = True
    mux_persist: str = "60"


def _mux_control_dir() -> str:
    """A writable dir for ControlMaster sockets (~/.ssh preferred, else temp)."""

    d = os.path.expanduser("~/.ssh")
    if os.path.isdir(d) and os.access(d, os.W_OK):
        return d
    return tempfile.gettempdir()


class SSHExecutor(ShellFilesystem, Executor):
    """Runs commands and inspects files on one remote host over ``ssh``.

    Env forwarding is *not* done here (that is the
    :class:`~transport.compose.EnvForwardingExecutor` decorator's job, which
    passes the resolved env dict down as ``env_extra``). This leaf only serializes
    whatever ``env_extra`` it receives into an ``export …;`` prefix, preserving the
    historical ordering ``export …; cd <cwd> && <cmd>``.

    Filesystem access comes from :class:`~transport.shellfs.ShellFilesystem`, so a
    caller holding a head-node path can stat or read it without knowing where it
    lives.
    """

    kind = "ssh"

    def __init__(self, config: SSHConfig):
        self.config = config

    # -- argv builders ------------------------------------------------------

    def ssh_argv(self, connect_timeout: str | None = None) -> list[str]:
        cfg = self.config
        if not cfg.host:
            # Typed so a polled predicate can tell "nowhere to ask" apart from "asked and
            # failed"; still a RuntimeError, so every existing catch keeps working.
            raise NotConfigured(
                "No SSH host is configured. Point RACK_SSH_HOST at the head-node "
                "hostname or IP."
            )
        argv = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_timeout or cfg.connect_timeout or '10'}",
            "-o", f"ServerAliveInterval={cfg.alive_interval or '5'}",
            "-o", f"ServerAliveCountMax={cfg.alive_count or '2'}",
            "-p", cfg.port or "22",
        ]
        if cfg.key and os.path.isfile(cfg.key):
            argv.extend(["-i", cfg.key])
        if cfg.multiplex:
            # %C = short hash of (localuser,host,port,remoteuser) -> stable, avoids the
            # ~104-char ControlPath limit. ControlPersist keeps the master briefly so a
            # fan-out's concurrent hops all attach to one connection.
            argv.extend([
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={_mux_control_dir()}/nvis-mux-%C",
                "-o", f"ControlPersist={cfg.mux_persist or '60'}",
            ])
        argv.extend(cfg.extra_opts)
        argv.append(f"{cfg.user}@{cfg.host}")
        return argv

    def interactive_argv(self) -> list[str]:
        """Argv for an interactive (PTY) session to this host.

        Ends at ``user@host``, so a hop can append its own command as one quoted word.

        Callers must not rebuild this argv themselves. One did, and omitted
        ``cfg.extra_opts`` -- which carries ``RACK_SSH_OPTS``, and therefore the ``-J`` that
        makes the head node reachable at all from outside the cluster. On such a deployment
        the head-node terminal worked while every compute terminal tried to reach the head's
        own address directly and timed out.
        """
        cfg = self.config
        argv = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "SetEnv=TERM=xterm-256color",
            *interactive_timeout_opts(cfg.connect_timeout),
            "-p", cfg.port or "22",
        ]
        if cfg.key and os.path.isfile(cfg.key):
            argv.extend(["-i", cfg.key])
        argv.extend(cfg.extra_opts)
        argv.append(f"{cfg.user}@{cfg.host}")
        return argv

    def interactive_command_argv(self, cmd: str) -> list[str]:
        """Run *cmd* under a PTY on this host: ``ssh -tt … user@host <cmd>``.

        *cmd* is appended as a single argv element, so the remote shell receives it whole
        however it was quoted. That is what lets a hop nest into this one level at a time.
        """
        return [*self.interactive_argv(), cmd]

    def _remote_cmd(
        self,
        cmd: str | list[str],
        cwd: str | None,
        env_extra: dict[str, str] | None,
    ) -> str:
        shell_str = as_shell_str(cmd)
        if cwd:
            shell_str = f"cd {shlex.quote(cwd)} && {shell_str}"
        return env_export_prefix(env_extra) + shell_str

    # -- execution ----------------------------------------------------------

    def run(
        self,
        cmd,
        *,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = True,  # remote is always a login shell; kept for parity
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = self.ssh_argv() + [self._remote_cmd(cmd, cwd, env_extra)]
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )

    def popen(
        self,
        cmd,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> Process:
        argv = self.ssh_argv() + [self._remote_cmd(cmd, cwd, env_extra)]
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return Process(_proc=proc)

    def wrap_argv(self, cmd, *, cwd: str | None = None, env_extra=None) -> list[str]:
        return self.ssh_argv() + [self._remote_cmd(cmd, cwd, env_extra)]

    def describe(self) -> dict[str, object]:
        return {"kind": self.kind, "host": self.config.host or None}


def SSHConnection(config: SSHConfig) -> Connection:
    """Back-compat factory: a :class:`Connection` over an :class:`SSHExecutor`."""
    return Connection(SSHExecutor(config))
