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
"""The universal log normaliser as an API: inventory a directory, parse one log, scan many.

A fallback path, not the primary one. ``nvis_logscan.py`` does the single whole-file pass for logs the
product recognises; this handles the ones it does not -- a tool's output nobody has written a parser
for yet -- by extracting whatever metrics, errors and node names it can from an arbitrary text log.

Every route is bounded on purpose. A log directory on a live cluster holds thousands of files reaching
hundreds of MB, so inventory and scan both cap the file count, and the parser itself caps head/tail
bytes. An unbounded scan here would read the whole corpus over SSH.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# =====================================================================================
# Universal log ingest + durable run history  (added 2026-07-28 — STRICTLY ADDITIVE)
#
# Nothing below changes an existing response shape.  Two new module surfaces are used,
# both optional (``results_history`` / ``universal_parser`` are soft-imported at the top
# of this file and may be ``None`` when their bind mount is missing):
#
#   * ``universal_parser``  normalises ANY log — including the *.log / *.json / no-extension
#     files and the sub-directories that ``native_status._host_log_entries`` never lists —
#     into one dict.  It is used as a FALLBACK: parser.sh stays authoritative whenever it
#     actually produced a number.
#   * ``results_history``   persists every parse as a run + metric rows in SQLite under
#     ``/app/var/history`` (the ``rack-data`` named volume), giving the first real history.
#
# Every call site is wrapped: an exception here must degrade to the pre-2026-07-28
# behaviour, never 500 a request that used to work.
# =====================================================================================
#
# Head+tail caps mirror universal_parser's own DEFAULT_HEAD_BYTES / DEFAULT_TAIL_BYTES so a
# remote read produces the same text the module would have produced locally.
# Extra extensions the legacy discovery path (*.txt / *.out only) can never see.
#
# --- whole-directory inventory (every extension, every depth) ------------------------
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import core
import host_exec
import native_status
from audit.actions import action
from core import truthy_arg as _truthy_arg
from flask import Blueprint, jsonify, request
from naming import _nemo_single_rack_log_basename, _nvis_naming
from paths import _resolve_log_dir
from results_ingest import (
    _history_ingest_universal,
    _universal_enabled,
    _universal_fallback_for_log,
    _universal_parse_log,
)

try:  # the normaliser itself is optional
    import universal_parser as _uparse
except ImportError:
    _uparse = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("universal", __name__, url_prefix="")


_UNIVERSAL_INVENTORY_MAX_FILES = 4000


_UNIVERSAL_SCAN_DEFAULT_FILES = 25


_UNIVERSAL_SCAN_MAX_FILES = 400


_UNIVERSAL_SCAN_IGNORE_RE = re.compile(
    os.environ.get("RACK_UNIVERSAL_SCAN_IGNORE_RE")
    # These are llmb's INTERMEDIATE files, not results. Each has a real aggregated
    # counterpart (<user>-NCCL-1Rack-… / <user>-NEMO-1Rack-…) that the collectors write;
    # ingesting the intermediates too produces duplicate or empty rows. Worse than
    # duplicates: the raw sbatch stdout is routinely judged ``fail`` because it still holds
    # srun/pmix noise from launch attempts llmb-run went on to retry successfully, so a
    # passing rack surfaced as failed in the dashboard. Judge a run by its aggregated log,
    # never by sbatch stdout.
    #   llmb-nccl_<label>_<jid>.out   sbatch stdout. <label> was `nodes` until 2026-07-31,
    #                                 when it became the rack id — so match ANY label, or
    #                                 rack-labelled files silently start being ingested.
    #   llmb-autocollect_<jid>.log    NCCL collector watcher log
    #   llmb-nemo-autocollect_<jid>.log  NEMO collector watcher log
    or r"llmb-nccl_.+_\d+\.out|llmb(-nemo)?-?autocollect_\d+\.log",
    re.IGNORECASE,
)


def _universal_inventory(log_dir: Path, *, maxdepth: int = 2, limit: int | None = None) -> dict[str, object]:
    """List EVERY file under the log dir on the exec host — not just ``*.txt`` / ``*.out``.

    ``native_status._host_log_entries`` (the only directory listing the rest of the app has)
    is hardcoded to ``-maxdepth 1 -type f ( -name '*.txt' -o -name '*.out' )``, so a ``*.log``,
    ``*.json``, ``*.csv``, extension-less or sub-directory result is invisible to every legacy
    endpoint. This is deliberately a SEPARATE listing so that behaviour is untouched.

    Each entry reports the legacy eligibility gates too, so an operator can see exactly WHY a
    file does or does not appear in the rack table.
    """
    base = str(native_status._host_resolve(log_dir)).rstrip("/") or str(log_dir)
    depth = max(1, min(int(maxdepth or 1), 6))
    cap = max(1, min(int(limit or _UNIVERSAL_INVENTORY_MAX_FILES), _UNIVERSAL_INVENTORY_MAX_FILES))
    err: str | None = None
    entries: list[tuple[str, int, float]] = [
        (path, st.size, st.mtime)
        for path, st in host_exec.stat_dir(base, maxdepth=depth).items()
    ]
    if not entries and not host_exec.is_dir(base):
        return {"ok": False, "log_dir": base, "error": f"not a directory: {base}", "files": []}

    entries.sort(key=lambda t: t[2], reverse=True)
    truncated = len(entries) > cap
    entries = entries[:cap]

    naming = _nvis_naming()
    files: list[dict[str, object]] = []
    kind_counts: dict[str, int] = {}
    for path, size, mtime in entries:
        name = os.path.basename(path)
        kind = "unknown"
        if _uparse is not None:
            try:
                # No content read here: sniff_kind falls back to the filename when head_text
                # is empty, which is exactly the cheap listing behaviour we want.
                kind = _uparse.sniff_kind(path, None)
            except Exception:  # noqa: BLE001
                kind = "unknown"
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        try:
            legacy_rack = naming.rack_id_from_log_basename(name)
        except Exception:  # noqa: BLE001
            legacy_rack = None
        try:
            legacy_listed = name.endswith((".txt", ".out")) and os.path.dirname(path).rstrip("/") == base
            legacy_eligible = bool(legacy_listed and _nemo_single_rack_log_basename(name) and legacy_rack)
        except Exception:  # noqa: BLE001
            legacy_listed, legacy_eligible = False, False
        files.append(
            {
                "path": path,
                "basename": name,
                "dir": os.path.dirname(path),
                "size_bytes": size,
                "mtime_epoch": mtime,
                "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "kind_guess": kind,
                "rack": legacy_rack,
                # Why the legacy path does / does not show this file:
                "legacy_listed": legacy_listed,       # survives the *.txt/*.out + maxdepth-1 find
                "legacy_eligible": legacy_eligible,   # …and the basename + one-rack-id gates
            }
        )
    return {
        "ok": True,
        "log_dir": base,
        "maxdepth": depth,
        "file_count": len(files),
        "truncated": truncated,
        "kind_counts": kind_counts,
        "legacy_visible_count": sum(1 for f in files if f["legacy_eligible"]),
        "error": err,
        "files": files,
    }


def _universal_path_under_log_dir(log_dir: Path, raw: str) -> str | None:
    """Resolve a caller-supplied path/basename to an absolute path INSIDE the log dir.

    Anything with ``..`` or outside the log-dir prefix is rejected — the value is fed to a
    remote shell, so a traversal here would be a real read-anything primitive.
    """
    s = (raw or "").strip()
    if not s or "\x00" in s:
        return None
    base = str(native_status._host_resolve(log_dir)).rstrip("/") or str(log_dir)
    cand = s if s.startswith("/") else f"{base}/{s}"
    cand = PurePosixPath(cand).as_posix()
    if ".." in PurePosixPath(cand).parts:
        return None
    if cand != base and not cand.startswith(base.rstrip("/") + "/"):
        return None
    return cand


@bp.route("/api/universal/inventory")
@action(get="results:list")
def api_universal_inventory():
    """Every file in the log dir on the exec host — all extensions, sub-dirs included."""
    if not _universal_enabled():
        return jsonify(
            {"ok": False, "error": "universal_parser unavailable",
             "detail": "check the backend/universal_parser.py bind mount in docker-compose.override.yml"}
        ), 503
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        depth = int(request.args.get("maxdepth") or 2)
    except (TypeError, ValueError):
        depth = 2
    try:
        out = _universal_inventory(log_dir, maxdepth=depth)
    except Exception as e:  # noqa: BLE001
        _logger.warning("universal inventory failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(out)


@bp.route("/api/universal/parse")
@action(get="results:read")
def api_universal_parse():
    """Normalise ONE log of any kind. ``?path=`` (abs or relative to the log dir) or ``?log_file=``."""
    if not _universal_enabled():
        return jsonify(
            {"ok": False, "error": "universal_parser unavailable",
             "detail": "check the backend/universal_parser.py bind mount in docker-compose.override.yml"}
        ), 503
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    raw = (request.args.get("path") or request.args.get("log_file") or "").strip()
    target = _universal_path_under_log_dir(log_dir, raw)
    if not target:
        return jsonify(
            {"ok": False, "error": "path is required and must resolve inside the log directory"}
        ), 400
    rack_hint = (request.args.get("rack") or "").strip() or None
    ingest = _truthy_arg(request.args.get("ingest"), default=True)
    res = _universal_fallback_for_log(log_dir, target, rack_hint=rack_hint, ingest=ingest)
    if res is None:
        return jsonify({"ok": False, "error": "universal parse failed", "path": target}), 500
    return jsonify({"ok": True, "log_dir": str(log_dir), "path": target, "result": res})


@bp.route("/api/universal/scan", methods=["POST"])
@action("results:scan")
def api_universal_scan():
    """Parse + persist a batch of logs from the log dir (newest first). Backfills history.

    Body/query: ``log_dir``, ``maxdepth`` (default 2), ``max_files`` (default 25, max 400),
    ``budget_sec`` (default 120), ``kind``/``search`` filters, ``ingest`` (default true).
    Safe to re-run: ingest is idempotent per (path, mtime, size) snapshot.
    """
    if not _universal_enabled():
        return jsonify(
            {"ok": False, "error": "universal_parser unavailable",
             "detail": "check the backend/universal_parser.py bind mount in docker-compose.override.yml"}
        ), 503
    body = request.get_json(silent=True) or {}

    def _arg(name, default=None):
        v = body.get(name)
        if v is None:
            v = request.args.get(name)
        return default if v is None or str(v).strip() == "" else v

    try:
        log_dir = _resolve_log_dir(str(_arg("log_dir")) if _arg("log_dir") else None)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        depth = max(1, min(int(_arg("maxdepth", 2)), 6))
        max_files = max(1, min(int(_arg("max_files", _UNIVERSAL_SCAN_DEFAULT_FILES)),
                               _UNIVERSAL_SCAN_MAX_FILES))
        budget = max(10, min(int(_arg("budget_sec", 120)), 900))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "maxdepth/max_files/budget_sec must be integers"}), 400
    want_kind = (str(_arg("kind", "")) or "").strip().lower()
    want_search = (str(_arg("search", "")) or "").strip().lower()
    ingest = _truthy_arg(_arg("ingest"), default=True)

    inv = _universal_inventory(log_dir, maxdepth=depth)
    if not inv.get("ok"):
        return jsonify(inv), 502
    started = time.monotonic()
    scanned: list[dict[str, object]] = []
    skipped = 0
    for f in inv.get("files") or []:
        if len(scanned) >= max_files:
            break
        if (time.monotonic() - started) > budget:
            break
        name = str(f.get("basename") or "")
        # Never ingest an intermediate artefact that an aggregated log supersedes.
        if _UNIVERSAL_SCAN_IGNORE_RE.fullmatch(name):
            skipped += 1
            continue
        if want_search and want_search not in name.lower():
            skipped += 1
            continue
        if want_kind and str(f.get("kind_guess") or "") != want_kind:
            skipped += 1
            continue
        path = str(f.get("path") or "")
        res = _universal_parse_log(log_dir, path)
        if res is None:
            skipped += 1
            continue
        run_id = None
        if ingest:
            run_id = _history_ingest_universal(
                res,
                source_log_path=path,
                log_mtime=f.get("mtime_epoch"),
                log_size=f.get("size_bytes"),
                rack_hint=f.get("rack"),
            )
        scanned.append(
            {
                "path": path,
                "basename": name,
                "kind": res.get("kind"),
                "status": res.get("status"),
                "rack": res.get("rack"),
                "job_id": res.get("job_id"),
                "metric_count": len(res.get("metrics") or []),
                "headline": res.get("headline"),
                "run_id": run_id,
            }
        )
    return jsonify(
        {
            "ok": True,
            "log_dir": inv.get("log_dir"),
            "file_count": inv.get("file_count"),
            "parsed": len(scanned),
            "skipped": skipped,
            "ingested": sum(1 for s in scanned if s.get("run_id")),
            "history_enabled": core.history_enabled(),
            "elapsed_sec": round(time.monotonic() - started, 3),
            "results": scanned,
        }
    )
