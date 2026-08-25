# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
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

import logging
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable

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
from transport.topology import (
    DEFAULT_ROLE,
    SSHDefaults,
    TopologyError,
    build_topology,
    describe_topology,
    role_roots,
)

# HostProcess is retained as a public name for back-compat; it is the transport
# Process wrapper (communicate/kill/poll/returncode).
HostProcess = Process

_logger = logging.getLogger(__name__)

_init_lock = threading.Lock()

# Cached, lazily-built state -------------------------------------------------
_CONN: RoutingConnection | None = None
_CONTROL_SSH: SSHExecutor | None = None  # raw control SSH leaf (reachability/PTY argv)
_CONFIG: TransportConfig | None = None

_ROLE_ROOTS: dict[str, str] = {}
_TOPOLOGY_DESC: dict[str, object] = {}

_which_lock = threading.Lock()
_which_cache: dict[tuple[str, str], str | None] = {}

_isdir_lock = threading.Lock()
_isdir_cache: dict[tuple[str, str], tuple[float, bool]] = {}

# Supplies the declarative topology (endpoints, roles). Injected so this module
# does not import the catalog (and so tests can hand it a topology directly);
# when unset, the catalog's active cluster is consulted lazily.
_topology_provider: Callable[[], tuple[list[dict], list[dict]] | None] | None = None


def set_topology_provider(
    fn: Callable[[], tuple[list[dict], list[dict]] | None] | None,
) -> None:
    """Install the source of ``(endpoints, roles)``; ``None`` restores the default."""
    global _topology_provider
    _topology_provider = fn
    invalidate_connection()


def _catalog_topology() -> tuple[list[dict], list[dict]] | None:
    """``(endpoints, roles)`` from the active cluster, or ``None`` if undeclared."""
    try:
        from catalog import context as catalog_context
    except ImportError:
        return None
    try:
        _project, cluster, _platform = catalog_context.resolve()
    except Exception:
        # A missing or corrupt store must not take execution down: fall back to env.
        _logger.debug("_catalog_topology: ignoring error", exc_info=True)
        return None
    if cluster is None or not cluster.connection.endpoints:
        return None
    conn = cluster.connection
    return (
        [e.__dict__ | {"opts": list(e.opts)} for e in conn.endpoints],
        [r.__dict__ for r in conn.roles],
    )


def _load_topology() -> tuple[list[dict], list[dict]] | None:
    provider = _topology_provider or _catalog_topology
    try:
        return provider()
    except Exception:
        _logger.exception("topology provider failed; falling back to environment")
        return None


def _legacy_role_roots() -> dict[str, str]:
    """Role roots implied by the flat env contract, for deployments without a topology.

    ``RACK_REMOTE_BASE_PATH`` is the single-var precursor of per-role placement: it
    points scripts *and* logs at one tree on one host. Expressing it as role roots
    means call sites can ask for ``role_root("scripts")`` uniformly, whether or not
    the deployment has moved to a declared topology.
    """
    roots: dict[str, str] = {}
    base = (os.environ.get("RACK_REMOTE_BASE_PATH") or "").strip()
    if base:
        roots["scripts"] = f"{base.rstrip('/')}/bin/scripts"
        roots["logs"] = f"{base.rstrip('/')}/var/logs"
    log_dir = (os.environ.get("RACK_STATUS_LOG_DIR") or "").strip()
    if log_dir:
        roots["logs"] = log_dir
    return roots


def _overlay_resolver():
    """The same env-forwarding policy the single-target factory applies."""
    from transport.compose import make_env_resolver
    from transport.factory import FORWARD_ENV_NAMES, FORWARD_ENV_PREFIXES

    return make_env_resolver(FORWARD_ENV_PREFIXES, FORWARD_ENV_NAMES, None)


def _ssh_defaults(cfg: TransportConfig) -> SSHDefaults:
    """Per-endpoint ssh tuning inherited from the existing RACK_SSH_* contract."""
    c = cfg.control
    if c is None:
        return SSHDefaults()
    return SSHDefaults(
        port=c.port,
        connect_timeout=c.connect_timeout,
        alive_interval=c.alive_interval,
        alive_count=c.alive_count,
        multiplex=c.multiplex,
        mux_persist=c.mux_persist,
        extra_opts=list(c.extra_opts),
        key=c.key,
        user=c.user,
    )


def _dir_cache_ttl() -> int:
    try:
        return max(0, int(os.environ.get("RACK_FS_DIR_CACHE_TTL_SEC", "30")))
    except (TypeError, ValueError):
        return 30


def _ensure_init() -> None:
    global _CONN, _CONTROL_SSH, _CONFIG, _ROLE_ROOTS, _TOPOLOGY_DESC
    if _CONN is not None:
        return
    with _init_lock:
        if _CONN is not None:
            return
        cfg = TransportConfig.from_env()
        conn: RoutingConnection | None = None
        roots: dict[str, str] = {}
        desc: dict[str, object] = {}

        # A declared topology wins: it is the only form that can express hop chains
        # and per-role placement. Without one, fall back to the flat single-target
        # env contract so existing deployments are unaffected.
        spec = _load_topology()
        if spec is not None:
            endpoints, roles = spec
            try:
                conn = build_topology(
                    endpoints,
                    roles,
                    resolver=_overlay_resolver(),
                    defaults=_ssh_defaults(cfg),
                    sudo_password=cfg.sudo_password,
                    sudo_preserve_env=cfg.sudo_preserve_env,
                )
                roots = role_roots(roles)
                desc = describe_topology(endpoints, roles)
            except TopologyError:
                _logger.exception(
                    "connection topology is invalid; falling back to the "
                    "RACK_SSH_* environment contract"
                )
                conn = None

        if conn is None:
            # No topology declared: keep the original single-target composition
            # (unchanged semantics for every existing deployment) but still answer
            # role queries, so call sites can be written role-first everywhere.
            conn = build_connection(cfg)
            roots = _legacy_role_roots()
            desc = {}
        _CONFIG = cfg
        _CONN = conn
        _ROLE_ROOTS = roots
        _TOPOLOGY_DESC = desc
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
    global _CONN, _CONTROL_SSH, _CONFIG, _ROLE_ROOTS, _TOPOLOGY_DESC
    with _init_lock:
        _CONN = None
        _CONTROL_SSH = None
        _CONFIG = None
        _ROLE_ROOTS = {}
        _TOPOLOGY_DESC = {}
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

def target(role: str = DEFAULT_ROLE):
    """The :class:`transport.Connection` bound to *role*.

    Roles name a place in the deployment's terms — ``logs``, ``scripts``, ``bcm``,
    or anything a site defines — so call sites never encode which host that is. An
    unknown role falls back to ``control``, which keeps a call site working on a
    deployment that has not split that role out.
    """
    return _conn().select(role)


def role_root(role: str) -> str:
    """Configured base directory for *role*, or ``""`` when it declares none."""
    _ensure_init()
    return _ROLE_ROOTS.get(role, "")


def role_names() -> list[str]:
    """Every role the active topology defines (empty under the legacy env config)."""
    _ensure_init()
    return sorted(_CONN.targets) if isinstance(_CONN, RoutingConnection) else []


def describe_roles() -> dict[str, object]:
    """Per-role endpoint, hop path and root — for diagnostics and the Settings UI."""
    _ensure_init()
    return dict(_TOPOLOGY_DESC)


def resolve(path: object, *, role: str = DEFAULT_ROLE) -> str:
    """Canonical path string for *path* on the host that owns *role*.

    The single path resolver for the whole app. Replaces the several local
    variants that each re-implemented "``.resolve()`` unless remote" and got the
    remote case subtly different.
    """
    return target(role).resolve(path)


def stat(path: object, *, role: str = DEFAULT_ROLE) -> FileStat | None:
    """``FileStat(mtime, size)`` from the role's host, or ``None`` if absent."""
    return target(role).stat(path)


def stat_dir(
    base: object,
    *,
    patterns: tuple[str, ...] | None = None,
    maxdepth: int = 1,
    role: str = DEFAULT_ROLE,
) -> dict[str, FileStat]:
    """``{path: FileStat}`` for regular files under *base* (one round trip)."""
    return target(role).stat_dir(base, patterns=patterns, maxdepth=maxdepth)


def makedirs(path: object, *, role: str = DEFAULT_ROLE) -> str | None:
    """Create *path* on the role's host. ``None`` on success, else an error string."""
    return target(role).makedirs(path)


def write_bytes(path: object, data: bytes, *, role: str = DEFAULT_ROLE) -> str | None:
    """Write *data* to *path* on the role's host, creating parents.

    Lets a handler drop a produced artifact (an export workbook, a report) beside
    the data it came from, without knowing whether that directory is in this
    container or on a host several hops away. ``None`` on success, else an error.
    """
    return target(role).write_bytes(path, data)


def read_text(
    path: object,
    *,
    head_bytes: int = DEFAULT_HEAD_BYTES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    whole_bytes: int | None = None,
    timeout: int = 120,
    role: str = DEFAULT_ROLE,
) -> tuple[str, dict[str, object]]:
    """Head+tail capped read of *path* on the role's host. Never raises."""
    return target(role).read_text(
        path, head_bytes=head_bytes, tail_bytes=tail_bytes,
        whole_bytes=whole_bytes, timeout=timeout,
    )


def is_file(path: object, *, role: str = DEFAULT_ROLE) -> bool:
    """True when *path* is an existing file on the role's host."""
    return target(role).is_file(path)


def is_dir(path: object, *, role: str = DEFAULT_ROLE) -> bool:
    """True when *path* is a directory on the role's host.

    Cached for ``RACK_FS_DIR_CACHE_TTL_SEC`` (default 30s). Callers use this to
    validate a log directory on request paths that are polled continuously, and
    for a remote target each miss is an SSH round trip. Directory existence is
    stable enough that a short TTL is safe. The role is part of the cache key: the
    same path can exist on one role's host and not on another's.
    """
    key = (role, str(path))
    now = time.monotonic()
    with _isdir_lock:
        hit = _isdir_cache.get(key)
        if hit is not None and (now - hit[0]) < _dir_cache_ttl():
            return hit[1]
    found = target(role).is_dir(path)
    with _isdir_lock:
        _isdir_cache[key] = (time.monotonic(), found)
    return found


def which(name: str, *, role: str = DEFAULT_ROLE) -> str | None:
    """Absolute path of executable *name* on the role's host, else ``None``.

    Memoised: for a remote target this is an SSH round trip, and callers use it as
    a cheap "is this tool installed?" guard on polled endpoints. The cache is
    dropped by :func:`invalidate_connection`.
    """
    key = (role, name)
    with _which_lock:
        if key in _which_cache:
            return _which_cache[key]
    found = target(role).which(name)
    with _which_lock:
        _which_cache[key] = found
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
    if _CONTROL_SSH is None:
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
    profile: str = DEFAULT_ROLE,
    role: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute *cmd* on the host that owns the requested role.

    Parameters
    ----------
    cmd
        A shell string **or** argv list. For an SSH target an argv list is joined
        with ``shlex.join``.
    profile, role
        The same thing under two names: which route runs this. ``profile`` is the
        historical spelling (workflow YAML, schedulers, older call sites);
        ``role`` matches the filesystem helpers and reads better for the named
        places a topology declares (``scripts``, ``logs``, ``bcm``). ``role``
        wins when both are given.
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
        cmd, timeout=timeout, cwd=cwd, env=env, login_shell=login_shell,
        profile=role or profile, env_extra=env_extra,
    )


def popen_on_host(
    cmd: str | list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    profile: str = DEFAULT_ROLE,
    role: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> HostProcess:
    """Start a long-running command, returning a :class:`HostProcess`.

    For an SSH target the command runs on the remote host via an SSH channel that
    stays open until ``communicate()`` / ``kill()`` is called. ``role`` is the
    preferred spelling of ``profile`` (see :func:`run_on_host`).
    """
    return _conn().popen(cmd, cwd=cwd, env=env, profile=role or profile, env_extra=env_extra)


# ---------------------------------------------------------------------------
# Interactive SSH argv  –  build argv for a PTY session to head node / a node
# ---------------------------------------------------------------------------

#: Timeouts applied to every hop of an interactive terminal session.
#:
#: An interactive shell has no deadline of its own, so without these a hop that never answers hangs
#: forever with a blank terminal and no diagnosis — which is exactly how an unreachable compute tray
#: presented on Nscale TX. `ConnectTimeout` bounds the TCP/auth phase; the keepalives detect a peer
#: that accepted the connection and then stopped responding, so the session ends with ssh's own
#: error on the PTY instead of silence.
#:
#: Deliberately not `BatchMode=yes`: a terminal is the one place where being prompted for a password
#: is legitimate, and disabling that would break a valid way to reach a node.
_INTERACTIVE_TIMEOUT_OPTS: tuple[str, ...] = (
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
)


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
            *_INTERACTIVE_TIMEOUT_OPTS,
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
            # The second hop needs its own timeouts. Without them an unreachable or wedged tray
            # leaves the operator staring at an empty terminal indefinitely: the outer hop is
            # healthy, so nothing times out and nothing is ever printed.
            *_INTERACTIVE_TIMEOUT_OPTS,
            node,
        ])
        return argv
    return [
        "ssh", "-tt",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        *_INTERACTIVE_TIMEOUT_OPTS,
        f"{ssh_user}@{node}",
    ]
