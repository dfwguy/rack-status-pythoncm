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


#: Floor for an interactive session's connect timeout, in seconds.
#:
#: An interactive shell carries no deadline of its own, so an unbounded leg that accepts the
#: connection and then never answers hangs forever behind a blank pane with no diagnosis --
#: which is exactly how an unreachable compute tray presented on Nscale TX, indistinguishable
#: from a broken UI. Higher than the batch default (10s) because a terminal is opened by a
#: human who will wait, while a batch command is one of thousands.
INTERACTIVE_CONNECT_TIMEOUT = 15


def interactive_timeout_opts(connect_timeout: str | int | None = None) -> list[str]:
    """``-o`` pairs bounding ONE leg of an interactive chain.

    Every leg gets these, not just the first: the legs upstream of a wedged middle hop are
    all healthy, so nothing upstream times out and nothing is ever printed.

    Takes the larger of the configured timeout and
    :data:`INTERACTIVE_CONNECT_TIMEOUT`, so a site that deliberately raised
    ``RACK_SSH_CONNECT_TIMEOUT`` for a long path (Nscale TX uses 20 for three hops) keeps its
    value, while a site on the batch default still gets a timeout suited to a human waiting.
    """
    try:
        configured = int(str(connect_timeout or "").strip() or 0)
    except ValueError:
        configured = 0
    return [
        "-o", f"ConnectTimeout={max(configured, INTERACTIVE_CONNECT_TIMEOUT)}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]


class NotConfigured(RuntimeError):
    """This deployment has not been told where to execute — there is no host to reach.

    A *configuration* state, not a failure: a deployment with no head node cannot be asked
    whether a remote directory exists, and that is different from asking and being refused.
    Separating the two is what lets a polled predicate answer "no, I cannot see it" while the
    reachability probe reports *why*, instead of every endpoint raising.

    Subclasses ``RuntimeError`` deliberately. This condition was a bare ``RuntimeError`` for
    most of the transport's life and callers catch it that way -- ``host_exec._probe_reachable``
    among them -- so widening the type would have silently stopped those catches working.
    """


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

    # -- interactive (PTY) sessions ------------------------------------------
    #
    # Separate from run/popen because the caller execs these against a pty master instead
    # of running them to completion, so what it needs is an *argv*, not a result.
    #
    # The composition rule is the one ``HopExecutor._ssh_command`` already uses for
    # non-interactive work: each hop renders the next hop's argv as ONE quoted shell word
    # for its predecessor. An N-hop chain therefore nests N times with the quoting handled
    # a single level at a time, and no code anywhere counts the hops. The web terminal used
    # to hand-build "head, then node" instead, which is why a deployment with a bastion in
    # front of the head had a working head-node shell and no compute shell at all.

    def interactive_argv(self) -> list[str]:
        """Local argv that opens an interactive login shell ON this target."""
        raise NotImplementedError(
            f"{type(self).__name__} cannot open an interactive session"
        )

    def interactive_command_argv(self, cmd: str) -> list[str]:
        """Local argv that runs the shell string *cmd* under a PTY on this target."""
        raise NotImplementedError(
            f"{type(self).__name__} cannot open an interactive session"
        )

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

    def interactive_argv(self) -> list[str]:
        """Argv that opens an interactive shell on this connection's target.

        Composed by the executor chain, so a connection reached through three hops yields a
        three-deep nested argv without any caller knowing the depth.
        """
        return self.executor.interactive_argv()

    def interactive_command_argv(self, cmd: str) -> list[str]:
        return self.executor.interactive_command_argv(cmd)

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
        gateway: bool = False,
    ) -> Connection:
        """Return a Connection that runs on *host* by tunnelling ssh through ``self``.

        Composition primitive: ``control.hop(node)`` runs ``ssh node <cmd>`` on the
        control target (e.g. the head), so the node leg uses the head's credentials.
        Chainable to N levels: ``self.hop(a).hop(b)``. ``sudo=True`` runs the payload
        under passwordless sudo on *host*.

        ``gateway=True`` marks *host* as infrastructure -- a head node, bastion or other declared
        endpoint -- rather than a compute node, which is what lets its leg verify host keys (see
        :meth:`transport.hop.HopExecutor._host_key_opts`). Off by default, so every node hop
        keeps working on a deployment that has turned verification on.

        Deliberately unconditional: this always relays through ``self``. Whether a site can
        instead reach a node *directly* is a property of that deployment, so it is resolved
        by ``host_exec.node_connection`` from the cluster's declared node access rather than
        decided here. This method briefly carried an inline
        ``re.match(r"^ink50-dh01-[a-z]\\d+-\\d+$", host)`` plus one site's key path, which put
        a single customer's hostname scheme in the generic transport layer and silently did
        nothing on every other cluster.
        """
        from transport.hop import HopExecutor

        return Connection(
            HopExecutor(
                self.executor, host, user=user, key=key, extra_opts=extra_opts,
                connect_timeout=connect_timeout, sudo=sudo, gateway=gateway,
            )
        )
