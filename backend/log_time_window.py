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
"""When did this run actually happen? Deriving a time window from a training log.

A Grafana panel needs a start and an end in unix seconds. Slurm knows when the *allocation* started,
which is not the same thing -- container pull and dataset staging can put minutes between the allocation
and the first training step, and a panel anchored on the allocation shows an idle GPU.

So the window comes from the log's own timestamps, first and last training line, and this module is only
that: read timestamps out of a log, fall back to step counting plus mtime when there are none.

Two things here are the scars of specific wrong answers:

* ``_llm_wall_times_from_log`` asks the single-pass logscan record for the whole-file first and last
  timestamps before it falls back to reading. The fallback reads only the first 64 MB, so on a 973 MB log
  the "end" of the run was whatever happened 7% of the way in, and the Grafana window closed hours early.
* ``_zoneinfo_for_log_parsing`` exists because these timestamps carry no offset. They are local to the
  cluster, so parsing them as UTC shifts an entire window by the site's offset -- a plausible-looking
  window pointing at the wrong hours.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from config_env import _eget, _env_int
from core import median_float as _median_float
from logscan_cache import _logscan_record
from results_ingest import _host_read_text

_logger = logging.getLogger(__name__)


_LLM_LINE_MARK_RE = re.compile(
    r"(train_step_timing|Step\s+Time.*MODEL_TFLOP/s/GPU|consumed_samples|iteration\s+\d+/)",
    re.IGNORECASE,
)


_LOG_TS_SEARCH_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)")


def _zoneinfo_for_log_parsing(override_name: str | None) -> tzinfo:
    """Zone for *naive* wall-clock strings in NEMO logs (no offset in the line).

    ``override_name`` comes from the browser ``display_timezone`` query (Charts TZ menu);
    otherwise ``RACK_PROMETHEUS_LOG_TIMESTAMP_TZ`` (default UTC).
    """

    if override_name and str(override_name).strip():
        on = str(override_name).strip()
        if on.lower() in ("utc", "gmt", "z", "uct"):
            return ZoneInfo("UTC")
        try:
            return ZoneInfo(on)
        except Exception:
            _logger.debug("_zoneinfo_for_log_parsing: ignoring error", exc_info=True)
    raw = (_eget("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ") or "UTC").strip() or "UTC"
    if raw.upper() in ("UTC", "GMT", "Z", "UCT"):
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(raw)
    except Exception:
        _logger.debug("_zoneinfo_for_log_parsing: ignoring error", exc_info=True)
        return timezone.utc


def _parse_ts_unix(raw_ts: str, tz: tzinfo) -> float | None:
    s = raw_ts.strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=tz)
        except Exception:
            _logger.debug("_parse_ts_unix: ignoring error", exc_info=True)
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


_LLM_WALL_MAX_BYTES = 64 * 1024 * 1024


def _llm_wall_times_from_log(path: Path, tz_override: str | None = None) -> list[float]:
    tz = _zoneinfo_for_log_parsing(tz_override)
    times: list[float] = []

    # Whole-file first and last training timestamps. The fallback below reads only the first
    # 64 MB, so on a 973 MB log the "end" of the run was whatever happened 7% of the way in and
    # the Grafana window closed hours early.
    rec = _logscan_record(path)
    if rec:
        wall = rec.get("wall") or {}
        for stamp in (wall.get("first"), wall.get("last")):
            if stamp:
                u = _parse_ts_unix(str(stamp), tz)
                if u is not None:
                    times.append(u)
        if times:
            return times

    content = _host_read_text(path, max_bytes=_LLM_WALL_MAX_BYTES)
    if content is None:
        return []
    for line in content.splitlines():
        if not _LLM_LINE_MARK_RE.search(line):
            continue
        m = _LOG_TS_SEARCH_RE.search(line)
        if not m:
            continue
        u = _parse_ts_unix(m.group("ts"), tz)
        if u is not None:
            times.append(u)
    return times


def _fallback_window_from_steps_and_mtime(
    path: Path,
    nemotron_row: dict | None,
) -> tuple[float, float, str] | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    ser = nemotron_row.get("step_series") if isinstance(nemotron_row, dict) else None
    if not isinstance(ser, list):
        ser = []
    steps: list[float] = []
    iters: list[int] = []
    for p in ser:
        if not isinstance(p, dict):
            continue
        st = p.get("step_time")
        if st is not None:
            try:
                sf = float(st)
            except (TypeError, ValueError):
                continue
            if math.isfinite(sf) and sf > 0:
                steps.append(sf)
        it = p.get("iteration", p.get("step"))
        if it is not None:
            try:
                iters.append(int(it))
            except (TypeError, ValueError):
                continue
    med = _median_float(steps)
    if med is None or not iters:
        return mtime - 7200, mtime, "mtime_2h"
    span = max(1, max(iters) - min(iters) + 1)
    est = max(120.0, span * med * 1.15)
    return mtime - est, mtime, "mtime_step_estimate"


def _compute_llm_time_window_unix(
    path: Path,
    nemotron_row: dict | None,
    log_tz_override: str | None = None,
) -> tuple[float, float, str] | None:
    pad = float(max(30, _env_int("RACK_WEB_GRAFANA_TIME_PAD_SEC", 120)))
    times = _llm_wall_times_from_log(path, log_tz_override)
    if times:
        lo_raw = min(times)
        hi_raw = max(times)
        return lo_raw - pad, hi_raw + pad, "log_timestamps"
    fb = _fallback_window_from_steps_and_mtime(path, nemotron_row)
    if not fb:
        return None
    lo, hi, note = fb
    return lo - pad, hi + pad, note


def _log_ts_zone() -> tzinfo:
    return _zoneinfo_for_log_parsing(None)
