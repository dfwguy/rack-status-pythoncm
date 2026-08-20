# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Composite executors (env forwarding, sudo escalation) and profile routing."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Callable

from transport.base import (
    DEFAULT_HEAD_BYTES,
    DEFAULT_TAIL_BYTES,
    Connection,
    Executor,
    FileStat,
    Process,
    as_shell_str,
)

_logger = logging.getLogger(__name__)

EnvResolver = Callable[[dict[str, str] | None], dict[str, str]]


def make_env_resolver(
    prefixes: tuple[str, ...],
    names: tuple[str, ...],
    overlay_provider: Callable[[], dict[str, str]] | None = None,
) -> EnvResolver:
    """Build a resolver that picks the forwardable subset of ``os.environ``.

    A var is forwarded when its name starts with one of *prefixes* or is listed
    in *names*. An optional *overlay_provider* (e.g. the catalog active-context
    overlay) contributes lowest-precedence values; per-call ``env_extra`` wins.
    """

    def resolve(env_extra: dict[str, str] | None = None) -> dict[str, str]:
        merged: dict[str, str] = {}
        if overlay_provider is not None:
            try:
                for key, val in (overlay_provider() or {}).items():
                    if val is not None:
                        merged[key] = str(val)
            except Exception:  # overlay is best-effort; never break execution
                _logger.debug("resolve: ignoring error", exc_info=True)
        for key, val in os.environ.items():
            if any(key.startswith(p) for p in prefixes) or key in names:
                merged[key] = val
        if env_extra:
            for key, val in env_extra.items():
                if val is not None:
                    merged[key] = str(val)
        return merged

    return resolve


class ExecutorDecorator(Executor):
    """Wraps another Executor, adding policy to *execution* only.

    Filesystem access is forwarded verbatim: escalating with sudo or forwarding
    environment variables changes how a command runs, never which host owns the
    files. Subclasses therefore only override the execution methods.
    """

    def __init__(self, inner: Executor):
        self.inner = inner

    @property
    def kind(self) -> str:  # type: ignore[override]
        return self.inner.kind

    # -- filesystem (pass-through) ------------------------------------------

    def resolve(self, path: object) -> str:
        return self.inner.resolve(path)

    def stat(self, path: object) -> FileStat | None:
        return self.inner.stat(path)

    def stat_dir(self, base: object, **kwargs) -> dict[str, FileStat]:
        return self.inner.stat_dir(base, **kwargs)

    def makedirs(self, path: object) -> str | None:
        return self.inner.makedirs(path)

    def write_bytes(self, path: object, data: bytes) -> str | None:
        return self.inner.write_bytes(path, data)

    def read_text(
        self,
        path: object,
        *,
        head_bytes: int = DEFAULT_HEAD_BYTES,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        whole_bytes: int | None = None,
        timeout: int = 120,
    ) -> tuple[str, dict[str, object]]:
        return self.inner.read_text(
            path, head_bytes=head_bytes, tail_bytes=tail_bytes,
            whole_bytes=whole_bytes, timeout=timeout,
        )

    def which(self, name: str) -> str | None:
        return self.inner.which(name)

    def _test_flag(self, path: object, flag: str) -> bool:
        return self.inner._test_flag(path, flag)

    # An interactive session passes straight through both decorators, and that is the
    # intended behaviour rather than an omission. Env forwarding has nothing to add: a login
    # shell sources the profile itself. And a terminal must NOT inherit a role's sudo -- the
    # threat model's recommendation is to keep the terminal on the unprivileged route, so a
    # sudo-wrapped role still hands the operator their own shell, not root's.

    def interactive_argv(self) -> list[str]:
        return self.inner.interactive_argv()

    def interactive_command_argv(self, cmd: str) -> list[str]:
        return self.inner.interactive_command_argv(cmd)

    def describe(self) -> dict[str, object]:
        return self.inner.describe()


class EnvForwardingExecutor(ExecutorDecorator):
    """Injects the forwardable env subset into every call.

    Policy (which vars to forward) lives here; mechanism (how to apply them:
    subprocess ``env=`` locally, ``export`` prefix over SSH) lives in the leaf,
    which receives the resolved dict via ``env_extra``.
    """

    def __init__(self, inner: Executor, resolver: EnvResolver):
        super().__init__(inner)
        self.resolver = resolver

    def run(self, cmd, *, timeout=120, cwd=None, env=None, login_shell=True, env_extra=None):
        return self.inner.run(
            cmd,
            timeout=timeout,
            cwd=cwd,
            env=env,
            login_shell=login_shell,
            env_extra=self.resolver(env_extra),
        )

    def popen(self, cmd, *, cwd=None, env=None, env_extra=None) -> Process:
        return self.inner.popen(cmd, cwd=cwd, env=env, env_extra=self.resolver(env_extra))

    def wrap_argv(self, cmd, *, cwd=None, env_extra=None) -> list[str]:
        return self.inner.wrap_argv(cmd, cwd=cwd, env_extra=self.resolver(env_extra))


class SudoExecutor(ExecutorDecorator):
    """Escalates every command with ``sudo`` before delegating.

    Applied to the privileged ("script") route so root-requiring node tools
    (``sysinfo.sh``, hardware checks, switch/IMEX/BMC actions) run as root even
    when the SSH/login user is unprivileged (e.g. ``cmods`` with passwordless
    sudo on the compute trays).

    Ordering matters: this wraps only the *command* (``sudo -n -E <cmd>``); the
    inner EnvForwarding/SSH layers then add the ``export …; cd …`` prefix in the
    login shell *before* sudo runs, and ``-E`` carries the forwarded env across
    the sudo boundary. With ``password`` set it uses ``sudo -S`` (stdin), else
    ``sudo -n`` (non-interactive; fails fast if sudo would prompt).
    """

    def __init__(
        self,
        inner: Executor,
        *,
        password: str | None = None,
        preserve_env: bool = True,
    ):
        super().__init__(inner)
        self.password = password or None
        self.preserve_env = preserve_env

    def _opts(self) -> list[str]:
        opts = ["sudo"]
        if self.password:
            opts += ["-S", "-p", ""]  # read password from stdin, empty prompt
        else:
            opts += ["-n"]  # non-interactive: never hang on a password prompt
        if self.preserve_env:
            opts.append("-E")
        return opts

    def _wrap(self, cmd: str | list[str]) -> str | list[str]:
        # Password mode requires a shell pipe, so collapse to a string.
        if self.password:
            piped = f"printf '%s\\n' {shlex.quote(self.password)} | "
            return piped + " ".join(self._opts()) + " " + as_shell_str(cmd)
        if isinstance(cmd, list):
            return [*self._opts(), *cmd]
        return " ".join(self._opts()) + " " + cmd

    def run(self, cmd, *, timeout=120, cwd=None, env=None, login_shell=True, env_extra=None):
        return self.inner.run(
            self._wrap(cmd),
            timeout=timeout,
            cwd=cwd,
            env=env,
            login_shell=login_shell,
            env_extra=env_extra,
        )

    def popen(self, cmd, *, cwd=None, env=None, env_extra=None) -> Process:
        return self.inner.popen(self._wrap(cmd), cwd=cwd, env=env, env_extra=env_extra)

    def wrap_argv(self, cmd, *, cwd=None, env_extra=None) -> list[str]:
        return self.inner.wrap_argv(self._wrap(cmd), cwd=cwd, env_extra=env_extra)


class RoutingConnection(Connection):
    """Routes by *profile* to one of several underlying connections.

    Makes the historical ``control`` (head / cmsh / slurm) vs ``script``
    (privileged node-operational) split an explicit, n-way composition. Also a
    valid :class:`Connection` itself, defaulting to the ``control`` profile —
    including for filesystem access, which always belongs to the control target.
    """

    def __init__(self, targets: dict[str, Connection], default: str = "control"):
        if default not in targets:
            raise ValueError(f"default profile {default!r} not in targets {list(targets)}")
        self.targets = targets
        self.default = default
        super().__init__(targets[default].executor)

    def select(self, profile: str = "control") -> Connection:
        return self.targets.get(profile) or self.targets[self.default]

    def run(
        self,
        cmd,
        *,
        timeout: int = 120,
        cwd=None,
        env=None,
        login_shell: bool = True,
        env_extra=None,
        profile: str = "control",
    ) -> subprocess.CompletedProcess[str]:
        return self.select(profile).run(
            cmd, timeout=timeout, cwd=cwd, env=env, login_shell=login_shell, env_extra=env_extra
        )

    def popen(self, cmd, *, cwd=None, env=None, env_extra=None, profile: str = "control") -> Process:
        return self.select(profile).popen(cmd, cwd=cwd, env=env, env_extra=env_extra)

    def wrap_argv(self, cmd, *, cwd=None, env_extra=None, profile: str = "control") -> list[str]:
        return self.select(profile).wrap_argv(cmd, cwd=cwd, env_extra=env_extra)

    def interactive_argv(self, profile: str = "control") -> list[str]:
        return self.select(profile).interactive_argv()

    def interactive_command_argv(self, cmd: str, profile: str = "control") -> list[str]:
        return self.select(profile).interactive_command_argv(cmd)
