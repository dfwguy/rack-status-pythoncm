# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Connection abstraction for NVIS host command execution.

A :class:`Connection` is the seam that every tool/launcher uses to *launch* work
and to *inspect files*, without caring whether the target is the local machine, a
head node over SSH, a privileged node, a bastion hop, or (later) a container.

Design: composition over inheritance.
  * An :class:`Executor` owns the *mechanism* — how to run a command and how to
    inspect the filesystem on one target. There are two leaves,
    :class:`~transport.local.LocalExecutor` and
    :class:`~transport.ssh.SSHExecutor`, plus decorators in
    :mod:`transport.compose` / :mod:`transport.hop` that add policy (env
    forwarding, sudo escalation, ssh hops) by wrapping another Executor.
  * :class:`Connection` is a single concrete class that *holds* an Executor and
    delegates to it. Callers depend only on Connection; swapping local for SSH is
    swapping the composed executor, never a different Connection subclass.

Because the Executor answers filesystem questions too, callers never need to ask
"am I remote?". A path is resolved, stat-ed, or read *through the connection*, and
the executor applies the right mechanism. This is what removed the old
``is_remote()`` predicate: mode was leaking into ~50 call sites, and every leak was
a chance to call a local ``pathlib`` method on a remote path.

Return types intentionally mirror :mod:`subprocess` (``CompletedProcess``) and the
historical ``HostProcess`` so existing call sites keep working unchanged.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterator
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


def as_shell_str(cmd: str | list[str]) -> str:
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

    def iter_stdout(self) -> Iterator[str]:
        """Yield stdout a line at a time as the process produces it.

        ``communicate()`` holds the entire output until exit, which is fine for a short command
        and wrong for one that reports progress: a batch log scan emits a record per file over
        tens of seconds, and buffering them all means the caller learns everything at the end.
        Consumes the pipe to completion, then sets ``returncode``.
        """
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for line in stdout:
                yield line.rstrip("\n")
        finally:
            try:
                stdout.close()
            except OSError:
                pass
            self._proc.wait()
            self.returncode = self._proc.returncode

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


@dataclass(frozen=True)
class FileStat:
    """Minimal stat result that is meaningful on every target.

    ``mtime`` is epoch seconds and ``size`` is bytes — the pair the history layer
    hashes into a stable ``run_id``, so it must always come from the *execution*
    host and never from a container-local ``os.stat``.
    """

    mtime: float
    size: int


# Head/tail capping defaults for :meth:`Executor.read_text`. A validation log can
# be gigabytes; the parser only ever needs the two ends.
DEFAULT_HEAD_BYTES = 256 * 1024
DEFAULT_TAIL_BYTES = 256 * 1024
DEFAULT_WHOLE_BYTES = DEFAULT_HEAD_BYTES + DEFAULT_TAIL_BYTES


class Executor(ABC):
    """Runs commands and inspects files on one target.

    Subclasses are either *leaves* (local subprocess, ssh) or *decorators* that
    wrap another Executor to add policy. Decorators must forward the filesystem
    methods to the inner executor unchanged: sudo and env-forwarding change how a
    command runs, not which host owns the files.
    """

    #: Short target discriminator, ``"local"`` or ``"ssh"``. Diagnostics and
    #: status endpoints report this; control flow should not branch on it.
    kind: str = "local"

    # -- execution ----------------------------------------------------------

    @abstractmethod
    def run(
        self,
        cmd: str | list[str],
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
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> Process:
        """Long-running execution (mirror of :class:`subprocess.Popen`)."""

    def wrap_argv(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> list[str]:
        """Return an argv suitable for ``subprocess.Popen`` (e.g. for PTYs)."""
        return list(cmd) if isinstance(cmd, list) else ["bash", "-lc", cmd]

    # -- filesystem ---------------------------------------------------------
    #
    # These exist so callers never reach for pathlib against a path that may live
    # on another host. Every method is total: it returns a neutral value rather
    # than raising, because "the head node is unreachable" is data the UI renders,
    # not an exception the request handler should die on.

    @abstractmethod
    def resolve(self, path: object) -> str:
        """Canonical absolute path string for this target.

        Local targets resolve symlinks. Remote targets normalise textually — a
        local ``.resolve()`` would rewrite the path through the *container's*
        filesystem (and macOS firmlinks) and break the remote ``cd``. Normalising
        still collapses ``..`` so containment checks stay meaningful.
        """

    @abstractmethod
    def stat(self, path: object) -> FileStat | None:
        """``FileStat`` for *path*, or ``None`` when it does not exist."""

    @abstractmethod
    def stat_dir(
        self,
        base: object,
        *,
        patterns: tuple[str, ...] | None = None,
        maxdepth: int = 1,
    ) -> dict[str, FileStat]:
        """``{absolute path: FileStat}`` for the regular files under *base*.

        One batched call per directory: the dashboard polls parser results, and a
        per-file round trip over SSH would be one connection per log. *patterns*
        filters on basename glob; *maxdepth* mirrors ``find -maxdepth``.
        """

    @abstractmethod
    def makedirs(self, path: object) -> str | None:
        """Create *path* (and parents) on this target.

        Returns ``None`` on success or an error string — never raises, matching
        the rest of the filesystem API.
        """

    @abstractmethod
    def write_bytes(self, path: object, data: bytes) -> str | None:
        """Write *data* to *path* on this target, creating parent directories.

        The write half of the filesystem API: a caller that produced an artifact
        (an export workbook, a generated config) can place it next to the data it
        describes without knowing whether that directory is local or on a host
        three hops away. ``None`` on success, else an error string.
        """

    @abstractmethod
    def read_text(
        self,
        path: object,
        *,
        head_bytes: int = DEFAULT_HEAD_BYTES,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        whole_bytes: int | None = None,
        timeout: int = 120,
    ) -> tuple[str, dict[str, object]]:
        """Head+tail capped text read. Returns ``(text, meta)`` and never raises.

        *meta* carries ``bytes`` (true size), ``read_bytes``, ``truncated``,
        ``gzip``, ``read_error`` and ``target``.
        """

    @abstractmethod
    def which(self, name: str) -> str | None:
        """Absolute path of executable *name* on this target, else ``None``."""

    def is_file(self, path: object) -> bool:
        st = self.stat(path)
        return st is not None

    def is_dir(self, path: object) -> bool:
        """True when *path* is a directory on this target."""
        return self._test_flag(path, "-d")

    @abstractmethod
    def _test_flag(self, path: object, flag: str) -> bool:
        """Backend for ``test <flag> <path>`` style predicates."""

    # -- introspection ------------------------------------------------------

    def describe(self) -> dict[str, object]:
        """Structured description of the target, for status endpoints."""
        return {"kind": self.kind}


class Connection:
    """Executes commands and inspects files on a target, via a composed Executor.

    Concrete by design: there is no ``LocalConnection`` / ``SSHConnection`` class
    hierarchy any more, only different executors handed to the same Connection.
    """

    def __init__(self, executor: Executor):
        self.executor = executor

    # -- introspection ------------------------------------------------------

    @property
    def kind(self) -> str:
        """``"local"`` or ``"ssh"`` — for diagnostics, not for branching."""
        return self.executor.kind

    def describe(self) -> dict[str, object]:
        return self.executor.describe()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Connection {self.executor.__class__.__name__} kind={self.kind}>"

    # -- execution ----------------------------------------------------------

    def run(
        self,
        cmd: str | list[str],
        *,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = True,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.executor.run(
            cmd, timeout=timeout, cwd=cwd, env=env, login_shell=login_shell,
            env_extra=env_extra,
        )

    def popen(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> Process:
        return self.executor.popen(cmd, cwd=cwd, env=env, env_extra=env_extra)

    def wrap_argv(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> list[str]:
        return self.executor.wrap_argv(cmd, cwd=cwd, env_extra=env_extra)

    # -- filesystem ---------------------------------------------------------

    def resolve(self, path: object) -> str:
        return self.executor.resolve(path)

    def stat(self, path: object) -> FileStat | None:
        return self.executor.stat(path)

    def stat_dir(
        self,
        base: object,
        *,
        patterns: tuple[str, ...] | None = None,
        maxdepth: int = 1,
    ) -> dict[str, FileStat]:
        return self.executor.stat_dir(base, patterns=patterns, maxdepth=maxdepth)

    def makedirs(self, path: object) -> str | None:
        return self.executor.makedirs(path)

    def write_bytes(self, path: object, data: bytes) -> str | None:
        return self.executor.write_bytes(path, data)

    def read_text(
        self,
        path: object,
        *,
        head_bytes: int = DEFAULT_HEAD_BYTES,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        whole_bytes: int | None = None,
        timeout: int = 120,
    ) -> tuple[str, dict[str, object]]:
        return self.executor.read_text(
            path, head_bytes=head_bytes, tail_bytes=tail_bytes,
            whole_bytes=whole_bytes, timeout=timeout,
        )

    def is_file(self, path: object) -> bool:
        return self.executor.is_file(path)

    def is_dir(self, path: object) -> bool:
        return self.executor.is_dir(path)

    def which(self, name: str) -> str | None:
        return self.executor.which(name)

    # Historical alias: ``resolve_path`` predates the wider filesystem API.
    def resolve_path(self, path: str) -> str:
        return self.executor.resolve(path)

    # -- composition --------------------------------------------------------

    def hop(
        self,
        host: str,
        *,
        user: str | None = None,
        key: str = "",
        extra_opts: list[str] | None = None,
        connect_timeout: str = "15",
        sudo: bool = False,
    ) -> Connection:
        """Return a Connection that runs on *host* by tunnelling ssh through ``self``.

        Composition primitive: ``control.hop(node)`` runs ``ssh node <cmd>`` on the
        control target (e.g. the head), so the node leg uses the head's credentials.
        Chainable to N levels: ``self.hop(a).hop(b)``. ``sudo=True`` runs the payload
        under passwordless sudo on *host*.
        """
        # DIRECT-TO-TRAY. This method exists because there was historically "no
        # direct app->node route" -- true until 2026-08-06, when the Align team
        # wired the high-speed links. The ObsVM now reaches every tray directly
        # (verified root@tray with the BCM key), so relaying through the head is
        # pure legacy, and an expensive one: EVERY tray hop is a session channel
        # multiplexed over one ControlMaster to the head, whose MaxSessions is
        # unset and therefore the OpenSSH default of 10. A fleet sweep is ~2,000
        # channels through a 10-slot ceiling -- the documented "no more sessions"
        # x215, and the sweep that silently lost 655 of 2,013 nodes. Direct hops
        # consume none of the head's slots and measured ~20% faster per hop.
        #
        # Opt-in via RACK_DIRECT_NODE_SSH so the relay stays available for any
        # target the app genuinely cannot reach itself; set the key with
        # RACK_NODE_SSH_KEY (default: the BCM key) and user with
        # RACK_NODE_SSH_USER.
        import os as _os
        import re as _re

        from transport.hop import HopExecutor

        if (_os.environ.get("RACK_DIRECT_NODE_SSH", "").lower()
                not in ("", "0", "false", "no")
                and _re.match(r"^ink50-dh\d{2}-[a-z]\d+-\d+$", host or "")):
            from transport.ssh import SSHConfig, SSHExecutor

            return Connection(SSHExecutor(SSHConfig(
                host=host,
                user=_os.environ.get("RACK_NODE_SSH_USER", "root"),
                key=(key or _os.environ.get("RACK_NODE_SSH_KEY",
                                            "/home/nvis/.ssh/id_bcm")),
                port="22",
            )))

        return Connection(
            HopExecutor(
                self.executor, host, user=user, key=key, extra_opts=extra_opts,
                connect_timeout=connect_timeout, sudo=sudo,
            )
        )
