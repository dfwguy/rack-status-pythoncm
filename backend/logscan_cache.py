# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Whole-file log scanning and host stat lookups, with the caches that make them affordable.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The client side of ``bin/scripts/nvis_logscan.py``: that script reads a log ONCE on the host that
holds it and returns a few KB of JSON, so a multi-GB log never crosses the SSH hop and no caller
has to guess from a head/tail window.

Extracted from ``app.py`` because these were among the last helpers binding it together -- the
scanner cache and the host stat map are each reachable from about 21 of the routes still there, so
no domain could move out while they stayed. Nothing here imports Flask or ``app``.

Two caches, for two different costs:

* ``_LOGSCAN_CACHE`` keys on path + mtime + size, so an appended log is rescanned and a stable one
  is not. ``_analytics_scan_ctx`` holds the batch the current thread is working through and is
  checked *first*, because the stat-validated key costs an SSH round trip per lookup -- doing that
  once per record turned a 40 s batch into roughly 30 s per file on the real corpus.
* ``_hist_stat_cache`` memoises one directory's ``{path: (mtime, size)}`` for ~20 s. The (mtime,
  size) pair is what makes a history ``run_id`` stable, so it must be the real head-node value and
  never a container-local ``stat``.

These are shared **objects**: importing ``_LOGSCAN_CACHE`` elsewhere binds the same dict, and
``_analytics_scan_ctx`` the same thread-local. Mutate them, never rebind them -- assigning
``_LOGSCAN_CACHE = {}`` in another module would give that module a private dict while the rest of
the app kept using this one, and the thread-local's batch hand-off would silently stop working.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# --- whole-file scanner -----------------------------------------------------------------------
# The scanner client and its caches live in logscan_cache.py, imported under these names above.

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

import host_exec
import native_status
import script_tree
from core import rpath as _rpath

_logger = logging.getLogger(__name__)


# --- whole-file scanner -----------------------------------------------------------------------
# Everything that still takes a byte cap is a fallback for when the scanner is missing.
_LOGSCAN_CACHE: dict[str, dict | None] = {}
_LOGSCAN_CACHE_MAX = 4096

#: Per-thread batch of scanner records for the aggregate currently running on this thread.
#: Thread-local rather than global: two operators can hit Analytics at once, and a batch keyed
#: only by path would let one request answer from the other's corpus.
_analytics_scan_ctx = threading.local()


def _logscan_cache_key(lp: str) -> str:
    """path + mtime + size, so an appended log is rescanned but a stable one is not."""
    try:
        st = host_exec.stat(lp)
        return f"{lp}:{getattr(st, 'mtime', 0)}:{getattr(st, 'size', 0)}"
    except Exception:  # noqa: BLE001 - a stat failure just means a weaker key
        return lp


def _logscan_cache_put(lp: str, rec: dict | None) -> None:
    if len(_LOGSCAN_CACHE) >= _LOGSCAN_CACHE_MAX:
        _LOGSCAN_CACHE.clear()
    _LOGSCAN_CACHE[_logscan_cache_key(lp)] = rec


def _logscan_batch(
    paths: list[str], *, on_progress=None, timeout: int = 1800
) -> dict[str, dict]:
    """Scan many logs in ONE process, calling *on_progress* as each finishes.

    The analytics aggregate used to run ``parser.sh --json`` per log -- a process launch plus
    about twenty subprocesses and sixteen file reads, each time. Measured on Nscale TX, the whole
    1020-file / 1973 MB log directory scans in 39.6 s in a single pass here.

    Records arrive as NDJSON, one per file, so progress is a real per-file event rather than a
    timer pretending to be one. The path list goes over stdin, not argv: 1020 paths is already a
    ~150 KB command line and a directory scan is capped at 4000.
    """
    out: dict[str, dict] = {}
    if not paths:
        return out
    script = script_tree.resolve("nvis_logscan.py")
    total = len(paths)

    # The list goes in a file on the host, not on the command line and not down stdin:
    # popen_on_host does not open a stdin pipe, and 1020 paths is a ~150 KB argv.
    listing = f"/tmp/nvis-logscan-{uuid.uuid4().hex}.txt"
    err = host_exec.write_bytes(listing, ("\n".join(paths) + "\n").encode("utf-8"))
    if err:
        _logger.warning("nvis_logscan batch could not stage its path list: %s", err)
        return out

    try:
        proc = host_exec.popen_on_host(
            f"python3 {shlex.quote(script)} --ndjson --no-inventory "
            f"--paths-from {shlex.quote(listing)}"
        )
    except (OSError, ValueError) as e:
        _logger.warning("nvis_logscan batch could not start: %s", e)
        return out

    # Publish the denominator before the first record: the operator should see "0 of 1020"
    # immediately rather than a spinner until the first file happens to finish.
    if on_progress:
        on_progress(0, total, "")

    try:
        done = 0
        for line in proc.iter_stdout():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = str(rec.get("path") or "")
            if p:
                out[p] = rec
            done += 1
            if on_progress:
                on_progress(done, total, p)
    except (OSError, ValueError) as e:
        _logger.warning("nvis_logscan batch failed after %d/%d: %s", len(out), total, e)
    finally:
        try:
            proc.kill()
        except Exception:  # already finished is the normal case
            _logger.debug("nvis_logscan batch already exited", exc_info=True)
        try:
            host_exec.run_on_host(["rm", "-f", listing], timeout=30)
        except Exception:  # a stray temp file is not worth failing the run
            _logger.debug("could not remove %s", listing, exc_info=True)
    return out


def _logscan_record(path, *, timeout: int = 300) -> dict | None:
    """Whole-file scan of *path* on the exec host, or ``None`` when unavailable.

    Cached per path+mtime: the analytics aggregate asks for the same log from several places.
    """
    lp = str(_rpath(path))

    # The batch this thread is working through, keyed by path and needing no stat. Checked first
    # because the stat-validated key below costs an SSH round trip per lookup: doing that once
    # per record turned a 40 s batch into roughly 30 s PER FILE on the real corpus.
    batch = getattr(_analytics_scan_ctx, "batch", None)
    if batch:
        hit = batch.get(lp)
        if hit is not None:
            return hit

    key = _logscan_cache_key(lp)
    if key in _LOGSCAN_CACHE:
        return _LOGSCAN_CACHE[key]

    rec: dict | None = None
    script = script_tree.resolve("nvis_logscan.py")
    try:
        proc = host_exec.run_on_host(
            ["python3", script, "--ndjson", "--no-inventory", lp], timeout=timeout
        )
        if getattr(proc, "returncode", 1) == 0:
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("{"):
                    cand = json.loads(line)
                    if cand.get("ok"):
                        rec = cand
                    break
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        _logger.debug("nvis_logscan unavailable for %s: %s", lp, e)

    _logscan_cache_put(lp, rec)
    return rec


# --- host-aware stat lookup ----------------------------------------------------------
_HIST_STAT_TTL_SEC = 20.0
_hist_stat_lock = threading.Lock()
_hist_stat_cache: dict[str, tuple[float, dict[str, tuple[float, int]]]] = {}


def _host_log_stat_map(base: str) -> dict[str, tuple[float, int]]:
    """``{abs path: (mtime_epoch, size_bytes)}`` for one directory on the EXEC host.

    ``native_status._host_log_entries`` shells out to ``find`` (over SSH when remote), and
    parser-results is polled by the dashboard, so the answer is memoised for ~20 s. The
    (mtime, size) pair is what makes a history ``run_id`` stable, so it must be the real
    head-node value -- never a container-local ``stat``. Never raises.
    """
    key = str(base).rstrip("/") or "/"
    now = time.time()
    with _hist_stat_lock:
        hit = _hist_stat_cache.get(key)
        if hit is not None and (now - hit[0]) < _HIST_STAT_TTL_SEC:
            return hit[1]
    out: dict[str, tuple[float, int]] = {}
    try:
        for path, mtime_ns, size in native_status._host_log_entries(key):
            out[str(path)] = (int(mtime_ns) / 1_000_000_000.0, int(size))
    except Exception as e:  # noqa: BLE001 - discovery is best-effort
        _logger.debug("host log stat map failed for %s: %s", key, e)
        return {}
    with _hist_stat_lock:
        _hist_stat_cache[key] = (now, out)
    return out


def _log_stat_for_path(log_dir: Path, log_path_s: str) -> tuple[float | None, int | None]:
    """``(mtime_epoch, size_bytes)`` for one log on the exec host, or ``(None, None)``."""
    p = str(log_path_s)
    bases: list[str] = []
    parent = os.path.dirname(p)
    if parent:
        bases.append(parent)
    try:
        root = str(native_status._host_resolve(log_dir)).rstrip("/")
    except Exception:  # noqa: BLE001
        root = ""
    if root and root not in bases:
        bases.append(root)
    for b in bases:
        m = _host_log_stat_map(b)
        hit = m.get(p)
        if hit is not None:
            return hit
    return (None, None)
