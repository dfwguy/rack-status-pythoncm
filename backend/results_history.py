# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Historic results store for NVIS Rack Status (SQLite, pure stdlib).

Why this exists
---------------
Every view in the dashboard is recomputed per request by re-running
``parser.sh`` over the log directory on the head node; nothing is retained.
There is no history, no trend, no fleet-deviation baseline.  The only on-disk
artifacts today are two *caches* (``var/cache/native_log_analysis.json`` and
``var/cache/analytics_aggregate_cache.json``): the first prunes a record the
moment the log disappears, the second keeps exactly one entry per log dir and
overwrites it on every POST.  This module is the missing durable layer.

Design notes
------------
* **Storage**: one SQLite file, ``$RACK_RESULTS_DB`` or
  ``<repo-root>/var/history/results.db``.  ``<repo-root>`` is the parent of
  ``backend/`` (``/app`` in the container), matching how ``app.py`` resolves
  ``var/cache/*`` — and ``/app/var`` is the named volume ``rack-data``, so the
  database survives container restarts *and* image rebuilds.
* **Idempotency**: ``run_id`` is a stable hash of
  ``(source_log_path, log_mtime, log_size)``.  Re-ingesting an unchanged log
  updates the same row instead of duplicating it; a log that grew (a running
  job) yields a new ``run_id``, so the growth history is preserved.
* **Schema churn**: workload-specific numbers live in a narrow key/value
  ``run_metrics`` table, so a new tool (nvbandwidth, HPL, DCGM, nvloom, ...)
  needs no migration — it just writes different ``metric_name`` rows.
* **Concurrency**: several Flask/gunicorn workers touch this file.  There is no
  module-level connection: every public call opens its own short-lived
  connection with WAL journalling and a busy timeout, and writers take
  ``BEGIN IMMEDIATE`` so two workers never deadlock on a lock upgrade.
* **Self-initialising**: every public function ensures the schema exists first,
  so callers may skip :func:`init_db` entirely.

Pure standard library.  No Flask, no third-party imports: importable from
``app.py``, from a cron script, or from a plain ``python3 -c``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "KNOWN_WORKLOAD_KINDS",
    "RUN_STATUSES",
    "SCHEMA_VERSION",
    "ResultsHistoryError",
    "classify_workload_kind",
    "count_runs",
    "db_path",
    "db_stats",
    "delete_run",
    "get_run",
    "ingest_parser_json",
    "ingest_run",
    "ingest_runs",
    "ingest_runs_checkpointed",
    "init_db",
    "latest_run_for_log",
    "latest_runs_by_rack",
    "latest_successful_runs_by_rack",
    "list_runs",
    "make_run_id",
    "metric_names",
    "metric_series",
    "normalize_status",
    "prune_runs",
    "records_from_parser_json",
    "summary_stats",
]

SCHEMA_VERSION = 2

#: Canonical workload kinds.  Unknown kinds are still stored verbatim (slugified)
#: rather than being collapsed to ``unknown`` — nothing is silently lost — but
#: these are the values the UI can rely on.
KNOWN_WORKLOAD_KINDS = (
    "nccl",
    "nemo",
    "mnubergemm",
    "nvbandwidth",
    "hpl",
    "dcgm",
    "nvloom",
    "clusterkit",
    "bringup",
    "unknown",
)

RUN_STATUSES = ("pass", "fail", "timeout", "unknown")

# Substring -> canonical kind.  Longest match wins (checked in this order), so
# "mnubergemm" is not swallowed by "ubergemm" and "nccl" does not steal a
# "nvbandwidth-nccl-sanity" style name from its more specific prefix.
_KIND_MARKERS: tuple[tuple[str, str], ...] = (
    ("mnubergemm", "mnubergemm"),
    ("ubergemm", "mnubergemm"),
    ("nvbandwidth", "nvbandwidth"),
    ("nvloom", "nvloom"),
    ("clusterkit", "clusterkit"),
    ("root-ck-", "clusterkit"),
    ("nemotron", "nemo"),
    ("nemo", "nemo"),
    ("nccl", "nccl"),
    ("healthcheck", "bringup"),
    ("health-check", "bringup"),
    ("bringup", "bringup"),
    ("sysinfo", "bringup"),
    ("dcgm", "dcgm"),
    ("nvswitch", "bringup"),
    ("imex", "bringup"),
    ("hpl", "hpl"),
)

_STATUS_ALIASES = {
    "ok": "pass",
    "pass": "pass",
    "passed": "pass",
    "success": "pass",
    "succeeded": "pass",
    "done": "pass",
    "good": "pass",
    "true": "pass",
    "1": "pass",
    "fail": "fail",
    "failed": "fail",
    "failure": "fail",
    "error": "fail",
    "bad": "fail",
    "false": "fail",
    "0": "fail",
    "timeout": "timeout",
    "timedout": "timeout",
    "timed_out": "timeout",
    "cancelled": "timeout",
    "canceled": "timeout",
    "unk": "unknown",
    "unknown": "unknown",
    "": "unknown",
    "-": "unknown",
    "?": "unknown",
    "n/a": "unknown",
    "none": "unknown",
    "pending": "unknown",
    "running": "unknown",
    "ongoing": "unknown",
}

# Values parser.sh emits when it has nothing (see parser.sh placeholder rows:
# a foreign log still produces a fully-formed row of dashes).  These must never
# be stored as real measurements.
_PLACEHOLDERS = frozenset({"", "-", "--", "---", "?", "??", "n/a", "na", "none", "null", "nan"})

_DEFAULT_BUSY_MS = 5000
_MAX_STATS_SAMPLES = 200_000

_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


class ResultsHistoryError(RuntimeError):
    """Raised for caller errors (bad record, unusable database path)."""


# ---------------------------------------------------------------------------
# paths / connections
# ---------------------------------------------------------------------------

_PKG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parent  # /app in the container, repo root on a dev box


def db_path() -> Path:
    """Absolute path of the history database.

    ``RACK_RESULTS_DB`` wins; otherwise ``<repo-root>/var/history/results.db``
    (``/app/var/history/results.db`` in the container, on the ``rack-data``
    volume).  Read on every call so the env var can be changed by tests.
    """
    raw = (os.environ.get("RACK_RESULTS_DB") or "").strip()
    if raw:
        return Path(raw).expanduser().absolute()
    return (_REPO_ROOT / "var" / "history" / "results.db").absolute()


def _busy_timeout_ms() -> int:
    try:
        val = int((os.environ.get("RACK_RESULTS_DB_BUSY_MS") or "").strip() or _DEFAULT_BUSY_MS)
    except (TypeError, ValueError):
        return _DEFAULT_BUSY_MS
    return max(100, min(val, 60_000))


@contextmanager
def _connect(*, write: bool = False, ensure: bool = True):
    """Yield a short-lived connection.  One per call; never shared or cached."""
    path = db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - unwritable volume
        raise ResultsHistoryError(f"cannot create history dir {path.parent}: {exc}") from exc

    busy_ms = _busy_timeout_ms()
    try:
        conn = sqlite3.connect(str(path), timeout=busy_ms / 1000.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise ResultsHistoryError(f"cannot open results db {path}: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        try:
            # WAL lets readers run while a worker writes.  Falls back silently
            # on filesystems without shared-memory support (e.g. some NFS).
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        conn.execute("PRAGMA foreign_keys=ON")
        if ensure:
            _ensure_schema(conn, str(path))
        if write:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if write:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        else:
            if write:
                conn.execute("COMMIT")
    finally:
        conn.close()


_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id          TEXT PRIMARY KEY,
        source_log_path TEXT NOT NULL,
        log_basename    TEXT NOT NULL,
        workload_kind   TEXT NOT NULL DEFAULT 'unknown',
        rack            TEXT,
        nodes           TEXT,            -- JSON array of node/tray ids
        job_id          TEXT,
        started_at      TEXT,            -- ISO-8601 UTC, sortable
        ended_at        TEXT,
        status          TEXT NOT NULL DEFAULT 'unknown',
        log_mtime       REAL,
        log_size        INTEGER,
        source          TEXT,            -- what produced this row (parser.sh, probe, ...)
        extra           TEXT,            -- JSON object, free-form
        first_seen_at   TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_metrics (
        run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        metric_name TEXT NOT NULL,
        -- seq 0 == the run's scalar summary for this metric (what trends and
        -- fleet stats use);  seq >= 1 == in-run series points (NCCL busbw curve,
        -- per-iteration step time), which stats deliberately ignore.
        seq         INTEGER NOT NULL DEFAULT 0,
        value       REAL,
        unit        TEXT,
        labels      TEXT,                        -- JSON object
        PRIMARY KEY (run_id, metric_name, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_kind_started  ON runs(workload_kind, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_rack_started  ON runs(rack, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_started       ON runs(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_log_path      ON runs(source_log_path, log_mtime)",
    "CREATE INDEX IF NOT EXISTS idx_runs_job           ON runs(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_name       ON run_metrics(metric_name)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_run        ON run_metrics(run_id)",
    """
    CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
        source_id       TEXT PRIMARY KEY,
        fingerprint     TEXT NOT NULL,
        source_path     TEXT NOT NULL,
        source_mtime_ns INTEGER,
        source_size     INTEGER,
        collected_at    TEXT,
        rows_ingested   INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT NOT NULL
    )
    """,
)


def _ensure_schema(conn: sqlite3.Connection, key: str) -> None:
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        # CREATE ... IF NOT EXISTS is safe to run concurrently from several
        # workers; a losing racer just sees the table already there.
        for stmt in _SCHEMA_SQL:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        _schema_ready.add(key)


def init_db() -> str:
    """Create the database and schema if absent.  Returns the db path.

    Optional: every other public function self-initialises.
    """
    path = db_path()
    with _connect(write=True):
        pass
    return str(path)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_placeholder(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() in _PLACEHOLDERS
    return False


def _to_float(val: Any) -> float | None:
    """Best-effort numeric coercion; ``None`` for parser placeholders/garbage."""
    if _is_placeholder(val):
        return None
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        out = float(val)
        return out if math.isfinite(out) else None
    if isinstance(val, str):
        txt = val.strip().replace(",", "")
        # tolerate a trailing unit ("123.4 GB/s", "45%")
        for cut in (" ", "\t"):
            if cut in txt:
                txt = txt.split(cut, 1)[0]
        txt = txt.rstrip("%")
        try:
            out = float(txt)
        except ValueError:
            return None
        return out if math.isfinite(out) else None
    return None


def _clean_str(val: Any) -> str | None:
    if _is_placeholder(val):
        return None
    txt = str(val).strip()
    return txt or None


def _json_dump(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        txt = val.strip()
        if not txt:
            return None
        try:  # already JSON?
            json.loads(txt)
            return txt
        except (TypeError, ValueError):
            return json.dumps([txt])
    try:
        return json.dumps(val, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps(str(val))


def _json_load(txt: Any, default: Any = None) -> Any:
    if not txt:
        return default
    try:
        return json.loads(txt)
    except (TypeError, ValueError):
        return default


def _iso(val: Any) -> str | None:
    """Coerce epoch seconds / datetime / string into a sortable ISO-8601 UTC string."""
    if _is_placeholder(val):
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return None
    txt = str(val).strip()
    if not txt:
        return None
    # bare epoch in a string
    if txt.replace(".", "", 1).isdigit() and len(txt.split(".")[0]) >= 9:
        return _iso(float(txt))
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        return txt  # keep whatever the caller had; still sorts lexically
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_workload_kind(name: Any, *, default: str = "unknown") -> str:
    """Map a log basename / tool name onto a canonical workload kind."""
    low = (str(name or "")).lower()
    if not low:
        return default
    for marker, kind in _KIND_MARKERS:
        if marker in low:
            return kind
    return default


def _norm_kind(val: Any, basename: str = "") -> str:
    txt = _clean_str(val)
    if txt:
        low = txt.strip().lower().replace(" ", "_")
        for marker, kind in _KIND_MARKERS:
            if marker in low:
                return kind
        if low in KNOWN_WORKLOAD_KINDS:
            return low
        # Unrecognised but explicit: keep it (slugified) rather than lose it.
        return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in low)[:64] or "unknown"
    return classify_workload_kind(basename)


def normalize_status(val: Any) -> str:
    """Coerce assorted status spellings into pass|fail|timeout|unknown."""
    if isinstance(val, bool):
        return "pass" if val else "fail"
    txt = (str(val) if val is not None else "").strip().lower()
    return _STATUS_ALIASES.get(txt, "unknown" if txt not in RUN_STATUSES else txt)


def make_run_id(source_log_path: Any, log_mtime: Any = None, log_size: Any = None, *, salt: str = "") -> str:
    """Stable 32-hex id for a log snapshot.

    Same (path, mtime, size) => same id => re-ingest is an UPSERT, not a
    duplicate.  A log that has grown since last ingest hashes differently and is
    stored as a new run, which is what you want for an in-progress job.
    """
    path = str(source_log_path or "").strip()
    if not path:
        raise ResultsHistoryError("source_log_path is required to build a run_id")
    mt = _to_float(log_mtime)
    sz = _to_float(log_size)
    parts = [
        path,
        "" if mt is None else f"{mt:.6f}",
        "" if sz is None else str(int(sz)),
        str(salt or ""),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# metric normalisation
# ---------------------------------------------------------------------------


def _normalize_metrics(raw: Any) -> list[dict[str, Any]]:
    """Accept several shapes and return ``[{metric_name, seq, value, unit, labels}]``.

    Accepted:
      * ``{"gflops_max": 1234.5, "verdict": "PASS"}``  (non-numeric silently dropped)
      * ``{"gflops_max": {"value": 1234.5, "unit": "GFLOP/s", "labels": {...}}}``
      * ``[{"name"/"metric_name": ..., "value": ..., "unit": ..., "labels": {...},
            "seq": 0}, ...]``
    Placeholder values ("-", "---", "?", "") are dropped, so a parser.sh
    all-dashes row contributes nothing instead of poisoning the trend.
    Duplicate (name, seq) pairs get the next free seq so series survive.
    """
    out: list[dict[str, Any]] = []
    if not raw:
        return out

    items: list[tuple[str, Any]] = []
    if isinstance(raw, Mapping):
        items = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, Mapping):
                name = entry.get("metric_name") or entry.get("name") or entry.get("metric")
                items.append((str(name or ""), entry))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                items.append((str(entry[0]), entry[1]))
    else:
        raise ResultsHistoryError(f"unsupported metrics container: {type(raw).__name__}")

    used: dict[str, set[int]] = {}
    for name, payload in items:
        metric = _clean_str(name)
        if not metric:
            continue
        metric = metric.strip().lower().replace(" ", "_")[:128]
        unit = None
        labels = None
        seq: int | None = None
        if isinstance(payload, Mapping):
            value = payload.get("value", payload.get("val"))
            unit = _clean_str(payload.get("unit"))
            labels = payload.get("labels")
            raw_seq = payload.get("seq", payload.get("index"))
            if raw_seq is not None:
                try:
                    seq = int(raw_seq)
                except (TypeError, ValueError):
                    seq = None
        else:
            value = payload
        num = _to_float(value)
        if num is None:
            continue  # non-numeric / placeholder: not a metric
        slot = used.setdefault(metric, set())
        if seq is None or seq in slot:
            seq = 0
            while seq in slot:
                seq += 1
        slot.add(seq)
        out.append(
            {
                "metric_name": metric,
                "seq": int(seq),
                "value": num,
                "unit": unit,
                "labels": _json_dump(labels) if labels else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def _prepare_run(record: Mapping[str, Any]) -> tuple[str, tuple[Any, ...], list[dict[str, Any]]]:
    if not isinstance(record, Mapping):
        raise ResultsHistoryError("record must be a mapping")

    src = _clean_str(record.get("source_log_path") or record.get("log_path") or record.get("path"))
    if not src:
        raise ResultsHistoryError("record.source_log_path is required")

    basename = _clean_str(record.get("log_basename")) or os.path.basename(src) or src
    mtime = _to_float(record.get("log_mtime") if record.get("log_mtime") is not None else record.get("mtime"))
    size = _to_float(record.get("log_size") if record.get("log_size") is not None else record.get("size"))
    run_id = _clean_str(record.get("run_id")) or make_run_id(src, mtime, size)

    kind = _norm_kind(record.get("workload_kind") or record.get("kind") or record.get("tool"), basename)
    rack = _clean_str(record.get("rack"))
    job_id = _clean_str(record.get("job_id") or record.get("jid"))
    nodes = record.get("nodes")
    if isinstance(nodes, str):
        nodes = [n for n in (part.strip() for part in nodes.replace(",", " ").split()) if n]
    nodes_json = _json_dump(list(nodes)) if isinstance(nodes, (list, tuple, set)) else _json_dump(nodes)

    started_at = _iso(record.get("started_at") or record.get("start_time"))
    if not started_at:
        # Fall back to the log mtime: it is the only real timestamp we have for
        # a Slurm stdout file, and a run with no time cannot appear in a trend.
        started_at = _iso(mtime) or _utcnow()
    ended_at = _iso(record.get("ended_at") or record.get("end_time"))
    status = normalize_status(record.get("status"))
    source = _clean_str(record.get("source")) or "unknown"
    extra_json = _json_dump(record.get("extra")) if record.get("extra") else None
    metrics = _normalize_metrics(record.get("metrics"))
    now = _utcnow()
    return run_id, (
        run_id, src, basename, kind, rack, nodes_json, job_id, started_at, ended_at,
        status, mtime, size, source, extra_json, now, now,
    ), metrics


def _upsert_prepared(
    conn: sqlite3.Connection,
    prepared: tuple[str, tuple[Any, ...], list[dict[str, Any]]],
) -> str:
    run_id, values, metrics = prepared
    conn.execute(
        """
        INSERT INTO runs (run_id, source_log_path, log_basename, workload_kind, rack, nodes,
                          job_id, started_at, ended_at, status, log_mtime, log_size, source,
                          extra, first_seen_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id) DO UPDATE SET
            source_log_path = excluded.source_log_path,
            log_basename    = excluded.log_basename,
            workload_kind   = excluded.workload_kind,
            rack            = COALESCE(excluded.rack, runs.rack),
            nodes           = COALESCE(excluded.nodes, runs.nodes),
            job_id          = COALESCE(excluded.job_id, runs.job_id),
            started_at      = excluded.started_at,
            ended_at        = COALESCE(excluded.ended_at, runs.ended_at),
            status          = excluded.status,
            log_mtime       = COALESCE(excluded.log_mtime, runs.log_mtime),
            log_size        = COALESCE(excluded.log_size, runs.log_size),
            source          = excluded.source,
            extra           = COALESCE(excluded.extra, runs.extra),
            updated_at      = excluded.updated_at
        """,
        values,
    )
    if metrics:
        conn.execute("DELETE FROM run_metrics WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO run_metrics (run_id, metric_name, seq, value, unit, labels) "
            "VALUES (?,?,?,?,?,?)",
            [(run_id, m["metric_name"], m["seq"], m["value"], m["unit"], m["labels"]) for m in metrics],
        )
    return run_id


def ingest_run(record: Mapping[str, Any]) -> str:
    """UPSERT one run (+ its metrics).  Returns the ``run_id``.

    Required: ``source_log_path``.  Everything else is optional and defaulted.
    Re-ingesting the identical log snapshot is a no-op update: the row keeps its
    original ``first_seen_at`` and the metric rows are replaced wholesale, so
    the run count never grows.
    """
    prepared = _prepare_run(record)

    with _connect(write=True) as conn:
        return _upsert_prepared(conn, prepared)


def ingest_runs(records: Iterable[Mapping[str, Any]]) -> list[str]:
    """Atomically ingest many records. Returns run ids in input order."""
    prepared = [_prepare_run(rec) for rec in records or []]
    with _connect(write=True) as conn:
        return [_upsert_prepared(conn, item) for item in prepared]


def ingest_runs_checkpointed(
    records: Iterable[Mapping[str, Any]],
    *,
    source_id: str,
    fingerprint: str,
    source_path: str,
    source_mtime_ns: int | None = None,
    source_size: int | None = None,
    collected_at: str | None = None,
) -> dict[str, Any]:
    """Atomically ingest an indexed snapshot and its durable checkpoint.

    An unchanged fingerprint performs no writes. A failed record rolls back the
    complete batch and leaves the prior checkpoint intact, so a partial refresh
    can never become authoritative.
    """
    source_key = _clean_str(source_id)
    digest = _clean_str(fingerprint)
    path = _clean_str(source_path)
    if not source_key or not digest or not path:
        raise ResultsHistoryError("source_id, fingerprint and source_path are required")
    prepared = [_prepare_run(rec) for rec in records or []]
    now = _utcnow()
    with _connect(write=True) as conn:
        prior = conn.execute(
            "SELECT fingerprint, rows_ingested FROM ingestion_checkpoints WHERE source_id = ?",
            (source_key,),
        ).fetchone()
        if prior is not None and str(prior["fingerprint"]) == digest:
            return {"skipped": True, "run_ids": [], "rows_ingested": int(prior["rows_ingested"] or 0)}
        run_ids = [_upsert_prepared(conn, item) for item in prepared]
        conn.execute(
            """
            INSERT INTO ingestion_checkpoints
              (source_id, fingerprint, source_path, source_mtime_ns, source_size,
               collected_at, rows_ingested, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
              fingerprint=excluded.fingerprint,
              source_path=excluded.source_path,
              source_mtime_ns=excluded.source_mtime_ns,
              source_size=excluded.source_size,
              collected_at=excluded.collected_at,
              rows_ingested=excluded.rows_ingested,
              updated_at=excluded.updated_at
            """,
            (source_key, digest, path, source_mtime_ns, source_size, collected_at, len(run_ids), now),
        )
    return {"skipped": False, "run_ids": run_ids, "rows_ingested": len(run_ids)}


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def _filter_sql(filters: Mapping[str, Any] | None, alias: str = "r") -> tuple[str, list[Any]]:
    """Build a WHERE clause from a filter mapping.

    Supported keys: ``workload_kind``/``kind`` (str or list), ``rack`` (str or
    list), ``job_id``, ``status`` (str or list), ``source``, ``run_id``, ``since``/``until``
    (compared against ``started_at``), ``source_log_path``, ``search``
    (substring of the basename), ``has_metric`` (metric_name that must exist).
    """
    where: list[str] = []
    args: list[Any] = []
    f = dict(filters or {})

    def _in(col: str, val: Any, transform=None) -> None:
        if val is None:
            return
        vals = list(val) if isinstance(val, (list, tuple, set)) else [val]
        vals = [transform(v) if transform else _clean_str(v) for v in vals]
        vals = [v for v in vals if v]
        if not vals:
            return
        if len(vals) == 1:
            where.append(f"{alias}.{col} = ?")
            args.append(vals[0])
        else:
            where.append(f"{alias}.{col} IN ({','.join('?' * len(vals))})")
            args.extend(vals)

    _in("workload_kind", f.get("workload_kind", f.get("kind")), lambda v: _norm_kind(v))
    _in("rack", f.get("rack"))
    _in("job_id", f.get("job_id"))
    _in("status", f.get("status"), normalize_status)
    _in("source", f.get("source"))
    _in("run_id", f.get("run_id"))
    _in("source_log_path", f.get("source_log_path"))

    since = _iso(f.get("since"))
    if since:
        where.append(f"{alias}.started_at >= ?")
        args.append(since)
    until = _iso(f.get("until"))
    if until:
        where.append(f"{alias}.started_at <= ?")
        args.append(until)

    search = _clean_str(f.get("search"))
    if search:
        where.append(f"{alias}.log_basename LIKE ?")
        args.append(f"%{search}%")

    has_metric = _clean_str(f.get("has_metric"))
    if has_metric:
        where.append(
            f"EXISTS (SELECT 1 FROM run_metrics m2 WHERE m2.run_id = {alias}.run_id "
            "AND m2.metric_name = ?)"
        )
        args.append(has_metric.lower())

    return (" WHERE " + " AND ".join(where)) if where else "", args


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["nodes"] = _json_load(out.get("nodes"), [])
    out["extra"] = _json_load(out.get("extra"), None)
    return out


def list_runs(
    filters: Mapping[str, Any] | None = None,
    limit: int = 100,
    offset: int = 0,
    *,
    order: str = "desc",
    include_metrics: bool = False,
) -> list[dict[str, Any]]:
    """Return runs newest-first (``order='asc'`` for oldest-first).

    ``include_metrics=True`` attaches ``metrics`` (scalar points only, seq 0) as
    a ``{name: {value, unit, labels}}`` dict per run — cheap enough for a page
    of rows, and skipped by default.
    """
    where, args = _filter_sql(filters)
    lim = max(1, min(int(limit or 100), 5000))
    off = max(0, int(offset or 0))
    direction = "ASC" if str(order).lower().startswith("a") else "DESC"
    sql = (
        "SELECT * FROM runs r"
        + where
        + f" ORDER BY r.started_at {direction}, r.updated_at {direction}, r.run_id {direction}"
        " LIMIT ? OFFSET ?"
    )
    with _connect() as conn:
        rows = [_row_to_run(r) for r in conn.execute(sql, [*args, lim, off]).fetchall()]
        if include_metrics and rows:
            ids = [r["run_id"] for r in rows]
            by_run: dict[str, dict[str, Any]] = {rid: {} for rid in ids}
            qmarks = ",".join("?" * len(ids))
            for m in conn.execute(
                f"SELECT run_id, metric_name, value, unit, labels FROM run_metrics "
                f"WHERE run_id IN ({qmarks}) AND seq = 0",
                ids,
            ):
                by_run[m["run_id"]][m["metric_name"]] = {
                    "value": m["value"],
                    "unit": m["unit"],
                    "labels": _json_load(m["labels"], None),
                }
            for r in rows:
                r["metrics"] = by_run.get(r["run_id"], {})
    return rows


def count_runs(filters: Mapping[str, Any] | None = None) -> int:
    """Total rows matching ``filters`` (for pagination alongside list_runs)."""
    where, args = _filter_sql(filters)
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs r" + where, args).fetchone()
    return int(row["n"] if row else 0)


def get_run(run_id: str) -> dict[str, Any] | None:
    """One run with every metric row (series included), or ``None``."""
    rid = _clean_str(run_id)
    if not rid:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (rid,)).fetchone()
        if row is None:
            return None
        run = _row_to_run(row)
        metrics: list[dict[str, Any]] = []
        for m in conn.execute(
            "SELECT metric_name, seq, value, unit, labels FROM run_metrics "
            "WHERE run_id = ? ORDER BY metric_name, seq",
            (rid,),
        ):
            metrics.append(
                {
                    "metric_name": m["metric_name"],
                    "seq": m["seq"],
                    "value": m["value"],
                    "unit": m["unit"],
                    "labels": _json_load(m["labels"], None),
                }
            )
    run["metrics"] = metrics
    run["metrics_by_name"] = {
        m["metric_name"]: m["value"] for m in metrics if m["seq"] == 0
    }
    return run


def metric_series(
    metric_name: str,
    filters: Mapping[str, Any] | None = None,
    *,
    limit: int = 5000,
    include_series_points: bool = False,
    order: str = "asc",
) -> list[dict[str, Any]]:
    """Time-ordered points for one metric — the trend-chart feed.

    Default returns one point per run (``seq = 0``, the scalar summary).  Set
    ``include_series_points=True`` to also get in-run series rows (e.g. the
    NCCL busbw-vs-size curve), each carrying its ``labels``.
    """
    name = _clean_str(metric_name)
    if not name:
        return []
    where, args = _filter_sql(filters)
    clause = where + (" AND " if where else " WHERE ") + "m.metric_name = ?"
    args = [*args, name.lower()]
    if not include_series_points:
        clause += " AND m.seq = 0"
    direction = "DESC" if str(order).lower().startswith("d") else "ASC"
    sql = (
        "SELECT r.run_id, r.rack, r.workload_kind, r.job_id, r.status, r.log_basename, "
        "       r.started_at, m.seq, m.value, m.unit, m.labels "
        "FROM run_metrics m JOIN runs r ON r.run_id = m.run_id"
        + clause
        + f" ORDER BY r.started_at {direction}, r.run_id {direction}, m.seq ASC LIMIT ?"
    )
    lim = max(1, min(int(limit or 5000), 200_000))
    with _connect() as conn:
        rows = conn.execute(sql, [*args, lim]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["labels"] = _json_load(d.get("labels"), None)
        d["metric_name"] = name.lower()
        out.append(d)
    return out


def metric_names(filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Distinct metric names (with run counts) available under ``filters``."""
    where, args = _filter_sql(filters)
    clause = where + (" AND " if where else " WHERE ") + "m.seq = 0"
    sql = (
        "SELECT m.metric_name, COUNT(*) AS runs, MAX(m.unit) AS unit "
        "FROM run_metrics m JOIN runs r ON r.run_id = m.run_id"
        + clause
        + " GROUP BY m.metric_name ORDER BY m.metric_name"
    )
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _percentile(sorted_vals: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (no interpolation, no numpy)."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    rank = math.ceil((pct / 100.0) * n)
    idx = min(max(rank - 1, 0), n - 1)
    return float(sorted_vals[idx])


def _median(sorted_vals: Sequence[float]) -> float | None:
    n = len(sorted_vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(sorted_vals[mid])
    return (float(sorted_vals[mid - 1]) + float(sorted_vals[mid])) / 2.0


def summary_stats(
    workload_kind: str | None = None,
    filters: Mapping[str, Any] | None = None,
    *,
    limit_samples: int = _MAX_STATS_SAMPLES,
) -> dict[str, Any]:
    """Fleet baseline for one workload kind: per-metric count/median/p95/latest.

    Percentiles are computed in Python (SQLite has no ``percentile``), over the
    scalar (``seq = 0``) points only, newest ``limit_samples`` rows.  Use the
    returned ``median``/``p05``/``p95`` as the deviation baseline and compare a
    single rack's ``latest`` against it.
    """
    f = dict(filters or {})
    if workload_kind:
        f["workload_kind"] = workload_kind
    where, args = _filter_sql(f)
    clause = where + (" AND " if where else " WHERE ") + "m.seq = 0"
    sql = (
        "SELECT m.metric_name, m.value, m.unit, r.started_at, r.run_id, r.rack "
        "FROM run_metrics m JOIN runs r ON r.run_id = m.run_id"
        + clause
        + " ORDER BY r.started_at ASC, r.run_id ASC LIMIT ?"
    )
    lim = max(1, min(int(limit_samples or _MAX_STATS_SAMPLES), _MAX_STATS_SAMPLES))

    with _connect() as conn:
        rows = conn.execute(sql, [*args, lim]).fetchall()
        run_where, run_args = _filter_sql(f)
        totals = conn.execute(
            "SELECT COUNT(*) AS runs, "
            "       SUM(CASE WHEN status='pass' THEN 1 ELSE 0 END) AS passed, "
            "       SUM(CASE WHEN status='fail' THEN 1 ELSE 0 END) AS failed, "
            "       MIN(started_at) AS first_started, MAX(started_at) AS last_started "
            "FROM runs r" + run_where,
            run_args,
        ).fetchone()

    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = r["metric_name"]
        b = buckets.setdefault(name, {"values": [], "unit": None, "latest": None})
        val = r["value"]
        if val is not None:
            b["values"].append(float(val))
        if r["unit"] and not b["unit"]:
            b["unit"] = r["unit"]
        # rows arrive oldest-first, so the last write wins = latest
        b["latest"] = {
            "value": val,
            "run_id": r["run_id"],
            "rack": r["rack"],
            "started_at": r["started_at"],
        }

    metrics: dict[str, Any] = {}
    for name, b in sorted(buckets.items()):
        vals = sorted(b["values"])
        metrics[name] = {
            "count": len(vals),
            "unit": b["unit"],
            "min": float(vals[0]) if vals else None,
            "max": float(vals[-1]) if vals else None,
            "mean": (sum(vals) / len(vals)) if vals else None,
            "median": _median(vals),
            "p05": _percentile(vals, 5),
            "p95": _percentile(vals, 95),
            "latest": b["latest"],
        }

    return {
        "workload_kind": _norm_kind(workload_kind) if workload_kind else None,
        "run_count": int(totals["runs"] or 0) if totals else 0,
        "pass_count": int(totals["passed"] or 0) if totals and totals["passed"] else 0,
        "fail_count": int(totals["failed"] or 0) if totals and totals["failed"] else 0,
        "first_started_at": totals["first_started"] if totals else None,
        "last_started_at": totals["last_started"] if totals else None,
        "metrics": metrics,
        "generated_at": _utcnow(),
    }


def latest_run_for_log(source_log_path: str) -> dict[str, Any] | None:
    """Most recent ingested snapshot of one log file (handles growing logs)."""
    src = _clean_str(source_log_path)
    if not src:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE source_log_path = ? "
            "ORDER BY COALESCE(log_mtime, 0) DESC, updated_at DESC LIMIT 1",
            (src,),
        ).fetchone()
    return _row_to_run(row) if row else None


def latest_successful_runs_by_rack(
    workload_kinds: Sequence[str] = ("nccl", "nemo"),
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return durable completion evidence, grouped by workload kind and rack.

    A later retry can fail without erasing the fact that a rack already passed
    its acceptance run.  This deliberately selects the newest *successful*
    run, rather than the newest attempt.  Attempts remain available through
    :func:`list_runs`; this query is the completion/acceptance rollup used by
    RackStatus after an ephemeral parser cache is replaced or rebuilt.
    """
    kinds: list[str] = []
    for raw in workload_kinds or ():
        kind = _norm_kind(raw)
        if kind and kind not in kinds:
            kinds.append(kind)
    if not kinds:
        return {}

    placeholders = ",".join("?" for _ in kinds)
    sql = (
        "SELECT * FROM runs "
        "WHERE status = 'pass' AND rack IS NOT NULL AND TRIM(rack) <> '' "
        f"AND workload_kind IN ({placeholders}) "
        "ORDER BY started_at DESC, updated_at DESC, run_id DESC"
    )
    grouped: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in kinds}
    with _connect() as conn:
        for row in conn.execute(sql, kinds):
            kind = str(row["workload_kind"] or "")
            rack = str(row["rack"] or "").strip()
            if not rack or kind not in grouped or rack in grouped[kind]:
                continue
            grouped[kind][rack] = _row_to_run(row)
    return grouped


def latest_runs_by_rack(
    workload_kinds: Sequence[str] = ("nccl", "nemo"),
    *,
    source: str | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the newest run of any verdict for each workload/rack.

    This is intentionally separate from the monotonic acceptance query above.
    Callers use it for a current-state overlay from a named authoritative
    source while retaining every historical pass and failure in ``runs``.
    """
    kinds: list[str] = []
    for raw in workload_kinds or ():
        kind = _norm_kind(raw)
        if kind and kind not in kinds:
            kinds.append(kind)
    if not kinds:
        return {}

    placeholders = ",".join("?" for _ in kinds)
    args: list[Any] = list(kinds)
    source_clause = ""
    source_value = _clean_str(source)
    if source_value:
        source_clause = " AND source = ?"
        args.append(source_value)
    sql = (
        "SELECT * FROM runs "
        "WHERE rack IS NOT NULL AND TRIM(rack) <> '' "
        f"AND workload_kind IN ({placeholders})"
        + source_clause
        + " ORDER BY COALESCE(log_mtime, 0) DESC, started_at DESC, "
          "updated_at DESC, run_id DESC"
    )
    grouped: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in kinds}
    with _connect() as conn:
        for row in conn.execute(sql, args):
            kind = str(row["workload_kind"] or "")
            rack = str(row["rack"] or "").strip()
            if not rack or kind not in grouped or rack in grouped[kind]:
                continue
            grouped[kind][rack] = _row_to_run(row)
    return grouped


def delete_run(run_id: str) -> bool:
    """Delete one run and its metrics.  True if a row was removed."""
    rid = _clean_str(run_id)
    if not rid:
        return False
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM run_metrics WHERE run_id = ?", (rid,))
        cur = conn.execute("DELETE FROM runs WHERE run_id = ?", (rid,))
        return cur.rowcount > 0


def prune_runs(older_than_days: float) -> int:
    """Delete runs whose ``started_at`` is older than N days.  Returns the count."""
    try:
        days = float(older_than_days)
    except (TypeError, ValueError):
        raise ResultsHistoryError("older_than_days must be numeric") from None
    if days <= 0:
        return 0
    cutoff = _iso(datetime.now(timezone.utc).timestamp() - days * 86400.0)
    with _connect(write=True) as conn:
        conn.execute(
            "DELETE FROM run_metrics WHERE run_id IN (SELECT run_id FROM runs WHERE started_at < ?)",
            (cutoff,),
        )
        cur = conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        return int(cur.rowcount or 0)


def db_stats() -> dict[str, Any]:
    """Health/whereabouts of the store — safe to expose on a status endpoint."""
    path = db_path()
    with _connect() as conn:
        runs = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        mets = conn.execute("SELECT COUNT(*) AS n FROM run_metrics").fetchone()["n"]
        kinds = [
            dict(r)
            for r in conn.execute(
                "SELECT workload_kind, COUNT(*) AS runs, MAX(started_at) AS last_started "
                "FROM runs GROUP BY workload_kind ORDER BY runs DESC"
            ).fetchall()
        ]
        racks = conn.execute(
            "SELECT COUNT(DISTINCT rack) AS n FROM runs WHERE rack IS NOT NULL"
        ).fetchone()["n"]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "db_path": str(path),
        "db_bytes": size,
        "journal_mode": journal,
        "schema_version": SCHEMA_VERSION,
        "runs": int(runs),
        "metric_rows": int(mets),
        "racks": int(racks),
        "by_workload_kind": kinds,
    }


# ---------------------------------------------------------------------------
# parser.sh --json adapter (convenience for app.py; no Flask involved)
# ---------------------------------------------------------------------------


def records_from_parser_json(
    payload: Mapping[str, Any],
    *,
    source_log_path: str,
    log_mtime: Any = None,
    log_size: Any = None,
    rack_hint: str | None = None,
    source: str = "parser.sh",
) -> list[dict[str, Any]]:
    """Turn one ``parser.sh --json`` object into ingestable run records.

    Handles export_version 6 through 8 (keys ``nccl_peak_out_of_place_busbw_gbps``,
    ``nemotron_5h_56b_train_window``, ``mnubergemm_stress``, ``parser_flags``).

    Abstention is deliberate: parser.sh emits a fully-formed placeholder row
    ("---", "?", status FAIL) for every input file regardless of whether the
    extractor applied, so a table entry that carries no usable number and no
    real status is DROPPED here instead of being stored as a fake failure.
    If nothing survives, a single ``unknown``-kind record is returned so the log
    is still tracked (with zero metrics).
    """
    if not isinstance(payload, Mapping):
        raise ResultsHistoryError("parser payload must be a mapping")

    basename = os.path.basename(str(source_log_path or "")) or str(source_log_path or "")
    flags_by_job: dict[str, Mapping[str, Any]] = {}
    for fl in payload.get("parser_flags") or []:
        if isinstance(fl, Mapping):
            flags_by_job[str(fl.get("job_id", "?"))] = fl

    def _base(entry: Mapping[str, Any], kind: str, salt: str) -> dict[str, Any]:
        rack = _clean_str(entry.get("rack")) or _clean_str(rack_hint)
        job = _clean_str(entry.get("job_id"))
        return {
            "run_id": make_run_id(source_log_path, log_mtime, log_size, salt=salt),
            "source_log_path": str(source_log_path),
            "log_basename": basename,
            "log_mtime": log_mtime,
            "log_size": log_size,
            "workload_kind": kind,
            "rack": rack,
            "job_id": job,
            "source": source,
            "extra": {"export_version": payload.get("export_version")},
        }

    records: list[dict[str, Any]] = []

    # --- NCCL ------------------------------------------------------------
    for entry in payload.get("nccl_peak_out_of_place_busbw_gbps") or []:
        if not isinstance(entry, Mapping):
            continue
        metrics: dict[str, Any] = {}
        for key, unit in (
            ("all_reduce_gbps", "GB/s"),
            ("all_gather_gbps", "GB/s"),
            ("alltoall_gbps", "GB/s"),
        ):
            num = _to_float(entry.get(key))
            if num is not None:
                metrics[key] = {"value": num, "unit": unit}
        series = []
        for i, pt in enumerate(entry.get("all_reduce_busbw_series") or []):
            if not isinstance(pt, Mapping):
                continue
            num = _to_float(pt.get("bus_bw_gbps"))
            if num is None:
                continue
            series.append(
                {
                    "metric_name": "all_reduce_busbw_curve",
                    "seq": i + 1,  # seq 0 stays reserved for scalar summaries
                    "value": num,
                    "unit": "GB/s",
                    "labels": {"size_bytes": pt.get("size_bytes")},
                }
            )
        if not metrics and not series:
            continue
        rec = _base(entry, "nccl", "nccl")
        flags = flags_by_job.get(str(entry.get("job_id", "?")), {})
        rec["status"] = "pass" if _to_float(flags.get("nccl_ok")) else "unknown"
        rec["metrics"] = [
            {"metric_name": k, "value": v["value"], "unit": v["unit"]} for k, v in metrics.items()
        ] + series
        records.append(rec)

    # --- NeMo / Nemotron training window ---------------------------------
    for entry in payload.get("nemotron_5h_56b_train_window") or []:
        if not isinstance(entry, Mapping):
            continue
        metrics = {}
        for key, unit in (
            ("step_s", "s"),
            ("tflops_per_gpu", "TFLOP/s"),
            ("step_time_std", "s"),
            ("perf_std", "TFLOP/s"),
            ("mfu_percent", "%"),
        ):
            num = _to_float(entry.get(key))
            if num is not None:
                metrics[key] = {"value": num, "unit": unit}
        series = []
        for i, pt in enumerate(entry.get("step_series") or []):
            if not isinstance(pt, Mapping):
                continue
            num = _to_float(pt.get("step_time"))
            if num is None:
                continue
            series.append(
                {
                    "metric_name": "step_time_curve",
                    "seq": i + 1,  # seq 0 stays reserved for scalar summaries
                    "value": num,
                    "unit": "s",
                    "labels": {"iteration": pt.get("iteration")},
                }
            )
        status = normalize_status(entry.get("status"))
        # A bare FAIL/UNK with no numbers is parser.sh's placeholder, not a result.
        if not metrics and not series:
            continue
        rec = _base(entry, "nemo", "nemo")
        rec["status"] = status
        rec["metrics"] = [
            {"metric_name": k, "value": v["value"], "unit": v["unit"]} for k, v in metrics.items()
        ] + series
        records.append(rec)

    # --- MNUBERGEMM stress ------------------------------------------------
    for entry in payload.get("mnubergemm_stress") or []:
        if not isinstance(entry, Mapping):
            continue
        metrics = {}
        for key, unit in (
            ("gflops_max", "GFLOP/s"),
            ("gflops_median", "GFLOP/s"),
            ("nodes", "nodes"),
            ("ranks", "ranks"),
            ("rank_samples", "samples"),
            ("qualified_ranks", "ranks"),
            ("gpus", "gpus"),
        ):
            num = _to_float(entry.get(key))
            if num is not None:
                metrics[key] = {"value": num, "unit": unit}
        verdict = _clean_str(entry.get("verdict"))
        if not metrics and not verdict:
            continue
        rec = _base(entry, "mnubergemm", "mnubergemm")
        rec["status"] = normalize_status(verdict)
        rec["metrics"] = [
            {"metric_name": k, "value": v["value"], "unit": v["unit"]} for k, v in metrics.items()
        ]
        records.append(rec)

    if not records:
        kind = classify_workload_kind(basename)
        records.append(
            {
                "run_id": make_run_id(source_log_path, log_mtime, log_size),
                "source_log_path": str(source_log_path),
                "log_basename": basename,
                "log_mtime": log_mtime,
                "log_size": log_size,
                "workload_kind": kind,
                "rack": _clean_str(rack_hint),
                "status": "unknown",
                "source": source,
                "metrics": [],
                "extra": {
                    "export_version": payload.get("export_version"),
                    "note": "parser.sh produced no usable rows for this log",
                },
            }
        )
    return records


def ingest_parser_json(
    payload: Mapping[str, Any],
    *,
    source_log_path: str,
    log_mtime: Any = None,
    log_size: Any = None,
    rack_hint: str | None = None,
    source: str = "parser.sh",
) -> list[str]:
    """``records_from_parser_json`` + ``ingest_runs``.  Returns the run_ids."""
    return ingest_runs(
        records_from_parser_json(
            payload,
            source_log_path=source_log_path,
            log_mtime=log_mtime,
            log_size=log_size,
            rack_hint=rack_hint,
            source=source,
        )
    )
