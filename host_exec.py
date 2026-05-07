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

def run_on_host(
    cmd: str | list[str],
    *,
    timeout: int = 120,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
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
        remote_cmd = shell_str
        if cwd:
            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
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
    return subprocess.run(
        ["bash", "-lc", shell_str],
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
        remote_cmd = shell_str
        if cwd:
            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
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
