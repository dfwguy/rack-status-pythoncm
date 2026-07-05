"""Composite connections: env-forwarding decorator, sudo escalation, profile routing."""
from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable

from transport.base import Connection, Process, as_shell_str

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
                pass
        for key, val in os.environ.items():
            if any(key.startswith(p) for p in prefixes) or key in names:
                merged[key] = val
        if env_extra:
            for key, val in env_extra.items():
                if val is not None:
                    merged[key] = str(val)
        return merged

    return resolve


class EnvForwardingConnection(Connection):
    """Decorator that injects the forwardable env subset into every call.

    Policy (which vars to forward) lives here; mechanism (how to apply them:
    subprocess ``env=`` locally, ``export`` prefix over SSH) lives in the leaf,
    which receives the resolved dict via ``env_extra``.
    """

    def __init__(self, inner: Connection, resolver: EnvResolver):
        self.inner = inner
        self.resolver = resolver

    def is_remote(self) -> bool:
        return self.inner.is_remote()

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

    def resolve_path(self, path: str) -> str:
        return self.inner.resolve_path(path)


class SudoConnection(Connection):
    """Decorator that escalates every command with ``sudo`` before delegating.

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
        inner: Connection,
        *,
        password: str | None = None,
        preserve_env: bool = True,
    ):
        self.inner = inner
        self.password = password or None
        self.preserve_env = preserve_env

    def is_remote(self) -> bool:
        return self.inner.is_remote()

    def _opts(self) -> list[str]:
        opts = ["sudo"]
        if self.password:
            opts += ["-S", "-p", ""]  # read password from stdin, empty prompt
        else:
            opts += ["-n"]  # non-interactive: never hang on a password prompt
        if self.preserve_env:
            opts.append("-E")
        return opts

    def _wrap(self, cmd: "str | list[str]") -> "str | list[str]":
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

    def resolve_path(self, path: str) -> str:
        return self.inner.resolve_path(path)


class RoutingConnection(Connection):
    """Routes by *profile* to one of several underlying connections.

    Makes the historical ``control`` (head / cmsh / slurm) vs ``script``
    (privileged node-operational) split an explicit, n-way composition. Also a
    valid :class:`Connection` itself, defaulting to the ``control`` profile.
    """

    def __init__(self, targets: dict[str, Connection], default: str = "control"):
        if default not in targets:
            raise ValueError(f"default profile {default!r} not in targets {list(targets)}")
        self.targets = targets
        self.default = default

    def select(self, profile: str = "control") -> Connection:
        return self.targets.get(profile) or self.targets[self.default]

    def is_remote(self) -> bool:
        return self.select().is_remote()

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
