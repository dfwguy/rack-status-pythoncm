"""Platform-agnostic host command executor (compatibility facade).

This module is now a thin facade over the composable :mod:`transport` layer.
It preserves the historical function surface (``run_on_host`` / ``popen_on_host``
/ ``run_slurm_on_host`` / ``wrap_for_host`` / reachability / interactive SSH
argv) so existing call sites keep working, while delegating actual execution to
a :class:`transport.RoutingConnection` composed of Local/SSH leaves and an
env-forwarding decorator.

Routes subprocess calls either locally (``RACK_EXEC_MODE=local``, the default)
or through SSH to a remote head node (``RACK_EXEC_MODE=ssh``). A privileged
``profile="script"`` target (``RACK_SCRIPT_SSH_*``) is exposed as a second route.

Environment variables
---------------------
``RACK_EXEC_MODE``   ``local`` (default) or ``ssh``
``RACK_SSH_HOST``    head-node hostname / IP  (required when mode is ``ssh``)
``RACK_SSH_USER``    SSH user (default ``root``)
``RACK_SSH_KEY``     path to SSH private key  (default ``/run/secrets/ssh_key``)
``RACK_SSH_PORT``    SSH port (default ``22``)
``RACK_SSH_OPTS``    extra ``ssh`` flags (space-separated)
"""
from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time

from transport import (  # noqa: F401  (Process re-exported as HostProcess)
    FORWARD_ENV_NAMES,
    FORWARD_ENV_PREFIXES,
    Process,
    RoutingConnection,
    SSHConnection,
    TransportConfig,
    build_connection,
)

# HostProcess is retained as a public name for back-compat; it is the transport
# Process wrapper (communicate/kill/poll/returncode).
HostProcess = Process

_init_lock = threading.Lock()

# Cached, lazily-built state -------------------------------------------------
_CONN: RoutingConnection | None = None
_CONTROL_SSH: SSHConnection | None = None  # raw control SSH leaf (reachability/PTY argv)
_CONFIG: TransportConfig | None = None
_EXEC_MODE: str | None = None


def _ensure_init() -> None:
    global _CONN, _CONTROL_SSH, _CONFIG, _EXEC_MODE
    if _CONN is not None:
        return
    with _init_lock:
        if _CONN is not None:
            return
        cfg = TransportConfig.from_env()
        conn = build_connection(cfg)
        _CONFIG = cfg
        _EXEC_MODE = cfg.exec_mode
        _CONN = conn
        # Keep a handle to the raw control SSH leaf for reachability probes and
        # interactive PTY argv (which need ssh_argv/interactive_argv, not run()).
        if cfg.exec_mode == "ssh":
            _CONTROL_SSH = SSHConnection(cfg.control) if cfg.control else None
        else:
            _CONTROL_SSH = None


def _conn() -> RoutingConnection:
    _ensure_init()
    assert _CONN is not None
    return _CONN


def is_remote() -> bool:
    """Return True when commands are routed over SSH."""
    _ensure_init()
    return _EXEC_MODE == "ssh"


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
        return False, "RACK_EXEC_MODE=ssh but RACK_SSH_HOST is not set."
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
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=budget)
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

    Always ``(True, "")`` in local mode. In SSH mode a fresh cached result is
    returned without re-probing unless *force* is set.
    """
    global _reach_ok, _reach_checked_at, _reach_wall_at, _reach_detail
    _ensure_init()
    if _EXEC_MODE != "ssh":
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
    if _EXEC_MODE != "ssh":
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
    if _EXEC_MODE == "ssh":
        # Preserve the historical remote form: export…; cd <cwd> && bash -c '<inner>'.
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
) -> subprocess.CompletedProcess[str]:
    """Execute *cmd* on the host, locally or via SSH.

    Parameters
    ----------
    cmd
        A shell string **or** argv list. In SSH mode an argv list is joined with
        ``shlex.join``.
    timeout
        Seconds before the call times out.
    cwd
        Working directory (local: passed to subprocess; SSH: prepends ``cd``).
    env
        Environment dict (local only; ignored in SSH mode — the remote login
        shell + forwarded env provide the cluster environment).
    """
    return _conn().run(
        cmd, timeout=timeout, cwd=cwd, env=env, login_shell=login_shell, profile=profile
    )


def popen_on_host(
    cmd: str | list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    profile: str = "control",
) -> HostProcess:
    """Start a long-running command, returning a :class:`HostProcess`.

    In SSH mode the command runs on the remote host via an SSH channel that
    stays open until ``communicate()`` / ``kill()`` is called.
    """
    return _conn().popen(cmd, cwd=cwd, env=env, profile=profile)


# ---------------------------------------------------------------------------
# Interactive SSH argv  –  build argv for a PTY session to head node / a node
# ---------------------------------------------------------------------------

def ssh_to_headnode_argv() -> list[str]:
    """Return an argv list to open an interactive SSH session to the head node.

    In SSH (remote) mode this connects to the configured head node. In local
    mode this opens a local shell (the app is already on the head node).
    """
    _ensure_init()
    if _EXEC_MODE == "ssh" and _CONTROL_SSH is not None:
        return _CONTROL_SSH.interactive_argv()
    return [os.environ.get("SHELL", "/bin/bash"), "-l"]


def ssh_to_node_argv(node: str) -> list[str]:
    """Return an argv list to open an interactive SSH session to *node*.

    In SSH (remote) mode the connection jumps through the head node:
        ssh -J headnode user@node
    In local mode the connection is direct:
        ssh user@node
    """
    _ensure_init()
    ctrl = _CONFIG.control if _CONFIG else None
    ssh_user = (ctrl.user if ctrl else "root") or "root"
    if _EXEC_MODE == "ssh" and ctrl:
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
