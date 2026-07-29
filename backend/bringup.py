# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Per-node bring-up results from Travis's automation tree (``/home/cmods/bringup``).

The automation runs continuously on the cluster (BCM triggers -> ``launch_job.sh``)
and writes per-node results under ``results/<node>/``. Rather than re-implement the
pass/fail logic (which lives in his ``parsers/<set>/verdict.sh`` and drifts as floors
are tuned), this module shells out to his own read-only reporting scripts on the head
node so verdicts stay authoritative:

* ``daily-report.sh -c`` -- cluster-wide CSV, ``node,slurm,<job columns...>``. Each
  job cell is the extracted **metric** (busbw GB/s, min GB/s, triad MB/s, gflops) when
  the run produced one, otherwise a **status word** (NOT_RUN/INCOMPLETE/RUNNING). It
  does NOT encode pass/fail for a numeric cell -- a FAIL keeps its number.
* ``status-check.sh <node>`` -- scoped, authoritative per-job **status + reason**
  (DONE/FAIL/RETRY/INCOMPLETE/RUNNING/NOT_RUN).

``node_summary()`` merges the two: status/reason from status-check, metric from
daily-report. The cluster-wide daily-report is cached briefly because it scans every
result directory and re-judges each on every call.
"""
from __future__ import annotations

import os
import re
import threading
import time

import host_exec

DEFAULT_HOME = "/home/cmods/bringup"

#: Metric label per job, mirroring ``daily-report.sh``'s ``metric_label()``. Jobs not
#: listed here (dcgm, memtester, stressapptest, mnubergemm) report a status only.
METRIC_LABELS: dict[str, str] = {
    "hpl": "gflops",
    "allreduce": "busbw GB/s",
    "alltoall": "busbw GB/s",
    "allreduce-nonvlink": "busbw GB/s",
    "alltoall-nonvlink": "busbw GB/s",
    "allreduce-forcesharp": "busbw GB/s",
    "nvbandwidth": "min GB/s",
    "stream": "triad MB/s",
    "fio": "bw",
}

_STATUS_WORDS = ("DONE", "FAIL", "RETRY", "INCOMPLETE", "RUNNING", "NOT_RUN")
# A status-check line is ``[<node>: ]<col>: <STATUS>[ -- <reason>]``. dshbak -c (when
# present) strips the ``<node>: `` prefix and groups; the ungrouped fallback keeps it.
# Either way the trailing ``<col>: <STATUS>`` is what we key on.
_STATUS_LINE = re.compile(
    r"(?:^|:\s)"
    r"(?P<col>[A-Za-z0-9][\w.-]*)\s*:\s*"
    r"(?P<status>" + "|".join(_STATUS_WORDS) + r")"
    r"(?:\s*--\s*(?P<reason>.*\S))?\s*$"
)
_NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")

_cache_lock = threading.Lock()
_daily_cache: dict[str, tuple[float, str]] = {}


def bringup_home() -> str:
    """Deployment root of the bring-up tree on the execution host."""
    return (os.environ.get("RACK_BRINGUP_HOME") or DEFAULT_HOME).strip().rstrip("/") or DEFAULT_HOME


def is_enabled() -> bool:
    """False when ``RACK_BRINGUP_DISABLED`` is truthy."""
    return os.environ.get("RACK_BRINGUP_DISABLED", "").strip().lower() not in ("1", "true", "yes", "on")


def _cache_ttl() -> int:
    try:
        return max(0, int(os.environ.get("RACK_BRINGUP_CACHE_TTL_SEC", "30")))
    except ValueError:
        return 30


def _slurm_env_extra() -> dict[str, str]:
    """Forward ``SLURM_CONF`` so the scripts' bare ``sinfo`` resolves the controller.

    The scripts also try ``module load slurm`` themselves; this is belt-and-suspenders
    for a non-login SSH invocation where the module hook may not fire. Values are
    exported on the remote side by ``run_on_host(env_extra=...)``.
    """
    conf = (os.environ.get("RACK_SLURM_CONF") or "").strip()
    return {"SLURM_CONF": conf} if conf else {}


def _base_job(col: str) -> str:
    """Strip a ``-runN`` / ``_runN`` suffix to the base job name."""
    return re.sub(r"[-_]run\d+$", "", col)


def parse_daily_report_csv(text: str) -> tuple[list[str], dict[str, dict[str, object]]]:
    """Parse ``daily-report.sh -c`` stdout.

    Returns ``(columns, rows)`` where ``columns`` is the ordered job columns (``node``
    and ``slurm`` removed) and ``rows`` maps a node name to
    ``{"slurm": <state>, "cells": {col: cell}}``.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return [], {}
    header = [h.strip() for h in lines[0].split(",")]
    if not header or header[0] != "node":
        return [], {}
    has_slurm = len(header) > 1 and header[1] == "slurm"
    first_job = 2 if has_slurm else 1
    columns = header[first_job:]
    rows: dict[str, dict[str, object]] = {}
    for ln in lines[1:]:
        parts = ln.split(",")
        if not parts or not parts[0].strip():
            continue
        node = parts[0].strip()
        slurm = parts[1].strip() if has_slurm and len(parts) > 1 else ""
        cells: dict[str, str] = {}
        for i, col in enumerate(columns):
            idx = first_job + i
            cells[col] = parts[idx].strip() if idx < len(parts) else ""
        rows[node] = {"slurm": slurm, "cells": cells}
    return columns, rows


def parse_status_check(text: str) -> dict[str, dict[str, str]]:
    """Parse ``status-check.sh <node>`` stdout into ``{col: {status, reason}}``.

    Handles both the dshbak-grouped form (``col: STATUS -- reason``) and the ungrouped
    fallback (``node: col: STATUS -- reason``); only the trailing ``col: STATUS`` is
    keyed on, so a single-node scope collapses to one entry per column.
    """
    out: dict[str, dict[str, str]] = {}
    for ln in (text or "").splitlines():
        m = _STATUS_LINE.search(ln.strip())
        if not m:
            continue
        out[m.group("col")] = {
            "status": m.group("status"),
            "reason": (m.group("reason") or "").strip(),
        }
    return out


def run_daily_report(*, timeout: int = 150) -> str:
    """Run (and briefly cache) ``daily-report.sh -c`` on the head node."""
    home = bringup_home()
    ttl = _cache_ttl()
    now = time.time()
    with _cache_lock:
        hit = _daily_cache.get(home)
        if hit and ttl and (now - hit[0]) < ttl:
            return hit[1]
    proc = host_exec.run_on_host(
        ["bash", f"{home}/daily-report.sh", "-c"],
        cwd=home,
        timeout=timeout,
        env_extra=_slurm_env_extra(),
    )
    out = proc.stdout or ""
    if proc.returncode != 0 and not out.strip():
        raise RuntimeError((proc.stderr or "").strip() or f"daily-report.sh exited {proc.returncode}")
    with _cache_lock:
        _daily_cache[home] = (time.time(), out)
    return out


def run_status_check(node: str, *, timeout: int = 90) -> str:
    """Run ``status-check.sh <node>`` (scoped to one node) on the head node."""
    home = bringup_home()
    proc = host_exec.run_on_host(
        ["bash", f"{home}/status-check.sh", node],
        cwd=home,
        timeout=timeout,
        env_extra=_slurm_env_extra(),
    )
    return proc.stdout or ""


def _lookup_status(sc: dict[str, dict[str, str]], col: str) -> dict[str, str]:
    """Find the status entry for a daily-report column across naming variants."""
    for key in (col, col.replace("_run", "-run"), _base_job(col)):
        if key in sc:
            return sc[key]
    return {"status": "", "reason": ""}


def node_summary(node: str) -> dict[str, object]:
    """Authoritative per-job bring-up summary for one node.

    ``jobs`` carries, per active benchmark column: ``name``, ``base`` job, ``status``
    (+ ``reason``), and ``metric`` / ``metric_label`` when the run produced a number.
    """
    node = (node or "").strip()
    daily = run_daily_report()
    columns, rows = parse_daily_report_csv(daily)
    row = rows.get(node)
    if row is None:  # case-insensitive fallback
        low = node.lower()
        for k, v in rows.items():
            if k.lower() == low:
                node, row = k, v
                break
    sc = parse_status_check(run_status_check(node))

    # Column order: daily-report header (authoritative active set); if the node was
    # absent there, fall back to whatever status-check reported.
    cols = columns or list(sc.keys())
    jobs: list[dict[str, object]] = []
    cells = (row or {}).get("cells", {}) if row else {}
    for col in cols:
        base = _base_job(col)
        cell = str(cells.get(col, "")) if isinstance(cells, dict) else ""
        st = _lookup_status(sc, col)
        metric = cell if _NUMERIC.match(cell) else None
        jobs.append(
            {
                "name": col,
                "base": base,
                "status": st["status"] or (cell if not metric else ""),
                "reason": st["reason"],
                "metric": metric,
                "metric_label": METRIC_LABELS.get(base),
            }
        )
    return {
        "node": node,
        "present": row is not None or bool(sc),
        "slurm_state": str((row or {}).get("slurm", "")) if row else "",
        "home": bringup_home(),
        "jobs": jobs,
    }


def config() -> dict[str, object]:
    """Lightweight discovery payload for the UI: enabled + whether the tree is present."""
    home = bringup_home()
    present = False
    if is_enabled():
        try:
            present = host_exec.is_dir(f"{home}/results")
        except Exception:  # noqa: BLE001 - discovery must never raise
            present = False
    return {"enabled": is_enabled(), "home": home, "present": present}


def demo_node_summary(node: str) -> dict[str, object]:
    """Synthetic per-node summary for ``RACK_STATUS_DEMO=1``."""
    node = (node or "demo-node").strip()
    jobs = [
        {"name": "dcgm", "base": "dcgm", "status": "DONE", "reason": "", "metric": None, "metric_label": None},
        {"name": "nvbandwidth", "base": "nvbandwidth", "status": "DONE", "reason": "", "metric": "612.4", "metric_label": "min GB/s"},
        {"name": "allreduce", "base": "allreduce", "status": "DONE", "reason": "", "metric": "687.9", "metric_label": "busbw GB/s"},
        {"name": "allreduce-forcesharp", "base": "allreduce-forcesharp", "status": "DONE", "reason": "", "metric": "684.1", "metric_label": "busbw GB/s"},
        {"name": "alltoall", "base": "alltoall", "status": "DONE", "reason": "", "metric": "627.8", "metric_label": "busbw GB/s"},
        {"name": "allreduce-nonvlink", "base": "allreduce-nonvlink", "status": "FAIL", "reason": "below floor (43): 2.28", "metric": "2.28", "metric_label": "busbw GB/s"},
        {"name": "alltoall-nonvlink", "base": "alltoall-nonvlink", "status": "RUNNING", "reason": "job is on the node now, no verdict until it finishes", "metric": None, "metric_label": "busbw GB/s"},
        {"name": "mnubergemm", "base": "mnubergemm", "status": "BURN-IN OFF", "reason": "", "metric": None, "metric_label": None},
    ]
    return {"node": node, "present": True, "slurm_state": "idle", "home": bringup_home(), "jobs": jobs}
