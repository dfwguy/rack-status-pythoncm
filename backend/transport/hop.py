# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Chained (jump) execution: run a command on a host *through* another Executor.

This is the composition primitive behind "a target can be a host OR another
connection": :meth:`Connection.hop` wraps any base executor and an ssh target so
that ``base.hop(node).run(cmd)`` executes ``ssh <node> <cmd>`` **on the base's
target**. For ``app -> head -> node`` that means the ``ssh node`` leg runs on the
head, using the head's credentials/reachability — no direct app->node route or app
key on the node required. Hops nest arbitrarily (``base.hop(a).hop(b)``).

The per-hop payload is base64-encoded, so nested shell quoting is a non-issue at
every level (each leg ships an opaque token that the *next* host decodes and runs).
"""
from __future__ import annotations

import base64
import shlex
import subprocess

from transport.base import (
    Connection,
    Executor,
    Process,
    as_shell_str,
    env_export_prefix,
    interactive_timeout_opts,
)
from transport.shellfs import ShellFilesystem


class HopExecutor(ShellFilesystem, Executor):
    """Runs commands on ``host`` by tunnelling an ssh invocation through ``base``.

    Filesystem access resolves against ``host`` (the far end of the hop), not the
    base target, because :class:`~transport.shellfs.ShellFilesystem` issues its
    ``stat`` / ``find`` / ``cat`` through this executor's own ``run``.
    """

    kind = "ssh"

    def __init__(
        self,
        base: Executor,
        host: str,
        *,
        user: str | None = None,
        key: str = "",
        extra_opts: list[str] | None = None,
        connect_timeout: str = "15",
        sudo: bool = False,
    ):
        self.base = base
        self.host = host
        self.user = user or None
        self.key = key or ""
        self.extra_opts = list(extra_opts) if extra_opts else []
        self.connect_timeout = connect_timeout or "15"
        # When True the decoded payload runs under passwordless sudo *on this host*
        # (sudo the `bash` runner, so whole pipelines run privileged — not just the
        # first command). Head→node ssh itself stays unprivileged.
        self.sudo = bool(sudo)

    def _ssh_prefix(self) -> list[str]:
        # Host keys are deliberately not verified on a hop, and this asymmetry with the
        # app->head link (`ssh.host_key_opts`, where `RACK_SSH_KNOWN_HOSTS` turns
        # verification on) is a decision rather than an oversight. A hop runs head->node
        # inside the cluster fabric; compute nodes are reimaged as a matter of routine, so
        # their keys change constantly and a pinned `known_hosts` for two thousand of them
        # would fail every bring-up. The link worth verifying is the one that crosses a
        # tunnel or a jumphost, and that is the first one.
        argv = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={self.connect_timeout}",
        ]
        if self.key:
            argv += ["-i", self.key]
        argv += self.extra_opts
        argv.append(f"{self.user}@{self.host}" if self.user else self.host)
        return argv

    def _interactive_ssh_prefix(self) -> list[str]:
        """Like :meth:`_ssh_prefix`, but for a PTY session on this hop's host.

        Two differences, both deliberate. ``-tt`` forces a tty, since this leg is only ever
        one link of an interactive chain. And ``BatchMode=yes`` is absent: a terminal is the
        one place where being prompted for a password is legitimate, so forbidding it would
        remove a working way to reach a node. Keepalives are set on *every* leg -- a middle
        hop that accepts the connection and then goes silent otherwise leaves the operator
        with a blank pane and no diagnosis, and the legs upstream of it are all healthy.
        """
        argv = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            *interactive_timeout_opts(self.connect_timeout),
        ]
        if self.key:
            argv += ["-i", self.key]
        argv += self.extra_opts
        argv.append(f"{self.user}@{self.host}" if self.user else self.host)
        return argv

    def interactive_argv(self) -> list[str]:
        """Open a shell on this hop's host, through however many legs precede it."""
        return self.base.interactive_command_argv(shlex.join(self._interactive_ssh_prefix()))

    def interactive_command_argv(self, cmd: str) -> list[str]:
        """Run *cmd* on this hop's host, through however many legs precede it.

        ``shlex.join`` quotes *cmd* as one word for the predecessor's shell, so each added
        leg deepens the quoting by exactly one level -- the same mechanism as
        :meth:`_ssh_command`, which is what makes an arbitrary chain work without any code
        knowing how long it is.
        """
        return self.base.interactive_command_argv(
            shlex.join([*self._interactive_ssh_prefix(), cmd])
        )

    def _ssh_command(self, cmd, cwd, env_extra) -> str:
        """The single shell string that runs *cmd* on ``host`` (to hand to ``base``)."""
        remote = env_export_prefix(env_extra)
        if cwd:
            remote += f"cd {shlex.quote(cwd)} && "
        remote += as_shell_str(cmd)
        b64 = base64.b64encode(remote.encode("utf-8")).decode("ascii")
        runner = "sudo -n -E bash" if self.sudo else "bash"
        inner = f"printf %s {b64} | base64 -d | {runner}"
        return shlex.join(self._ssh_prefix()) + " " + shlex.quote(inner)

    def run(
        self,
        cmd,
        *,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = True,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.base.run(self._ssh_command(cmd, cwd, env_extra), timeout=timeout)

    def popen(
        self,
        cmd,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> Process:
        return self.base.popen(self._ssh_command(cmd, cwd, env_extra))

    def wrap_argv(self, cmd, *, cwd: str | None = None, env_extra=None) -> list[str]:
        return self.base.wrap_argv(self._ssh_command(cmd, cwd, env_extra))

    def describe(self) -> dict[str, object]:
        return {"kind": self.kind, "host": self.host}


def HopConnection(base: Connection, host: str, **kwargs) -> Connection:
    """Back-compat factory: a :class:`Connection` hopping from *base* to *host*."""
    return Connection(HopExecutor(base.executor, host, **kwargs))
