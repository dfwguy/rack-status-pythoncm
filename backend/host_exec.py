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

import io
import logging
import os
import shlex
import subprocess
import tarfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from transport import (
    DEFAULT_HEAD_BYTES,
    DEFAULT_TAIL_BYTES,
    Connection,
    FileStat,
    NotConfigured,
    Process,
    RoutingConnection,
    SSHConfig,
    SSHExecutor,
    TransportConfig,
    build_connection,
)
from transport.compose import SudoExecutor, make_env_resolver
from transport.factory import FORWARD_ENV_NAMES, FORWARD_ENV_PREFIXES
from transport.fanout import NodeResult, fanout_via
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


#: How much of a copy bundle rides in one `write_bytes`. The cap is `MAX_ARG_STRLEN` (a single argv
#: element, ~128 KiB), not `ARG_MAX`, because `write_bytes` puts the base64 payload inline in one
#: `sh -c` -- stdin is not plumbed through a hop chain. 64 KiB raw is ~88 KiB of base64, under the
#: cap with room for the quoting each extra hop adds. The same reasoning script_deploy chose.
_COPY_CHUNK_BYTES = 64 * 1024


def copy_tree_to_host(
    local_dir: object,
    remote_dir: str,
    *,
    role: str = DEFAULT_ROLE,
    conn: Connection | None = None,
    timeout: int = 1800,
    skip_suffixes: tuple[str, ...] = (".pyc",),
) -> str | None:
    """Copy a local directory tree to *remote_dir* on the role's host. ``None`` on success, else error.

    Streams as a chunked gzipped-tar bundle -- the one shape that crosses a *multi-hop* chain,
    because stdin is not plumbed through the hops (rsync needs a single hop, and a Teleport-fronted
    head is two). The mechanism is the script-deploy bundle push generalised: numbered parts under a
    staging dir, reassembled and extracted in one remote command so the chain is crossed once more
    rather than per file, and the staging dir still existing afterwards is treated as failure however
    the exit code read -- "up is not working" applied to a copy.

    On size: each part rides inline in one ``sh -c``, so the cost is one round trip per 64 KiB. That
    is nothing for config and scripts and slow for a multi-hundred-MB payload over a slow chain; when
    that matters the right fix is plumbing stdin through the chain, not a bigger inline chunk.
    """
    local = Path(str(local_dir))
    if not local.is_dir():
        return f"not a directory: {local}"

    buf = io.BytesIO()
    sent = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(local.rglob("*")):
            if path.is_dir() or path.suffix in skip_suffixes or "__pycache__" in path.parts:
                continue
            tar.add(str(path), arcname=str(path.relative_to(local)))
            sent += 1
    bundle = buf.getvalue()

    # An explicit connection targets a host the roles do not name (the P2P app server); otherwise
    # the role's configured connection.
    conn = conn if conn is not None else connection(role)
    # Prove the target runs a command before writing a byte: with no SSH host configured write_bytes
    # and makedirs still succeed against the LOCAL filesystem while run fails, so writing first would
    # scatter parts into the app's own tree and only then discover it cannot extract them.
    probe = conn.run(["sh", "-c", "true"], timeout=30)
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip()
        return f"target not reachable: {detail[:200]}"

    staging = f"{remote_dir.rstrip('/')}/.nvis-p2p-stage-{uuid.uuid4().hex[:8]}"
    try:
        err = conn.makedirs(staging)
        if err:
            return f"cannot create {staging}: {err}"
        for index in range(0, len(bundle), _COPY_CHUNK_BYTES):
            part = f"{staging}/part-{index // _COPY_CHUNK_BYTES:04d}"
            err = conn.write_bytes(part, bundle[index:index + _COPY_CHUNK_BYTES])
            if err:
                return f"upload failed at {part}: {err}"
        q, t = shlex.quote(staging), shlex.quote(remote_dir)
        script = (
            f"set -e; cd {q}; cat part-* > bundle.tgz; mkdir -p {t}; "
            f"tar -xzf bundle.tgz -C {t}; cd /; rm -rf {q}"
        )
        proc = conn.run(["sh", "-c", script], timeout=timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return f"extract failed: {detail[:400]}"
        if conn.is_dir(staging):
            return f"extract reported success but {staging} still exists; nothing landed"
    except Exception as exc:  # noqa: BLE001 - the transport raises several unrelated types
        return f"{type(exc).__name__}: {exc}"[:400]
    finally:
        try:
            if conn.is_dir(staging):
                conn.run(["sh", "-c", f"rm -rf {shlex.quote(staging)}"], timeout=60)
        except Exception:
            _logger.debug("copy_tree_to_host: staging cleanup ignored", exc_info=True)
    _logger.info("copy_tree_to_host: sent %d file(s) to %s", sent, remote_dir)
    return None


def execution_host_configured(role: str = DEFAULT_ROLE) -> bool:
    """Is there anywhere to execute for *role*?

    A configuration question, answered without touching the network — distinct from
    :func:`reachability_state`, which probes. Callers that want to *verify* something on the
    execution host need this first: on a deployment with no head node there is nothing to
    verify, and treating that as a verification failure blames the wrong thing.
    """
    _ensure_init()
    try:
        conn = target(role)
    except NotConfigured:
        return False
    # Walk the decorator chain rather than reading the outer executor: env forwarding and sudo
    # are wrappers that delegate through `.inner`, so `ssh_argv` is never on the object
    # `Connection.executor` hands back. Reading only the outer one reported every SSH
    # deployment as "configured" — which is how this helper was wrong the first time.
    executor = getattr(conn, "executor", None)
    argv_builder = None
    seen = 0
    while executor is not None and seen < 10:  # bounded: a cycle must not hang a polled path
        argv_builder = getattr(executor, "ssh_argv", None)
        if argv_builder is not None:
            break
        executor = getattr(executor, "inner", None)
        seen += 1
    if argv_builder is None:
        return True  # a local target always has somewhere to run
    try:
        argv_builder()
    except NotConfigured:
        return False
    return True


def _probe_or_false(what: str, probe) -> bool:
    """Run a filesystem predicate, answering ``False`` when there is no host to ask.

    The predicates below are used as guards on continuously-polled endpoints, so an
    unconfigured deployment used to turn each one into an unhandled ``RuntimeError`` — 65 call
    sites across 20 modules, every one a 500. ``_probe_reachable`` already treats this exact
    condition as a reportable state rather than an error; this makes the predicates agree with
    it, which is what lets ``/api/status`` answer 200 with ``cluster_reachable: false`` and let
    the dashboard render the reason.

    Scoped to :class:`NotConfigured`. A genuine SSH failure still raises, because "the head
    node refused us" is a real fault that must not be flattened into "the file is absent".
    """
    try:
        return bool(probe())
    except NotConfigured as e:
        _logger.debug("%s: no execution host configured (%s)", what, e)
        return False


def is_file(path: object, *, role: str = DEFAULT_ROLE) -> bool:
    """True when *path* is an existing file on the role's host.

    ``False`` when nothing is configured to ask — see :func:`_probe_or_false`.
    """
    return _probe_or_false("is_file", lambda: target(role).is_file(path))


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
    found = _probe_or_false("is_dir", lambda: target(role).is_dir(path))
    with _isdir_lock:
        _isdir_cache[key] = (time.monotonic(), found)
    return found


def which(name: str, *, role: str = DEFAULT_ROLE) -> str | None:
    """Absolute path of executable *name* on the role's host, else ``None``.

    Memoised: for a remote target this is an SSH round trip, and callers use it as
    a cheap "is this tool installed?" guard on polled endpoints. The cache is
    dropped by :func:`invalidate_connection`.

    ``None`` when nothing is configured to ask, for the same reason as the predicates above:
    a guard that raises turns an unconfigured deployment into a 500 on every polled endpoint.
    """
    key = (role, name)
    with _which_lock:
        if key in _which_cache:
            return _which_cache[key]
    try:
        found = target(role).which(name)
    except NotConfigured as e:
        _logger.debug("which(%s): no execution host configured (%s)", name, e)
        found = None
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


def run_pdsh_job(
    cmd: list[str],
    *,
    pdsh_w: str,
    timeout: int,
    dry_run: bool = False,
    dry_run_label: str = "",
) -> dict:
    """Run an already-built pdsh argv and shape the outcome the fan-out endpoints report.

    The endpoints differ in what they ask pdsh to do -- collect nvidia-bug-report.sh, restart
    munge/slurmd/nvidia-imex -- but not in what happens around it, which is the part worth having
    once: render the argv for the operator, honour the dry run, and turn the two failures that are
    not the command's fault into an answer rather than a traceback.

    Takes a BUILT argv rather than a rack and an inner command, deliberately. Composing it here
    would mean importing ``platforms`` into the transport layer, and ``platforms.bcm`` runs its
    cmsh through this module -- so the convenience would buy an import cycle. The caller owns
    ``plat.pdsh_cmd(...)``; this owns everything after it.

    A non-zero return code is DATA, not an error: pdsh exits non-zero when any single node fails,
    and a 71-of-72 result is something the operator needs to see rather than a 500.
    """
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry_run:
        note = f"dry-run ({dry_run_label})" if dry_run_label else "dry-run"
        return {
            "returncode": 0,
            "stdout": f"{note}\n{cmd_display}",
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": pdsh_w,
        }
    try:
        proc = run_on_host(cmd, timeout=timeout)
    except FileNotFoundError:
        # `from None` on purpose: the caller wants "pdsh is not installed on the head node", not a
        # chained ENOENT naming a path that means nothing to an operator.
        raise OSError("pdsh executable not found") from None
    except subprocess.TimeoutExpired:
        # Deliberately not re-raised. A wedged fan-out still has to return the command text, or the
        # UI shows a failure with no way to tell what was attempted.
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": pdsh_w,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "pdsh_w": pdsh_w,
    }


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


def communicate_or_kill(
    proc: HostProcess, timeout: int, *, grace: int = 15
) -> tuple[str | None, str | None]:
    """Wait for *proc*, killing and reaping it if it overruns *timeout*, then re-raising.

    Callers that hold a process open (rather than using :func:`run_on_host`'s internal deadline) all
    need the same three steps on overrun: kill it, reap it so the SSH channel closes, and report a
    timeout. Skipping the reap leaves the channel open, and an SSH channel that is never closed is
    how a ControlMaster session slot is lost -- the head's ``MaxSessions`` is 10, so leaking a few
    silently shrinks every later fan-out.

    Raises ``subprocess.TimeoutExpired`` after cleaning up, so a caller can treat overrun the same
    way whether the deadline was enforced here or inside ``run_on_host``.
    """
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=grace)
        except (subprocess.TimeoutExpired, OSError):
            # Best effort: the kill is what matters, and there is nothing better to do if the
            # already-killed process will not report back either.
            _logger.debug("communicate_or_kill: reap after kill did not complete", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Interactive SSH argv  –  a PTY session to the control host or to a compute node
#
# Both are rendered by the transport chain (``Executor.interactive_argv``), so the hop
# count comes from the declared topology and nothing here counts hops. The per-leg
# timeouts live in ``transport.base.interactive_timeout_opts``.
# ---------------------------------------------------------------------------


def ssh_to_headnode_argv() -> list[str]:
    """Return an argv list to open an interactive SSH session to the control host.

    Composed by the transport chain, so this is one ssh when the head is directly
    reachable, two when it sits behind a bastion, and a local login shell when the app
    already runs on it -- none of which this function needs to know.
    """
    return _conn().select(DEFAULT_ROLE).interactive_argv()


def _on(value: str | None) -> bool:
    # Inline rather than core.truthy_env: core imports this module, so importing it back
    # would close a cycle.
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


def node_sudo_enabled() -> bool:
    """The cluster's "run node commands with sudo" setting (``RACK_NODE_SUDO``)."""
    return _on(os.environ.get("RACK_NODE_SUDO"))


def node_exec_direct() -> bool:
    """The cluster's "reach compute nodes directly from the app server" setting."""
    return _on(os.environ.get("RACK_DIRECT_NODE_SSH"))


def node_connection(node: str, *, sudo: bool = False) -> Connection:
    """A connection to compute *node*, honouring how this deployment reaches nodes.

    Two models, and the difference is a property of the deployment rather than of the code:

    * **relay** (default) -- ``control.hop(node)``, so the node leg authenticates *from* the
      control host with the control host's credentials. This is what works when only the
      head node is trusted by the trays, which is the usual cluster arrangement.
    * **direct** -- the app opens its own ssh to the node. Faster and it consumes none of
      the head's ``MaxSessions`` slots (the ceiling behind the "no more sessions" failures
      and a fleet sweep that silently lost 655 of 2,013 nodes), but it requires the app
      itself to hold a credential the node accepts.

    Selected by ``RACK_DIRECT_NODE_SSH`` (the cluster's ``connection.node_exec_direct``), with
    ``RACK_NODE_SSH_USER`` / ``RACK_NODE_SSH_KEY`` for the direct case. No hostname pattern:
    this used to be gated on one customer's tray regex inside ``Connection.hop``, which meant
    the setting did nothing anywhere else.

    *sudo* runs each command under ``sudo -n -E`` on the node, on either route.
    """
    _ensure_init()
    ctrl = _CONFIG.control if _CONFIG else None
    timeout = (ctrl.connect_timeout if ctrl else "") or "15"
    # Unset means "inherit whoever runs this leg" -- the control host's ssh user on a relay,
    # the app's own account when the app is already on the head. Set it when the node expects
    # a different account than the host launching the leg.
    node_user = (os.environ.get("RACK_NODE_SSH_USER") or "").strip() or None
    if not node_exec_direct():
        return _conn().select(DEFAULT_ROLE).hop(
            node, user=node_user, connect_timeout=timeout, sudo=sudo
        )
    executor = SSHExecutor(SSHConfig(
        host=node,
        # A direct leg has no predecessor to inherit from, so it needs a concrete account: the
        # one named for nodes, else the cluster connection's own (its key is the one used), and
        # root only when neither is set.
        user=node_user or (ctrl.user if ctrl else "") or "root",
        key=(os.environ.get("RACK_NODE_SSH_KEY") or (ctrl.key if ctrl else "") or "").strip(),
        port=(ctrl.port if ctrl else "22") or "22",
        connect_timeout=timeout,
    ))
    return Connection(SudoExecutor(executor) if sudo else executor)


def run_on_nodes(
    nodes: list[str],
    command: str,
    *,
    timeout: int = 600,
    max_workers: int | None = None,
) -> list[NodeResult]:
    """Run shell *command* on every node in *nodes*; one :class:`NodeResult` per node.

    The cluster's settings decide how: straight from the app server or through the head node
    (``node_exec_direct``), and as the connection's account or under ``sudo -n``
    (``node_sudo``). The command always runs as ``bash -c <command>``, so sudo covers all of
    it rather than only its first word. A node that could not be reached is a failed result
    of its own -- never folded into an overall exit code that hides it, which is how a pdsh
    run that reached no node at all used to report success.
    """
    sudo = node_sudo_enabled()
    workers = max_workers or (32 if node_exec_direct() else 8)
    warm = None if node_exec_direct() else _conn().select(DEFAULT_ROLE)
    return fanout_via(
        lambda n: node_connection(n, sudo=sudo),
        list(dict.fromkeys(n for n in nodes if n)),
        ["bash", "-c", command],
        timeout=timeout,
        max_workers=workers,
        warm=warm,
    )


def node_results_summary(results: list[NodeResult], command: str) -> dict:
    """``run_pdsh_job``-shaped dict for per-node results: rc 0 only when every node succeeded.

    Output lines are prefixed ``<node>: `` like pdsh's, so existing parsers and the job panel
    read them unchanged; ``failed_nodes`` names the nodes that did not succeed.
    """
    def _prefixed(attr: str) -> str:
        return "\n".join(
            f"{r.node}: {line}" for r in results for line in (getattr(r, attr) or "").splitlines()
        )

    failed = [r.node for r in results if r.returncode != 0]
    route = "direct" if node_exec_direct() else "via head node"
    return {
        "returncode": 0 if results and not failed else 1,
        "stdout": _prefixed("stdout"),
        "stderr": _prefixed("stderr"),
        "command_display": (
            f"[{route}{', sudo -n' if node_sudo_enabled() else ''}] on {len(results)} node(s): {command}"
        ),
        "pdsh_w": ",".join(r.node for r in results),
        "nodes_ok": [r.node for r in results if r.returncode == 0],
        "failed_nodes": failed,
    }


def ssh_to_node_argv(node: str) -> list[str]:
    """Return an argv list to open an interactive SSH session to *node*.

    Shaped by the chain, with one nesting level per leg -- so the app on the head node gives
    ``ssh <node>``, the container beside it gives ``ssh head 'ssh <node>'``, and a laptop
    behind a bastion gives ``ssh -J bastion head 'ssh <node>'``. Nothing here counts legs.
    """
    return node_connection(node).interactive_argv()


def connection_to(
    host: str,
    *,
    user: str = "",
    key: str = "",
    port: str = "",
    connect_timeout: str = "",
) -> Connection:
    """An ad-hoc **direct** connection to an arbitrary *host*, independent of the configured roles.

    For work that targets a host the topology does not name -- the NVIS-P2P app server, which is a
    host in its own right and, on a cluster like NC, not the control connection (that is the BCM
    head). The caller supplies the address and credentials from the P2P settings; blanks fall back
    to the control profile's key/port so a deployment that reaches its app server the same way it
    reaches its head need only name the host. It does **not** relay through the control host -- the
    app server is reached directly, which is the whole reason it is separate from `node_connection`.
    """
    _ensure_init()
    ctrl = _CONFIG.control if _CONFIG else None
    return Connection(SSHExecutor(SSHConfig(
        host=host,
        user=(user or "").strip() or (ctrl.user if ctrl else "") or "root",
        key=(key or "").strip() or (ctrl.key if ctrl else ""),
        port=(port or "").strip() or (ctrl.port if ctrl else "22") or "22",
        connect_timeout=(connect_timeout or "").strip() or (ctrl.connect_timeout if ctrl else "") or "15",
    )))
