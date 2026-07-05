"""SSH leaf connection (single target)."""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field

from transport.base import Connection, Process, as_shell_str, env_export_prefix


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


class SSHConnection(Connection):
    """Runs commands on one remote host over ``ssh``.

    Env forwarding is *not* done here (that is the
    :class:`~transport.compose.EnvForwardingConnection` decorator's job, which
    passes the resolved env dict down as ``env_extra``). This leaf only serializes
    whatever ``env_extra`` it receives into an ``export …;`` prefix, preserving the
    historical ordering ``export …; cd <cwd> && <cmd>``.
    """

    def __init__(self, config: SSHConfig):
        self.config = config

    def is_remote(self) -> bool:
        return True

    # -- argv builders ------------------------------------------------------

    def ssh_argv(self, connect_timeout: str | None = None) -> list[str]:
        cfg = self.config
        if not cfg.host:
            raise RuntimeError(
                "RACK_EXEC_MODE=ssh but no SSH host is set. "
                "Point RACK_SSH_HOST at the head-node hostname or IP."
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
        argv.extend(cfg.extra_opts)
        argv.append(f"{cfg.user}@{cfg.host}")
        return argv

    def interactive_argv(self) -> list[str]:
        """Argv for an interactive (PTY) session to this host."""
        cfg = self.config
        argv = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "SetEnv=TERM=xterm-256color",
            "-p", cfg.port or "22",
        ]
        if cfg.key and os.path.isfile(cfg.key):
            argv.extend(["-i", cfg.key])
        argv.extend(cfg.extra_opts)
        argv.append(f"{cfg.user}@{cfg.host}")
        return argv

    def _remote_cmd(
        self,
        cmd: "str | list[str]",
        cwd: str | None,
        env_extra: dict[str, str] | None,
    ) -> str:
        shell_str = as_shell_str(cmd)
        if cwd:
            shell_str = f"cd {shlex.quote(cwd)} && {shell_str}"
        return env_export_prefix(env_extra) + shell_str

    # -- Connection API -----------------------------------------------------

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
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

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
