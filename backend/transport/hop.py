"""Chained (jump) connection: run a command on a host *through* another Connection.

This is the composition primitive behind "a Connection's target can be a host OR
another Connection": :meth:`Connection.hop` wraps any base connection and an ssh
target so that ``base.hop(node).run(cmd)`` executes ``ssh <node> <cmd>`` **on the
base's target**. For ``app -> head -> node`` that means the ``ssh node`` leg runs
on the head, using the head's credentials/reachability — no direct app->node route
or app key on the node required. Hops nest arbitrarily (``base.hop(a).hop(b)``).

The per-hop payload is base64-encoded, so nested shell quoting is a non-issue at
every level (each leg ships an opaque token that the *next* host decodes and runs).
"""
from __future__ import annotations

import base64
import shlex
import subprocess

from transport.base import Connection, Process, as_shell_str, env_export_prefix


class HopConnection(Connection):
    """Run commands on ``host`` by tunnelling an ssh invocation through ``base``."""

    def __init__(
        self,
        base: Connection,
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

    def is_remote(self) -> bool:
        return True

    def _ssh_prefix(self) -> list[str]:
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
