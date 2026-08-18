# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Turning one log file into a stored result: normalise, overlay metrics, record, publish.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The middle of the results pipeline (``core-designs.mdc`` §7). ``nvis_logscan.py`` does the single
whole-file pass on the host; ``logscan_cache.py`` caches it; this module takes what comes back, folds
in the universal normaliser, records a durable run in ``results_history`` and publishes to the lake.

Extracted because it was the last thing binding app.py together: these twelve names were reachable
from exactly the same 17 routes -- parser results, log reads, analytics and validation activities -- so
none of those domains could move out while they stayed. That they all shared one number is what
identified them as a unit rather than a pile.

Read caps are deliberate. ``_read_log_text_capped`` bounds head/tail reads because validation logs
reach hundreds of MB, and a probe that might not find its answer must not cost more than one that
does. The caps are the fallback path for when the scanner is unavailable; prefer the scanner.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import core
import host_exec
import lake_sink
import results_history
from core import truthy_env as _truthy_env
from logscan_cache import _log_stat_for_path, _logscan_record

try:
    import universal_parser as _uparse
except ImportError:  # optional normaliser
    _uparse = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)


_UNIVERSAL_HEAD_BYTES = 2 * 1024 * 1024


_UNIVERSAL_TAIL_BYTES = 1 * 1024 * 1024


_UNIVERSAL_WHOLE_BYTES = _UNIVERSAL_HEAD_BYTES + _UNIVERSAL_TAIL_BYTES


def _universal_enabled() -> bool:
    """True when the universal log normaliser is importable and not disabled by env."""
    return _uparse is not None and not _truthy_env("RACK_UNIVERSAL_PARSER_DISABLED")


def _read_log_text_capped(log_path_s: str, *, timeout: int = 120) -> tuple[str, dict[str, object]]:
    """Head+tail capped read of a log on the execution host. ``(text, meta)``; never raises.

    Thin wrapper over the connection, which applies the same head/tail cap wherever
    the log lives. ``remote`` is kept in *meta* for the existing API consumers.
    """
    text, meta = host_exec.read_text(
        log_path_s,
        head_bytes=_UNIVERSAL_HEAD_BYTES,
        tail_bytes=_UNIVERSAL_TAIL_BYTES,
        whole_bytes=_UNIVERSAL_WHOLE_BYTES,
        timeout=timeout,
    )
    meta["remote"] = meta.get("target") == "ssh"
    return text, meta


def _universal_parse_log(
    log_dir: Path,
    log_path_s: str,
    *,
    kind: str | None = None,
    running_hint: bool | None = None,
    timeout: int = 120,
) -> dict[str, object] | None:
    """Run ``universal_parser.parse`` over one log on the exec host. ``None`` when unavailable."""
    if not _universal_enabled():
        return None
    text, meta = _read_log_text_capped(log_path_s, timeout=timeout)
    if meta.get("read_error") and not text:
        # A read failure is still a result: universal_parser turns it into a ``fail`` with the
        # reason attached, which is far more useful than a blank panel.
        text = ""
    try:
        res = _uparse.parse(
            log_path_s,
            text,
            kind=kind,
            running_hint=running_hint,
            meta={k: v for k, v in meta.items() if k in ("bytes", "truncated", "read_error", "gzip")},
            head_bytes=_UNIVERSAL_HEAD_BYTES,
            tail_bytes=_UNIVERSAL_TAIL_BYTES,
        )
    except Exception as e:  # noqa: BLE001 - the module promises not to raise; belt and braces
        _logger.warning("universal_parser.parse failed for %s: %s", log_path_s, e)
        return None
    return _overlay_logscan_metrics(res, log_path_s, timeout=timeout)


_LOGSCAN_METRIC_OVERLAY = (
    ("nemo_step_time_mean", ("nemo", "step_time_s"), "s"),
    ("nemo_step_time_stdev", ("nemo", "step_time_std"), "s"),
    ("nemo_model_tflops_per_gpu_mean", ("nemo", "tflops_per_gpu"), "TFLOP/s"),
)


def _overlay_logscan_metrics(
    res: dict[str, object] | None, log_path_s: str, *, timeout: int = 120
) -> dict[str, object] | None:
    """Replace metrics that a capped read can only guess at with whole-file values.

    universal_parser sees 2 MB of head and 1 MB of tail. That is fine for a 5 MB log and useless
    for a 973 MB one, where every training sample sits in the discarded middle: the run reached
    history and the lake with no metrics at all. When the scanner has read the file, its numbers
    win and ``truncated`` becomes false, because nothing was truncated.
    """
    if not isinstance(res, dict) or not res.get("truncated"):
        return res
    rec = _logscan_record(log_path_s, timeout=timeout)
    if not rec:
        return res

    by_name = {m.get("name"): m for m in res.get("metrics", []) if isinstance(m, dict)}
    for name, (section, key), unit in _LOGSCAN_METRIC_OVERLAY:
        val = (rec.get(section) or {}).get(key)
        if val is None:
            continue
        if name in by_name:
            by_name[name]["value"] = val
        else:
            res.setdefault("metrics", []).append({"name": name, "value": val, "unit": unit})

    it = (rec.get("nemo") or {}).get("iteration")
    extra = res.get("extra")
    if isinstance(extra, dict):
        extra["nemo_training_finished"] = bool((rec.get("nemo") or {}).get("finished"))
        if it:
            extra["nemo_last_iteration"] = list(it)
        extra["metrics_from_whole_file_scan"] = True
    res["truncated"] = False
    res["bytes"] = rec.get("bytes", res.get("bytes"))
    return res


def _history_record_from_universal(
    res: dict[str, object],
    *,
    source_log_path: str,
    log_mtime: float | None,
    log_size: int | None,
    rack_hint: str | None = None,
    source: str = "universal_parser",
) -> dict[str, object]:
    """Map a ``universal_parser.parse`` result onto a ``results_history.ingest_run`` record."""
    size = log_size if log_size is not None else res.get("bytes")
    extra: dict[str, object] = {
        "parser_version": res.get("parser_version"),
        "headline": res.get("headline"),
        "exit_code": res.get("exit_code"),
        "truncated": bool(res.get("truncated")),
        "racks": res.get("racks") or [],
        "errors": (res.get("errors") or [])[:10],
        "warnings": (res.get("warnings") or [])[:10],
    }
    if isinstance(res.get("extra"), dict) and res["extra"]:
        extra["parser_extra"] = res["extra"]
    rec: dict[str, object] = {
        # Salted so a universal record can never collide with the parser.sh records for the
        # same log snapshot (results_history salts those with ""/nccl/nemo/mnubergemm).
        "run_id": results_history.make_run_id(source_log_path, log_mtime, size, salt="universal"),
        "source_log_path": str(source_log_path),
        "log_basename": res.get("log_basename") or os.path.basename(str(source_log_path)),
        "log_mtime": log_mtime,
        "log_size": size,
        "workload_kind": res.get("kind"),
        "rack": res.get("rack") or rack_hint,
        "nodes": res.get("nodes") or [],
        "job_id": res.get("job_id"),
        "started_at": res.get("started_at"),
        "ended_at": res.get("ended_at"),
        "status": res.get("status"),
        "metrics": res.get("metrics") or [],
        "source": source,
        "extra": extra,
    }
    return rec


def _history_ingest_universal(
    res: dict[str, object] | None,
    *,
    source_log_path: str,
    log_mtime: float | None = None,
    log_size: int | None = None,
    rack_hint: str | None = None,
    source: str = "universal_parser",
) -> str | None:
    """Persist one universal_parser result. Returns the run_id, or ``None`` on any problem."""
    if not res or not core.history_enabled():
        return None
    run_id: str | None = None
    try:
        run_id = results_history.ingest_run(
            _history_record_from_universal(
                res,
                source_log_path=source_log_path,
                log_mtime=log_mtime,
                log_size=log_size,
                rack_hint=rack_hint,
                source=source,
            )
        )
    except Exception as e:  # noqa: BLE001 - history is best-effort
        _logger.warning("results_history universal ingest failed for %s: %s", source_log_path, e)
        return None

    # Every run that reaches SQLite must also reach the Parquet lake — this is the single choke
    # point all parsed runs flow through, so wiring it here is what makes "no more misses" true.
    # Best-effort: the lake must never break a validation run, but a failure is LOGGED, never
    # silently swallowed (a quiet provenance gap is indistinguishable from a successful capture).
    _lake_push_universal(res, source_log_path=source_log_path, run_id=run_id)
    return run_id


def _lake_push_job(job_id: str, row: dict) -> None:
    """Mirror ANY completed Rack Status action (hw_check, validation, reset, stress, bug-report…)
    into the Parquet lake.

    The user's requirement, verbatim: *"any check any click on any test I do in rackstatus goes into
    parquet"*. Result-log parsing alone missed everything that is not a Slurm workload — a hardware
    check that finds a fault, a gpu-reset, a drain — none of which write a log to the log dir. Those
    ARE events and must be as recoverable as a benchmark number.

    Best-effort and never raises: an action must not fail because provenance capture did.
    """
    if lake_sink is None or lake_sink.is_disabled():
        return
    try:
        kind = str(row.get("kind") or "action")
        rack = str(row.get("rack") or "")
        phase = str(row.get("phase") or "")
        rc = row.get("returncode")
        status = "pass" if (phase == "done" and (rc in (0, None))) else "fail"
        record = {
            "kind": f"rackstatus_{kind}",
            "status": status,
            "job_id": job_id,
            "rack": rack,
            "phase": phase,
            "returncode": rc,
            "operator": row.get("operator"),
            "preset": row.get("preset"),
            "checker_args": row.get("checker_args"),
            "command": row.get("command_display"),
            "created_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
            "error": row.get("error"),
            "nodes": row.get("specs") or row.get("nodes") or [],
        }
        # stdout/stderr are the actual evidence (which trays failed, and why) — archive verbatim.
        body = "\n".join(
            x for x in (
                f"$ {row.get('command_display') or ''}",
                "--- stdout ---", str(row.get("stdout") or ""),
                "--- stderr ---", str(row.get("stderr") or ""),
            ) if x is not None
        )
        out = lake_sink.push_run(
            run_id=lake_sink.make_run_id(f"action-{kind}", rack, job_id),
            workload=f"rackstatus_{kind}",
            rack=rack,
            job_id=job_id,
            nodes=[str(n) for n in (record["nodes"] or [])],
            status=status,
            parsed=record,
            log_path=f"synthesized:{kind}-{job_id}.log",
            log_text=body,
        )
        if not out.get("ok"):
            _logger.warning("lake action push failed (%s %s): %s", kind, job_id, out.get("error"))
    except Exception as e:  # noqa: BLE001 - provenance must never break an action
        _logger.warning("lake action push raised (%s): %s", job_id, e)


def _lake_push_universal(
    res: dict[str, object],
    *,
    source_log_path: str,
    run_id: str | None,
) -> None:
    """Mirror one parsed run into the Parquet lake (run_artifacts: result + raw log + inputs)."""
    if lake_sink is None or lake_sink.is_disabled():
        return
    try:
        workload = str(res.get("kind") or "unknown")
        rack = str(res.get("rack") or "")
        job_id = res.get("job_id")
        lake_run_id = lake_sink.make_run_id(workload, rack, job_id or run_id)
        out = lake_sink.push_run(
            run_id=lake_run_id,
            workload=workload,
            rack=rack,
            job_id=job_id,
            nodes=[str(n) for n in (res.get("nodes") or [])],
            status=str(res.get("status") or "unknown"),
            parsed=res,
            log_path=source_log_path,
            # `tail` is what the parser actually read; the full log lives on the head node and is
            # re-readable via /api/rack-log-content, so we archive the excerpt, not a re-fetch.
            log_text=str(res.get("tail") or ""),
        )
        if not out.get("ok"):
            _logger.warning("lake push failed for %s: %s", source_log_path, out.get("error"))
    except Exception as e:  # noqa: BLE001 - never let provenance break a run
        _logger.warning("lake push raised for %s: %s", source_log_path, e)


def _universal_fallback_for_log(
    log_dir: Path,
    log_path_s: str,
    *,
    rack_hint: str | None = None,
    ingest: bool = True,
) -> dict[str, object] | None:
    """Universal parse + optional history ingest for one log. ``None`` when unavailable."""
    res = _universal_parse_log(log_dir, log_path_s)
    if res is None:
        return None
    if ingest:
        mt, sz = _log_stat_for_path(log_dir, log_path_s)
        rid = _history_ingest_universal(
            res,
            source_log_path=log_path_s,
            log_mtime=mt,
            log_size=sz,
            rack_hint=rack_hint,
        )
        if rid:
            res = {**res, "history_run_id": rid}
    return res
