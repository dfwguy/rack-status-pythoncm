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
"""Cross-rack analytics: the fleet aggregate, its history, and the scaling series.

Where the rest of the API answers "what is this rack doing now", these routes answer "what has the
fleet produced" -- one pass over every finished run's log, reduced to NCCL bandwidth and NeMo
throughput rows, a per-metric history, and the Nemotron scaling series.

The cost model is the thing to keep in mind before editing anything here. The aggregate is built from
hundreds of logs that can each be hundreds of MB, on the far side of an SSH hop:

* ``_analytics_aggregate_async`` exists because the blocking route cannot finish inside a browser's
  patience on a full fleet. Both share ``_analytics_aggregate_guard`` and ``_analytics_aggregate_inputs``
  so the two paths cannot drift on validation or refusals.
* Results are memoised on disk by ``analytics_cache`` under a fingerprint of the input rows, so a
  second identical request costs nothing. That is what makes the blocking route usable at all.
* ``_read_log_text_chunk`` caps every read. Analytics touches the most files of any surface here, so an
  uncapped read is the difference between a slow page and an OOM.
* ``_parser_json_from_scan`` prefers the single-pass logscan record over invoking parser.sh per file
  (core-designs.mdc citizen 7); the per-file call is the fallback, not the plan.

``RACK_ANALYTICS_DISABLED`` is read from the process environment rather than through ``_eget``, so it is
an environment-level kill switch and is NOT settable from dashboard settings. That is preserved from
before the move deliberately: routing it through ``_eget`` would widen its reach, which is a policy
change and does not belong in a refactor.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# Metrics worth a fleet baseline, per workload kind. Names match what
# ``results_history`` stores from parser.sh (records_from_parser_json) and from
# ``universal_parser`` — both write the same lower-cased metric_name.
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import core
import host_exec
import native_status
import nvis_id_templates
import results_history
from analytics_cache import (
    _analytics_aggregate_cache_get,
    _analytics_aggregate_cache_put,
    _analytics_rows_fingerprint,
)
from audit.actions import action
from config_env import _job_env_int, _subprocess_env
from core import last_regex_int as _last_regex_int
from core import mean_finite as _mean_finite
from core import rpath as _rpath
from core import truthy_env as _truthy_env
from flask import Blueprint, jsonify, request
from job_registry import _analytics_jobs, _analytics_jobs_lock
from log_discovery import find_log_paths_for_rack
from logscan_cache import (
    _analytics_scan_ctx,
    _logscan_batch,
    _logscan_record,
)
from naming import (
    _is_multirack_log_basename,
    _jobid_from_nemo_log_basename,
    _multirack_physical_rack_ids,
    _nemo_nccl_in_log_basename,
    _nemo_single_rack_log_basename,
    _normalize_rack_id,
    _nvis_naming,
    _rack_id_from_log_basename,
    multirack_display_rack_from_basename,
)
from paths import _parser_path, _resolve_log_dir, _script_exists
from results_ingest import (
    _history_ingest_parser_json,
    _host_read_text,
    _nemo_log_file_looks_successful,
    _parser_extra_args,
    _parser_json_has_usable_rows,
)
from slurm_query import _infer_num_nodes_from_nodelist

from . import _common as _api_common

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("analytics", __name__, url_prefix="")


def _parser_json_from_scan(rec: dict, rack: str, job_id: str) -> dict:
    """Shape one scanner record like ``parser.sh --json`` so consumers need no changes.

    _pick_nccl_row / _pick_nemotron_row fall back to ``rows[0]`` when no rack matches, so a
    single-row payload is safe even where the rack string is derived differently.
    """
    n, c = rec.get("nemo") or {}, rec.get("nccl") or {}
    peaks = c.get("peak_busbw_gbps") or {}

    def g(v):
        return "---" if v is None else str(v)

    started = int(c.get("sweep_blocks_started") or 0)
    concluded = int(c.get("sweep_blocks_concluded") or 0)
    nccl_row = {
        "rack": rack,
        "job_id": job_id,
        "all_reduce_gbps": g(peaks.get("ar")),
        "all_gather_gbps": g(peaks.get("ag")),
        "alltoall_gbps": g(peaks.get("aa")),
        "reduce_scatter_gbps": g(peaks.get("rs")),
        "sendrecv_gbps": g(peaks.get("sr")),
        "oob_ar_ag_aa": ",".join(c.get("out_of_bounds") or []) or "---",
        "sweep_blocks_started": started,
        "sweep_blocks_concluded": concluded,
        "sweep_blocks_missing": max(0, started - concluded),
        # A batch scan is never looking at a job it is also running, so a gap here is a lost
        # marker, not a partial read.
        "sweep_complete": True,
        "sweep_running": False,
        "all_reduce_busbw_series": c.get("all_reduce_series") or [],
        "busbw_series_by_collective": c.get("series_by_collective") or {},
    }
    it = n.get("iteration") or [None, None]
    nemo_row = {
        "rack": rack,
        "job_id": job_id,
        "status": "OK" if (n.get("step_time_s") is not None or n.get("finished")) else "UNK",
        "step_s": n.get("step_time_s"),
        "median_step_time": n.get("step_time_s"),
        "tflops_per_gpu": n.get("tflops_per_gpu"),
        "step_time_std": n.get("step_time_std"),
        "perf_std": n.get("perf_std"),
        "mfu_percent": n.get("mfu_percent"),
        "steps_completed": it[0],
        "steps_target": it[1],
        "step_series": [
            {"iteration": i, "step_time": st}
            for i, st in enumerate(n.get("step_series") or [], start=1)
            if i >= 11
        ],
    }
    # A row means a NeMo run was seen. parser.sh emits an all-"---" Nemotron row for ANY file, so an
    # NCCL log used to reach the scaling table as a "successful run with no metrics" -- 52 of them on
    # Nscale NC, filed under Rack = 2/3/4. A NeMo log leaves a trace even when too short to average
    # (samples, an iteration counter, a declared step target, a series); an NCCL log leaves none.
    # Deliberately not the placeholder predicate: a finished NeMo run whose timings could not be
    # averaged is exactly what the "no metrics" tag exists to report.
    has_nemo_evidence = bool(
        n.get("samples") or n.get("iteration") or n.get("declared_max_steps") or n.get("step_series")
    )
    return {
        "export_version": 7,
        "nccl_peak_out_of_place_busbw_gbps": [nccl_row],
        "nemotron_5h_56b_train_window": [nemo_row] if has_nemo_evidence else [],
        "mnubergemm_stress": [],
        "parser_flags": [],
        "from_logscan": True,
    }


def _parser_json_for_log_path(log_dir: Path, log_path: Path) -> tuple[dict | None, str | None]:
    """Parsed results for one log under ``log_dir``. Returns ``(parsed, err)``.

    Answers from the thread's batch scan when the aggregate pre-scanned this path, which is the
    difference between one process for the whole fleet and one ``parser.sh`` per log.
    """
    batch = getattr(_analytics_scan_ctx, "batch", None)
    if batch:
        rec = batch.get(str(_rpath(log_path)))
        if rec:
            base = PurePosixPath(str(log_path)).name
            # Best-effort only: both pick helpers fall back to rows[0], and a batch payload
            # carries exactly one row, so a multirack basename with no single rack id is fine.
            rack = _rack_id_from_log_basename(base) or ""
            jid = _jobid_from_nemo_log_basename(base) or "?"
            parsed = _parser_json_from_scan(rec, rack, jid)
            try:
                if _parser_json_has_usable_rows(parsed):
                    _history_ingest_parser_json(
                        parsed, log_dir, str(_rpath(log_path)), source="logscan_batch"
                    )
            except Exception as e:  # noqa: BLE001
                _logger.debug("history ingest (logscan batch) failed for %s: %s", log_path, e)
            return parsed, None

    parser = _parser_path()
    if not _script_exists(parser):
        return None, "parser.sh not found"
    env = _subprocess_env({"NO_COLOR": "1"})
    # Route parser.sh through host_exec (SSH when remote) and pass an absolute path; don't
    # local-resolve a remote path (``.resolve()`` breaks on the PurePosixPath returned in SSH mode).
    lp = str(_rpath(log_path))
    cmd = ["bash", str(parser), *_parser_extra_args(), "--json", lp]
    timeout = max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300))
    try:
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(log_dir),
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "parser.sh timed out"
    except Exception as e:
        _logger.debug("_parser_json_for_log_path: ignoring error", exc_info=True)
        return None, str(e)
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[:800]
        return None, tail or "parser non-zero exit"
    raw = (proc.stdout or "").strip()
    if not raw:
        return None, "empty parser stdout"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, "parser stdout is not valid JSON"
    if not isinstance(parsed, dict):
        return None, None
    # Durable history (2026-07-28). This is the single choke point every analytics /
    # multirack parser call goes through, so hooking it here records a run for each parsed
    # log with no change to the return contract. Best-effort: swallowed on any failure.
    try:
        if _parser_json_has_usable_rows(parsed):
            _history_ingest_parser_json(parsed, log_dir, lp, source="parser_json_for_log_path")
    except Exception as e:  # noqa: BLE001
        _logger.debug("history ingest (parser_json_for_log_path) failed for %s: %s", lp, e)
    return parsed, None


def _parse_gbps_scalar(v: object) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "?"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pick_nccl_row(parsed: dict, rack: str) -> dict | None:
    rows = parsed.get("nccl_peak_out_of_place_busbw_gbps")
    if not isinstance(rows, list) or not rows:
        return None
    rk = rack.strip().lower()
    for row in rows:
        if isinstance(row, dict) and str(row.get("rack", "")).strip().lower() == rk:
            return row
    first = rows[0]
    return first if isinstance(first, dict) else None


def _pick_nemotron_row(parsed: dict, rack: str) -> dict | None:
    rows = parsed.get("nemotron_5h_56b_train_window")
    if not isinstance(rows, list) or not rows:
        return None
    rk = rack.strip().lower()
    for row in rows:
        if isinstance(row, dict) and str(row.get("rack", "")).strip().lower() == rk:
            return row
    first = rows[0]
    return first if isinstance(first, dict) else None


def _step_series_cv_pct(row: dict) -> float | None:
    raw = row.get("step_time_series") or row.get("step_series") or []
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    vals: list[float] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            st = float(p.get("step_time", p.get("step", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if st > 0:
            vals.append(st)
    if len(vals) < 2:
        return None
    try:
        mu = statistics.mean(vals)
        if mu <= 0:
            return None
        return round(statistics.stdev(vals) / mu * 100.0, 3)
    except statistics.StatisticsError:
        return None


def _multirack_rack_count_from_basename(name: str) -> int:
    # A declared count wins over counting ids in the name. The compacted tag keeps only the first
    # and last rack, so counting ids read a 32-rack / 576-node run as two racks and filed it under
    # Rack=2 in the scaling table, next to genuine 36-node pairs.
    declared = nvis_id_templates.multirack_rack_count(name)
    if declared:
        return declared
    phy = _multirack_physical_rack_ids(name)
    return len(phy) if phy else 0


def _multirack_candidate_log_paths(log_dir: Path) -> list[tuple[str, float, Path]]:
    """Every multirack NEMO/NCCL log, BEFORE the successful-log filter.

    Split out so a caller can pre-scan the candidates in one batch; the success test reads the
    file, so filtering first would cost a process per candidate.
    """
    base_root = str(native_status._host_resolve(log_dir)).rstrip("/")
    out: list[tuple[str, float, Path]] = []
    for base in (base_root, base_root + "/failed"):
        for path, mtime_ns, _size in native_status._host_log_entries(base):
            name = os.path.basename(path)
            if not _nemo_nccl_in_log_basename(name):
                continue
            if not _is_multirack_log_basename(name):
                continue
            out.append((path, mtime_ns / 1_000_000_000, native_status._host_path(path)))
    return out


def _multirack_done_log_paths(log_dir: Path) -> list[Path]:
    """Multirack NEMO/NCCL logs under log_dir root and ``failed/`` that pass the successful-log heuristic.

    Host-aware: discovers via ``host_exec`` (SSH when remote) so multirack analytics works whether the
    app runs on the head node or on a laptop over SSH.
    """
    merged: dict[str, tuple[float, Path]] = {}
    for path, mtime, hp in _multirack_candidate_log_paths(log_dir):
        if not _nemo_log_file_looks_successful(hp):
            continue
        merged[path] = (mtime, hp)
    return [p for _m, p in sorted(merged.values(), key=lambda t: t[0], reverse=True)]


def _read_log_text_chunk(path: Path, max_bytes: int = 14 * 1024 * 1024) -> str:
    """Head-capped read, kept ONLY as the fallback when nvis_logscan is unavailable.

    Prefer :func:`_logscan_record`: this returns the first *max_bytes* and nothing else, so a
    ``-gb`` or ``NODELIST:`` line further into a large log is invisible and the analytics scaling
    table silently loses that run's throughput.
    """
    # Host-aware (local or SSH); tolerates PurePosixPath remote paths.
    return _host_read_text(path, max_bytes=max_bytes) or ""


_RE_RUN_GB = re.compile(r"(?<![\w-])-gb\s+(\d+)\b", re.IGNORECASE)


_RE_RUN_NUM_GPUS = re.compile(r"--num_gpus\s+(\d+)\b", re.IGNORECASE)


_RE_RUN_GPUS_PER_NODE = re.compile(r"--gpus_per_node\s+(\d+)\b", re.IGNORECASE)


_RE_RUN_SLURM_NNODES = re.compile(r"SLURM_NNODES=(\d+)\b")


_RE_RUN_NODELIST = re.compile(r"NODELIST:\s*(\S+)")


_RE_SEQ_LEN_CANDIDATES = (
    re.compile(r"(?:^|[\s|])seq(?:uence)?[_-]?length[=:\s]+(\d+)\b", re.IGNORECASE),
    re.compile(r"context[_-]?length[=:\s]+(\d+)\b", re.IGNORECASE),
    re.compile(r"(?<![\w-])-sl\s+(\d+)\b"),
)


def _nemotron_log_run_hw_tokens(
    log_text: str, step_s: float, *, default_seq_len: int = 8192
) -> dict[str, int | float | None]:
    """Parse Slurm / ``run_script.py`` fragments from a NEMO validation log.

    Token throughput uses ``global_batch × sequence_length ÷ step_time (s)`` when ``-gb`` is present
    (Megatron-Bridge ``run_script.py``). Sequence length is read from the log when possible; otherwise
    ``default_seq_len`` (8192 — Nemotron-H 56B default).

    Time to train 1T tokens (days) = 1e12 / (throughput_tokens_s × 86400).
    """
    num_gpus = _last_regex_int(_RE_RUN_NUM_GPUS, log_text)
    gpn = _last_regex_int(_RE_RUN_GPUS_PER_NODE, log_text)
    gbs = _last_regex_int(_RE_RUN_GB, log_text)
    seq_len: int | None = None
    for rx in _RE_SEQ_LEN_CANDIDATES:
        m = rx.search(log_text)
        if m:
            try:
                seq_len = int(m.group(1))
                break
            except (ValueError, IndexError):
                continue
    seq_eff = int(seq_len) if seq_len is not None else int(default_seq_len)

    num_nodes: int | None = None
    if gpn and num_gpus and gpn > 0 and num_gpus % gpn == 0:
        num_nodes = num_gpus // gpn
    m = _RE_RUN_SLURM_NNODES.search(log_text)
    if num_nodes is None and m:
        try:
            num_nodes = int(m.group(1))
        except ValueError:
            pass
    if num_nodes is None:
        m2 = _RE_RUN_NODELIST.search(log_text)
        if m2:
            num_nodes = _infer_num_nodes_from_nodelist(m2.group(1))

    tps: float | None = None
    if gbs and step_s > 0:
        tps = (float(gbs) * float(seq_eff)) / float(step_s)
    tps_gpu: float | None = (tps / float(num_gpus)) if (tps is not None and num_gpus) else None
    days_1t: float | None = None
    if tps and tps > 0:
        days_1t = 1_000_000_000_000.0 / (float(tps) * 86400.0)
    return {
        "num_nodes": num_nodes,
        "num_gpus": num_gpus,
        "global_batch": gbs,
        "seq_len_used": seq_eff,
        "throughput_tokens_s": tps,
        "throughput_tokens_s_per_gpu": tps_gpu,
        "time_to_train_1t_days": days_1t,
    }


def _log_mtime_or_zero(path: Path) -> float:
    # AttributeError guards a remote PurePosixPath (no .stat()); mtime is only used for rep selection.
    try:
        return float(path.stat().st_mtime)
    except (OSError, AttributeError):
        return 0.0


def _nemotron_run_hw_tokens_for_path(lp: Path, st_f: float) -> dict[str, int | float | None]:
    """Run geometry for *lp*, from the whole file when the scanner is available.

    The fallback reads a 14 MB head, which is why a big multirack log could produce step time and
    TFLOPs but no throughput: its ``-gb`` / ``NODELIST:`` lines sat past the cap, so the run
    showed up in the scaling table with blank tokens/s.
    """
    rec = _logscan_record(lp)
    if rec:
        g = rec.get("geometry") or {}
        # _nemotron_log_run_hw_tokens is regex-over-text; hand it a tiny synthetic text carrying
        # exactly the tokens the scanner found, so the arithmetic stays in one place.
        parts = []
        if g.get("num_gpus"):
            parts.append(f"--num_gpus {g['num_gpus']}")
        if g.get("gpus_per_node"):
            parts.append(f"--gpus_per_node {g['gpus_per_node']}")
        if g.get("global_batch"):
            parts.append(f"-gb {g['global_batch']}")
        if g.get("slurm_nnodes"):
            parts.append(f"SLURM_NNODES={g['slurm_nnodes']}")
        if g.get("nodelist"):
            parts.append(f"NODELIST: {g['nodelist']}")
        if g.get("seq_len"):
            parts.append(f"seq_length={g['seq_len']}")
        if parts:
            return _nemotron_log_run_hw_tokens("\n".join(parts), st_f)
    return _nemotron_log_run_hw_tokens(_read_log_text_chunk(lp), st_f)


def _nemotron_scaling_append_entry(
    bucket: dict[int, list[dict[str, object]]], n_r: int, lp: Path, tf_f: float, st_f: float
) -> None:
    """Append one log's Nemotron stats + parsed run geometry / throughput for analytics buckets."""
    mrun = _nemotron_run_hw_tokens_for_path(lp, st_f)
    bucket.setdefault(n_r, []).append(
        {
            "path": lp,
            "tf": tf_f,
            "st": st_f,
            "throughput_tokens_s": mrun["throughput_tokens_s"],
            "throughput_tokens_s_per_gpu": mrun["throughput_tokens_s_per_gpu"],
            "num_nodes": mrun["num_nodes"],
            "num_gpus": mrun["num_gpus"],
        }
    )


#: The collectives the single-rack view can switch between, keyed as the scanner names them.
_SINGLE_RACK_COLLECTIVES = ("all_reduce", "all_gather", "alltoall")


def _clean_busbw_series(raw: object) -> list[dict[str, float]]:
    """``[{size_bytes, bus_bw_gbps}]`` with unparseable or non-positive sizes dropped."""
    out: list[dict[str, float]] = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict):
            continue
        try:
            sz = int(p.get("size_bytes") or 0)
            bw = float(p.get("bus_bw_gbps") or 0)
        except (TypeError, ValueError):
            continue
        if sz > 0 and bw >= 0:
            out.append({"size_bytes": float(sz), "bus_bw_gbps": bw})
    return out


def _collective_series_for_log(lp: Path, nc: dict) -> dict[str, list[dict[str, float]]]:
    """Bus bandwidth vs message size for each collective in one NCCL log.

    parser.sh reports only the all_reduce series; the whole-file scan records every collective's.
    The scan-shaped row carries them already; otherwise ask the scan for this log (cached per
    path and mtime), and fall back to parser.sh's all_reduce series so that view never regresses.
    """
    by_coll = nc.get("busbw_series_by_collective")
    if not isinstance(by_coll, dict) or not by_coll:
        try:
            rec = _logscan_record(lp) or {}
        except Exception:  # noqa: BLE001 - the extra collectives are enrichment
            rec = {}
        by_coll = (rec.get("nccl") or {}).get("series_by_collective") or {}
    out = {c: _clean_busbw_series(by_coll.get(c)) for c in _SINGLE_RACK_COLLECTIVES}
    if not out["all_reduce"]:
        out["all_reduce"] = _clean_busbw_series(nc.get("all_reduce_busbw_series"))
    return {c: ser for c, ser in out.items() if ser}


def _busbw_series_block(
    series_by_rack: dict[str, list[dict[str, float]]], log_by_rack: dict[str, str]
) -> dict[str, object]:
    """Fleet median per message size plus each rack's series and its % off that median."""
    size_to_vals: dict[int, list[float]] = {}
    for ser in series_by_rack.values():
        for p in ser:
            size_to_vals.setdefault(int(p["size_bytes"]), []).append(float(p["bus_bw_gbps"]))
    sizes_sorted = sorted(size_to_vals)
    median_bw = {sz: statistics.median(v) for sz, v in size_to_vals.items() if v}
    per_rack: list[dict[str, object]] = []
    for rack in sorted(series_by_rack):
        ser = series_by_rack[rack]
        bws: list[float | None] = []
        pcts: list[float | None] = []
        for sz in sizes_sorted:
            med = median_bw.get(sz)
            v = next((float(p["bus_bw_gbps"]) for p in ser if int(p["size_bytes"]) == sz), None)
            if v is None or med is None or med <= 0:
                bws.append(None)
                pcts.append(None)
            else:
                bws.append(round(v, 4))
                pcts.append(round((v - med) / med * 100.0, 2))
        per_rack.append({"rack": rack, "log_file": log_by_rack.get(rack, ""), "bus_bw_gbps": bws, "pct_vs_median": pcts})
    return {
        "sizes_bytes": sizes_sorted,
        "median_bus_bw_gbps": [round(median_bw[s], 4) for s in sizes_sorted],
        "per_rack": per_rack,
    }


def _nccl_log_gpu_count(lp: Path) -> int | None:
    """GPUs an NCCL log's run used: the launcher header's MPI rank count (one rank per GPU).

    ``None`` when the log does not say; the page then shows nodes x the platform's GPUs per node,
    marked as the design figure rather than a measured one.
    """
    rec = _logscan_record(lp)
    try:
        n = int(((rec or {}).get("geometry") or {}).get("nccl_np") or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else None


_RE_NODES_IN_NAME = re.compile(r"(?<![A-Za-z0-9])(\d{1,4})[nN](?![A-Za-z0-9])")


def _nccl_log_node_count(lp: Path) -> int | None:
    """Nodes an NCCL log's run used, or ``None`` when the log does not say.

    In order of authority: the launcher's own ``nccl_allreduce: nodes=N`` header (the run's declared
    scale, read by the whole-file scan), the Slurm allocation's ``SLURM_NNODES``, then a count the
    file name declares (``-18N-``, ``-30n-``). The name is last because it is only a label.
    """
    rec = _logscan_record(lp)
    if rec:
        g = rec.get("geometry") or {}
        for key in ("nccl_nodes", "slurm_nnodes"):
            try:
                n = int(g.get(key) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n
    m = _RE_NODES_IN_NAME.search(Path(str(lp)).name)
    return int(m.group(1)) if m else None


_NCCL_SCALING_KEYS = (
    "all_reduce_gbps", "all_gather_gbps", "alltoall_gbps", "reduce_scatter_gbps", "sendrecv_gbps",
)


def _nccl_scaling_append_entry(bucket: dict[int, list[dict[str, object]]], lp: Path, nc: dict) -> None:
    """Append one NCCL log's collective peaks to the bucket for the node count it ran on.

    Keyed by nodes, not by a rack count read out of the file name: that inference counts ids in the
    name and filed a 6-node all-to-all under "2 racks" beside a 36-node pass, which is not a scale
    anyone can read. The node count comes from the whole-file scan (SLURM_NNODES / NODELIST); 0 means
    the log did not say.
    """
    entry: dict[str, object] = {"path": lp}
    for key in _NCCL_SCALING_KEYS:
        entry[key] = _parse_gbps_scalar(nc.get(key))
    try:
        nodes = _nccl_log_node_count(lp)
    except Exception:  # noqa: BLE001 - geometry is enrichment; the bandwidth is the point
        nodes = None
    entry["num_nodes"] = nodes
    try:
        entry["num_gpus"] = _nccl_log_gpu_count(lp)
    except Exception:  # noqa: BLE001 - geometry is enrichment
        entry["num_gpus"] = None
    bucket.setdefault(int(nodes) if isinstance(nodes, int) and nodes > 0 else 0, []).append(entry)


def _nccl_scaling_rows(bucket: dict[int, list[dict[str, object]]]) -> list[dict[str, object]]:
    """One row per node count: the mean of each collective's peak bus bandwidth across its logs.

    A log with no measured collective (a sweep that never concluded) is reported in
    ``n_logs_no_metrics`` instead of being averaged in as zero or dropped without a trace.
    """
    rows: list[dict[str, object]] = []
    for nodes in sorted(bucket, key=lambda n: (n == 0, n)):
        ents = bucket[nodes]
        measured = [e for e in ents if any(e.get(k) is not None for k in _NCCL_SCALING_KEYS)]
        unmeasured = [e for e in ents if e not in measured]
        row: dict[str, object] = {
            "num_nodes": nodes or None,
            "n_logs": len(measured),
            "n_logs_no_metrics": len(unmeasured),
            "logs_no_metrics": [Path(str(e["path"])).name for e in unmeasured],
            # From the newest measured log that states it; None when no log in the row does.
            "num_gpus": next(
                (
                    e["num_gpus"]
                    for e in sorted(measured, key=lambda e: _log_mtime_or_zero(e["path"]), reverse=True)  # type: ignore[arg-type]
                    if e.get("num_gpus")
                ),
                None,
            ),
        }
        for key in _NCCL_SCALING_KEYS:
            vals = [float(e[key]) for e in measured if e.get(key) is not None]  # type: ignore[arg-type]
            row[f"mean_{key}"] = round(statistics.mean(vals), 3) if vals else None
        rows.append(row)
    return rows


def _demo_analytics_aggregate() -> dict[str, object]:
    return {
        "ok": True,
        "single_rack": {
            "nccl_collectives": [
                {"rack": "p5-r01", "log_file": "demo.txt", "all_reduce_gbps": 90.0, "all_gather_gbps": 12.0, "alltoall_gbps": 8.0},
                {"rack": "p5-r02", "log_file": "demo2.txt", "all_reduce_gbps": 92.0, "all_gather_gbps": 11.5, "alltoall_gbps": 8.2},
            ],
            "nccl_collectives_deviation_pct": [
                {"metric": "all_reduce_gbps", "rack": "p5-r01", "value_gbps": 90.0, "fleet_median_gbps": 91.0, "pct_vs_median": -1.1},
                {"metric": "all_reduce_gbps", "rack": "p5-r02", "value_gbps": 92.0, "fleet_median_gbps": 91.0, "pct_vs_median": 1.1},
            ],
            "nccl_ar_series": {
                "sizes_bytes": [1048576, 16777216],
                "median_bus_bw_gbps": [10.0, 95.0],
                "per_rack": [
                    {
                        "rack": "p5-r01",
                        "log_file": "demo.txt",
                        "bus_bw_gbps": [10.1, 94.0],
                        "pct_vs_median": [1.0, -1.05],
                    },
                    {
                        "rack": "p5-r02",
                        "log_file": "demo2.txt",
                        "bus_bw_gbps": [9.9, 96.0],
                        "pct_vs_median": [-1.0, 1.05],
                    },
                ],
            },
            "nemotron": [
                {
                    "rack": "p5-r01",
                    "log_file": "demo.txt",
                    "median_step_time": 1.5,
                    "tflops_per_gpu": 400.0,
                    "step_time_cv_pct": 2.1,
                    "pct_vs_mean_tflops": -1.2,
                    "pct_vs_mean_step_time": 0.5,
                },
                {
                    "rack": "p5-r02",
                    "log_file": "demo2.txt",
                    "median_step_time": 1.48,
                    "tflops_per_gpu": 410.0,
                    "step_time_cv_pct": 1.8,
                    "pct_vs_mean_tflops": 1.2,
                    "pct_vs_mean_step_time": -0.5,
                },
            ],
        },
        "multirack": {
            "nccl_series_by_log": [
                {
                    "label": "p5-r01+p5-r02",
                    "basename": "root-NEMO-2rack-demo.txt",
                    "series": [{"size_bytes": 1048576, "bus_bw_gbps": 88.0}, {"size_bytes": 16777216, "bus_bw_gbps": 170.0}],
                }
            ],
            "nccl_scaling": [
                {"num_nodes": 18, "num_gpus": 72, "n_logs": 12, "n_logs_no_metrics": 0, "logs_no_metrics": [],
                 "mean_all_reduce_gbps": 690.0, "mean_all_gather_gbps": 520.0, "mean_alltoall_gbps": 140.0,
                 "mean_reduce_scatter_gbps": 510.0, "mean_sendrecv_gbps": 180.0},
                {"num_nodes": 36, "num_gpus": 144, "n_logs": 1, "n_logs_no_metrics": 0, "logs_no_metrics": [],
                 "mean_all_reduce_gbps": 798.0, "mean_all_gather_gbps": None, "mean_alltoall_gbps": 135.0,
                 "mean_reduce_scatter_gbps": None, "mean_sendrecv_gbps": None},
            ],
            "nemotron_scaling": [
                {"n_racks": 1, "n_logs": 12, "mean_tflops_per_gpu": 390.0, "mean_step_s": 1.95},
                {"n_racks": 2, "n_logs": 1, "mean_tflops_per_gpu": 380.0, "mean_step_s": 1.9},
                {"n_racks": 4, "n_logs": 1, "mean_tflops_per_gpu": 360.0, "mean_step_s": 2.1},
            ],
            "nemotron_throughput_scaling": [
                {
                    "n_racks": 1,
                    "n_logs": 12,
                    "num_nodes": 18,
                    "num_gpus": 72,
                    "mean_step_s": 1.95,
                    "mean_tflops_per_gpu": 390.0,
                    "mean_throughput_tokens_s": 40000.0,
                    "mean_throughput_tokens_s_per_gpu": 5000.0,
                    "time_to_train_1t_days": 289.35,
                },
                {
                    "n_racks": 2,
                    "n_logs": 1,
                    "num_nodes": 1,
                    "num_gpus": 8,
                    "mean_step_s": 1.9,
                    "mean_tflops_per_gpu": 380.0,
                    "mean_throughput_tokens_s": 39261.76,
                    "mean_throughput_tokens_s_per_gpu": 4907.72,
                    "time_to_train_1t_days": 294.79,
                },
                {
                    "n_racks": 4,
                    "n_logs": 1,
                    "num_nodes": 2,
                    "num_gpus": 16,
                    "mean_step_s": 2.1,
                    "mean_tflops_per_gpu": 360.0,
                    "mean_throughput_tokens_s": 35000.0,
                    "mean_throughput_tokens_s_per_gpu": 4375.0,
                    "time_to_train_1t_days": 330.69,
                },
            ],
        },
    }


_ANALYTICS_HISTORY_METRICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nccl", ("all_reduce_gbps", "all_gather_gbps", "alltoall_gbps", "busbw_gbps",
              "reduce_scatter_gbps", "sendrecv_gbps")),
    ("nemo", ("step_s", "step_time_s", "tflops_per_gpu", "mfu_percent")),
    ("mnubergemm", ("gflops_max", "gflops_median")),
    ("hpl", ("gflops", "tflops")),
    ("nvbandwidth", ("bandwidth_gbps",)),
    ("nvloom", ("bandwidth_gbps",)),
)


_ANALYTICS_HISTORY_SERIES_LIMIT = 5000


def _analytics_history_block(since: str | None = None) -> dict[str, object]:
    """Fleet baseline computed over the PERSISTED run history, not just today's log dir.

    The existing single-rack/multirack tables in ``/api/analytics/aggregate`` are derived
    from whatever logs happen to sit in ``log_dir`` right now *and* are gated on the rack's
    ``nemo`` cell being ``done``; both facts are unchanged here. This adds a parallel view
    whose baseline is every run ever ingested, so a deviation figure exists even when zero
    racks are currently ``done``.

    Returned under a NEW top-level ``history`` key — no existing field is touched.
    """
    if not core.history_enabled():
        return {
            "available": False,
            "reason": "results_history unavailable (check the bind mount in docker-compose.override.yml)",
        }
    base_filters: dict[str, object] = {}
    if since:
        base_filters["since"] = since
    out: dict[str, object] = {
        "available": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "workloads": {},
    }
    try:
        out["db"] = results_history.db_stats()
    except Exception as e:  # noqa: BLE001
        out["db"] = {"error": str(e)}
    for kind, metric_names in _ANALYTICS_HISTORY_METRICS:
        try:
            summary = results_history.summary_stats(kind, base_filters or None)
        except Exception as e:  # noqa: BLE001
            _logger.debug("history summary for %s failed: %s", kind, e)
            continue
        if not summary or not int(summary.get("run_count") or 0):
            continue
        stats = summary.get("metrics") or {}
        per_metric: dict[str, object] = {}
        for metric in metric_names:
            st = stats.get(metric)
            if not isinstance(st, dict) or not st.get("count"):
                continue
            median = st.get("median")
            filt = dict(base_filters)
            filt["workload_kind"] = kind
            try:
                pts = results_history.metric_series(
                    metric, filt, limit=_ANALYTICS_HISTORY_SERIES_LIMIT
                )
            except Exception as e:  # noqa: BLE001
                _logger.debug("history series for %s/%s failed: %s", kind, metric, e)
                pts = []
            # metric_series returns oldest-first, so the last row per rack is its latest run.
            latest_by_rack: dict[str, dict[str, object]] = {}
            for p in pts:
                rk = p.get("rack")
                if not rk:
                    continue
                latest_by_rack[str(rk)] = p
            racks_out: list[dict[str, object]] = []
            for rk in sorted(latest_by_rack):
                p = latest_by_rack[rk]
                val = p.get("value")
                dev = None
                if val is not None and isinstance(median, (int, float)) and float(median) > 0:
                    dev = round((float(val) - float(median)) / float(median) * 100.0, 2)
                racks_out.append(
                    {
                        "rack": rk,
                        "value": val,
                        "unit": p.get("unit"),
                        "run_id": p.get("run_id"),
                        "job_id": p.get("job_id"),
                        "status": p.get("status"),
                        "log_file": p.get("log_basename"),
                        "started_at": p.get("started_at"),
                        "pct_vs_history_median": dev,
                    }
                )
            per_metric[metric] = {
                "unit": st.get("unit"),
                "sample_count": st.get("count"),
                "median": median,
                "mean": st.get("mean"),
                "p05": st.get("p05"),
                "p95": st.get("p95"),
                "min": st.get("min"),
                "max": st.get("max"),
                "latest_per_rack": racks_out,
            }
        if not per_metric:
            continue
        out["workloads"][kind] = {
            "run_count": summary.get("run_count"),
            "pass_count": summary.get("pass_count"),
            "fail_count": summary.get("fail_count"),
            "first_started_at": summary.get("first_started_at"),
            "last_started_at": summary.get("last_started_at"),
            "metrics": per_metric,
        }
    return out


@bp.route("/api/analytics/history", methods=["GET"])
@action(get="analytics:read")
def api_analytics_history():
    """Fleet baseline over the persisted run history (``?since=`` ISO-8601 or epoch)."""
    if not core.history_enabled():
        return _api_common.history_unavailable_response()
    since = (request.args.get("since") or "").strip() or None
    try:
        return jsonify({"ok": True, "history": _analytics_history_block(since)})
    except Exception as e:  # noqa: BLE001
        _logger.warning("analytics history failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/analytics/last", methods=["GET"])
@action(get="analytics:read")
def api_analytics_last():
    """Return the last saved analytics aggregate for ``log_dir`` (written after a successful POST)."""
    if os.environ.get("RACK_ANALYTICS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "analytics disabled (RACK_ANALYTICS_DISABLED)"}), 403
    if _truthy_env("RACK_STATUS_DEMO"):
        d = dict(_demo_analytics_aggregate())
        d["cached"] = True
        d["analyzed_at"] = "1970-01-01T00:00:00+00:00"
        d["rows_fingerprint"] = "demo"
        return jsonify(d)
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    hit = _analytics_aggregate_cache_get(str(_rpath(log_dir)))
    if not hit:
        return jsonify({"ok": False, "error": "no cached analysis for this log directory"}), 404
    out: dict[str, object] = dict(hit)
    out["ok"] = True
    out["cached"] = True
    return jsonify(out)


@bp.route("/api/analytics/aggregate", methods=["POST"])
@action("analytics:aggregate")
def api_analytics_aggregate():
    """POST JSON ``{log_dir, rows:[{rack,nemo},…]}`` — parser-based NCCL/Nemotron analytics.

    Blocking. On a large fleet this can run for minutes, which is fragile behind a proxy, so the
    SPA uses ``/api/analytics/aggregate-async`` and polls for progress. This route stays for API
    callers and returns exactly the same payload.
    """
    err = _analytics_aggregate_guard()
    if err is not None:
        return err
    body = request.get_json(silent=True) or {}
    parsed, bad = _analytics_aggregate_inputs(body)
    if bad is not None:
        return bad
    log_dir, done_racks = parsed
    return jsonify(_analytics_aggregate_compute(log_dir, done_racks, body))


_ANALYTICS_JOBS_MAX = 8


@bp.route("/api/analytics/aggregate-async", methods=["POST"])
@action("analytics:aggregate", variant="async")
def api_analytics_aggregate_async():
    """Start the aggregate on a worker thread; poll GET /api/analytics/aggregate-status?job_id=…

    The blocking route can run for minutes on a large fleet, which browsers and proxies both
    dislike, and it gave the operator nothing but a spinner. The work is a known number of logs,
    so the job reports ``done``/``total`` as each one is scanned.
    """
    err = _analytics_aggregate_guard()
    if err is not None:
        return err
    body = request.get_json(silent=True) or {}
    parsed, bad = _analytics_aggregate_inputs(body)
    if bad is not None:
        return bad
    log_dir, rows_in = parsed

    job_id = uuid.uuid4().hex
    with _analytics_jobs_lock:
        # Keep the table small; an abandoned tab should not pin payloads forever.
        if len(_analytics_jobs) >= _ANALYTICS_JOBS_MAX:
            for stale in sorted(_analytics_jobs, key=lambda k: _analytics_jobs[k].get("started", 0))[:2]:
                _analytics_jobs.pop(stale, None)
        _analytics_jobs[job_id] = {
            "state": "running",
            "done": 0,
            "total": 0,
            "current": "",
            "started": time.time(),
        }

    def _progress(done: int, total: int, current: str) -> None:
        with _analytics_jobs_lock:
            row = _analytics_jobs.get(job_id)
            if row is not None:
                row["done"] = done
                row["total"] = total
                row["current"] = os.path.basename(current or "")

    def _worker() -> None:
        try:
            payload = _analytics_aggregate_compute(log_dir, rows_in, body, progress=_progress)
            with _analytics_jobs_lock:
                row = _analytics_jobs.get(job_id)
                if row is not None:
                    row.update(state="done", payload=payload, current="")
        except Exception as e:  # a worker crash must reach the operator, not just a log
            _logger.exception("analytics aggregate job failed")
            with _analytics_jobs_lock:
                row = _analytics_jobs.get(job_id)
                if row is not None:
                    row.update(state="error", error=str(e))

    threading.Thread(target=_worker, name=f"analytics-{job_id[:8]}", daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/analytics/aggregate-status", methods=["GET"])
@action(get="analytics:read")
def api_analytics_aggregate_status():
    """Progress for a job started by ``/api/analytics/aggregate-async``.

    The payload rides along only once the job is done, so polling stays cheap.
    """
    job_id = (request.args.get("job_id") or "").strip()
    with _analytics_jobs_lock:
        row = _analytics_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        out = {k: v for k, v in row.items() if k != "payload"}
        if row.get("state") == "done":
            out["payload"] = row.get("payload")
            # One successful hand-off is enough; free the payload so a stale tab cannot pin it.
            row.pop("payload", None)
            row["state"] = "collected"
    out["ok"] = True
    return jsonify(out)


def _analytics_aggregate_guard():
    """Shared refusals for both the blocking and async routes. ``None`` means proceed."""
    if os.environ.get("RACK_ANALYTICS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "analytics disabled (RACK_ANALYTICS_DISABLED)"}), 403
    return None


def _analytics_aggregate_inputs(body: dict):
    """Validate the POST body. Returns ``((log_dir, done_racks), None)`` or ``(None, response)``."""
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rows_in = body.get("rows")
    if not isinstance(rows_in, list):
        return None, (
            jsonify(
                {"ok": False, "error": 'send JSON {"log_dir": "…", "rows": [{"rack":"…","nemo":"…"}, …]}'}
            ),
            400,
        )
    return (log_dir, rows_in), None


def _analytics_aggregate_compute(log_dir, rows_in, body, progress=None):
    """The aggregate itself. *progress* is called as ``(done, total, current_path)``.

    Multirack: successful multirack logs only; NCCL one series per log. Nemotron scaling /
    throughput / efficiency tables group logs by inferred rack count, including bucket Rack=1 from
    successful single-rack logs. Per-bucket means for step and TFLOPs; nodes/# GPU from the newest
    log in each bucket.
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        demo = dict(_demo_analytics_aggregate())
        demo["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        demo["rows_fingerprint"] = "demo"
        demo["cached"] = False
        return demo

    done_racks: list[str] = []
    for ent in rows_in:
        if not isinstance(ent, dict):
            continue
        rk = str(ent.get("rack", "")).strip()
        nemo = str(ent.get("nemo", "")).strip().lower()
        # A rack qualifies if EITHER plane has a result. Gating on nemo=="done" alone made
        # Analytics empty for any rack that ran only a Stage-1 NCCL sweep -- and with no
        # Nemotron run anywhere on the cluster that meant "Run Analytics" returned nothing at
        # all (48 racks: 12 nemo=fail, 36 nemo=pending, 0 done, while h03 had a COMPLETED
        # 24-test NCCL sweep, job 65828). The NCCL and NEMO logs are already selected
        # independently below, so admitting NCCL-only racks here needs no downstream change.
        stage1 = str(ent.get("stage1", "")).strip().lower()
        nccl = str(ent.get("nccl", "")).strip().lower()
        if not rk or not (nemo == "done" or stage1 == "done" or nccl == "done"):
            continue
        try:
            done_racks.append(_normalize_rack_id(rk))
        except ValueError:
            continue

    # The SPA posts ONLY {rack, nemo} -- AnalyticsView.tsx bodyRows does
    #   rows.map(r => ({ rack: r.rack, nemo: r.nemo }))
    # so stage1/nccl above can never arrive from the browser (they only work for API callers).
    # The frontend is baked into the image, so instead of requiring a rebuild, discover racks
    # that have a single-rack NCCL log straight from the log dir. ONE listing for the whole
    # directory, not one per rack, so this costs a single round trip over SSH.
    try:
        _naming = _nvis_naming()
        _base_root = str(native_status._host_resolve(log_dir)).rstrip("/")
        for _base in (_base_root, _base_root + "/failed"):
            for _path, _mt, _sz in native_status._host_log_entries(_base):
                _name = os.path.basename(_path)
                if "nccl" not in _name.lower():
                    continue
                if not _nemo_single_rack_log_basename(_name) or _is_multirack_log_basename(_name):
                    continue
                if _naming.is_llama_slurm_stdout_basename(_name):
                    continue
                _rid = _naming.rack_id_from_log_basename(_name) or ""
                if not _rid:
                    continue
                try:
                    done_racks.append(_normalize_rack_id(_rid))
                except ValueError:
                    continue
    except Exception as _e:  # discovery is best-effort; never break the posted-rows path
        _logger.warning("analytics: NCCL rack discovery failed: %s", _e, exc_info=True)

    done_racks = sorted(set(done_racks))

    # ---- ONE scan for every log this aggregate will look at -----------------------------------
    # Previously each log below cost a parser.sh launch (about twenty subprocesses and sixteen
    # reads of the file), which is why this endpoint took minutes on a large fleet. Scanning the
    # whole corpus in a single pass measured 39.6 s for 1020 files / 1973 MB on Nscale TX, and it
    # is the only place that knows how many files there are — so it is also what makes an honest
    # progress bar possible instead of a spinner.
    prescan_paths: list[str] = []
    _seen_paths: set[str] = set()
    try:
        for rack in done_racks:
            for p in find_log_paths_for_rack(log_dir, rack):
                sp = str(_rpath(p))
                if sp not in _seen_paths:
                    _seen_paths.add(sp)
                    prescan_paths.append(sp)
        # Candidates, not the filtered list: the filter reads each file, so filtering before the
        # batch would spend a process per candidate to decide what the batch should read.
        for _path, _mt, p in _multirack_candidate_log_paths(log_dir):
            sp = str(_rpath(p))
            if sp not in _seen_paths:
                _seen_paths.add(sp)
                prescan_paths.append(sp)
    except Exception:  # enumeration is best effort; the loops below re-derive what they need
        _logger.debug("analytics prescan enumeration failed", exc_info=True)

    _analytics_scan_ctx.batch = _logscan_batch(prescan_paths, on_progress=progress) if prescan_paths else {}

    series_by_rack: dict[str, list[dict[str, float]]] = {}
    coll_series_by_rack: dict[str, dict[str, list[dict[str, float]]]] = {c: {} for c in _SINGLE_RACK_COLLECTIVES}
    scalar_rows: list[dict[str, object]] = []
    nemotron_rows: list[dict[str, object]] = []
    scaling_entries: dict[int, list[dict[str, object]]] = {}
    # Successful runs whose step/TFLOPs could not be averaged, keyed the same way. They get a
    # row with null means instead of being dropped: a 50-step multirack NeMo run used to pass
    # every gate, contribute its NCCL series to the chart, and then vanish from the tables with
    # no indication it had ever been considered.
    scaling_no_metrics: dict[int, list[str]] = {}
    nccl_scaling_entries: dict[int, list[dict[str, object]]] = {}

    for rack in done_racks:
        cands = find_log_paths_for_rack(log_dir, rack)
        if not cands:
            continue
        # Select the NCCL and NEMO single-rack logs INDEPENDENTLY (candidates are newest-first).
        # Previously we picked the single newest *1Rack*.txt and gated it on the NEMO-success
        # grep (LLM step markers); that silently dropped any rack whose most-recent single-rack
        # log was a standalone NCCL sweep — so a fresh NCCL run would hide the rack from
        # analytics even though its logs are present and valid.
        nccl_lp = next((p for p in cands if "nccl" in p.name.lower()), None)
        nemo_lp = next(
            (p for p in cands if "nccl" not in p.name.lower() and _nemo_log_file_looks_successful(p)),
            None,
        )

        # --- NCCL (all_reduce bus-bw) from the newest NCCL single-rack log --------------
        if nccl_lp is not None:
            parsed_nc, _ = _parser_json_for_log_path(log_dir, nccl_lp)
            nc = _pick_nccl_row(parsed_nc, rack) if isinstance(parsed_nc, dict) else None
            if isinstance(nc, dict):
                lf = nccl_lp.name
                ar_series: list[dict[str, float]] = []
                raw_ser = nc.get("all_reduce_busbw_series") or []
                if isinstance(raw_ser, list):
                    for p in raw_ser:
                        if not isinstance(p, dict):
                            continue
                        try:
                            sz = int(p.get("size_bytes") or 0)
                            bw = float(p.get("bus_bw_gbps") or 0)
                        except (TypeError, ValueError):
                            continue
                        if sz > 0 and bw >= 0:
                            ar_series.append({"size_bytes": float(sz), "bus_bw_gbps": bw})
                if ar_series:
                    series_by_rack[rack] = ar_series
                for coll, ser in _collective_series_for_log(nccl_lp, nc).items():
                    coll_series_by_rack[coll][rack] = ser
                scalar_rows.append(
                    {
                        "rack": rack,
                        "log_file": lf,
                        "all_reduce_gbps": _parse_gbps_scalar(nc.get("all_reduce_gbps")),
                        "all_gather_gbps": _parse_gbps_scalar(nc.get("all_gather_gbps")),
                        "alltoall_gbps": _parse_gbps_scalar(nc.get("alltoall_gbps")),
                        # NVIS 2026-07-28: parser.sh now also emits these two (nccl_busbw_ext_csv).
                        "reduce_scatter_gbps": _parse_gbps_scalar(nc.get("reduce_scatter_gbps")),
                        "sendrecv_gbps": _parse_gbps_scalar(nc.get("sendrecv_gbps")),
                    }
                )
                _nccl_scaling_append_entry(nccl_scaling_entries, nccl_lp, nc)

        # --- Nemotron from the newest successful NEMO single-rack log -------------------
        if nemo_lp is not None:
            parsed_nm, _ = _parser_json_for_log_path(log_dir, nemo_lp)
            nm = _pick_nemotron_row(parsed_nm, rack) if isinstance(parsed_nm, dict) else None
            if isinstance(nm, dict):
                lf = nemo_lp.name
                med = _parse_gbps_scalar(nm.get("median_step_time"))
                if med is None:
                    med = _parse_gbps_scalar(nm.get("step_s"))
                tf = _parse_gbps_scalar(nm.get("tflops_per_gpu"))
                nemotron_rows.append(
                    {
                        "rack": rack,
                        "log_file": lf,
                        "median_step_time": med,
                        "tflops_per_gpu": tf,
                        "step_time_cv_pct": _step_series_cv_pct(nm),
                    }
                )
                if med is not None and tf is not None:
                    _nemotron_scaling_append_entry(scaling_entries, 1, nemo_lp, float(tf), float(med))
                else:
                    scaling_no_metrics.setdefault(1, []).append(lf)

    log_by_rack = {str(r["rack"]): str(r["log_file"]) for r in scalar_rows}
    ar_block = _busbw_series_block(series_by_rack, log_by_rack)

    coll_dev: list[dict[str, object]] = []
    for metric, key in (
        ("all_reduce_gbps", "all_reduce_gbps"),
        ("all_gather_gbps", "all_gather_gbps"),
        ("alltoall_gbps", "alltoall_gbps"),
        ("reduce_scatter_gbps", "reduce_scatter_gbps"),
        ("sendrecv_gbps", "sendrecv_gbps"),
    ):
        vals = [float(r[key]) for r in scalar_rows if r.get(key) is not None]
        if not vals:
            continue
        med = statistics.median(vals)
        for r in scalar_rows:
            v = r.get(key)
            if v is None or med <= 0:
                continue
            vf = float(v)
            coll_dev.append(
                {
                    "metric": metric,
                    "rack": r["rack"],
                    "log_file": r["log_file"],
                    "value_gbps": round(vf, 4),
                    "fleet_median_gbps": round(med, 4),
                    "pct_vs_median": round((vf - med) / med * 100.0, 2),
                }
            )

    mean_tf = statistics.mean([float(r["tflops_per_gpu"]) for r in nemotron_rows if r.get("tflops_per_gpu") is not None]) if any(
        r.get("tflops_per_gpu") is not None for r in nemotron_rows
    ) else None
    mean_step = statistics.mean([float(r["median_step_time"]) for r in nemotron_rows if r.get("median_step_time") is not None]) if any(
        r.get("median_step_time") is not None for r in nemotron_rows
    ) else None
    for r in nemotron_rows:
        tf = r.get("tflops_per_gpu")
        st = r.get("median_step_time")
        if mean_tf is not None and tf is not None and float(mean_tf) > 0:
            r["pct_vs_mean_tflops"] = round((float(tf) - float(mean_tf)) / float(mean_tf) * 100.0, 2)
        else:
            r["pct_vs_mean_tflops"] = None
        if mean_step is not None and st is not None and float(mean_step) > 0:
            r["pct_vs_mean_step_time"] = round((float(st) - float(mean_step)) / float(mean_step) * 100.0, 2)
        else:
            r["pct_vs_mean_step_time"] = None

    nccl_multirack: list[dict[str, object]] = []
    for lp in _multirack_done_log_paths(log_dir):
        parsed, _err = _parser_json_for_log_path(log_dir, lp)
        if parsed is None:
            continue
        label = multirack_display_rack_from_basename(lp.name)
        nc = _pick_nccl_row(parsed, "")
        ser_out: list[dict[str, float]] = []
        if isinstance(nc, dict):
            raw_ser = nc.get("all_reduce_busbw_series") or []
            if isinstance(raw_ser, list):
                for p in raw_ser:
                    if not isinstance(p, dict):
                        continue
                    try:
                        sz = int(p.get("size_bytes") or 0)
                        bw = float(p.get("bus_bw_gbps") or 0)
                    except (TypeError, ValueError):
                        continue
                    if sz > 0:
                        ser_out.append({"size_bytes": float(sz), "bus_bw_gbps": bw})
        if ser_out:
            nccl_multirack.append({"label": label, "basename": lp.name, "series": ser_out})
        if isinstance(nc, dict):
            _nccl_scaling_append_entry(nccl_scaling_entries, lp, nc)
        # parser.sh (the non-batch path) emits a placeholder Nemotron row for any file, so an NCCL log
        # is not a NeMo run however its row looks. The single-rack loop above already selects NeMo
        # logs by this same name test; the multirack loop did not, and filed 52 NCCL logs under
        # Rack 2-4 as NeMo runs with no step timings.
        is_nccl_only = "nccl" in lp.name.lower() and "nemo" not in lp.name.lower()
        nm = None if is_nccl_only else _pick_nemotron_row(parsed, "")
        if isinstance(nm, dict):
            n_r = _multirack_rack_count_from_basename(lp.name)
            n_r = max(n_r, 1)
            tf = _parse_gbps_scalar(nm.get("tflops_per_gpu"))
            st = _parse_gbps_scalar(nm.get("median_step_time"))
            if st is None:
                st = _parse_gbps_scalar(nm.get("step_s"))
            if tf is not None and st is not None:
                tf_f = float(tf)
                st_f = float(st)
                _nemotron_scaling_append_entry(scaling_entries, n_r, lp, tf_f, st_f)
            else:
                scaling_no_metrics.setdefault(n_r, []).append(lp.name)

    nemotron_scaling: list[dict[str, object]] = []
    nemotron_throughput_scaling: list[dict[str, object]] = []
    for n_r in sorted(set(scaling_entries) | set(scaling_no_metrics)):
        ents = scaling_entries.get(n_r) or []
        no_metrics = scaling_no_metrics.get(n_r) or []
        if not ents and not no_metrics:
            continue
        mean_tf = round(statistics.mean(float(e["tf"]) for e in ents), 3) if ents else None
        mean_st = round(statistics.mean(float(e["st"]) for e in ents), 4) if ents else None
        n_logs = len(ents)
        nemotron_scaling.append(
            {
                "n_racks": n_r,
                "n_logs": n_logs,
                "n_logs_no_metrics": len(no_metrics),
                "logs_no_metrics": no_metrics,
                "mean_tflops_per_gpu": mean_tf,
                "mean_step_s": mean_st,
            }
        )
        num_nodes: int | None = None
        num_gpus: int | None = None
        mtps = mtpsg = None
        if ents:
            rep = max(ents, key=lambda e: _log_mtime_or_zero(e["path"]))  # type: ignore[arg-type]
            nn = rep.get("num_nodes")
            ng = rep.get("num_gpus")
            num_nodes = int(nn) if nn is not None else None
            num_gpus = int(ng) if ng is not None else None
            mtps = _mean_finite([e.get("throughput_tokens_s") for e in ents])  # type: ignore[list-item]
            mtpsg = _mean_finite([e.get("throughput_tokens_s_per_gpu") for e in ents])  # type: ignore[list-item]
        days: float | None = None
        if mtps is not None and mtps > 0:
            days = 1_000_000_000_000.0 / (mtps * 86400.0)
        nemotron_throughput_scaling.append(
            {
                "n_racks": n_r,
                "n_logs": n_logs,
                "n_logs_no_metrics": len(no_metrics),
                "logs_no_metrics": no_metrics,
                "num_nodes": num_nodes,
                "num_gpus": num_gpus,
                "mean_step_s": mean_st,
                "mean_tflops_per_gpu": mean_tf,
                "mean_throughput_tokens_s": None if mtps is None else round(mtps, 4),
                "mean_throughput_tokens_s_per_gpu": None if mtpsg is None else round(mtpsg, 4),
                "time_to_train_1t_days": None if days is None else round(days, 4),
            }
        )

    analyzed_at = datetime.now(timezone.utc).isoformat()
    rows_fp = _analytics_rows_fingerprint(rows_in)
    payload: dict[str, object] = {
        "ok": True,
        "analyzed_at": analyzed_at,
        "rows_fingerprint": rows_fp,
        "cached": False,
        "single_rack": {
            "nccl_collectives": scalar_rows,
            "nccl_collectives_deviation_pct": coll_dev,
            "nccl_ar_series": ar_block,
            # The same block for each collective, so the view can switch between them.
            "nccl_series_by_collective": {
                coll: _busbw_series_block(by_rack, log_by_rack)
                for coll, by_rack in coll_series_by_rack.items()
                if by_rack
            },
            "nemotron": nemotron_rows,
        },
        "multirack": {
            "nccl_series_by_log": nccl_multirack,
            "nemotron_scaling": nemotron_scaling,
            "nccl_scaling": _nccl_scaling_rows(nccl_scaling_entries),
            "nemotron_throughput_scaling": nemotron_throughput_scaling,
        },
    }
    # NEW key only — ``single_rack`` / ``multirack`` above are byte-for-byte what they were.
    # The fleet baseline here is computed over the durable run history, so it is populated
    # even when no rack's ``nemo`` cell is ``done`` (the gate the tables above still apply).
    try:
        payload["history"] = _analytics_history_block(
            (body.get("since") or "").strip() or None if isinstance(body.get("since"), str) else None
        )
    except Exception as e:  # noqa: BLE001 - never fail the analytics response for this
        _logger.warning("analytics history block failed: %s", e)
        payload["history"] = {"available": False, "reason": str(e)}
    try:
        _analytics_aggregate_cache_put(str(_rpath(log_dir)), payload)
    except OSError:
        _logger.exception("analytics aggregate cache write failed")
    return payload
