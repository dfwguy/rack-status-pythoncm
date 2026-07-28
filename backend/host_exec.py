"""Platform-agnostic host command executor (compatibility facade).

This module is a thin facade over the composable :mod:`transport` layer. It
preserves the historical function surface (``run_on_host`` / ``popen_on_host`` /
``run_slurm_on_host`` / ``wrap_for_host`` / reachability / interactive SSH argv)
so existing call sites keep working, while delegating actual execution to a
:class:`transport.RoutingConnection` whose executor is composed from a local or
SSH leaf plus env-forwarding and (optionally) sudo decorators.

It also exposes the *filesystem* half of that connection — :func:`resolve`,
:func:`stat`, :func:`stat_dir`, :func:`read_text`, :func:`which`, :func:`is_file`,
:func:`is_dir`. Callers use these instead of :mod:`pathlib` whenever a path may
belong to the execution host, which is what allows the old ``is_remote()``
predicate to be gone: nothing above this layer needs to know where a file lives.

Environment variables
---------------------
``RACK_SSH_HOST``    head-node hostname / IP
``RACK_SSH_USER``    SSH user (default ``root``)
``RACK_SSH_KEY``     path to SSH private key  (default ``/run/secrets/ssh_key``)
``RACK_SSH_PORT``    SSH port (default ``22``)
``RACK_SSH_OPTS``    extra ``ssh`` flags (space-separated)
``RACK_EXEC_MODE``   **deprecated** — ``local`` or ``ssh``; connections default to
                     ``ssh``. Set ``local`` only while the app runs directly on the
                     target host. A warning is logged when it is set at all.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time

from transport import (
    DEFAULT_HEAD_BYTES,
    DEFAULT_TAIL_BYTES,
    FileStat,
    Process,
    RoutingConnection,
    SSHExecutor,
    TransportConfig,
    build_connection,
)

# HostProcess is retained as a public name for back-compat; it is the transport
# Process wrapper (communicate/kill/poll/returncode).
HostProcess = Process

_init_lock = threading.Lock()

# Cached, lazily-built state -------------------------------------------------
_CONN: RoutingConnection | None = None
_CONTROL_SSH: SSHExecutor | None = None  # raw control SSH leaf (reachability/PTY argv)
_CONFIG: TransportConfig | None = None

_which_lock = threading.Lock()
_which_cache: dict[str, str | None] = {}

_isdir_lock = threading.Lock()
_isdir_cache: dict[str, tuple[float, bool]] = {}


def _dir_cache_ttl() -> int:
    try:
        return max(0, int(os.environ.get("RACK_FS_DIR_CACHE_TTL_SEC", "30")))
    except (TypeError, ValueError):
        return 30


def _ensure_init() -> None:
    global _CONN, _CONTROL_SSH, _CONFIG
    if _CONN is not None:
        return
    with _init_lock:
        if _CONN is not None:
            return
        cfg = TransportConfig.from_env()
        conn = build_connection(cfg)
        _CONFIG = cfg
        _CONN = conn
        # Keep a handle to the raw control SSH leaf for reachability probes and
        # interactive PTY argv (which need ssh_argv/interactive_argv, not run()).
        if cfg.exec_mode == "ssh":
            _CONTROL_SSH = SSHExecutor(cfg.control) if cfg.control else None
        else:
            _CONTROL_SSH = None


def _conn() -> RoutingConnection:
    _ensure_init()
    assert _CONN is not None
    return _CONN


def connection(profile: str = "control"):
    """Return the composed :class:`transport.Connection` for *profile*.

    Exposed so higher layers (e.g. the workflow fan-out) can use the app's active
    connection as the base of a chain (``conn.hop(node)``) instead of re-deriving
    transport settings. ``profile`` selects the control (default) or script route.
    """
    return _conn().select(profile)


def invalidate_connection() -> None:
    """Drop the cached connection so the next call rebuilds it from current env.

    Call this after a runtime environment change (e.g. saving Settings that affect
    transport/sudo, like ``RACK_SCRIPT_SUDO``) so the change takes effect without
    an app restart.
    """
    global _CONN, _CONTROL_SSH, _CONFIG
    with _init_lock:
        _CONN = None
        _CONTROL_SSH = None
        _CONFIG = None
    with _which_lock:
        _which_cache.clear()
    with _isdir_lock:
        _isdir_cache.clear()


def exec_kind() -> str:
    """``"local"`` or ``"ssh"`` — the control target's mechanism.

    For diagnostics and status payloads only. Application code should not branch
    on this: run commands with :func:`run_on_host` and touch files with the
    filesystem helpers below, and the connection applies the right mechanism.
    """
    return _conn().kind


def script_target_configured() -> bool:
    """True when a dedicated privileged ``script`` SSH target is configured."""
    _ensure_init()
    return bool(_CONFIG and _CONFIG.script is not None)


def wrap_for_host(
    cmd: list[str],
    *,
    cwd: str | None = None,
    profile: str = "control",
    env_extra: dict[str, str] | None = None,
) -> list[str]:
    """Return *cmd* as-is locally, or wrapped in an SSH argv when remote.

    The returned list is suitable for ``subprocess.Popen(…)``. Forwarded env vars
    (prefix/name allowlist) plus any request-scoped *env_extra* are exported on
    the remote side. ``profile="script"`` routes to the privileged target.
    """
    return _conn().wrap_argv(cmd, cwd=cwd, env_extra=env_extra, profile=profile)


# ---------------------------------------------------------------------------
# Filesystem  –  the execution host's files, wherever that host is
# ---------------------------------------------------------------------------

def resolve(path: object) -> str:
    """Canonical path string for *path* on the execution host.

    The single path resolver for the whole app. Replaces the several local
    variants that each re-implemented "``.resolve()`` unless remote" and got the
    remote case subtly different.
    """
    return _conn().resolve(path)


def stat(path: object) -> FileStat | None:
    """``FileStat(mtime, size)`` from the execution host, or ``None`` if absent."""
    return _conn().stat(path)


def stat_dir(
    base: object,
    *,
    patterns: tuple[str, ...] | None = None,
    maxdepth: int = 1,
) -> dict[str, FileStat]:
    """``{path: FileStat}`` for regular files under *base* (one round trip)."""
    return _conn().stat_dir(base, patterns=patterns, maxdepth=maxdepth)


def makedirs(path: object) -> str | None:
    """Create *path* on the execution host. ``None`` on success, else an error string."""
    return _conn().makedirs(path)


def read_text(
    path: object,
    *,
    head_bytes: int = DEFAULT_HEAD_BYTES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    whole_bytes: int | None = None,
    timeout: int = 120,
) -> tuple[str, dict[str, object]]:
    """Head+tail capped read of *path* on the execution host. Never raises."""
    return _conn().read_text(
        path, head_bytes=head_bytes, tail_bytes=tail_bytes,
        whole_bytes=whole_bytes, timeout=timeout,
    )


def is_file(path: object) -> bool:
    """True when *path* is an existing file on the execution host."""
    return _conn().is_file(path)


def is_dir(path: object) -> bool:
    """True when *path* is a directory on the execution host.

    Cached for ``RACK_FS_DIR_CACHE_TTL_SEC`` (default 30s). Callers use this to
    validate a log directory on request paths that are polled continuously, and
    for a remote target each miss is an SSH round trip. Directory existence is
    stable enough that a short TTL is safe.
    """
    key = str(path)
    now = time.monotonic()
    with _isdir_lock:
        hit = _isdir_cache.get(key)
        if hit is not None and (now - hit[0]) < _dir_cache_ttl():
            return hit[1]
    found = _conn().is_dir(path)
    with _isdir_lock:
        _isdir_cache[key] = (time.monotonic(), found)
    return found


def which(name: str) -> str | None:
    """Absolute path of executable *name* on the execution host, else ``None``.

    Memoised: for a remote target this is an SSH round trip, and callers use it as
    a cheap "is this tool installed?" guard on polled endpoints. The cache is
    dropped by :func:`invalidate_connection`.
    """
    with _which_lock:
        if name in _which_cache:
            return _which_cache[name]
    found = _conn().which(name)
    with _which_lock:
        _which_cache[name] = found
    return found


# ---------------------------------------------------------------------------
# Reachability  –  cheap, cached "is the cluster head node reachable?" probe
# ---------------------------------------------------------------------------

_reach_lock = threading.Lock()
_reach_ok: bool | None = None
_reach_checked_at: float = 0.0  # time.monotonic() of last probe
_reach_wall_at: float = 0.0  # time.time() of last probe (for display)
_reach_detail: str = ""


def _reachability_ttl() -> int:
    try:
        return max(3, int(os.environ.get("RACK_SSH_REACHABILITY_TTL_SEC", "15")))
    except (TypeError, ValueError):
        return 15


def _probe_reachable() -> tuple[bool, str]:
    """Run a minimal ``ssh … true`` with a short connect timeout."""
    _ensure_init()
    if _CONTROL_SSH is None:
        return False, "No SSH host is configured (set RACK_SSH_HOST)."
    ct = (os.environ.get("RACK_SSH_REACHABILITY_CONNECT_TIMEOUT") or "5").strip() or "5"
    try:
        argv = _CONTROL_SSH.ssh_argv(connect_timeout=ct) + ["true"]
    except RuntimeError as e:
        return False, str(e)
    try:
        budget = max(5, int(ct) + 3)
    except (TypeError, ValueError):
        budget = 8
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=budget, check=False
        )
    except subprocess.TimeoutExpired:
        return False, "ssh reachability probe timed out"
    except OSError as e:
        return False, str(e)
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or "").strip().splitlines()
    return False, (detail[-1][:300] if detail else f"ssh exit {proc.returncode}")


def reachability_state(force: bool = False) -> tuple[bool, str]:
    """Return ``(reachable, detail)``; cached for ``RACK_SSH_REACHABILITY_TTL_SEC``.

    Always ``(True, "")`` for a local target. For an SSH target a fresh cached
    result is returned without re-probing unless *force* is set.
    """
    global _reach_ok, _reach_checked_at, _reach_wall_at, _reach_detail
    _ensure_init()
    if _CONTROL_SSH is None:
        return True, ""
    now = time.monotonic()
    with _reach_lock:
        fresh = _reach_ok is not None and (now - _reach_checked_at) < _reachability_ttl()
        if not force and fresh:
            return bool(_reach_ok), _reach_detail
    ok, detail = _probe_reachable()
    with _reach_lock:
        _reach_ok = ok
        _reach_checked_at = time.monotonic()
        _reach_wall_at = time.time()
        _reach_detail = detail
    return ok, detail


def cached_reachability_state() -> tuple[bool | None, str]:
    """Return last-known reachability **without** running ``ssh … true``.

    Local mode always ``(True, "")``. SSH mode returns ``(None, "")`` when no
    probe has completed yet; otherwise the cached ``(ok, detail)`` even when the
    TTL has expired. Callers that must never block on SSH (e.g. base Overview)
    should use this instead of :func:`reachability_state`.
    """
    _ensure_init()
    if _EXEC_MODE != "ssh":
        return True, ""
    with _reach_lock:
        if _reach_ok is None:
            return None, ""
        return bool(_reach_ok), _reach_detail


def cluster_reachable(force: bool = False) -> bool:
    """Convenience boolean wrapper around :func:`reachability_state`."""
    ok, _ = reachability_state(force=force)
    return ok


def invalidate_reachability() -> None:
    """Drop the cached reachability result so the next check re-probes immediately."""
    global _reach_checked_at, _reach_ok
    with _reach_lock:
        _reach_checked_at = 0.0
        _reach_ok = None


def reachability_info(force: bool = False) -> dict[str, object]:
    """Structured reachability snapshot for the ``/api/cluster-health`` endpoint."""
    _ensure_init()
    if _CONTROL_SSH is None:
        return {"mode": "local", "reachable": True, "detail": "", "host": None, "checked_at": None}
    ok, detail = reachability_state(force=force)
    with _reach_lock:
        wall = _reach_wall_at
    host = _CONFIG.control.host if (_CONFIG and _CONFIG.control) else None
    return {
        "mode": "ssh",
        "reachable": ok,
        "detail": detail,
        "host": host or None,
        "checked_at": wall or None,
        "ttl_sec": _reachability_ttl(),
    }


# ---------------------------------------------------------------------------
# run_on_host  –  blocking command execution (replaces subprocess.run)
# ---------------------------------------------------------------------------

def _slurm_module_init_snippet() -> str:
    """Minimal Environment Modules init (no login profile / bash-completion)."""
    custom = (os.environ.get("RACK_MODULES_INIT") or "").strip()
    if custom:
        return f"source {shlex.quote(custom)} 2>/dev/null; "
    return (
        "source /etc/profile.d/lmod.sh 2>/dev/null || "
        "source /usr/share/Modules/init/bash 2>/dev/null || "
        "source /cm/local/apps/environment-modules/current/init/bash 2>/dev/null; "
        "true; "
    )


def run_slurm_on_host(
    shell_cmd: str,
    *,
    timeout: int = 120,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Slurm CLI (*shell_cmd* often ``module load slurm && squeue``).

    Uses ``bash -c`` with a minimal modules init instead of ``bash -lc``, so BCM
    ``module load slurm`` does not pull in bash-completion (the common
    ``FATAL: Missing required source file`` on head nodes).
    """
    inner = _slurm_module_init_snippet() + shell_cmd
    conn = _conn()
    if conn.kind == "ssh":
        # Preserve the historical remote form: export…; cd <cwd> && bash -c '<inner>'.
        # This is a genuine mechanism difference (the remote side is always a login
        # shell), so it is resolved here at the transport boundary rather than leaked
        # to callers.
        return conn.run(f"bash -c {shlex.quote(inner)}", timeout=timeout, cwd=cwd)
    return conn.run(inner, timeout=timeout, cwd=cwd, env=env, login_shell=False)


def run_on_host(
    cmd: str | list[str],
    *,
    timeout: int = 120,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    login_shell: bool = True,
    profile: str = "control",
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute *cmd* on the host, locally or via SSH.

    Parameters
    ----------
    cmd
        A shell string **or** argv list. For an SSH target an argv list is joined
        with ``shlex.join``.
    timeout
        Seconds before the call times out.
    cwd
        Working directory (local: passed to subprocess; SSH: prepends ``cd``).
    env
        Environment dict (local only; ignored for an SSH target — the remote login
        shell + forwarded env provide the cluster environment).
    env_extra
        Per-call vars exported on the REMOTE side (``export k=v; ...`` prefix).
        Unlike ``env`` these survive the SSH hop, and unlike the ambient
        environment they bypass the FORWARD_ENV_* allowlist — which is what makes
        the launch dialog's "Extra environment variables" field work for keys
        outside the allowlisted prefixes (e.g. ``NCCL_*``).
    """
    return _conn().run(
        cmd, timeout=timeout, cwd=cwd, env=env, login_shell=login_shell, profile=profile,
        env_extra=env_extra,
    )


def popen_on_host(
    cmd: str | list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    profile: str = "control",
    env_extra: dict[str, str] | None = None,
) -> HostProcess:
    """Start a long-running command, returning a :class:`HostProcess`.

    For an SSH target the command runs on the remote host via an SSH channel that
    stays open until ``communicate()`` / ``kill()`` is called.
    """
    return _conn().popen(cmd, cwd=cwd, env=env, profile=profile, env_extra=env_extra)


# ---------------------------------------------------------------------------
# Interactive SSH argv  –  build argv for a PTY session to head node / a node
# ---------------------------------------------------------------------------

def ssh_to_headnode_argv() -> list[str]:
    """Return an argv list to open an interactive SSH session to the head node.

    With an SSH target this connects to the configured head node. With a local
    target it opens a local shell (the app is already on the head node).
    """
    _ensure_init()
    if _CONTROL_SSH is not None:
        return _CONTROL_SSH.interactive_argv()
    return [os.environ.get("SHELL", "/bin/bash"), "-l"]


def ssh_to_node_argv(node: str) -> list[str]:
    """Return an argv list to open an interactive SSH session to *node*.

    With an SSH target the connection jumps through the head node:
        ssh -J headnode user@node
    With a local target the connection is direct:
        ssh user@node
    """
    _ensure_init()
    ctrl = _CONFIG.control if _CONFIG else None
    ssh_user = (ctrl.user if ctrl else "root") or "root"
    if _CONTROL_SSH is not None and ctrl:
        argv = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-p", ctrl.port or "22",
        ]
        if ctrl.key and os.path.isfile(ctrl.key):
            argv.extend(["-i", ctrl.key])
        argv.extend([
            f"{ctrl.user}@{ctrl.host}",
            "TERM=xterm-256color",
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            node,
        ])
        return argv
    return [
        "ssh", "-tt",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        f"{ssh_user}@{node}",
    ]
