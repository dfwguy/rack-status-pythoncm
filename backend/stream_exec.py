# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Streaming a subprocess to the browser, on the right host, from the right cwd.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Long tool runs (workspace installers, sysinfo sweeps, switch audits) stream merged stdout/stderr to
the SPA as they go, because a fifteen-minute install behind a spinner is indistinguishable from a
hang. Extracted from app.py because the tool installers and the node-operational sweeps stream the
same way but now live in different API modules.

Two details that are easy to get wrong and expensive to debug:

* ``privileged=True`` routes to the dedicated root "script" SSH target (``RACK_SCRIPT_SSH_*``). Tools
  needing root + pdsh cannot run as the unprivileged control-plane user; on Boston those are two
  different accounts on two different hosts.
* ``_translate_cwd_for_remote`` maps a dashboard-host cwd onto the cluster filesystem. Callers pass a
  *local* path (usually REPO_ROOT) so the command runs from the repo root locally; over SSH that path
  does not exist on the cluster, so anything at or under REPO_ROOT is rewritten onto
  RACK_REMOTE_BASE_PATH.
"""
from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import host_exec
from config_env import _eget, _subprocess_env
from paths import _REMOTE_BASE, REPO_ROOT

_logger = logging.getLogger(__name__)


def _translate_cwd_for_remote(local_cwd: str) -> str | None:
    """Map a dashboard-host cwd onto the cluster filesystem for SSH execution.

    Streamed tool/validation commands pass a *local* path (usually ``REPO_ROOT``) as
    their cwd so the script runs from the repo root locally. Over SSH that path does not
    exist on the cluster, so map any path at/under ``REPO_ROOT`` onto
    ``RACK_REMOTE_BASE_PATH``. Paths already on the remote filesystem (e.g. a remote log
    dir, or callers that pre-translate to the remote base) are returned unchanged.
    Returns ``None`` when no remote base is configured (run in the remote home).
    """
    if not _REMOTE_BASE:
        return None if local_cwd == str(REPO_ROOT) else local_cwd
    try:
        rel = Path(local_cwd).resolve().relative_to(REPO_ROOT.resolve())
    except (ValueError, OSError):
        return local_cwd
    rel_s = str(rel)
    return _REMOTE_BASE if rel_s == "." else str(PurePosixPath(_REMOTE_BASE) / rel_s)


def _merged_stdout_stream_generator(
    cmd: list[str],
    cwd: str,
    display_cmd: str,
    timeout_sec: int,
    *,
    linebuffer_env_var: str,
    linebuffer_default: str = "1",
    env_extra: dict[str, str] | None = None,
    privileged: bool = False,
) -> Iterator[bytes]:
    """Yield merged stdout/stderr from a subprocess until exit (or timeout).

    ``privileged=True`` routes the command to the dedicated root "script" SSH
    target (``RACK_SCRIPT_SSH_*``) when configured, for node-operational tools
    (sysinfo, switch audits) that require root + ``pdsh`` and therefore cannot run
    as the unprivileged control-plane user on the head node.
    """
    cmd_run = list(cmd)
    # Transport-boundary code: this builds a streaming Popen by hand out of
    # host_exec.wrap_for_host(), so unlike ordinary callers it does have to know the
    # mechanism (whether cwd/env apply locally or travel inside the SSH command).
    # It should eventually move behind a streaming Connection API; until then the
    # dependency is explicit rather than hidden behind a predicate.
    remote = host_exec.exec_kind() == "ssh"
    linebuffer_on = (_eget(linebuffer_env_var, linebuffer_default) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # Force line-buffering at the source so the UI gets a steady stream instead of
    # block-buffered clumps. Over SSH there is no TTY, so most programs default to
    # full block buffering on their stdout pipe. `stdbuf -oL -eL` sets glibc stdio to
    # line-buffered for the whole process tree (via LD_PRELOAD/_STDBUF_*), which fixes
    # shell/curl/C tools. coreutils `stdbuf` is standard on the cluster nodes; for local
    # we can verify it exists first.
    if linebuffer_on:
        stdbuf = host_exec.which("stdbuf")
        if stdbuf:
            cmd_run = [stdbuf, "-oL", "-eL", *cmd_run]
    # Python keeps its OWN block buffer on a non-TTY stdout that stdbuf cannot change, so
    # force unbuffered Python explicitly (the common cause of "logs only update in bursts"
    # for Python tools like llmb-install). Harmless for non-Python commands. Forwarded over
    # SSH via env_extra; merged into the subprocess env locally.
    env_extra = dict(env_extra or {})
    env_extra.setdefault("PYTHONUNBUFFERED", "1")
    remote_cwd = _translate_cwd_for_remote(cwd) if remote else None
    cmd_run = host_exec.wrap_for_host(
        cmd_run,
        cwd=remote_cwd,
        profile="script" if privileged else "control",
        env_extra=env_extra,
    )
    env = None if remote else _subprocess_env(env_extra)
    proc = subprocess.Popen(
        cmd_run,
        cwd=None if remote else cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=0,
    )
    outq: queue.Queue[bytes | None] = queue.Queue()

    def feeder() -> None:
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                block = stdout.read(8192)
                if not block:
                    break
                outq.put(block)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            outq.put(None)

    threading.Thread(target=feeder, daemon=True).start()
    header = f"# {display_cmd}\n# cwd: {(remote_cwd if remote else cwd) or '(remote home)'}\n\n"
    yield header.encode("utf-8")
    deadline = time.monotonic() + timeout_sec
    rc: int | None = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if proc.poll() is None:
                    proc.kill()
                yield b"\n*** timed out ***\n"
                try:
                    rc = proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    rc = -1
                yield f"\n--- exit {rc} ---\n".encode()
                return
            try:
                chunk = outq.get(timeout=min(0.5, max(0.05, remaining)))
            except queue.Empty:
                continue
            if chunk is None:
                break
            yield chunk
        try:
            rc = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            rc = proc.poll()
        yield f"\n--- exit {rc if rc is not None else '?'} ---\n".encode()
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass


_STREAM_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Accel-Buffering": "no",
}
