# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
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

import logging
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
_DECORATION_ONLY_NODE = re.compile(r"^[*=_-]+$")

_cache_lock = threading.Lock()
_daily_cache: dict[str, tuple[float, str]] = {}
_fleet_cache_condition = threading.Condition()
_fleet_cache: dict[str, tuple[float, dict[str, object]]] = {}
_fleet_refreshing: set[str] = set()
_fleet_errors: dict[str, Exception] = {}
_logger = logging.getLogger(__name__)


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


def _fleet_cache_ttl() -> int:
    try:
        return max(0, int(os.environ.get("RACK_BRINGUP_FLEET_CACHE_TTL_SEC", "300")))
    except ValueError:
        return 300


def _scan_jobs() -> int:
    """Bound report-side verdict workers launched on behalf of Rack Status."""
    try:
        return min(32, max(1, int(os.environ.get("RACK_BRINGUP_SCAN_JOBS", "16"))))
    except ValueError:
        return 16


def _slurm_env_extra() -> dict[str, str]:
    """Forward ``SLURM_CONF`` so the scripts' bare ``sinfo`` resolves the controller.

    The scripts also try ``module load slurm`` themselves; this is belt-and-suspenders
    for a non-login SSH invocation where the module hook may not fire. Values are
    exported on the remote side by ``run_on_host(env_extra=...)``.
    """
    conf = (os.environ.get("RACK_SLURM_CONF") or "").strip()
    extra = {"SLURM_CONF": conf} if conf else {}
    # verdict-scan.sh otherwise defaults to nproc (128 on the appserver).  A web
    # refresh must never be allowed to consume the entire host.
    extra["SCAN_JOBS_OVERRIDE"] = str(_scan_jobs())
    return extra


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
        # Some reviewed bringup revisions print a banner separator as a CSV
        # record. It is presentation, never a host, and must not become a fake
        # node in fleet validation or generated reports.
        if _DECORATION_ONLY_NODE.fullmatch(node):
            continue
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


def run_status_check_all(*, timeout: int = 180) -> str:
    """Run ``status-check.sh`` with no pattern: every node under ``results/``.

    Its own scan cache makes this cheap (~0.5 s for 564 nodes), so a fleet view costs one
    round trip instead of one per node.
    """
    home = bringup_home()
    proc = host_exec.run_on_host(
        ["bash", f"{home}/status-check.sh"],
        cwd=home,
        timeout=timeout,
        env_extra=_slurm_env_extra(),
    )
    out = proc.stdout or ""
    if proc.returncode != 0 and not out.strip():
        raise RuntimeError((proc.stderr or "").strip() or f"status-check.sh exited {proc.returncode}")
    return out


def _expand_nodeset(spec: str) -> list[str]:
    """Expand ``a-[01-03,07],b-02`` into hostnames, reusing the tested expander.

    Imported lazily: ``universal_parser`` is an optional module in some deployments, and a
    missing expander must degrade to "treat the line as one host" rather than break the scan.
    """
    try:
        import universal_parser

        return universal_parser.expand_nodelist(spec)
    except Exception:  # noqa: BLE001 - fall back to comma splitting
        return [p.strip() for p in spec.split(",") if p.strip()]


def parse_status_check_fleet(text: str) -> dict[str, dict[str, dict[str, str]]]:
    """Parse the fleet form of ``status-check.sh`` into ``{node: {col: {status, reason}}}``.

    With no pattern the script groups nodes that share an identical verdict set, ``dshbak -c``
    style::

        ----------------
        rack-[11-18],rack-28
        ----------------
        allreduce: DONE
        allreduce-nonvlink: FAIL -- run error

    So a block is a nodeset line followed by ``col: STATUS[ -- reason]`` lines, and the
    statuses apply to every node in the set. Blocks are separated by dashed rules; a node line
    is simply the non-status line that follows one.
    """
    out: dict[str, dict[str, dict[str, str]]] = {}
    pending: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or set(line) == {"-"}:
            continue
        m = _STATUS_LINE.search(line)
        if m and pending:
            entry = {"status": m.group("status"), "reason": (m.group("reason") or "").strip()}
            for node in pending:
                out.setdefault(node, {})[m.group("col")] = entry
            continue
        if m:
            continue  # a status line with no node scope yet — nothing to attribute it to
        # Not a status line: this is the nodeset heading a new block.
        pending = _expand_nodeset(line)
    return out


def _build_fleet_summary() -> dict[str, object]:
    """Bring-up state for every node, in two calls rather than one per node.

    Statuses and reasons come from the fleet ``status-check.sh`` (authoritative verdicts);
    metrics come from the ``daily-report.sh -c`` CSV. This is the split ``node_summary()``
    makes for a single node — the CSV alone is not enough because a FAIL keeps its number
    there, so a numeric cell says nothing about pass/fail.
    """
    daily = run_daily_report()
    columns, rows = parse_daily_report_csv(daily)
    fleet = parse_status_check_fleet(run_status_check_all())

    # Union so a node known to only one source still appears, ordered by name for a stable UI.
    names = sorted(set(rows) | set(fleet))
    cols = columns or sorted({c for per_node in fleet.values() for c in per_node})
    nodes: list[dict[str, object]] = []
    for name in names:
        row = rows.get(name) or {}
        cells = row.get("cells") if isinstance(row.get("cells"), dict) else {}
        sc = fleet.get(name, {})
        jobs: list[dict[str, object]] = []
        for col in cols:
            base = _base_job(col)
            cell = str((cells or {}).get(col, ""))
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
        nodes.append({"node": name, "slurm_state": str(row.get("slurm", "")), "jobs": jobs})
    return {
        "home": bringup_home(),
        "columns": cols,
        "metric_labels": {c: METRIC_LABELS[b] for c in cols if (b := _base_job(c)) in METRIC_LABELS},
        "nodes": nodes,
    }


def _fleet_result(
    payload: dict[str, object],
    cached_at: float,
    *,
    refreshing: bool,
) -> dict[str, object]:
    """Return a copy with cache state; never mutate the shared cached payload."""
    result = dict(payload)
    result["cache"] = {
        "age_sec": max(0, round(time.monotonic() - cached_at, 3)),
        "refreshing": refreshing,
    }
    return result


def _refresh_fleet(home: str) -> None:
    """Refresh one fleet cache entry and wake any cold-cache waiters."""
    try:
        payload = _build_fleet_summary()
    except Exception as exc:
        _logger.exception("bring-up fleet cache refresh failed")
        with _fleet_cache_condition:
            _fleet_errors[home] = exc
            _fleet_refreshing.discard(home)
            _fleet_cache_condition.notify_all()
        return
    with _fleet_cache_condition:
        _fleet_cache[home] = (time.monotonic(), payload)
        _fleet_errors.pop(home, None)
        _fleet_refreshing.discard(home)
        _fleet_cache_condition.notify_all()


def fleet_summary() -> dict[str, object]:
    """Return fleet verdicts with one bounded refresh shared by every client.

    A warm but stale entry is returned immediately while one daemon thread rebuilds it.
    On a cold cache, one caller performs the build and concurrent callers wait for that
    same result instead of launching duplicate verdict scans.
    """
    home = bringup_home()
    ttl = _fleet_cache_ttl()
    with _fleet_cache_condition:
        hit = _fleet_cache.get(home)
        now = time.monotonic()
        if hit and ttl and (now - hit[0]) < ttl:
            return _fleet_result(hit[1], hit[0], refreshing=False)
        if hit:
            if home not in _fleet_refreshing:
                _fleet_refreshing.add(home)
                threading.Thread(
                    target=_refresh_fleet,
                    args=(home,),
                    name="bringup-fleet-refresh",
                    daemon=True,
                ).start()
            return _fleet_result(hit[1], hit[0], refreshing=True)
        if home in _fleet_refreshing:
            while home in _fleet_refreshing and home not in _fleet_cache:
                _fleet_cache_condition.wait()
            hit = _fleet_cache.get(home)
            if hit:
                return _fleet_result(hit[1], hit[0], refreshing=False)
            error = _fleet_errors.get(home)
            if error:
                raise RuntimeError(str(error)) from error
        _fleet_refreshing.add(home)

    # The first request after process startup has no stale value to serve, so it owns
    # the initial build. All concurrent requests join it through the condition above.
    _refresh_fleet(home)
    with _fleet_cache_condition:
        hit = _fleet_cache.get(home)
        if hit:
            return _fleet_result(hit[1], hit[0], refreshing=False)
        error = _fleet_errors.get(home)
        if error:
            raise RuntimeError(str(error)) from error
    raise RuntimeError("bring-up fleet refresh completed without a result")


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


def demo_fleet_summary(nodes: list[str] | None = None) -> dict[str, object]:
    """Synthetic fleet summary for ``RACK_STATUS_DEMO=1``.

    Spreads statuses and busbw values across the given nodes so the heatmap shows a range
    rather than one flat colour.
    """
    names = [n for n in (nodes or []) if n] or [f"demo-r01-{i:02d}" for i in range(1, 19)]
    cols = ["dcgmi-r1", "nvbandwidth", "allreduce", "alltoall", "allreduce-nonvlink"]
    out: list[dict[str, object]] = []
    for i, name in enumerate(names):
        jobs: list[dict[str, object]] = []
        for col in cols:
            base = _base_job(col)
            label = METRIC_LABELS.get(base)
            # Deterministic spread: every 7th node fails, every 11th has not run yet.
            if i % 11 == 10:
                status, metric = "NOT_RUN", None
            elif i % 7 == 6:
                status, metric = "FAIL", (f"{120 + (i % 5) * 7:.2f}" if label else None)
            else:
                status, metric = "DONE", (f"{600 + (i * 13) % 120:.2f}" if label else None)
            jobs.append(
                {
                    "name": col,
                    "base": base,
                    "status": status,
                    "reason": "below floor" if status == "FAIL" else "",
                    "metric": metric,
                    "metric_label": label,
                }
            )
        out.append(
            {
                "node": name,
                "slurm_state": "down*" if i % 9 == 8 else "idle",
                "jobs": jobs,
            }
        )
    return {
        "home": bringup_home(),
        "columns": cols,
        "metric_labels": {c: METRIC_LABELS[b] for c in cols if (b := _base_job(c)) in METRIC_LABELS},
        "nodes": out,
    }
