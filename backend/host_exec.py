"""Platform-agnostic host command executor.

Routes subprocess calls either locally (``RACK_EXEC_MODE=local``, the default)
or through SSH to a remote head node (``RACK_EXEC_MODE=ssh``).

When running directly on the head node this module is a thin wrapper around
``subprocess``.  Inside a Docker container the SSH path lets the app execute
Slurm / cmsh / pdsh commands on the actual cluster head node without needing
those tools installed in the image.

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
from dataclasses import dataclass, field

_EXEC_MODE: str | None = None
_SSH_HOST: str | None = None
_SSH_USER: str | None = None
_SSH_KEY: str | None = None
_SSH_PORT: str | None = None
_SSH_EXTRA_OPTS: list[str] = []
_init_lock = threading.Lock()

_FORWARD_ENV_PREFIXES = ("RACK_", "NEMO_", "VALIDATION_", "SQUEUE_", "NVIS_", "PDSH_")

# Platform-default knobs (from the catalog's tool_param_defaults) that scripts/sbatch
# consume but which don't carry a forwarded prefix. Forwarded by exact name so the
# active platform's generation reaches remote jobs over SSH.
_FORWARD_ENV_NAMES = ("CUDA_MAJ", "GPU_MODEL_FLAG", "MNNVL_TOPOLOGY", "CORE_TO_HCA_SCRIPT")


def _build_env_exports() -> str:
    """Build shell export statements for env vars that should be forwarded to the remote host."""
    exports: list[str] = []
    for key, val in os.environ.items():
        if any(key.startswith(p) for p in _FORWARD_ENV_PREFIXES) or key in _FORWARD_ENV_NAMES:
            exports.append(f"export {key}={shlex.quote(val)}")
    return "; ".join(exports) + "; " if exports else ""


def _ensure_init() -> None:
    global _EXEC_MODE, _SSH_HOST, _SSH_USER, _SSH_KEY, _SSH_PORT, _SSH_EXTRA_OPTS
    if _EXEC_MODE is not None:
        return
    with _init_lock:
        if _EXEC_MODE is not None:
            return
        _EXEC_MODE = (os.environ.get("RACK_EXEC_MODE") or "local").strip().lower()
        _SSH_HOST = (os.environ.get("RACK_SSH_HOST") or "").strip()
        _SSH_USER = (os.environ.get("RACK_SSH_USER") or "root").strip() or "root"
        _SSH_KEY = (os.environ.get("RACK_SSH_KEY") or "/run/secrets/ssh_key").strip()
        _SSH_PORT = (os.environ.get("RACK_SSH_PORT") or "22").strip() or "22"
        raw_opts = (os.environ.get("RACK_SSH_OPTS") or "").strip()
        _SSH_EXTRA_OPTS = shlex.split(raw_opts) if raw_opts else []


def is_remote() -> bool:
    """Return True when commands are routed over SSH."""
    _ensure_init()
    return _EXEC_MODE == "ssh"


def wrap_for_host(cmd: list[str], *, cwd: str | None = None) -> list[str]:
    """Return *cmd* as-is locally, or wrapped in an SSH argv when remote.

    The returned list is suitable for ``subprocess.Popen(…)``.  Environment
    variables matching the forwarding prefixes are exported on the remote side.
    """
    _ensure_init()
    if _EXEC_MODE != "ssh":
        return list(cmd)
    env_prefix = _build_env_exports()
    shell_str = shlex.join(cmd)
    if cwd:
        shell_str = f"cd {shlex.quote(cwd)} && {shell_str}"
    shell_str = env_prefix + shell_str
    return _ssh_base_argv() + [shell_str]


def _ssh_base_argv() -> list[str]:
    """Build the ``ssh …`` prefix used for both run and popen."""
    _ensure_init()
    if not _SSH_HOST:
        raise RuntimeError(
            "RACK_EXEC_MODE=ssh but RACK_SSH_HOST is not set. "
            "Point it to the head-node hostname or IP."
        )
    argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-p", _SSH_PORT or "22",
    ]
    if _SSH_KEY and os.path.isfile(_SSH_KEY):
        argv.extend(["-i", _SSH_KEY])
    argv.extend(_SSH_EXTRA_OPTS)
    argv.append(f"{_SSH_USER}@{_SSH_HOST}")
    return argv


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
    ``module load slurm`` does not pull in ``slurm_completion.sh`` / bash-completion
    (the common ``FATAL: Missing required source file`` on head nodes).
    """
    _ensure_init()
    inner = _slurm_module_init_snippet() + shell_cmd
    if _EXEC_MODE == "ssh":
        env_prefix = _build_env_exports()
        remote_cmd = f"bash -c {shlex.quote(inner)}"
        remote_cmd = env_prefix + remote_cmd
        if cwd:
            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
        argv = _ssh_base_argv() + [remote_cmd]
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return subprocess.run(
        ["bash", "-c", inner],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )


def run_on_host(
    cmd: str | list[str],
    *,
    timeout: int = 120,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    login_shell: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute *cmd* on the host, locally or via SSH.

    Parameters
    ----------
    cmd
        A shell string **or** argv list.  In SSH mode only the string form
        is sent; an argv list is joined with ``shlex.join``.
    timeout
        Seconds before the call times out.
    cwd
        Working directory (local mode only; SSH mode prepends ``cd <cwd> &&``).
    env
        Environment dict (local mode only; ignored in SSH mode – the remote
        login shell provides the cluster environment).
    """
    _ensure_init()
    shell_str = cmd if isinstance(cmd, str) else shlex.join(cmd)

    if _EXEC_MODE == "ssh":
        env_prefix = _build_env_exports()
        remote_cmd = shell_str
        if cwd:
            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
        remote_cmd = env_prefix + remote_cmd
        argv = _ssh_base_argv() + [remote_cmd]
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    # Local execution
    if isinstance(cmd, list):
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    bash_flag = "-lc" if login_shell else "-c"
    return subprocess.run(
        ["bash", bash_flag, shell_str],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )


# ---------------------------------------------------------------------------
# HostProcess  –  long-running command wrapper (replaces subprocess.Popen)
# ---------------------------------------------------------------------------

@dataclass
class HostProcess:
    """Wraps a ``Popen`` (local) or SSH-tunnelled process so callers get a
    uniform ``communicate()`` / ``kill()`` / ``returncode`` interface."""

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


def popen_on_host(
    cmd: str | list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> HostProcess:
    """Start a long-running command, returning a :class:`HostProcess`.

    In SSH mode the command runs on the remote host via an SSH channel that
    stays open until ``communicate()`` / ``kill()`` is called.
    """
    _ensure_init()
    shell_str = cmd if isinstance(cmd, str) else shlex.join(cmd)

    if _EXEC_MODE == "ssh":
        env_prefix = _build_env_exports()
        remote_cmd = shell_str
        if cwd:
            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
        remote_cmd = env_prefix + remote_cmd
        argv = _ssh_base_argv() + [remote_cmd]
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return HostProcess(_proc=proc)

    # Local execution
    if isinstance(cmd, list):
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    else:
        proc = subprocess.Popen(
            ["bash", "-lc", shell_str],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    return HostProcess(_proc=proc)


# ---------------------------------------------------------------------------
# ssh_to_node_argv  –  build argv for an interactive SSH session to a node
# ---------------------------------------------------------------------------

def ssh_to_headnode_argv() -> list[str]:
    """Return an argv list to open an interactive SSH session to the head node.

    In SSH (remote) mode this connects to the configured head node.
    In local mode this opens a local shell (the app is already on the head node).
    """
    _ensure_init()
    if _EXEC_MODE == "ssh":
        argv = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "SetEnv=TERM=xterm-256color",
            "-p", _SSH_PORT or "22",
        ]
        if _SSH_KEY and os.path.isfile(_SSH_KEY):
            argv.extend(["-i", _SSH_KEY])
        argv.append(f"{_SSH_USER}@{_SSH_HOST}")
        return argv
    return [os.environ.get("SHELL", "/bin/bash"), "-l"]


def ssh_to_node_argv(node: str) -> list[str]:
    """Return an argv list to open an interactive SSH session to *node*.

    In SSH (remote) mode the connection jumps through the head node:
        ssh -J headnode user@node
    In local mode the connection is direct:
        ssh user@node
    """
    _ensure_init()
    ssh_user = _SSH_USER or "root"
    if _EXEC_MODE == "ssh":
        # SSH to head node, then from there SSH to the compute node
        # (compute nodes use munge-based auth from the head node).
        argv = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-p", _SSH_PORT or "22",
        ]
        if _SSH_KEY and os.path.isfile(_SSH_KEY):
            argv.extend(["-i", _SSH_KEY])
        argv.extend([
            f"{_SSH_USER}@{_SSH_HOST}",
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
