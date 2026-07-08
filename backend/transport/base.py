"""Connection abstraction for NVIS host command execution.

A :class:`Connection` is the composable transport seam that every tool/launcher
uses to *launch* work, without caring whether it lands on the local machine, a
head node over SSH, a privileged node, a bastion hop, or (later) a container.

Design: policy vs mechanism.
  * Leaf connections (:class:`~transport.local.LocalConnection`,
    :class:`~transport.ssh.SSHConnection`) own the *mechanism* of running a
    command and how to apply an env dict natively (subprocess ``env=`` locally,
    ``export …;`` prefix over SSH).
  * Composite connections (:mod:`transport.compose`) own *policy*: which env
    vars to forward (:class:`~transport.compose.EnvForwardingConnection`) and
    which target a profile routes to (:class:`~transport.compose.RoutingConnection`).

Return types intentionally mirror :mod:`subprocess` (``CompletedProcess``) and
the historical ``HostProcess`` so existing call sites in ``host_exec`` / ``app``
keep working unchanged.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# A command is either a shell string or an argv list.
Command = "str | list[str]"


def merge_env(
    base_env: dict[str, str] | None,
    env_extra: dict[str, str] | None,
) -> dict[str, str] | None:
    """Merge *env_extra* onto *base_env* for local subprocess execution.

    When there is no *env_extra*, *base_env* is returned unchanged so the
    historical subprocess semantics are preserved exactly (``None`` inherits the
    parent environment; an explicit dict fully replaces it). When *env_extra* is
    present it is layered on top of *base_env* (or ``os.environ`` if none given),
    since a bare overlay should not wipe the ambient environment.
    """
    if not env_extra:
        return base_env
    merged: dict[str, str] = dict(base_env) if base_env is not None else dict(os.environ)
    for key, val in env_extra.items():
        if val is not None:
            merged[key] = str(val)
    return merged


def env_export_prefix(env_extra: dict[str, str] | None) -> str:
    """Serialize *env_extra* into a shell ``export k=v; …`` prefix (SSH side)."""
    if not env_extra:
        return ""
    exports = [
        f"export {key}={shlex.quote(str(val))}"
        for key, val in env_extra.items()
        if val is not None
    ]
    return "; ".join(exports) + "; " if exports else ""


def as_shell_str(cmd: "str | list[str]") -> str:
    """Render a command (string or argv) as a single shell string."""
    return cmd if isinstance(cmd, str) else shlex.join(cmd)


@dataclass
class Process:
    """Uniform wrapper over a local ``Popen`` or an SSH-tunnelled process.

    Mirrors the historical ``host_exec.HostProcess`` interface (``communicate`` /
    ``kill`` / ``poll`` / ``returncode``) so it is a drop-in.
    """

    _proc: subprocess.Popen = field(repr=False)
    returncode: int | None = None

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        out, err = self._proc.communicate(timeout=timeout)
        self.returncode = self._proc.returncode
        return (out or ""), (err or "")

    def kill(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass

    def poll(self) -> int | None:
        rc = self._proc.poll()
        if rc is not None:
            self.returncode = rc
        return rc


class Connection(ABC):
    """Executes commands on some target. Composable via decorators/composites."""

    @abstractmethod
    def is_remote(self) -> bool:
        """True when commands leave the local machine (e.g. SSH)."""

    @abstractmethod
    def run(
        self,
        cmd: "str | list[str]",
        *,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = True,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Blocking execution (mirror of :func:`subprocess.run`)."""

    @abstractmethod
    def popen(
        self,
        cmd: "str | list[str]",
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> Process:
        """Long-running execution (mirror of :class:`subprocess.Popen`)."""

    def wrap_argv(
        self,
        cmd: "str | list[str]",
        *,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> list[str]:
        """Return an argv suitable for ``subprocess.Popen`` (e.g. for PTYs).

        Local default: the command as-is. SSH overrides to wrap in ``ssh …``.
        """
        return list(cmd) if isinstance(cmd, list) else ["bash", "-lc", cmd]

    def hop(
        self,
        host: str,
        *,
        user: str | None = None,
        key: str = "",
        extra_opts: list[str] | None = None,
        connect_timeout: str = "15",
        sudo: bool = False,
    ) -> "Connection":
        """Return a Connection that runs on *host* by tunnelling ssh through ``self``.

        Composition primitive: ``control.hop(node)`` runs ``ssh node <cmd>`` on the
        control target (e.g. the head), so the node leg uses the head's credentials.
        Chainable to N levels: ``self.hop(a).hop(b)``. ``sudo=True`` runs the payload
        under passwordless sudo on *host*.
        """
        from transport.hop import HopConnection

        return HopConnection(
            self, host, user=user, key=key, extra_opts=extra_opts,
            connect_timeout=connect_timeout, sudo=sudo,
        )

    def resolve_path(self, path: str) -> str:
        """Resolve *path* for this target.

        Remote targets return the path unchanged (a local ``.resolve()`` would
        rewrite it through macOS firmlinks and break the remote ``cd``). Local
        targets resolve to an absolute canonical path.
        """
        if self.is_remote():
            return str(path)
        return str(os.path.realpath(path))
