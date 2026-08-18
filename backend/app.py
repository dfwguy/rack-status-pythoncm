#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""
NVIS Rack Status — web dashboard for rack validation status: the default /api/status table is built in Python
(``native_status.py``) from ``cmsh -c "rack; list"`` plus async ``cmsh -c ds``, Slurm ``sinfo``,
and per-log probes via ``rack_status.sh --probe-json`` (cached under var/cache/native_log_analysis.json).

Set ``RACK_STATUS_USE_SHELL_TABLE=1`` to restore the legacy path: run ``rack_status.sh`` and parse
NO_COLOR output.

Bundled shell defaults live under this package: bin/scripts/ (rack_status.sh, parser.sh, …).
/api/status includes physical_rf0 (hostname -> rf0 IP) from a one-shot cmsh foreach unless
RACK_PHYSICAL_RF0_MAP_DISABLED=1.
Default log root: var/logs/ under the same package. Override with RACK_STATUS_* env vars.

Unless RACK_STATUS_BACKGROUND_DISABLED=1, status refresh runs on a daemon thread on
RACK_STATUS_BACKGROUND_INTERVAL_SEC (default 30) and /api/status reads the latest cached
result per log_dir so concurrent dashboard users do not each spawn heavy work per request.

NVIS Rack Status runs ``cmsh -c "rack; list"`` at most once per process, writes var/cache/cmsh_rack_list.txt,
and sets RACK_CMSH_RACK_LIST_FILE for subprocesses that still invoke ``rack_status.sh`` (probe-json,
or the full shell table when ``RACK_STATUS_USE_SHELL_TABLE=1``). Disable with
RACK_CMSH_RACK_LIST_CACHE_DISABLED=1.

Per-log NCCL/LLM heuristics for the **shell** table are cached in var/cache/log_analysis_cache.json
(mtime_ns + size per path). When enabled with the shell table, rack_status.sh is run with
``--tmp-status`` built from cache + fresh squeue. Disable with RACK_LOG_ANALYSIS_CACHE_DISABLED=1.
Cap with RACK_LOG_ANALYSIS_CACHE_MAX_FILES (default 200).

Optional HTTP login (enabled by default) checks the same PAM password as system login for
the account named by RACK_WEB_AUTH_USER (default: root) using RACK_WEB_PAM_SERVICE (default:
login), via vendored ctypes bindings to libpam (no PyPI pam package). If the Flask
interpreter cannot load ctypes (common with a mismatched module-loaded Python), set
RACK_WEB_PAM_HELPER_PYTHON to a working interpreter (default /usr/bin/python3) to run
rack_web/pam_helper.py for checks. Disable auth with RACK_WEB_AUTH_DISABLED=1. Set
RACK_WEB_SECRET_KEY for stable sessions across restarts. HTTPS: RACK_WEB_COOKIE_SECURE=1.

Parser results dialog: optional embedded Grafana solo panels (GPU thermal / power) use
``RACK_WEB_GRAFANA_URI`` (Grafana base URL, no trailing slash; empty = same hostname as the
browser with ``RACK_WEB_GRAFANA_PORT``, default 3000), ``RACK_WEB_GRAFANA_DASH_UID`` /
``RACK_WEB_GRAFANA_DASH_SLUG``, ``RACK_WEB_GRAFANA_EMBED_DISABLED``, ``RACK_WEB_GRAFANA_TIME_PAD_SEC``,
and log timezone ``RACK_PROMETHEUS_LOG_TIMESTAMP_TZ`` for timestamp parsing. Host list prefers
``sacct`` for the Slurm job id; otherwise expands the rack validation CT bracket via ``scontrol``.
Grafana embeds and parser Chart.js time axes use ``RACK_WEB_GRAFANA_TIMEZONE`` if set
(``browser``, ``utc``, or an IANA name); otherwise the same zone as ``RACK_PROMETHEUS_LOG_TIMESTAMP_TZ``
(default ``utc``) so labels align with how the server parsed log timestamps.
The browser ``Charts TZ`` menu sends ``GET /api/parser-results?...&display_timezone=<IANA>`` so naive
NEMO log timestamps and the Grafana ``from``/``to`` window use the same zone (avoids a fixed offset
when logs are wall-clock local but the server env was UTC).
"""
from __future__ import annotations

import fcntl
import io
import json
import logging
import math
import os
import re
import secrets
import shlex
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
import zipfile
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone, tzinfo
from pathlib import Path, PurePosixPath

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    request,
    send_file,
    stream_with_context,
    url_for,
)

APP_DIR = Path(__file__).resolve().parent
# Repo root (parent of backend/) holds runtime assets that live outside the Python
# tree: bin/scripts, static/spa, var/, help_content.json, rack_comments.json.
REPO_ROOT = APP_DIR.parent
# Canonical overrides file for this install; native_status reads the same path via this env
# (set before ``import native_status`` so the first mtime load targets the app tree).
os.environ.setdefault(
    "NVIS_JOB_ENV_OVERRIDES_FILE",
    str((REPO_ROOT / "var" / "cache" / "job_env_overrides.json").resolve()),
)
_app_dir_s = str(APP_DIR)
if _app_dir_s not in sys.path:
    sys.path.insert(0, _app_dir_s)

import cluster_overview_live
import cluster_snapshot
import native_status
import nvis_id_templates

# --- optional backend modules (added 2026-07-28) -------------------------------------
# Both are bind-mounted read-only from the repo (docker-compose.override.yml). A missing
# mount, a syntax error or an unavailable stdlib feature MUST degrade to the previous
# behaviour, never stop the app booting mid cluster bring-up — hence the soft import and
# the ``is None`` guards at every call site (``_history_enabled`` / ``_universal_enabled``).
try:
    import lake_sink
    import results_history
except Exception as _rh_exc:  # pragma: no cover - defensive
    results_history = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("results_history unavailable: %s", _rh_exc, exc_info=True)
try:
    import universal_parser as _uparse
except Exception as _up_exc:  # pragma: no cover - defensive
    _uparse = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("universal_parser unavailable: %s", _up_exc, exc_info=True)
try:
    import bringup as _bringup
except Exception as _bu_exc:  # pragma: no cover - defensive
    _bringup = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("bringup unavailable: %s", _bu_exc, exc_info=True)
try:
    import node_report as _node_report
except Exception as _nr_exc:  # pragma: no cover - defensive
    _node_report = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("node_report unavailable: %s", _nr_exc, exc_info=True)


import ai_chat
import app_version as _app_version_mod
import core
import host_exec
import nvftp_cache_sync
import nvlink_preflight
import validation_activities
import validation_presets
from api import _common as _api_common
from platforms import get_platform as _get_platform

# Catalog (platforms/tools/projects). Optional at import time so the app still boots if
# the store is unavailable; all call sites degrade to the legacy env behavior.
try:
    from catalog import context as catalog_context
    from catalog import seed as catalog_seed
    from catalog import store as catalog_store
    from catalog.api import catalog_bp

    _CATALOG_AVAILABLE = True
except Exception as _catalog_exc:  # pragma: no cover - defensive
    logging.getLogger(__name__).warning("catalog unavailable: %s", _catalog_exc, exc_info=True)
    catalog_context = None  # type: ignore[assignment]
    catalog_seed = None  # type: ignore[assignment]
    catalog_store = None  # type: ignore[assignment]
    catalog_bp = None  # type: ignore[assignment]
    _CATALOG_AVAILABLE = False

# ---- Host paths, naming and per-request identity now live in their own modules ------------
# Imported under the existing names so the ~200 call sites below are untouched.
# Request-scoped helpers need Flask, so they live with the API package, not in core.
from api._common import (
    operator_from_request as _operator_from_request,
)
from api._common import (
    serve_spa_asset as _serve_spa_asset,
)
from log_discovery import (
    _resolve_nemo_log_basename_in_dir,
    find_log_paths_for_rack,
    pick_logs_for_rack,
)
from logscan_cache import (
    _analytics_scan_ctx,
    _log_stat_for_path,
    _logscan_batch,
    _logscan_record,
)
from naming import (
    _ct_power_token_re,
    _full_rack_expected_nodes,
    _is_multirack_log_basename,
    _jobid_from_nemo_log_basename,
    _multirack_site_prefix,
    _nemo_nccl_in_log_basename,
    _nemo_single_rack_log_basename,
    _normalize_nvswitch_host_for_rack,
    _normalize_rack_id,
    _nvis_naming,
    _nvswitch_host_sort_key,
    _rack_id_from_log_basename,
    _rack_id_from_nemo_1rack_filename,
    multirack_display_rack_from_basename,
)
from paths import (
    _REMOTE_BASE,
    DEFAULT_SCRIPT,
    SCRIPTS_DIR,
    _default_log_dir,
    _resolve_log_dir,
    _script_exists,
    _scripts_dir,
)

# Output basename for Tools → 05-download-containers.sh (must match script).


app = Flask(__name__, static_folder=str(REPO_ROOT / "static"), static_url_path="/static")
_logger = logging.getLogger(__name__)

# Gzip/brotli compression for static assets (the ~2.2 MB SPA bundle) and JSON API
# payloads. The Vite bundle is served as ``text/javascript``, which is not in
# Flask-Compress's default mimetype list, so set the list explicitly.
try:
    from flask_compress import Compress

    # NOTE: deliberately EXCLUDE "text/plain". The live-tool log endpoints stream
    # ``text/plain`` via a generator; Flask-Compress buffers the ENTIRE response to
    # gzip it (consuming the generator) before sending, so streamed output only
    # appears once the command finishes — the root cause of "logs show only at the
    # end". (X-Accel-Buffering only affects nginx, not Flask-Compress.) The SPA bundle
    # and JSON payloads — the responses worth compressing — stay compressed.
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html",
        "text/css",
        "text/xml",
        "text/javascript",
        "application/json",
        "application/javascript",
        "application/x-javascript",
        "application/manifest+json",
        "image/svg+xml",
    ]
    app.config["COMPRESS_LEVEL"] = 6
    app.config["COMPRESS_BR_LEVEL"] = 5
    app.config["COMPRESS_MIN_SIZE"] = 1024
    Compress(app)
except Exception as _compress_exc:  # pragma: no cover - defensive
    _logger.warning(
        "flask-compress unavailable; responses will be uncompressed: %s",
        _compress_exc,
        exc_info=True,
    )

try:
    from flask_sock import Sock
    _sock = Sock(app)
except ImportError:
    _sock = None
    _logger.warning("flask-sock not installed; /ws/terminal will be unavailable")

if _CATALOG_AVAILABLE and catalog_bp is not None:
    app.register_blueprint(catalog_bp)

# Grafana panel-embed API (/api/observability/dashboards, /dashboards/<view>, /health).
# Optional by design: a missing bind mount or an unreadable grafana_embed_map.json must
# not stop the app booting — the SPA renders its own "not wired yet" state instead.
try:
    from grafana_embeds import grafana_embeds_bp

    app.register_blueprint(grafana_embeds_bp)
    from metrics_query import metrics_query_bp
    app.register_blueprint(metrics_query_bp)

    @app.route("/api/lake/health")
    def api_lake_health():
        """Is the Parquet lake reachable and accepting run provenance writes?"""
        if lake_sink is None:
            return jsonify({"ok": False, "error": "lake_sink not loaded"})
        return jsonify(lake_sink.health())
except Exception as _ge_exc:  # pragma: no cover - observability embeds are optional
    _logger.warning("grafana_embeds blueprint unavailable: %s", _ge_exc, exc_info=True)

# ---- Async job registries live in job_registry.py ----------------------------------------
# Imported under the existing names. These are shared objects: the dicts here ARE the dicts there,
# so the existing mutate-under-lock call sites are unaffected. Never rebind one.
from job_registry import (
    _analytics_jobs,
    _analytics_jobs_lock,
    _llama_jobs,
    _llama_jobs_lock,
    _reset_job_purge_later,
    _reset_jobs,
    _reset_jobs_lock,
    _stress_jobs,
    _stress_jobs_lock,
    _track_sync_op,
    _validation_jobs,
    _validation_jobs_lock,
    _xrack_jobs,
    _xrack_jobs_lock,
)

_physical_rf0_lock = threading.Lock()
_physical_rf0_map: dict[str, str] = {}
_physical_rf0_loaded = False

_rack_status_cache_lock = threading.Lock()
_rack_status_exec_lock = threading.Lock()
_rack_status_cache: dict[str, dict] = {}
_rack_status_registry: set[str] = set()
_rack_status_bg_thread_lock = threading.Lock()
_rack_status_bg_thread: threading.Thread | None = None
_cluster_snapshot_repo_lock = threading.Lock()
_cluster_snapshot_repo_cache: tuple[str, cluster_snapshot.SnapshotRepository] | None = None
_cluster_overview_live_service_lock = threading.Lock()
_cluster_overview_live_service_cache: (
    tuple[str, cluster_overview_live.ClusterOverviewLiveService] | None
) = None

# Dashboard Settings → job / validation env overrides (merged into subprocess env and ``_eget`` reads).
# ``section`` groups rows in Settings (see ``JOB_ENV_SECTION_ORDER``).
JOB_ENV_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard & charts"),
    ("external", "External connections"),
    ("nvftp", "NVFTP cache sync"),
    ("native_cmsh", "Native status, parser & cmsh"),
    ("jobs_cluster", "Validation, jobs & cluster tools"),
)
# ---- Configuration resolution lives in config_env.py -------------------------------------
# Imported under the existing private names so the ~120 _eget / 95 _env_int / 35 _subprocess_env
# call sites below are untouched by the move. The writer (_job_env_overrides_write) stays in this
# file: it invalidates the rack-status cache, which config_env has no business knowing about.
import config_env
from comments_store import (
    read_comments,
)
from config_env import (
    _JOB_ENV_ALLOWED_KEYS,
    _STATUS_ROW_ENV_KEYS,
    JOB_ENV_UI_DEFINITIONS,
    _eget,
    _eget_int,
    _env_int,
    _job_env_flock,
    _job_env_int,
    _job_env_overrides_invalidate_cache,
    _job_env_overrides_load,
    _job_env_overrides_path,
    _job_env_status_payload_needs_rebuild,
    _materialize_job_env_overrides_into_os_environ,
    _subprocess_env,
)

# _JOB_ENV_BOOTSTRAP_DONE stays here: only the catalog bootstrap below touches it, and a `global`
# statement cannot rebind another module's name.
_JOB_ENV_BOOTSTRAP_DONE = False

_JOB_ENV_BOOTSTRAP_DONE = False















def _job_env_overrides_write(data: dict[str, str], *, bust_status_cache: bool = False) -> dict[str, str]:
    p = _job_env_overrides_path()
    os.environ["NVIS_JOB_ENV_OVERRIDES_FILE"] = str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v[:8192] for k, v in data.items() if k in _JOB_ENV_ALLOWED_KEYS and v.strip()}
    tmp = p.with_suffix(".tmp")
    with _job_env_flock(shared=False):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, p)
        _job_env_overrides_invalidate_cache()
    # Materialize into os.environ *after* releasing the exclusive lock. Materialization
    # calls _apply_catalog_active_to_environ() -> _job_env_overrides_load(), which acquires a
    # *shared* lock on this same file via a new fd; doing that while still holding the
    # exclusive lock above deadlocks (flock locks are per-open-file-description, so the
    # shared request blocks against our own exclusive lock).
    _materialize_job_env_overrides_into_os_environ(clean)
    try:
        native_status.invalidate_job_env_file_cache()
    except Exception:
        _logger.debug("_job_env_overrides_write: ignoring error", exc_info=True)
    # Rebuild the transport connection so transport-affecting settings (e.g.
    # RACK_SCRIPT_SUDO / RACK_SUDO_PASSWORD) take effect without an app restart.
    try:
        host_exec.invalidate_connection()
    except Exception:
        _logger.debug("_job_env_overrides_write: ignoring error", exc_info=True)
    if bust_status_cache:
        _rack_status_cache_clear_all()
    return clean


_PHYSICAL_RF0_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_APP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")


def _parse_physical_rf0_stdout(text: str) -> dict[str, str]:
    """hostname(lower) -> first IPv4 after that line (skips interfaces / noise until get ip)."""
    out: dict[str, str] = {}
    raw_lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines: list[str] = []
    for ln in raw_lines:
        t = ln.strip()
        if not t or t.startswith((">", "#")):
            continue
        lines.append(t)

    loose = os.environ.get("RACK_PHYSICAL_RF0_LOOSE_HOSTNAMES", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    naming = _nvis_naming()

    def host_ok(h: str) -> bool:
        if naming.is_tray_hostname(h):
            return True
        if not loose:
            return False
        if _PHYSICAL_RF0_IPV4_RE.match(h.split("/")[0].strip()):
            return False
        if len(h) < 2 or len(h) > 253:
            return False
        return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", h))

    i = 0
    while i < len(lines):
        h = lines[i]
        i += 1
        if not host_ok(h):
            continue
        ip_token = ""
        while i < len(lines):
            cand = lines[i].split()[0].split("/")[0].strip()
            i += 1
            if _PHYSICAL_RF0_IPV4_RE.match(cand):
                ip_token = cand
                break
        if ip_token:
            out[h.lower()] = ip_token
    return out


def get_physical_rf0_map() -> dict[str, str]:
    """Run cmsh foreach once per process; merged into /api/status. Disable with RACK_PHYSICAL_RF0_MAP_DISABLED=1."""
    global _physical_rf0_loaded, _physical_rf0_map
    if os.environ.get("RACK_PHYSICAL_RF0_MAP_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return {}
    with _physical_rf0_lock:
        if _physical_rf0_loaded:
            return dict(_physical_rf0_map)
        _physical_rf0_map = {}
        plat = _get_platform()
        cmd = plat.physical_rf0_cmd()
        if not host_exec.which(cmd[0]):
            _physical_rf0_loaded = True
            return {}
        try:
            proc = host_exec.run_on_host(
                cmd,
                timeout=max(20, _env_int("RACK_PHYSICAL_RF0_TIMEOUT", 180)),
            )
        except (OSError, subprocess.TimeoutExpired):
            _physical_rf0_loaded = True
            return {}
        if proc.returncode == 0 and proc.stdout:
            _physical_rf0_map = _parse_physical_rf0_stdout(proc.stdout)
        _physical_rf0_loaded = True
        return dict(_physical_rf0_map)


def _validation_tool_and_stage() -> tuple[str, str]:
    """Map configured validation script to activity tool + stage."""
    name = _validation_script_path().name.lower()
    if "llmb_nemo" in name:
        return "llmb-nemo-combined", "combined"
    return "singlerack", "stage2"




























def _refresh_validation_activities_lazy(rows: list[dict]) -> None:
    """Best-effort evidence-ladder refresh for tasks that still need hydration."""
    if (_eget("RACK_VALIDATION_ACTIVITIES_REFRESH_DISABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return
    for rec in rows:
        aid = str(rec.get("activity_id") or rec.get("job_id") or "")
        if not aid:
            continue
        tasks = rec.get("tasks") or []
        if not any(_task_needs_slurm_hydration(t) for t in tasks if isinstance(t, dict)):
            # Still refresh rollup for legacy rows missing work_phase.
            if tasks and not rec.get("work_phase"):
                rec["work_phase"] = validation_activities.rollup_work_phase(
                    [t for t in tasks if isinstance(t, dict)]
                )
            continue
        _hydrate_activity_task_logs(aid, rec)


_validation_activities_refresh_thread: threading.Thread | None = None
_validation_activities_refresh_thread_lock = threading.Lock()


def _validation_activities_refresh_disabled() -> bool:
    return (_eget("RACK_VALIDATION_ACTIVITIES_REFRESH_DISABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _validation_activities_refresh_number(name: str, default: float, minimum: float) -> float:
    try:
        value = float((_eget(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _refresh_validation_activities_cycle() -> int:
    """Reconcile the whole live activity working set within a time budget.

    Browser-filtered GETs still refresh immediately, but they are no longer the
    only reconciliation mechanism.  A sidecar flock/cursor gives all gunicorn
    workers one fair, restart-safe round robin over open scheduler tasks.
    """
    if _validation_activities_refresh_disabled():
        return 0
    rows = validation_activities.list_activities(limit=0)
    candidates = [
        rec
        for rec in rows
        if isinstance(rec, dict)
        and any(
            _task_needs_slurm_hydration(task)
            for task in (rec.get("tasks") or [])
            if isinstance(task, dict)
        )
    ]
    if not candidates:
        return 0

    lock_raw = (_eget("RACK_VALIDATION_ACTIVITIES_REFRESH_LOCK_FILE") or "").strip()
    lock_path = (
        Path(lock_raw).expanduser()
        if lock_raw
        else validation_activities.ACTIVITIES_FILE.with_name(
            ".validation_activities_refresh.lock"
        )
    )
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lease = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
    except OSError as exc:
        _logger.warning("validation activity refresh lease unavailable %s: %s", lock_path, exc)
        return 0

    refreshed = 0
    with lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return 0
        lease.seek(0)
        cursor = lease.read().strip()
        ids = [str(rec.get("activity_id") or rec.get("job_id") or "") for rec in candidates]
        if cursor in ids:
            pivot = ids.index(cursor) + 1
            candidates = candidates[pivot:] + candidates[:pivot]
        deadline = time.monotonic() + _validation_activities_refresh_number(
            "RACK_VALIDATION_ACTIVITIES_REFRESH_BUDGET_SEC", 3.0, 0.1
        )
        last_attempted = cursor
        for rec in candidates:
            if refreshed and time.monotonic() >= deadline:
                break
            aid = str(rec.get("activity_id") or rec.get("job_id") or "").strip()
            if not aid:
                continue
            last_attempted = aid
            try:
                _hydrate_activity_task_logs(aid, rec)
            except Exception:  # one unreachable job must not stop the sweep
                _logger.warning(
                    "background validation activity refresh failed for %s", aid, exc_info=True
                )
            refreshed += 1
        if last_attempted:
            lease.seek(0)
            lease.truncate()
            lease.write(last_attempted + "\n")
            lease.flush()
            try:
                os.fsync(lease.fileno())
            except OSError:
                pass
        try:
            fcntl.flock(lease, fcntl.LOCK_UN)
        except OSError:
            pass
    return refreshed


def _validation_activities_refresh_loop() -> None:
    while not _validation_activities_refresh_disabled():
        try:
            _refresh_validation_activities_cycle()
        except Exception:  # daemon must survive transient store/transport errors
            _logger.exception("validation activity background refresh cycle failed")
        time.sleep(
            _validation_activities_refresh_number(
                "RACK_VALIDATION_ACTIVITIES_REFRESH_INTERVAL_SEC", 30.0, 5.0
            )
        )


def _bootstrap_validation_activities_refresh_at_startup() -> None:
    """Start one daemon per process; the file lease elects one active worker."""
    global _validation_activities_refresh_thread
    if _validation_activities_refresh_disabled():
        return
    if os.environ.get("RACK_STATUS_DEMO", "").strip().lower() in ("1", "true", "yes"):
        return
    with _validation_activities_refresh_thread_lock:
        thread = _validation_activities_refresh_thread
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=_validation_activities_refresh_loop,
            name="validation-activities-refresh",
            daemon=True,
        )
        _validation_activities_refresh_thread = thread
        thread.start()


def _maybe_adopt_validation_log(log_path: Path, log_dir: Path, rack: str) -> None:
    """Assign an activity_id when an external validation log is first accessed."""
    if os.environ.get("RACK_VALIDATION_ACTIVITIES_ADOPT_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return
    try:
        validation_activities.adopt_log(
            str(log_path),
            log_dir=str(log_dir),
            rack=rack,
            operator=_operator_from_request() or "external",
        )
    except Exception as e:
        _logger.debug("validation log adopt skipped for %s: %s", log_path, e, exc_info=True)


















def _power_reset_worker(job_id: str, specs: list[str]) -> None:
    """Run cmsh power cycle in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    sleep_sec = max(0, _env_int("RACK_POWER_RESET_SLEEP_SEC", 10))
    try:
        _patch(
            phase="running",
            reset_sleep_sec=sleep_sec,
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        results = run_cmsh_power_reset(specs)
        ok_all = all(r.get("returncode") == 0 for r in results)
        _patch(
            phase="done" if ok_all else "error",
            ok=ok_all,
            results=results,
            reset_sleep_sec=sleep_sec,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=None if ok_all else "one or more cmsh power reset steps failed (see results)",
        )
    except Exception as e:
        _logger.debug("_power_reset_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            results=[],
            reset_sleep_sec=sleep_sec,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    _reset_job_purge_later(job_id)


def run_services_restart(pdsh_w: str) -> dict:
    """Run pdsh across hosts: mount -a, restart munge/slurmd/nvidia-imex, then nvidia-dcgm or cuda-dcgm."""
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    plat = _get_platform()
    timeout = max(30, _env_int("RACK_SERVICES_RESTART_TIMEOUT", 1800))
    dry = os.environ.get("RACK_SERVICES_RESTART_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = plat.pdsh_cmd(w, plat.services_restart_remote_cmd())
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": "dry-run (RACK_SERVICES_RESTART_DRY_RUN)",
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError("pdsh executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "pdsh_w": w,
    }


_PDSH_HOST_LINE_RE = re.compile(r"^([^:]+):\s*(.*)$")


def _parse_pdsh_prefixed_lines(stdout: str) -> list[dict[str, str]]:
    """Parse default pdsh stdout lines ``hostname: remote command output``."""
    rows: list[dict[str, str]] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _PDSH_HOST_LINE_RE.match(line)
        if m:
            rows.append({"host": m.group(1).strip(), "uptime": m.group(2).strip()})
        else:
            rows.append({"host": "", "uptime": line})
    rows.sort(key=lambda r: (r.get("host") or "", r.get("uptime") or ""))
    return rows


def run_rack_uptime_pdsh(rack: str) -> dict:
    """Run ``pdsh -w <rack><ct_joiner>[RANGE] uptime`` (RANGE from RACK_VALIDATION_CT_RANGE)."""
    rk = _normalize_rack_id(rack)
    w = _expand_pdsh_w_slurm_nodenames(
        _nvis_naming().uptime_pdsh_wildcard(rack, _validation_ct_inner())
    )
    timeout = max(15, _env_int("RACK_UPTIME_PDSH_TIMEOUT", 120))
    dry = os.environ.get("RACK_UPTIME_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    plat = _get_platform()
    cmd = plat.pdsh_cmd(w, "uptime")
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        sample = f"{rk}-ct01:  12:00:00 up 1 day,  1 user,  load average: 0.00, 0.00, 0.00"
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "dry-run (RACK_UPTIME_DRY_RUN)\n" + sample,
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
            "rack": rk,
            "rows": _parse_pdsh_prefixed_lines(sample),
        }
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError("pdsh executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
            "rack": rk,
            "rows": [],
            "error": f"pdsh uptime timed out after {timeout}s",
        }
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    rows = _parse_pdsh_prefixed_lines(out)
    ok = proc.returncode == 0
    payload: dict[str, object] = {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "command_display": cmd_display,
        "pdsh_w": w,
        "rack": rk,
        "rows": rows,
    }
    if not ok:
        payload["error"] = "pdsh uptime exited non-zero (see stdout/stderr)"
    return payload


def _services_restart_worker(job_id: str, pdsh_w: str) -> None:
    """Run services-restart pdsh in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        result = run_services_restart(pdsh_w)
        ok = result.get("returncode") == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=result.get("returncode"),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            command_display=result.get("command_display", ""),
            pdsh_w=result.get("pdsh_w", ""),
            error=None if ok else "services restart (pdsh) exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_services_restart_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display="",
            pdsh_w=pdsh_w,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


def _gpu_reset_worker(job_id: str, pdsh_w: str) -> None:
    """Run gpu_reset.sh in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        result = run_gpu_reset(pdsh_w)
        ok = result.get("returncode") == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=result.get("returncode"),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            command_display=result.get("command_display", ""),
            pdsh_w=result.get("pdsh_w", ""),
            error=None if ok else "gpu_reset.sh exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_gpu_reset_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display="",
            pdsh_w=pdsh_w,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


# One implementation, in the module the API packages can also import. Aliased rather than
# re-exported under a new name so the 38 call sites here stay untouched.
_truthy_env = core.truthy_env
_SLURM_STDOUT_RE = core.SLURM_STDOUT_RE
_slurm_job_base_id = core.slurm_job_base_id
from activity_hydration import (
    _hydrate_activity_task_logs,
    _record_validation_activity,
    _task_needs_slurm_hydration,
)
from analytics_cache import (
    _analytics_aggregate_cache_get,
    _analytics_aggregate_cache_put,
    _analytics_rows_fingerprint,
)
from app_clients import _app_client_ttl_sec, _touch_app_client
from node_targets import (
    _CT_RANGE_INNER_RE,
    _expand_pdsh_w_slurm_nodenames,
    _scontrol_hostnames_preserving_case,
    _validation_ct_inner,
    validation_target_for_rack,
)
from results_ingest import (
    _lake_push_job,
    _universal_fallback_for_log,
)
from slurm_query import (
    _all_slurm_nodenames,
    _dedupe_hostnames_preserve_spelling,
    _infer_num_nodes_from_nodelist,
    _parse_scontrol_job,
    _rack_node_hostnames_from_inventory,
    _resolve_slurm_job_stdout_path,
    _run_scontrol_show_job,
    _run_squeue,
)
from stream_exec import _merged_stdout_stream_generator
from validation_launch import (
    _host_job_cwd,
    _llama_worker,
    _multirack_campaign_snapshot,
    _prepare_validation_preset,
    _stress_worker,
    _validation_node_eligibility,
    _validation_preset_status_payload,
    _validation_worker,
    _xrack_worker,
    merge_ct_individuals_for_power,
)
from web_auth import _auth_enabled, _safe_next, _session_lifetime, _session_ok


def _project_name() -> str:
    """Optional label appended to page titles (``PROJECT_NAME`` or ``RACK_WEB_PROJECT_NAME``)."""
    v = (_eget("RACK_WEB_PROJECT_NAME") or "").strip()
    if v:
        return v.replace("\n", " ").replace("\r", " ").strip()
    v = (os.environ.get("PROJECT_NAME") or "").strip()
    if v:
        return v.replace("\n", " ").replace("\r", " ").strip()
    return ""


def _count_running_async_commands() -> int:
    active_phases = {"starting", "running"}
    with _validation_jobs_lock:
        n_validation = sum(
            1 for row in _validation_jobs.values() if str(row.get("phase") or "").lower() in active_phases
        )
    with _stress_jobs_lock:
        n_stress = sum(
            1 for row in _stress_jobs.values() if str(row.get("phase") or "").lower() in active_phases
        )
    with _reset_jobs_lock:
        n_reset = sum(1 for row in _reset_jobs.values() if str(row.get("phase") or "").lower() in active_phases)
    return n_validation + n_stress + n_reset


# ---------------------------------------------------------------------------
# Rack-locks helpers: operator label from request, active-job iterator for
# ``rack_locks.build_lock_snapshot``, and a context manager that registers
# long-running sync / stream handlers in ``_reset_jobs`` so they show up in
# the same advisory snapshot the SPA polls.
# ---------------------------------------------------------------------------


_RACK_WEB_SECRET_FILE = (REPO_ROOT / "var" / "secrets" / ".rack_web_secret_key").resolve()


def _bootstrap_rack_web_secret_key() -> None:
    """Ensure ``RACK_WEB_SECRET_KEY`` exists: env override, else load/create ``var/secrets/.rack_web_secret_key``."""
    if (os.environ.get("RACK_WEB_SECRET_KEY") or "").strip():
        return
    _RACK_WEB_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _RACK_WEB_SECRET_FILE.is_file():
        try:
            existing = _RACK_WEB_SECRET_FILE.read_text(encoding="utf-8").strip()
            if existing:
                os.environ["RACK_WEB_SECRET_KEY"] = existing
                return
        except OSError:
            pass
    key = secrets.token_hex(32)
    try:
        tmp = _RACK_WEB_SECRET_FILE.with_suffix(".tmp")
        tmp.write_text(key + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _RACK_WEB_SECRET_FILE)
        os.chmod(_RACK_WEB_SECRET_FILE, 0o600)
    except OSError as exc:
        warnings.warn(
            f"RACK_WEB_SECRET_KEY not persisted ({exc}); using an ephemeral key for this process only.",
            stacklevel=2,
        )
    os.environ["RACK_WEB_SECRET_KEY"] = key
    logging.getLogger(__name__).info(
        "Created persistent RACK_WEB_SECRET_KEY at %s (first dashboard start)",
        _RACK_WEB_SECRET_FILE,
    )


def _configure_session(app: Flask) -> None:
    _bootstrap_rack_web_secret_key()
    key = (os.environ.get("RACK_WEB_SECRET_KEY") or "").strip()
    app.secret_key = key or secrets.token_hex(32)
    app.permanent_session_lifetime = _session_lifetime()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _truthy_env("RACK_WEB_COOKIE_SECURE")


_configure_session(app)


# Keys we materialized into os.environ from the catalog, with their pre-existing value
# (or None if absent) so a project switch can restore the baseline before re-applying.
_catalog_env_saved: dict[str, str | None] = {}
_catalog_status_context_fingerprint: str | None = None


def _catalog_restore_environ_baseline() -> None:
    for key, orig in list(_catalog_env_saved.items()):
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig
    _catalog_env_saved.clear()


def _catalog_set_environ(key: str, val: str) -> None:
    if key not in _catalog_env_saved:
        _catalog_env_saved[key] = os.environ.get(key)
    os.environ[key] = val


def _apply_catalog_active_to_environ() -> None:
    """Materialize the active cluster's overlay into ``os.environ``.

    ``_eget``/``_subprocess_env`` already consult the catalog overlays, but several
    modules read ``os.environ`` directly (``nvis_id_templates``, ``host_exec`` SSH
    forwarding, ``native_status``, workflow steps). Pushing the overlay into
    ``os.environ`` keeps them consistent. The previous materialization is restored
    first so switching projects never leaves stale values behind.

    Precedence preserved: the *high* overlay (cluster identity/naming/connection) wins
    and is always written; the *low* overlay (platform geometry/tool defaults) only
    fills keys that have no explicit env/Settings value.
    """
    global _catalog_status_context_fingerprint
    if not (_CATALOG_AVAILABLE and catalog_context is not None):
        return
    try:
        _catalog_restore_environ_baseline()
        high, low = catalog_context.overlays_for_active()
        active = catalog_context.get_active()
        fingerprint = json.dumps(
            {
                "active": active,
                "status_high": {k: high.get(k, "") for k in sorted(_STATUS_ROW_ENV_KEYS)},
                "status_low": {k: low.get(k, "") for k in sorted(_STATUS_ROW_ENV_KEYS)},
                "connection": {
                    k: high.get(k, "")
                    for k in (
                        "RACK_EXEC_MODE",
                        "RACK_PLATFORM",
                        "RACK_REMOTE_BASE_PATH",
                        "RACK_SSH_HOST",
                        "RACK_SSH_USER",
                    )
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for key, val in high.items():
            if val and val.strip():
                _catalog_set_environ(key, val.strip())
        ov = _job_env_overrides_load()
        for key, val in low.items():
            if not val or not val.strip():
                continue
            if (os.environ.get(key) or "").strip():
                continue
            if (ov.get(key) or "").strip():
                continue
            _catalog_set_environ(key, val.strip())
        # Reset naming/status caches so the new cluster's templates are picked up.
        try:
            nvis_id_templates.clear_nvis_naming_cache()
        except Exception:
            _logger.debug("_apply_catalog_active_to_environ: ignoring error", exc_info=True)
        try:
            native_status.invalidate_job_env_file_cache()
        except Exception:
            _logger.debug("_apply_catalog_active_to_environ: ignoring error", exc_info=True)
        if (
            _catalog_status_context_fingerprint is not None
            and fingerprint != _catalog_status_context_fingerprint
        ):
            _rack_status_cache_clear_all()
        _catalog_status_context_fingerprint = fingerprint
    except Exception:
        _logger.exception("catalog: applying active context to environ failed")


# Materialising overrides into os.environ strips Settings-managed keys, so the active overlay has
# to be re-applied straight after or platform-derived values vanish. config_env owns the reader and
# cannot import this (it reaches the rack-status cache), so it is injected here -- registered at
# import time, immediately after the definition, because the very first _eget can trigger it.
config_env.set_materialize_hook(_apply_catalog_active_to_environ)


def _bootstrap_catalog_at_startup() -> None:
    """Seed/migrate the catalog and apply the active project/cluster to the environment."""
    if not (_CATALOG_AVAILABLE and catalog_seed is not None and catalog_store is not None):
        return
    try:
        repo = catalog_store.get_repo()
        summary = catalog_seed.migrate(repo)
        if summary.get("platforms_added") or summary.get("tools_added") or summary.get(
            "default_project_created"
        ):
            _logger.info("catalog seeded: %s", summary)
        # Re-materialize on any later catalog change (active switch or doc edits).
        catalog_context.add_change_hook(_apply_catalog_active_to_environ)
        # The cluster document also carries the connection topology, so a catalog
        # change can move where commands run. Drop the composed transport with it,
        # or the app keeps talking to the previous cluster's hosts.
        catalog_context.add_change_hook(host_exec.invalidate_connection)
        _apply_catalog_active_to_environ()
    except Exception:
        _logger.exception("catalog bootstrap failed; continuing with legacy env behavior")


def _bootstrap_nvftp_sync_at_startup() -> None:
    """Load job-env overrides and start NVFTP background sync (retries when config becomes ready)."""
    try:
        _job_env_overrides_load()
    except Exception:
        _logger.exception("NVFTP sync startup: job env load failed")
    nvftp_cache_sync.ensure_background_thread()


def _is_public_spa_static_path() -> bool:
    """Vite build assets and icons must be served without a session.

    The login screen is part of the React SPA; if ``/assets/*.js`` is redirected to
  ``/login``, the browser receives HTML instead of JavaScript and the page stays blank.
    """
    p = request.path
    if p in ("/login", "/favicon.svg", "/icons.svg"):
        return True
    return bool(p.startswith(("/assets/", "/spa/")))


#: Endpoints reachable WITHOUT a session. Every other request needs one.
#:
#: A constant rather than a literal inside the gate so it can be reviewed and asserted on: these are
#: Flask *endpoint* names, so moving a route into a blueprint renames it (``login`` ->
#: ``auth.login``) and a stale entry here does not fail loudly -- it makes ``/api/login`` itself
#: require the session you are trying to obtain, locking every operator out with the suite still
#: green. ``test_web_auth_gate.py`` asserts each name resolves to a real endpoint.
#:
#: ``auth.login`` is belt-and-braces: ``_is_public_spa_static_path`` already allows ``/login`` by
#: path. ``auth.api_login`` and ``auth.api_auth_check`` have no such second layer and are the
#: load-bearing entries. The ``auth.`` prefix is the blueprint name in ``api/auth.py``.
_AUTH_BYPASS_ENDPOINTS = ("auth.login", "auth.api_login", "auth.api_auth_check", "static")


@app.before_request
def _require_dashboard_auth():
    global _JOB_ENV_BOOTSTRAP_DONE
    if not _JOB_ENV_BOOTSTRAP_DONE:
        try:
            _job_env_overrides_load()
        except Exception:
            _logger.exception("bootstrap job env overrides load failed")
        else:
            _JOB_ENV_BOOTSTRAP_DONE = True
    if not _auth_enabled():
        return None
    if request.endpoint in _AUTH_BYPASS_ENDPOINTS:
        return None
    if _is_public_spa_static_path():
        return None
    if _session_ok():
        return None
    if request.path.startswith("/api/") or request.path.startswith("/ws/"):
        return (
            jsonify({"ok": False, "error": "authentication required", "login": "/login"}),
            401,
        )
    nxt = _safe_next(request.full_path)
    if nxt:
        return redirect(url_for("auth.login", next=nxt))
    return redirect(url_for("auth.login"))


@app.after_request
def _api_json_no_store(response):
    """Avoid stale dashboard JSON behind browsers or reverse proxies."""
    if request.path.startswith("/api/") and response.content_type and "application/json" in response.content_type:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _script_path() -> Path:
    p = _eget("RACK_STATUS_SCRIPT", str(DEFAULT_SCRIPT)) or str(DEFAULT_SCRIPT)
    return _rpath(Path(p))


def _parser_path() -> Path:
    p = _eget("RACK_PARSER_SCRIPT", str(SCRIPTS_DIR / "parser.sh")) or str(SCRIPTS_DIR / "parser.sh")
    return _rpath(Path(p))


def _parser_extra_args() -> list[str]:
    extra = _eget("RACK_PARSER_EXTRA_ARGS", "") or ""
    if not extra.strip():
        return []
    return shlex.split(extra)


def _parser_export_glob() -> str:
    """Basename-only glob under log_dir root for exports (default *.txt)."""
    raw = (_eget("RACK_PARSER_EXPORT_GLOB") or "*.txt").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return "*.txt"
    return raw


# Log-basename classification lives in nvis_id_templates so this module, native_status and
# the shell probes cannot drift apart on which table a log belongs to. These wrappers exist
# only to bind the caller's NvisNaming instance; keep them free of logic.












def _expand_r_inner_to_rack_nums(inner: str) -> list[int]:
    """Expand ``13-14,16`` or ``01-04`` inside ``r[…]`` brackets to rack number ints."""
    out: list[int] = []
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a, 10), int(b, 10)
            except ValueError:
                continue
            if lo <= hi:
                out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part, 10))
            except ValueError:
                continue
    return sorted(set(out))






def _multirack_physical_rack_ids(name: str) -> list[str]:
    """Rack ids for Slurm sinfo aggregation; mirrors ``multirack_display_rack_from_basename`` cases."""
    naming = _nvis_naming()
    racks = sorted(naming.find_rack_ids_in_string(name))
    if len(racks) >= 2:
        return list(racks)
    pod_re = naming.multirack_pod_r_bracket_re()
    if pod_re:
        m = pod_re.search(name)
        if m:
            expanded = naming.expand_pod_r_inner(m.group(1), m.group(2))
            if expanded:
                return expanded
    bare_re = naming.multirack_bare_r_bracket_re()
    if bare_re:
        m2 = bare_re.search(name)
        if m2:
            expanded = naming.expand_site_r_inner(_multirack_site_prefix(), m2.group(1))
            if expanded:
                return expanded
    return []








def _nemo_log_file_looks_successful(path: Path) -> bool:
    """Match parser.sh llm_label: exclude Slurm hard-fail; require a completed-run marker (OK path).

    Accepts either a NEMO training run (``train_step_timing`` / ``MODEL_TFLOP/s/GPU``) or a completed
    NCCL sweep (``Avg bus bandwidth`` is printed by ``all_reduce_perf`` only after every message size
    finishes) so multi-rack NCCL logs (``nccl-mpi_*.out`` / ``llmb-nccl_*.out``) surface in the
    Multirack analytics. An aborted NCCL run never reaches the ``Avg bus bandwidth`` line, so it's
    still excluded.

    Host-aware: runs ``grep`` through ``host_exec`` so it works whether the log is on the local fs
    (app on the head node) or on a remote head (app on a laptop over SSH). A remote path is passed
    verbatim (no local ``.resolve()``, which would break on a ``PurePosixPath``).
    """
    lp = str(_rpath(path))
    timeout = max(30, _env_int("RACK_EXPORT_SUCCESS_GREP_TIMEOUT", 180))
    # One whole-file pass already answered both questions; the two greps below are the fallback.
    rec = _logscan_record(path, timeout=timeout)
    if rec:
        if rec["failures"]["slurm_hard_fail"]:
            return False
        n, c = rec["nemo"], rec["nccl"]
        return bool(n["samples"] or n["finished"] or c["avg_bus_bandwidth"])
    try:
        fail = host_exec.run_on_host(
            ["grep", "-E", "-q", r"srun:.*error:.*Exited|error:.*SIGNAL", lp], timeout=timeout
        )
        if getattr(fail, "returncode", 1) == 0:
            return False
        ok = host_exec.run_on_host(
            [
                "grep", "-E", "-q",
                r"train_step_timing.*consumed_samples|Step Time.*MODEL_TFLOP/s/GPU|Avg bus bandwidth",
                lp,
            ],
            timeout=timeout,
        )
        return getattr(ok, "returncode", 1) == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _multirack_txt_paths_under_failed(log_dir: Path, pat: str) -> list[Path]:
    """Multirack NEMO/NCCL *.txt under log_dir/failed/ (same glob as root export).

    Multirack jobs often write logs only under ``failed/``; include them in Excel/zip export so
    parser results match the Multirack tab.
    """
    failed = _rpath(log_dir) / "failed"
    out: list[Path] = []
    # One listing through the connection: the directory lives on the execution
    # host, and stat_dir already returns regular files only (absent dir -> {}).
    for path_s in host_exec.stat_dir(failed, patterns=(pat,)):
        p = PurePosixPath(path_s)
        if _nemo_nccl_in_log_basename(p.name) and _is_multirack_log_basename(p.name):
            out.append(p)  # type: ignore[arg-type]
    return out


def list_log_txt_paths_for_export(
    log_dir: Path, *, include_multirack_under_failed: bool = True
) -> list[Path]:
    """Matching *.txt under log_dir root, optionally plus multirack logs under ``failed/``.

    NEMO one-rack logs (*1Rack-pNN-rMM-*.txt): at most one file per rack — the newest by mtime
    among logs that look successful (same heuristics as parser.sh: no Slurm hard-fail grep, LLM
    markers present). Only racks whose **nemo** cell from ``rack_status.sh`` is ``done`` (not
    ongoing / fail / pending) are included unless ``RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER`` is set.
    Racks with no successful log contribute no 1Rack file. Other matching files in the root are
    included unchanged (including multirack logs in the root). When ``include_multirack_under_failed``
    is true, multirack logs under ``failed/`` are merged in (deduped by resolved path); Excel export
    passes false so nothing under ``failed/`` is included.
    """
    pat = _parser_export_glob()
    root_files: list[Path] = []
    # One directory listing through the connection. Listing locally used to yield
    # ZERO files against a remote log dir, which is why the Logs page reported
    # "1 of 1" while 13 real logs sat in the directory.
    root = _rpath(log_dir)
    root_stats = host_exec.stat_dir(root, patterns=(pat,))
    for path_s in root_stats:
        name = PurePosixPath(path_s).name
        if name and _nemo_nccl_in_log_basename(name):
            root_files.append(PurePosixPath(path_s))  # type: ignore[arg-type]

    done_racks: frozenset[str] | None = None
    if not _truthy_env("RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER"):
        done_racks = _nemo_done_rack_ids_from_rack_status(log_dir)

    rack_buckets: dict[str, list[Path]] = {}
    others: list[Path] = []
    for p in root_files:
        rk = _rack_id_from_nemo_1rack_filename(p.name)
        if rk is None:
            others.append(p)
        else:
            rack_buckets.setdefault(rk, []).append(p)

    selected_rack: list[Path] = []
    for _rk, paths in rack_buckets.items():
        if done_racks is not None and _rk not in done_racks:
            continue
        successful = [p for p in paths if _nemo_log_file_looks_successful(p)]
        if not successful:
            continue
        # mtimes come from the listing above; the files are on the execution host,
        # so p.stat() would be a local call against a path that is not local.
        best = max(successful, key=lambda x: root_stats[str(x)].mtime)
        selected_rack.append(best)

    tail_failed = (
        _multirack_txt_paths_under_failed(log_dir, pat) if include_multirack_under_failed else []
    )
    merged: dict[str, Path] = {}
    for p in others + selected_rack + tail_failed:
        # Already absolute and normalised by _rpath/stat_dir, so dedupe on the
        # string; .resolve() is a local-filesystem call and these are exec-host paths.
        merged[str(p)] = p
    out = sorted(merged.values(), key=lambda p: p.name.lower())
    return out




def find_nvloom_log_paths_for_rack(log_dir: Path, rack: str) -> list[Path]:
    """Newest-first paths to ``*NVLOOM-<rack>-*.txt`` under ``log_dir/nvloom`` (and ``nvloom/failed``)."""
    try:
        rk = _normalize_rack_id(rack)
    except ValueError:
        return []
    pat = f"*NVLOOM-{rk}-*.txt"
    root = _rpath(log_dir)
    # mtime comes back with the listing, so ordering costs no extra round trip.
    scored: list[tuple[float, Path]] = []
    for base in (root / "nvloom", root / "nvloom" / "failed"):
        for path_s, st in host_exec.stat_dir(base, patterns=(pat,)).items():
            scored.append((st.mtime, PurePosixPath(path_s)))  # type: ignore[arg-type]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [p for _m, p in scored]


_NVLOOM_HEATMAPS_LOG_MAX_BYTES = 40 * 1024 * 1024












# Which parser.sh table each stage is the authority for. A stage's rows are taken from that
# stage's own log and nowhere else, so a NeMo log can never contribute NCCL numbers.
_PARSER_STAGE_TABLES = {
    "nccl": "nccl_peak_out_of_place_busbw_gbps",
    "nemo": "nemotron_5h_56b_train_window",
}

# Scalar/series fields that carry an actual measurement, per stage table. A row holding none of
# these is parser.sh's all-"---" placeholder rather than a result.
_PARSER_TABLE_VALUE_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "nccl_peak_out_of_place_busbw_gbps": (
        ("all_reduce_gbps", "all_gather_gbps", "alltoall_gbps"),
        ("all_reduce_busbw_series",),
    ),
    "nemotron_5h_56b_train_window": (
        ("step_s", "tflops_per_gpu", "step_time_std", "perf_std", "mfu_percent"),
        ("step_series",),
    ),
}


def _parser_row_has_measurement(row: object, table: str) -> bool:
    """True when *row* carries at least one real number for *table*.

    parser.sh emits a fully-formed placeholder row (every field ``"-"``/``"---"``, status UNK or
    FAIL, empty series) for whatever file it is handed, whether or not that file is the kind the
    table describes. Parsing an NCCL log therefore still yields a Nemotron row. Mirrors the
    abstention already applied in ``results_history.records_from_parser_json``.
    """
    if not isinstance(row, Mapping):
        return False
    scalars, series_keys = _PARSER_TABLE_VALUE_FIELDS.get(table, ((), ()))
    for key in scalars:
        raw = row.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        return True
    return any(row.get(key) for key in series_keys)


def _drop_parser_placeholder_rows(data: dict[str, object]) -> dict[str, list[str]]:
    """Strip placeholder rows from every stage table in *data*, in place.

    Returns ``{table: [rack…]}`` for what was dropped, so a rack with no run for a stage reads as
    an empty table (the truth) instead of a UNK row keyed to the *other* stage's job id. Without
    this, j15/j16 — NCCL-only racks with no NeMo log at all — rendered a Nemotron row carrying the
    NCCL job id, which reads as "NeMo ran and was inconclusive" when NeMo never ran.
    """
    dropped: dict[str, list[str]] = {}
    for table in _PARSER_STAGE_TABLES.values():
        rows = data.get(table)
        if not isinstance(rows, list) or not rows:
            continue
        keep = [r for r in rows if _parser_row_has_measurement(r, table)]
        if len(keep) == len(rows):
            continue
        gone = [
            str((r or {}).get("rack") or (r or {}).get("job_id") or "?")
            for r in rows
            if not _parser_row_has_measurement(r, table)
        ]
        data[table] = keep
        dropped[table] = gone
    return dropped


def _primary_stage_log(stage_paths: dict[str, Path], job_id: str | None) -> Path | None:
    """The log the request is 'about': the one carrying *job_id*, else NeMo, else NCCL.

    Everything that can only sensibly take one log (the Grafana time window, sn-llama, history
    ingest, log adoption) uses this, so adding the second stage cannot change their behaviour.
    """
    if not stage_paths:
        return None
    j = (job_id or "").strip()
    if j.isdigit():
        suf = f"N-{j}.txt"
        for p in stage_paths.values():
            if p.name.endswith(suf):
                return p
    return stage_paths.get("nemo") or stage_paths.get("nccl")


def _parser_secondary_stages(stage_paths: dict[str, Path], primary: Path) -> list[tuple[str, Path]]:
    """Stages whose log is a DIFFERENT file from *primary*, so they need their own parse.

    On the older combined format both stages resolve to the same file, which yields an empty
    list — the primary parse already produced both tables and must not be run twice.
    """
    seen = {str(primary)}
    out: list[tuple[str, Path]] = []
    for stage, path in stage_paths.items():
        if str(path) in seen:
            continue
        seen.add(str(path))
        out.append((stage, path))
    return out








def _hostnames_for_slurm_job(job_raw: str | None) -> tuple[list[str], str]:
    bid = _slurm_job_base_id(job_raw)
    if not bid:
        return [], "no_job_id"
    proc = host_exec.run_slurm_on_host(
        _get_platform().sacct_job_cmd(bid),
        timeout=max(20, _env_int("RACK_SACCT_TIMEOUT", 90)),
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return [], "sacct_failed"
    hosts_flat: list[str] = []
    for line in (proc.stdout or "").splitlines():
        seg = line.strip()
        if not seg:
            continue
        for part in seg.split("+"):
            part = part.strip()
            if not part:
                continue
            hosts_flat.extend(_scontrol_hostnames_preserving_case(part))
    hosts = _dedupe_hostnames_preserve_spelling(hosts_flat)
    return hosts, ("sacct" if hosts else "sacct_empty")


def _hostnames_for_rack_validation_expand(rack: str) -> tuple[list[str], str]:
    try:
        tgt = validation_target_for_rack(rack, None)
    except ValueError:
        return [], "target_error"
    hs = _dedupe_hostnames_preserve_spelling(_scontrol_hostnames_preserving_case(tgt))
    return hs, ("rack_ct_expand" if hs else "rack_ct_expand_failed")


def _job_hosts_for_grafana(rack: str, job_raw: str | None) -> tuple[list[str], str]:
    hosts, src = _hostnames_for_slurm_job(job_raw)
    if hosts:
        return hosts, src
    hosts2, src2 = _hostnames_for_rack_validation_expand(rack)
    if hosts2:
        return hosts2, src2
    return [], "none"


def _grafana_bright_hostname_promql_regex(hosts: list[str]) -> str:
    """Build ``hostname=~`` RHS for Bright ``gpu_*`` metrics (Prometheus matches the **whole** label).

    Slurm ``NodeName`` casing may differ from Prometheus (Bright often lowercases ``uname -n``;
    DCGM may keep Slurm-style ``CBR01-...``). We emit **lowercase and verbatim** alternates when
    they differ so PromQL stays portable (no ``(?i)`` — VictoriaMetrics / some stacks reject it
    in label matchers, which would blank **every** branch including ``.*``).

    Per Slurm host we add: full name (with any domain), name before the first ``.``, a long final
    hyphen segment (≥6 chars, e.g. ``cn010211``), and the last two hyphen segments when distinct
    (e.g. ``svr-cn010211``). Each literal is escaped and followed by an optional FQDN suffix
    (parenthesized dot + any chars, quantified with ``?``); we avoid ``(?:`` because Grafana
    treats ``{`` in panel queries as template syntax and can break variables.

    ``RACK_WEB_GRAFANA_HOST_REGEX_STRICT=1``: ``|``-joined ``re.escape`` of each Slurm name
    (lowercase and verbatim when different; no short forms, no domain tail).
    """
    if not hosts:
        return ".*"
    if _truthy_env("RACK_WEB_GRAFANA_HOST_REGEX_STRICT"):
        parts_s: list[str] = []
        seen_s: set[str] = set()
        for h in hosts:
            raw = (h or "").strip()
            if not raw:
                continue
            variants = (raw.lower(), raw) if raw != raw.lower() else (raw,)
            for v in variants:
                pat = re.escape(v)
                if pat in seen_s:
                    continue
                seen_s.add(pat)
                parts_s.append(pat)
        return "|".join(parts_s) if parts_s else ".*"

    parts: list[str] = []
    seen_pat: set[str] = set()

    def add_pat_for_string(lit: str) -> None:
        raw = lit.strip()
        if not raw:
            return
        variants = (raw.lower(), raw) if raw != raw.lower() else (raw.lower(),)
        for v in variants:
            pat = re.escape(v) + r"(\..*)?"
            if pat in seen_pat:
                continue
            seen_pat.add(pat)
            parts.append(pat)

    for h in hosts:
        t = (h or "").strip()
        if not t:
            continue
        nodots = t.split(".", 1)[0].strip()
        add_pat_for_string(t)
        add_pat_for_string(nodots)
        if "-" in nodots:
            segs = nodots.split("-")
            last = segs[-1]
            if len(last) >= 6:
                add_pat_for_string(last)
            if len(segs) >= 2:
                two = f"{segs[-2]}-{segs[-1]}"
                if len(two) >= 6 and two.lower() != nodots.lower():
                    add_pat_for_string(two)
    if not parts:
        return ".*"
    return "|".join(parts)


def _grafana_embed_rack_canonical_id(rack: str, hosts: list[str]) -> str | None:
    """Resolve NVIS canonical rack id for Grafana ``<rack-id>.*`` embed filter."""
    n = _nvis_naming()
    rid = (rack or "").strip()
    rid_main = rid.split("(", 1)[0].strip()
    c = n.canonical_rack_id(rid_main)
    if c:
        return c
    for seg in rid_main.split("+"):
        seg = seg.strip()
        if not seg:
            continue
        cc = n.canonical_rack_id(seg)
        if cc:
            return cc
    for r2 in n.find_rack_ids_in_string(rid):
        cc = n.canonical_rack_id(r2)
        if cc:
            return cc
    fk = n.rack_id_from_nemo_1rack_filename(rid)
    if fk:
        return fk
    for h in hosts or ():
        hit = n.sinfo_rack_from_host(h)
        if hit:
            rr, _ = hit
            return n.canonical_rack_id(rr) or rr
    for h in hosts or ():
        for r2 in n.find_rack_ids_in_string(h):
            cc = n.canonical_rack_id(r2)
            if cc:
                return cc
    return None


def _grafana_rack_prefix_promql_regex(rack: str, hosts: list[str]) -> str | None:
    """PromQL label regex for Grafana embed: ``<canonical-rack-id>.*`` only.

    Matches full hostnames such as ``CBR01-DH01-SVR-CN010211``. This must match the form users
    verify in Grafana (e.g. ``CBR01-DH01-SVR-CN0102.*``): **no** ``re.escape`` on ordinary rack
    tokens (avoids ``\\-`` in URLs) and **no** ``|`` alternates — ``/d-solo/`` + text variables can
    fail when the regex contains a pipe. Do not emit ``{`` anywhere (Grafana template parsing).

    Set ``RACK_WEB_GRAFANA_EMBED_RACK_REGEX_LOWER=1`` to use lowercase ``<rack>.*`` when Prometheus
    labels are only lowercased.
    """
    rk = _grafana_embed_rack_canonical_id(rack, hosts)
    if not rk:
        return None
    if _truthy_env("RACK_WEB_GRAFANA_EMBED_RACK_REGEX_LOWER"):
        body = rk.lower()
    else:
        body = rk
    # Safe unescaped body: Prometheus RE2 treats ``-`` literally outside ``[]``.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,191}", body):
        return re.escape(body) + r".*"
    return body + r".*"


def _grafana_embed_host_regex_and_mode(rack: str, hosts: list[str]) -> tuple[str, str]:
    """``(regex, mode)`` for Grafana ``var-job_host_regex``. See ``RACK_WEB_GRAFANA_HOST_REGEX_PER_HOST``."""
    if _truthy_env("RACK_WEB_GRAFANA_HOST_REGEX_PER_HOST"):
        if hosts:
            return _grafana_bright_hostname_promql_regex(hosts), "hosts"
        return ".*", "open"
    pref = _grafana_rack_prefix_promql_regex(rack, hosts)
    if pref:
        return pref, "rack_prefix"
    if hosts:
        return _grafana_bright_hostname_promql_regex(hosts), "hosts"
    return ".*", "open"


_LLM_LINE_MARK_RE = re.compile(
    r"(train_step_timing|Step\s+Time.*MODEL_TFLOP/s/GPU|consumed_samples|iteration\s+\d+/)",
    re.IGNORECASE,
)

# --- whole-file scanner -----------------------------------------------------------------------
# The scanner client and its caches live in logscan_cache.py, imported under these names above.


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
    return {
        "export_version": 7,
        "nccl_peak_out_of_place_busbw_gbps": [nccl_row],
        "nemotron_5h_56b_train_window": [nemo_row],
        "mnubergemm_stress": [],
        "parser_flags": [],
        "from_logscan": True,
    }


_LOG_TS_SEARCH_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)")
_DISPLAY_TZ_QUERY_RE = re.compile(r"^[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*$")


def _sanitize_display_timezone_query(raw: object) -> str | None:
    """``display_timezone`` query for ``/api/parser-results`` (IANA). Reject junk / path tricks."""
    s = (raw if isinstance(raw, str) else str(raw or "")).strip()
    if not s or len(s) > 80 or ".." in s:
        return None
    if s.lower() == "browser":
        return None
    if not _DISPLAY_TZ_QUERY_RE.fullmatch(s):
        return None
    return s


def _zoneinfo_for_log_parsing(override_name: str | None) -> tzinfo:
    """Zone for *naive* wall-clock strings in NEMO logs (no offset in the line).

    ``override_name`` comes from the browser ``display_timezone`` query (Charts TZ menu);
    otherwise ``RACK_PROMETHEUS_LOG_TIMESTAMP_TZ`` (default UTC).
    """
    from zoneinfo import ZoneInfo

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


def _log_ts_zone() -> tzinfo:
    return _zoneinfo_for_log_parsing(None)


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


def _host_read_text(path: Path, *, max_bytes: int = 0) -> str | None:
    """Read a file's text from the execution host. ``None`` on error.

    ``max_bytes>0`` caps the read at the head of the file (the historical
    ``head -c`` semantics); 0 means "no cap", expressed as a very large head.
    """
    cap = max_bytes if max_bytes > 0 else (1 << 62)
    text, meta = host_exec.read_text(
        path,
        head_bytes=cap,
        tail_bytes=0,
        whole_bytes=cap,
        timeout=max(30, _job_env_int("RACK_HOST_READ_TIMEOUT", 90)),
    )
    if meta.get("read_error"):
        return None
    return text


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


def _nemotron_row_for_rack(
    parsed: dict | None,
    rack: str,
    job_raw: str | None,
) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    ll = parsed.get("nemotron_5h_56b_train_window")
    if not isinstance(ll, list):
        return None
    rk = rack.strip().lower()
    jwant = (job_raw or "").strip()
    if jwant and jwant != "-":
        for row in ll:
            if not isinstance(row, dict):
                continue
            if str(row.get("rack", "")).strip().lower() != rk:
                continue
            if str(row.get("job_id", "")).strip() == jwant:
                return row
    for row in ll:
        if isinstance(row, dict) and str(row.get("rack", "")).strip().lower() == rk:
            return row
    return None


def _grafana_dash_uid() -> str:
    return (_eget("RACK_WEB_GRAFANA_DASH_UID") or "bright-gpu-per-host").strip() or "bright-gpu-per-host"


def _grafana_dash_slug() -> str:
    s = (_eget("RACK_WEB_GRAFANA_DASH_SLUG") or "bright-gpu-thermal-and-power").strip()
    return s or "bright-gpu-thermal-and-power"


def _rack_web_grafana_uri() -> str:
    u = (_eget("RACK_WEB_GRAFANA_URI") or "").strip()
    return u.rstrip("/") if u else ""


def _rack_web_grafana_port() -> str:
    p = (_eget("RACK_WEB_GRAFANA_PORT") or "3000").strip() or "3000"
    return p


# --- Grafana deep-links (open full BCM/NMC dashboards in a new tab, scoped by rack/host) --------
#
# Unlike the /d-solo/ *embeds* (which need GF_SECURITY_ALLOW_EMBEDDING and a homegrown 2-panel
# dashboard), deep-links open a full Grafana dashboard in a new tab. On a BCM 11 / Mission Control
# (NMC) head node these dashboards already exist and are scoped by the ``$Rack`` / ``$hostname``
# template variables — so we just build ``/d/<uid>/<slug>?var-<Rack>=..&var-<hostname>=..`` URLs.
# The catalog below defaults to the stock NMC GB200 dashboards but is fully overridable via
# ``RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS`` (JSON) so this is not hardwired to any one site.

_GRAFANA_DEEPLINK_DEFAULT: list[dict[str, str]] = [
    {
        "id": "dcgm",
        "label": "GPU / DCGM",
        "uid": "f4f4474d-f3e1-4e73-a2dc-33c52cb07bd4",
        "slug": "02-dgx-gb200-dcgm-dashboard",
        "scope": "host",
        "desc": "GPU util, temp, power, NVLink BW, XID errors, HBM (per node/GPU).",
    },
    {
        "id": "power",
        "label": "Rack power & thermal",
        "uid": "2394651a-f07b-42d4-b0ff-affa31d0a254",
        "slug": "05-dgx-gb200-rack-power-analysis",
        "scope": "rack",
        "desc": "Rack-level power draw and thermal analysis.",
    },
    {
        "id": "nvlink",
        "label": "NVLink",
        "uid": "0f0286bc-b007-4a38-a359-8384ee11a90a",
        "slug": "10-dgx-gb200-nvlink-dashboard",
        "scope": "host",
        "desc": "NVLink / NVSwitch fabric bandwidth and errors.",
    },
    {
        "id": "health",
        "label": "Health status",
        "uid": "16a8973d-e9fd-4cdf-9649-b2041705a810",
        "slug": "04-dgx-gb200-health-status",
        "scope": "rack",
        "desc": "GB200 node/GPU health status roll-up.",
    },
    {
        "id": "slurm",
        "label": "Slurm",
        "uid": "006a04e0-fd06-4ba4-9d44-251394198796",
        "slug": "06-slurm-dashboard",
        "scope": "cluster",
        "desc": "Slurm queue, allocation and node-state overview.",
    },
    {
        "id": "overall",
        "label": "SuperPOD overview",
        "uid": "b0dfdff4-f570-47a9-949a-3ab535474c2b",
        "slug": "01-superpod-overall-dashboard",
        "scope": "cluster",
        "desc": "Cluster-wide overview (all racks).",
    },
]


def _grafana_var_rack() -> str:
    return (_eget("RACK_WEB_GRAFANA_VAR_RACK") or "Rack").strip() or "Rack"


def _grafana_var_host() -> str:
    return (_eget("RACK_WEB_GRAFANA_VAR_HOST") or "hostname").strip() or "hostname"


def _grafana_deeplinks_enabled() -> bool:
    """Deep-links are enabled unless explicitly disabled; a base URL is required to be useful."""
    return not _truthy_env("RACK_WEB_GRAFANA_DEEPLINK_DISABLED")


def _grafana_deeplink_dashboards() -> list[dict[str, str]]:
    """Parsed deep-link dashboard catalog.

    Override with ``RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS`` = JSON list of objects, each with
    ``uid`` (required), ``label``, ``slug``, ``scope`` (``host``|``rack``|``cluster``) and
    ``desc``. Invalid JSON or a non-list falls back to the built-in NMC catalog.
    """
    raw = (_eget("RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS") or "").strip()
    if not raw:
        return [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        _logger.warning("RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS is not valid JSON; using defaults")
        return [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]
    if not isinstance(parsed, list):
        return [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]
    out: list[dict[str, str]] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "").strip()
        if not uid:
            continue
        scope = str(item.get("scope") or "rack").strip().lower()
        if scope not in ("host", "rack", "cluster"):
            scope = "rack"
        out.append(
            {
                "id": str(item.get("id") or f"dash{i}").strip() or f"dash{i}",
                "label": str(item.get("label") or uid).strip() or uid,
                "uid": uid,
                "slug": str(item.get("slug") or "d").strip() or "d",
                "scope": scope,
                "desc": str(item.get("desc") or "").strip(),
            }
        )
    return out or [dict(d) for d in _GRAFANA_DEEPLINK_DEFAULT]




def _grafana_url_timezone_param() -> str:
    """Value for Grafana ``timezone`` query param and parser UI chart labels.

    ``RACK_WEB_GRAFANA_TIMEZONE``: ``browser``, ``utc``, or an IANA zone (e.g. ``America/Los_Angeles``).
    When unset, use the same zone as log timestamp parsing (``RACK_PROMETHEUS_LOG_TIMESTAMP_TZ``),
    normalised to ``utc`` for UTC-like names so panel axes match server-derived ``from``/``to``.
    """
    g = (_eget("RACK_WEB_GRAFANA_TIMEZONE") or "").strip()
    if g:
        gl = g.lower()
        if gl in ("browser", "utc"):
            return gl
        return g
    logtz = (_eget("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ") or "UTC").strip() or "UTC"
    if logtz.upper() in ("UTC", "GMT", "Z", "UCT"):
        return "utc"
    return logtz


def _effective_display_tz_for_embed(log_tz_query: str | None) -> str:
    """Grafana ``timezone=`` and ``display_timezone`` when user passes ``display_timezone`` query."""
    if log_tz_query and log_tz_query.strip():
        q = log_tz_query.strip()
        if q.lower() in ("utc", "gmt", "z", "uct"):
            return "utc"
        return q
    return _grafana_url_timezone_param()


def _grafana_parser_embed_context(
    log_path: Path,
    rack: str,
    job_raw: str | None,
    parsed: dict | None,
    log_timestamp_tz: str | None = None,
) -> dict[str, object]:
    dtz = _effective_display_tz_for_embed(log_timestamp_tz)
    if _truthy_env("RACK_WEB_GRAFANA_EMBED_DISABLED"):
        return {
            "ok": False,
            "iframe_ready": False,
            "disabled": True,
            "note": "Grafana embed disabled (RACK_WEB_GRAFANA_EMBED_DISABLED).",
            "display_timezone": dtz,
        }
    row = _nemotron_row_for_rack(parsed, rack, job_raw)
    win = _compute_llm_time_window_unix(log_path, row, log_timestamp_tz)
    if not win:
        return {
            "ok": False,
            "iframe_ready": False,
            "note": "Could not derive LLM time window.",
            "display_timezone": dtz,
        }
    lo, hi, tsrc = win
    if hi <= lo:
        return {
            "ok": False,
            "iframe_ready": False,
            "note": "Invalid time window (end <= start).",
            "display_timezone": dtz,
        }
    hosts, hsrc = _job_hosts_for_grafana(rack, job_raw)
    host_regex, host_regex_mode = _grafana_embed_host_regex_and_mode(rack, hosts)
    return {
        "ok": True,
        "iframe_ready": True,
        "from_ms": int(lo * 1000),
        "to_ms": int(hi * 1000),
        "from_iso_utc": _iso_utc_z(lo),
        "to_iso_utc": _iso_utc_z(hi),
        "host_regex": host_regex,
        "host_regex_mode": host_regex_mode,
        "hosts": hosts,
        "hosts_source": hsrc,
        "time_source": tsrc,
        "dashboard_uid": _grafana_dash_uid(),
        "dash_slug": _grafana_dash_slug(),
        "panel_ids": [1, 2],
        "display_timezone": dtz,
    }


def _grafana_live_5m_embed_context(
    rack: str,
    log_timestamp_tz: str | None = None,
) -> dict[str, object]:
    """Grafana solo embed for the last five minutes (wall clock), hosts = rack CT expand."""
    dtz = _effective_display_tz_for_embed(log_timestamp_tz)
    if _truthy_env("RACK_WEB_GRAFANA_EMBED_DISABLED"):
        return {
            "ok": False,
            "iframe_ready": False,
            "disabled": True,
            "note": "Grafana embed disabled (RACK_WEB_GRAFANA_EMBED_DISABLED).",
            "display_timezone": dtz,
        }
    now = time.time()
    lo, hi = now - 300.0, now
    if hi <= lo:
        return {
            "ok": False,
            "iframe_ready": False,
            "note": "Invalid time window (end <= start).",
            "display_timezone": dtz,
        }
    hosts, hsrc = _hostnames_for_rack_validation_expand(rack)
    host_regex, host_regex_mode = _grafana_embed_host_regex_and_mode(rack, hosts)
    return {
        "ok": True,
        "iframe_ready": True,
        "from_ms": int(lo * 1000),
        "to_ms": int(hi * 1000),
        "from_iso_utc": _iso_utc_z(lo),
        "to_iso_utc": _iso_utc_z(hi),
        "host_regex": host_regex,
        "host_regex_mode": host_regex_mode,
        "hosts": hosts,
        "hosts_source": hsrc,
        "time_source": "live_last_5m",
        "dashboard_uid": _grafana_dash_uid(),
        "dash_slug": _grafana_dash_slug(),
        "panel_ids": [1, 2],
        "display_timezone": dtz,
    }






# The single path resolver, shared with paths.py and the api/ modules rather than redeclared:
# this module carried a byte-identical copy of core.rpath. It replaced the three overlapping
# resolvers app.py used to have (``_resolve_path``, ``_lp_resolve``, and the inline
# ``str(x) if is_remote() else str(x.resolve())`` idiom); keeping a second definition of the
# replacement would have reintroduced exactly the drift that consolidation removed.
_rpath = core.rpath
_iso_utc_z = core.utc_iso_z
_mean_finite = core.mean_finite
_median_float = core.median_float
_last_regex_int = core.last_regex_int


def _validation_script_path() -> Path:
    p = _eget("RACK_VALIDATION_SCRIPT", str(SCRIPTS_DIR / "run_singlerack.sh")) or str(
        SCRIPTS_DIR / "run_singlerack.sh"
    )
    return _rpath(Path(p))


def _mnubergemm_stress_script_path() -> Path:
    p = _eget("RACK_MNUBERGEMM_STRESS_SCRIPT", str(SCRIPTS_DIR / "run_mnubergemm_stress.sh")) or str(
        SCRIPTS_DIR / "run_mnubergemm_stress.sh"
    )
    return _rpath(Path(p))


def _nvloom_singlerack_script_path() -> Path:
    p = _eget("RACK_NVLOOM_SINGLERACK_SCRIPT", str(SCRIPTS_DIR / "run_nvloom_singlerack.sh")) or str(
        SCRIPTS_DIR / "run_nvloom_singlerack.sh"
    )
    return _rpath(Path(p))


def _clusterkit_script_path() -> Path:
    p = _eget("RACK_CLUSTERKIT_SCRIPT", str(SCRIPTS_DIR / "run_clusterkit.sh")) or str(SCRIPTS_DIR / "run_clusterkit.sh")
    return _rpath(Path(p))


def _run_llmb_nccl_script_path() -> Path:
    p = _eget("RACK_LLMB_NCCL_SCRIPT", str(SCRIPTS_DIR / "run_llmb_nccl.sh")) or str(SCRIPTS_DIR / "run_llmb_nccl.sh")
    return _rpath(Path(p))


def _run_nccl_mpi_script_path() -> Path:
    """Direct-NCCL launcher: resolves racks → nodes → submits nccl_allreduce.sbatch
    (host nccl-tests + mpirun over IB; no llmb-run/container/UCX-verbs guesswork)."""
    p = _eget("RACK_NCCL_MPI_SCRIPT", str(SCRIPTS_DIR / "run_nccl_mpi.sh")) or str(SCRIPTS_DIR / "run_nccl_mpi.sh")
    return _rpath(Path(p))


def _nccl_fullrack_script_path() -> Path:
    p = _eget("RACK_NCCL_FULLRACK_SCRIPT", str(SCRIPTS_DIR / "nccl_fullrack_rounds.sh")) or str(
        SCRIPTS_DIR / "nccl_fullrack_rounds.sh"
    )
    return _rpath(Path(p))


def _clusterkit_product_line_for_job(body: dict) -> str:
    """Prefer POST product_line, then saved cluster dashboard metadata, then process env."""
    v = body.get("product_line")
    if isinstance(v, str) and v.strip():
        return v.strip()
    try:
        meta = read_cluster_dashboard_metadata()
        pl = (meta.get("product_line") or "").strip()
        if pl:
            return pl
    except Exception:
        _logger.debug("_clusterkit_product_line_for_job: ignoring error", exc_info=True)
    return (_eget("RACK_CLUSTER_PRODUCT_LINE") or _eget("RACK_PRODUCT_LINE") or "").strip()


_nvis_workspace_dir_effective = config_env._nvis_workspace_dir_effective


def _sysinfo_script_path() -> Path:
    p = _eget("RACK_SYSINFO_SCRIPT", str(SCRIPTS_DIR / "sysinfo.sh")) or str(SCRIPTS_DIR / "sysinfo.sh")
    return _rpath(Path(p))


def _nvswitches_rack_audit_script_path() -> Path:
    p = _eget("RACK_NVSWITCH_AUDIT_SCRIPT", str(SCRIPTS_DIR / "nvswitches_rack_audit.sh")) or str(
        SCRIPTS_DIR / "nvswitches_rack_audit.sh"
    )
    return _rpath(Path(p))


def _nvswitch_sdn_reset_script_path() -> Path:
    p = _eget("RACK_NVSWITCH_SDN_RESET_SCRIPT", str(SCRIPTS_DIR / "nvswitch_sdn_reset.sh")) or str(
        SCRIPTS_DIR / "nvswitch_sdn_reset.sh"
    )
    return _rpath(Path(p))


def _nvswitch_system_reset_script_path() -> Path:
    p = _eget("RACK_NVSWITCH_SYSTEM_RESET_SCRIPT", str(SCRIPTS_DIR / "nvswitch_system_reset.sh")) or str(
        SCRIPTS_DIR / "nvswitch_system_reset.sh"
    )
    return _rpath(Path(p))


def _imex_reset_script_path() -> Path:
    p = _eget("RACK_IMEX_RESET_SCRIPT", str(SCRIPTS_DIR / "imex_reset.sh")) or str(
        SCRIPTS_DIR / "imex_reset.sh"
    )
    return _rpath(Path(p))


def _imex_check_script_path() -> Path:
    p = _eget("RACK_IMEX_CHECK_SCRIPT", str(SCRIPTS_DIR / "imex_check.sh")) or str(
        SCRIPTS_DIR / "imex_check.sh"
    )
    return _rpath(Path(p))


def _nemo_xrack_script_path() -> Path:
    p = _eget("RACK_NEMO_XRACK_SCRIPT", str(SCRIPTS_DIR / "run-nemo-xrack.sh")) or str(
        SCRIPTS_DIR / "run-nemo-xrack.sh"
    )
    return _rpath(Path(p))


def _run_llama_script_path() -> Path:
    p = _eget("RACK_LLAMA_SCRIPT", str(SCRIPTS_DIR / "run-llama.sh")) or str(SCRIPTS_DIR / "run-llama.sh")
    return _rpath(Path(p))


def _run_exemplar_llama_script_path() -> Path:
    p = _eget(
        "RACK_EXEMPLAR_LLAMA_SCRIPT",
        str(SCRIPTS_DIR / "run-exemplar-llama.sh"),
    ) or str(SCRIPTS_DIR / "run-exemplar-llama.sh")
    return _rpath(Path(p))


def _job_env_effective_values() -> dict[str, str]:
    """Resolved values for Settings → job environment (same as runtime after overrides)."""
    ex = _extra_script_args()
    px = _parser_extra_args()
    out: dict[str, str] = {
        "RACK_STATUS_BACKGROUND_INTERVAL_SEC": str(_rack_status_background_interval_sec()),
        "RACK_PROMETHEUS_LOG_TIMESTAMP_TZ": (_eget("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ") or "UTC").strip() or "UTC",
        "RACK_WEB_GRAFANA_URI": _rack_web_grafana_uri(),
        "RACK_WEB_GRAFANA_PORT": _rack_web_grafana_port(),
        "RACK_WEB_GRAFANA_DASH_UID": _grafana_dash_uid(),
        "RACK_WEB_GRAFANA_DASH_SLUG": _grafana_dash_slug(),
        "RACK_WEB_GRAFANA_TIMEZONE": (_eget("RACK_WEB_GRAFANA_TIMEZONE") or "").strip(),
        "RACK_WEB_GRAFANA_VAR_RACK": _grafana_var_rack(),
        "RACK_WEB_GRAFANA_VAR_HOST": _grafana_var_host(),
        "RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS": (_eget("RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS") or "").strip(),
        "RACK_WEB_GRAFANA_DEEPLINK_DISABLED": (_eget("RACK_WEB_GRAFANA_DEEPLINK_DISABLED") or "").strip(),
        "RACK_PROMETHEUS_BASIC_AUTH_USER": (_eget("RACK_PROMETHEUS_BASIC_AUTH_USER") or "").strip(),
        "RACK_PROMETHEUS_BASIC_AUTH_PASSWORD": (_eget("RACK_PROMETHEUS_BASIC_AUTH_PASSWORD") or "").strip(),
        "RACK_WEB_PROJECT_NAME": (_eget("RACK_WEB_PROJECT_NAME") or "").strip(),
        "RACK_PROJECT_TOKEN": (_eget("RACK_PROJECT_TOKEN") or "").strip(),
        "RACK_NVFTP_SFTP_USER": (_eget("RACK_NVFTP_SFTP_USER") or "").strip(),
        "RACK_NVFTP_REMOTE_BASE": (_eget("RACK_NVFTP_REMOTE_BASE", "") or "").strip(),
        "RACK_NVFTP_SYNC_INTERVAL_SEC": str(max(300, _job_env_int("RACK_NVFTP_SYNC_INTERVAL_SEC", 7200))),
        "RACK_NVFTP_SFTP_HOST": (_eget("RACK_NVFTP_SFTP_HOST", "transfer.nvftp.nvidia.com") or "transfer.nvftp.nvidia.com").strip(),
        "RACK_VALIDATION_CT_RANGE": _validation_ct_inner(),
        "VALIDATION_TEST": (_eget("VALIDATION_TEST") or "nccl_all_reduce,llm").strip(),
        "RACK_VALIDATION_SCRIPT": str(_validation_script_path()),
        "RACK_VALIDATION_TIMEOUT": str(max(30, _job_env_int("RACK_VALIDATION_TIMEOUT", 600))),
        "RACK_STATUS_SCRIPT": str(_script_path()),
        "RACK_STATUS_EXTRA_ARGS": shlex.join(ex) if ex else "",
        "RACK_PARSER_SCRIPT": str(_parser_path()),
        "RACK_PARSER_EXTRA_ARGS": shlex.join(px) if px else "",
        "RACK_PARSER_EXPORT_GLOB": _parser_export_glob(),
        "RACK_PARSER_TIMEOUT": str(max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300))),
        "RACK_PARSER_EXCEL_REPORT_DIR": (_eget("RACK_PARSER_EXCEL_REPORT_DIR") or "").strip(),
        "RACK_MULTIRACK_SITE_PREFIX": _multirack_site_prefix(),
        "RACK_MULTIRACK_VALIDATION_TEST": (_eget("RACK_MULTIRACK_VALIDATION_TEST") or "nccl_all_reduce").strip(),
        "RACK_CMSH": (_eget("RACK_CMSH", "cmsh") or "cmsh").strip(),
        "NVIS_RACK_ID": native_status._nvis_rack_id_template(),
        "NVIS_CT_ID": native_status._nvis_ct_id_template(),
        "NVIS_NVSW_ID": native_status._nvis_nvsw_id_template(),
        "NEMO_SQSH_SRC": (_eget("NEMO_SQSH_SRC") or "").strip(),
        "RACK_NEMO_XRACK_SCRIPT": str(_nemo_xrack_script_path()),
        "RACK_NEMO_XRACK_TIMEOUT": str(max(300, _job_env_int("RACK_NEMO_XRACK_TIMEOUT", 7200))),
        "RACK_LLAMA_SCRIPT": str(_run_llama_script_path()),
        "RACK_LLAMA_TIMEOUT": str(max(120, _job_env_int("RACK_LLAMA_TIMEOUT", 7200))),
        "RACK_EXEMPLAR_LLAMA_SCRIPT": str(_run_exemplar_llama_script_path()),
        "RACK_EXEMPLAR_LLAMA_TIMEOUT": str(max(120, _job_env_int("RACK_EXEMPLAR_LLAMA_TIMEOUT", 900))),
        "RACK_EXEMPLAR_LLAMA_CT_RANGE": (
            _eget("RACK_EXEMPLAR_LLAMA_CT_RANGE", "11-18,28-35") or "11-18,28-35"
        ).strip(),
        "RACK_MNUBERGEMM_STRESS_SCRIPT": str(_mnubergemm_stress_script_path()),
        "RACK_MNUBERGEMM_CUDA_MAJOR": (_eget("RACK_MNUBERGEMM_CUDA_MAJOR") or _eget("RACK_NVLOOM_CUDA_MAJOR") or "13").strip(),
        "RACK_NVLOOM_SINGLERACK_SCRIPT": str(_nvloom_singlerack_script_path()),
        "RACK_NVLOOM_SINGLERACK_TIMEOUT": str(max(120, _job_env_int("RACK_NVLOOM_SINGLERACK_TIMEOUT", 10800))),
        "RACK_NVLOOM_RACK_SAMPLES": str(max(1, min(256, _job_env_int("RACK_NVLOOM_RACK_SAMPLES", 5)))),
        "RACK_CLUSTERKIT_SCRIPT": str(_clusterkit_script_path()),
        "RACK_CLUSTERKIT_TIMEOUT": str(max(120, _job_env_int("RACK_CLUSTERKIT_TIMEOUT", 14400))),
        "RACK_CLUSTERKIT_CUDA_MAJOR": (
            _eget("RACK_CLUSTERKIT_CUDA_MAJOR") or _eget("RACK_NVLOOM_CUDA_MAJOR") or "13"
        ).strip(),
        "RACK_LLMB_NCCL_SCRIPT": str(_run_llmb_nccl_script_path()),
        "RACK_LLMB_NCCL_TIMEOUT": str(max(120, _job_env_int("RACK_LLMB_NCCL_TIMEOUT", 14400))),
        "RACK_LLMB_NCCL_RESERVATION": (_eget("RACK_LLMB_NCCL_RESERVATION") or "").strip(),
        "NVIS_WORKSPACE_DIR": _nvis_workspace_dir_effective(),
        "RACK_WORKSPACE_DIR": (_eget("RACK_WORKSPACE_DIR") or "").strip(),
        "RACK_NVLOOM_CUDA_MAJOR": (_eget("RACK_NVLOOM_CUDA_MAJOR") or "13").strip(),
        "RACK_SYSINFO_SCRIPT": str(_sysinfo_script_path()),
        "RACK_SYSINFO_TIMEOUT": str(max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))),
        "RACK_HW_CHECK_SCRIPT": str(_hw_check_pdsh_script_path()),
        "RACK_GPU_RESET_SCRIPT": (_eget("RACK_GPU_RESET_SCRIPT") or "").strip(),
        "RACK_NVSWITCH_AUDIT_SCRIPT": str(_nvswitches_rack_audit_script_path()),
        "RACK_NVSWITCH_SDN_RESET_SCRIPT": str(_nvswitch_sdn_reset_script_path()),
        "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT": str(_nvswitch_system_reset_script_path()),
        "RACK_IMEX_RESET_SCRIPT": str(_imex_reset_script_path()),
        "RACK_IMEX_CHECK_SCRIPT": str(_imex_check_script_path()),
        "RACK_NVLMAPPER_REPORTS_BASE": str(_nvlmapper_reports_base()),
        "NVLMAPPER_RC": (_eget("NVLMAPPER_RC") or "").strip(),
        "NVLMAPPER_USERNAME": (_eget("NVLMAPPER_USERNAME") or "").strip(),
        "NVLMAPPER_PASSWORD": (_eget("NVLMAPPER_PASSWORD") or "").strip(),
        "RACK_SLURM_DRAIN_REASON": (_eget("RACK_SLURM_DRAIN_REASON") or "admin").strip() or "admin",
    }
    # Keys listed in ``JOB_ENV_UI_DEFINITIONS`` must appear above with computed defaults, **or** be filled
    # from ``_eget`` here. Otherwise Settings GET/PUT shows empty inputs even though overrides JSON saved
    # correctly (regression when new env rows are added without updating this dict).
    for k in _JOB_ENV_ALLOWED_KEYS:
        if k not in out:
            out[k] = (_eget(k) or "").strip()
    return out


def _normalize_llama_nodes_spec(raw: object) -> str:
    """Slurm nodeset / hostlist fragment for ``run-llama.sh`` (no shell metacharacters)."""
    if not isinstance(raw, str):
        raise ValueError("nodes_spec must be a string")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
    t = raw.strip()
    if not t or len(t) > 800:
        raise ValueError("nodes_spec is empty or too long")
    for c in ";|&$`'\n\r()<>":
        if c in t:
            raise ValueError("nodes_spec contains forbidden characters")
    return t
















def _tools_download_destination_default() -> str:
    """Directory default for Tools → download containers: parent of ``NEMO_SQSH_SRC`` when it is a .sqsh path.

    Intentionally avoids ``Path.resolve()``: in SSH mode the path refers to the *remote*
    cluster filesystem, so resolving it against the local dashboard host is wrong. On macOS
    that rewrites ``/home/cmods`` to ``/System/Volumes/Data/home/cmods`` via firmlinks.
    Use a pure-path parent so the remote path is preserved verbatim.
    """
    raw = (_eget("NEMO_SQSH_SRC") or "").strip()
    if not raw:
        return ""
    p = PurePosixPath(raw)
    if p.suffix.lower() == ".sqsh":
        parent = str(p.parent)
        return parent if parent not in ("", ".") else ""
    return str(p)


def _extra_script_args() -> list[str]:
    extra = _eget("RACK_STATUS_EXTRA_ARGS", "") or ""
    if not extra.strip():
        return []
    return shlex.split(extra)




def power_specs_from_ct_down_unk(raw: str | None) -> list[str]:
    """Extract tray specs from ct_down_unk for cmsh power reset — only DN: (and legacy UNK:) bodies.

    INST:/UP: trays are excluded (non-down / installing / healthy).
    """
    s = (raw or "").strip()
    if not s or s in ("-", "?"):
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add_from_fragment(fragment: str) -> None:
        for m in _ct_power_token_re().finditer(fragment):
            tok = m.group(1).lower()
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)

    if re.search(r"(^|;)\s*(dn|unk)\s*:", s, re.IGNORECASE):
        for seg in s.split(";"):
            t = seg.strip()
            m = re.match(r"^(dn|unk)\s*:\s*(.*)$", t, re.IGNORECASE)
            if not m:
                continue
            add_from_fragment(m.group(2))
    else:
        add_from_fragment(s)
    return out








def _run_cmsh_device_power(
    cmsh: str,
    inner: str,
    timeout: int,
    spec: str,
    phase: str,
) -> dict:
    plat = _get_platform()
    cmd = plat.device_power_cmd(inner)
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError(f"cluster manager executable not found: {cmsh}") from None
    except subprocess.TimeoutExpired:
        return {
            "spec": spec,
            "phase": phase,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command": cmd_display,
        }
    return {
        "spec": spec,
        "phase": phase,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command": cmd_display,
    }


def run_cmsh_power_reset(specs: list[str]) -> list[dict]:
    """For each tray spec: cmsh device; power … off, sleep, then device; power … on."""
    cmsh = _eget("RACK_CMSH", "cmsh") or "cmsh"
    timeout = max(5, _env_int("RACK_POWER_TIMEOUT", 120))
    sleep_sec = max(0, _env_int("RACK_POWER_RESET_SLEEP_SEC", 10))
    dry = os.environ.get("RACK_POWER_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    results: list[dict] = []
    for spec in specs:
        inner_off = f"device; power -n {spec} off"
        inner_on = f"device; power -n {spec} on"
        if dry:
            results.append(
                {
                    "spec": spec,
                    "phase": "off",
                    "returncode": 0,
                    "stdout": "dry-run (RACK_POWER_DRY_RUN)",
                    "stderr": "",
                    "command": f"{shlex.quote(cmsh)} -c {shlex.quote(inner_off)}",
                }
            )
            results.append(
                {
                    "spec": spec,
                    "phase": "on",
                    "returncode": 0,
                    "stdout": "dry-run (RACK_POWER_DRY_RUN)",
                    "stderr": "",
                    "command": f"{shlex.quote(cmsh)} -c {shlex.quote(inner_on)}",
                }
            )
            continue
        results.append(_run_cmsh_device_power(cmsh, inner_off, timeout, spec, "off"))
        if sleep_sec:
            time.sleep(float(sleep_sec))
        results.append(_run_cmsh_device_power(cmsh, inner_on, timeout, spec, "on"))
    return results








def nvswitch_hosts_for_rack_list(rack: str) -> list[str]:
    """Expand NV switch hostnames for *rack* via ``nodeset`` when available; else template fallbacks."""
    naming = _nvis_naming()
    rk = _normalize_rack_id(rack)
    out: list[str] = []
    timeout = max(5, _env_int("RACK_NVSWITCH_NODESET_TIMEOUT", 30))
    for spec in naming.nvsw_nodeset_specs(rk):
        try:
            proc = subprocess.run(
                ["nodeset", "-e", spec],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            break
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode != 0:
            continue
        for tok in (proc.stdout or "").split():
            t = tok.strip().lower()
            if not t:
                continue
            try:
                _normalize_nvswitch_host_for_rack(rk, t)
            except ValueError:
                continue
            out.append(t)
        if out:
            break
    if not out:
        out = naming.nvsw_fallback_hosts(rk)
    return sorted(set(out), key=_nvswitch_host_sort_key)


def _normalize_comments_map(raw: object) -> dict[str, str]:
    """Legacy rack-only normalization (``pN-rMM`` keys). Prefer :func:`_split_stored_comments`."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        try:
            rk = _normalize_rack_id(k)
        except ValueError:
            continue
        t = v.strip()
        if t:
            out[rk] = t
    return out
















def _merge_fresh_comments_into_status_payload(out: dict) -> None:
    """Re-read rack comments from disk. Cached /api/status rows omit updates until rack_status re-runs; comments are edited separately, so always merge."""
    try:
        out["comments"] = read_comments()
    except Exception:
        _logger.debug("_merge_fresh_comments_into_status_payload: ignoring error", exc_info=True)
        out.setdefault("comments", {})


def _merge_live_native_workers_into_status(out: dict, log_dir: Path) -> None:
    """Refresh native table rows and ``native_workers`` from in-memory async workers.

    Cached ``/api/status`` JSON can otherwise keep ``sinfo`` / ``ct_down_unk`` as ``?`` forever
    (first snapshot was taken before ds/sinfo threads finished) while only worker metadata was merged.
    """
    if _truthy_env("RACK_STATUS_USE_SHELL_TABLE"):
        return
    ld = _rpath(log_dir)
    snap = native_status.worker_snapshot_for_log_dir(ld)
    if snap is not None:
        out["native_workers"] = snap
    live = native_status.live_table_snapshot_for_log_dir(ld)
    if live is not None:
        out["rows"] = live["rows"]
        out["summary"] = live["summary"]
        fn = live.get("footnotes") or []
        out["footnotes"] = fn
        out["stderr"] = "\n".join(fn) if fn else ""
        if live.get("legend"):
            out["legend"] = live["legend"]
        out["updated_at"] = datetime.now(timezone.utc).isoformat()


_FOOTNOTE_MARKERS = (
    "squeue disabled",
    "sinfo disabled",
    "cmsh not in PATH",
    "cmsh produced no",
    "cmsh -c ds produced",
    "rack(s) in logs but not matched",
)


def parse_rack_output(text: str) -> dict:
    """Parse stdout from rack_status.sh with NO_COLOR=1."""
    lines = text.splitlines()
    legend = ""
    rows: list[dict[str, str]] = []
    summary: dict[str, int] | None = None
    footnotes: list[str] = []

    hline_re = re.compile(r"^\s*\+[-+]+\+")
    row_re = re.compile(r"^\s*\|")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("=========="):
            continue
        if stripped.startswith(("rack; list", "cm: rack; list")):
            legend = stripped
            continue
        if hline_re.match(line):
            continue
        if row_re.match(line):
            parts = [p.strip() for p in line.split("|")]
            if parts and parts[0] == "":
                parts = parts[1:]
            if parts and parts[-1] == "":
                parts = parts[:-1]
            if len(parts) < 5:
                continue
            if parts[0].lower() == "rack" and any(p.lower() == "nemo" for p in parts):
                continue
            # New: rack, nemo, last_job, sinfo, ct_down_unk [, nemo_hint]. Legacy: rack, cm, nemo, last_job, sinfo, ct_down_unk.
            legacy_cm = len(parts) >= 6 and parts[1].lower() in ("yes", "no", "?", "n/a")
            if legacy_cm:
                rows.append(
                    {
                        "rack": parts[0],
                        "nemo": parts[2],
                        "last_job": parts[3],
                        "sinfo": parts[4],
                        "ct_down_unk": parts[5],
                    }
                )
            else:
                hint = parts[5] if len(parts) > 5 else ""
                rows.append(
                    {
                        "rack": parts[0],
                        "nemo": parts[1],
                        "last_job": parts[2],
                        "sinfo": parts[3],
                        "ct_down_unk": parts[4],
                        "nemo_hint": hint,
                    }
                )
            continue
        m = re.search(
            r"Summary:\s*(\d+)\s*done\s+(\d+)\s*fail\s+(\d+)\s*ongoing\s+(\d+)\s*pending",
            line,
        )
        if m:
            summary = {
                "done": int(m.group(1)),
                "fail": int(m.group(2)),
                "ongoing": int(m.group(3)),
                "pending": int(m.group(4)),
            }
            continue
        if any(mk in stripped for mk in _FOOTNOTE_MARKERS) and stripped not in footnotes:
            footnotes.append(stripped)

    return {
        "legend": legend,
        "rows": rows,
        "summary": summary,
        "footnotes": footnotes,
    }


_CMSH_RACK_LIST_LOCK = threading.Lock()
_CMSH_RACK_LIST_STATE: str = "pending"  # pending | ok | failed
_CMSH_RACK_LIST_PATH = REPO_ROOT / "var" / "cache" / "cmsh_rack_list.txt"
_CMSH_RACK_LIST_WARM_THREAD_STARTED = False
_CMSH_RACK_WARM_START_LOCK = threading.Lock()


def _rack_sort_key_for_cmsh_list(rid: str) -> tuple[int, int, str]:
    m = re.match(r"^p(\d+)-r(\d+)$", rid.strip().lower())
    if not m:
        return (10**9, 10**9, rid)
    return (int(m.group(1)), int(m.group(2)), rid)


def _try_populate_cmsh_rack_list_file() -> None:
    """Run ``cmsh -c 'rack; list'`` at most once per process; write one rack id per line for rack_status.sh."""
    global _CMSH_RACK_LIST_STATE
    if _truthy_env("RACK_CMSH_RACK_LIST_CACHE_DISABLED"):
        with _CMSH_RACK_LIST_LOCK:
            if _CMSH_RACK_LIST_STATE == "pending":
                _CMSH_RACK_LIST_STATE = "failed"
        return

    with _CMSH_RACK_LIST_LOCK:
        if _CMSH_RACK_LIST_STATE != "pending":
            return
        plat = _get_platform()
        cmd = plat.list_racks_cmd()
        if not host_exec.which(cmd[0]):
            _CMSH_RACK_LIST_STATE = "failed"
            return
        try:
            proc = host_exec.run_on_host(
                cmd,
                timeout=max(30, _env_int("RACK_CMSH_RACK_LIST_TIMEOUT_SEC", 120)),
            )
        except (OSError, subprocess.TimeoutExpired):
            _CMSH_RACK_LIST_STATE = "failed"
            return
        text = proc.stdout or ""
        ids = native_status.extract_rack_ids_from_cmsh_text(text)
        if proc.returncode != 0 and not ids:
            _CMSH_RACK_LIST_STATE = "failed"
            return
        body = "\n".join(ids) + ("\n" if ids else "")
        try:
            _CMSH_RACK_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CMSH_RACK_LIST_PATH.with_name(_CMSH_RACK_LIST_PATH.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(str(tmp), str(_CMSH_RACK_LIST_PATH))
        except OSError:
            _CMSH_RACK_LIST_STATE = "failed"
            return
        _CMSH_RACK_LIST_STATE = "ok"


def _ensure_cmsh_rack_list_env_for_subprocess(env: dict) -> None:
    """Always set RACK_CMSH_RACK_LIST_FILE to the default cache path (rack_status uses it only if readable)."""
    if _truthy_env("RACK_CMSH_RACK_LIST_CACHE_DISABLED"):
        env.pop("RACK_CMSH_RACK_LIST_FILE", None)
        return
    _try_populate_cmsh_rack_list_file()
    try:
        env["RACK_CMSH_RACK_LIST_FILE"] = str(_CMSH_RACK_LIST_PATH.resolve())
    except OSError:
        pass


def _start_cmsh_rack_list_warm_thread() -> None:
    """Background: populate cmsh rack list before the first rack_status run when possible."""
    global _CMSH_RACK_LIST_WARM_THREAD_STARTED
    if _truthy_env("RACK_CMSH_RACK_LIST_CACHE_DISABLED"):
        return
    with _CMSH_RACK_WARM_START_LOCK:
        if _CMSH_RACK_LIST_WARM_THREAD_STARTED:
            return
        _CMSH_RACK_LIST_WARM_THREAD_STARTED = True

    def _run() -> None:
        _try_populate_cmsh_rack_list_file()

    threading.Thread(target=_run, name="cmsh-rack-list-warm", daemon=True).start()


def run_rack_status(log_dir: Path, include_raw: bool = False) -> dict:
    if _truthy_env("RACK_STATUS_DEMO"):
        import demo_data
        return demo_data.generate_demo_status(log_dir, include_raw)

    if _truthy_env("RACK_STATUS_USE_SHELL_TABLE"):
        script = _script_path()
        if not _script_exists(script):
            raise FileNotFoundError(f"rack_status script not found: {script}")

        env = _subprocess_env({"NO_COLOR": "1"})
        _ensure_cmsh_rack_list_env_for_subprocess(env)

        tmp_pre: Path | None = None
        proc: subprocess.CompletedProcess | None = None
        try:
            tmp_pre = _maybe_build_tmp_status_from_log_cache(log_dir)
            if tmp_pre is not None:
                cmd = [str(script), *_extra_script_args(), "--tmp-status", str(tmp_pre)]
            else:
                cmd = [str(script), *_extra_script_args()]
            proc = host_exec.run_on_host(
                cmd,
                cwd=str(log_dir),
                env=env,
                timeout=max(1, _env_int("RACK_STATUS_TIMEOUT", 300)),
            )
        finally:
            if tmp_pre is not None:
                try:
                    tmp_pre.unlink(missing_ok=True)
                except OSError:
                    pass
        assert proc is not None
        parsed = parse_rack_output(proc.stdout)
        out: dict = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "log_dir": str(log_dir),
            "script": str(script),
            "stderr": proc.stderr,
            **parsed,
        }
        if include_raw:
            out["stdout"] = proc.stdout
        try:
            out["comments"] = read_comments()
        except Exception:
            _logger.debug("run_rack_status: ignoring error", exc_info=True)
            out["comments"] = {}
        out["physical_rf0"] = get_physical_rf0_map()
        out["native_workers"] = {
            "mode": "shell_table",
            "note": "Table from rack_status.sh; native async workers are not used.",
        }
        return out

    out = native_status.build_native_rack_status(
        log_dir,
        include_raw,
        _probe_log_json_via_rack_status,
        # Never persist the placeholder table produced before a cold worker's
        # log scan has recovered completed NCCL/NeMo evidence.
        wait_for_workers=not include_raw,
        worker_timeout_sec=max(1, _env_int("RACK_STATUS_TIMEOUT", 300)),
    )
    summ = out.get("summary")
    if not isinstance(summ, dict):
        out["summary"] = {"done": 0, "fail": 0, "ongoing": 0, "pending": 0}
    try:
        out["comments"] = read_comments()
    except Exception:
        _logger.debug("run_rack_status: ignoring error", exc_info=True)
        out["comments"] = {}
    out["physical_rf0"] = get_physical_rf0_map()
    return out


def _rack_status_background_enabled() -> bool:
    return not _truthy_env("RACK_STATUS_BACKGROUND_DISABLED")


def _rack_status_background_interval_sec() -> int:
    return max(5, _job_env_int("RACK_STATUS_BACKGROUND_INTERVAL_SEC", 30))


def _cluster_snapshot_db_path() -> Path:
    configured = (os.environ.get("RACK_CLUSTER_SNAPSHOT_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (REPO_ROOT / "var" / "cache" / "cluster_snapshots.sqlite3").resolve()


def _cluster_snapshot_repository() -> cluster_snapshot.SnapshotRepository:
    global _cluster_snapshot_repo_cache
    path = str(_cluster_snapshot_db_path())
    with _cluster_snapshot_repo_lock:
        if _cluster_snapshot_repo_cache is None or _cluster_snapshot_repo_cache[0] != path:
            _cluster_snapshot_repo_cache = (
                path,
                cluster_snapshot.SnapshotRepository(path),
            )
        return _cluster_snapshot_repo_cache[1]


def _cluster_snapshot_identity() -> str:
    """Stable, non-secret active cluster identity used in the shared snapshot key."""
    if _CATALOG_AVAILABLE and catalog_context is not None:
        try:
            active = catalog_context.get_active()
            project_id = str(active.get("project") or "").strip()
            cluster_id = str(active.get("cluster") or "").strip()
            if project_id or cluster_id:
                return f"catalog:{project_id}/{cluster_id}"
        except Exception:
            _logger.debug("_cluster_snapshot_identity: ignoring error", exc_info=True)
    configured = (
        os.environ.get("RACK_CLUSTER_ID")
        or os.environ.get("RACK_CLUSTER_NAME")
        or os.environ.get("CLUSTER_NAME")
        or os.environ.get("RACK_SSH_HOST")
        or ""
    ).strip()
    return f"cluster:{configured or socket.gethostname()}"


def _cluster_overview_live_db_path() -> Path:
    """SQLite path for progressive overview live probes (sibling of inventory snapshot DB)."""
    configured = (os.environ.get("RACK_CLUSTER_OVERVIEW_LIVE_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (_cluster_snapshot_db_path().parent / "cluster_overview_live.sqlite3").resolve()


def _live_collect_bcm_version() -> str:
    value, err = _cluster_probe_bcm_cmsh_version()
    err_s = (err or "").strip()
    val_s = (value or "").strip()
    if err_s or not val_s:
        raise RuntimeError(err_s or "empty bcm version")
    return val_s


def _live_collect_slurm_version() -> str:
    value, err = _cluster_probe_slurm_version()
    err_s = (err or "").strip()
    val_s = (value or "").strip()
    if err_s or not val_s:
        raise RuntimeError(err_s or "empty slurm version")
    return val_s


def _live_collect_queue() -> dict[str, object]:
    block = _cluster_squeue_overview_block()
    if not isinstance(block, dict):
        raise RuntimeError("invalid squeue overview block")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
    if not block.get("ok"):
        raise RuntimeError(str(block.get("error") or "squeue failed"))
    return block


def _get_cluster_overview_live_service() -> cluster_overview_live.ClusterOverviewLiveService:
    """Lazy singleton keyed by live DB path so Gunicorn workers share one SQLite file."""
    global _cluster_overview_live_service_cache
    path = str(_cluster_overview_live_db_path())
    with _cluster_overview_live_service_lock:
        if (
            _cluster_overview_live_service_cache is None
            or _cluster_overview_live_service_cache[0] != path
        ):
            _cluster_overview_live_service_cache = (
                path,
                cluster_overview_live.ClusterOverviewLiveService(
                    path,
                    collectors={
                        "bcm": _live_collect_bcm_version,
                        "slurm": _live_collect_slurm_version,
                        "queue": _live_collect_queue,
                    },
                    ttls={"bcm": 3600, "slurm": 3600, "queue": 15},
                ),
            )
        return _cluster_overview_live_service_cache[1]


def _collect_canonical_rack_status(log_dir: Path) -> dict:
    """Collect inventory before it becomes a persisted snapshot.

    ``run_rack_status`` / ``build_native_rack_status`` already produce rows and
    ``native_workers``. Do not call ``_merge_live_native_workers_into_status``
    here — that rebuilds the table and re-runs ``squeue``.
    """
    return run_rack_status(log_dir, include_raw=False)


def get_cluster_snapshot(
    log_dir: Path,
    *,
    force_refresh: bool = False,
    wait: bool = True,
) -> cluster_snapshot.ClusterSnapshot:
    """Return the canonical cross-process inventory snapshot for both APIs."""
    log_root = _rack_status_cache_key(log_dir)
    _register_rack_status_log_dir(log_dir)
    service = cluster_snapshot.ClusterSnapshotService(
        _cluster_snapshot_repository(),
        collector=lambda: _collect_canonical_rack_status(log_dir),
        lease_seconds=max(30, _env_int("RACK_STATUS_TIMEOUT", 300) + 30),
    )
    return service.get_cluster_snapshot(
        _cluster_snapshot_identity(),
        log_root,
        max_age_seconds=_rack_status_background_interval_sec(),
        force_refresh=force_refresh,
        wait=wait,
    )


def _rack_status_cache_clear_all() -> None:
    """Shared invalidation entry point for status-affecting configuration changes."""
    _cluster_snapshot_repository().bump_configuration_generation()
    with _rack_status_cache_lock:
        _rack_status_cache.clear()


def _rack_status_cache_key(log_dir: Path) -> str:
    # Do NOT local-resolve in SSH mode: the background loop uses this key as the actual log_dir,
    # and resolving a remote path on the app host corrupts it (/home/... -> /System/Volumes/Data/...),
    # so the SSH `find` would look for a nonexistent path and every rack stays "pending".
    return str(_rpath(log_dir))


def _register_rack_status_log_dir(log_dir: Path) -> str:
    key = _rack_status_cache_key(log_dir)
    with _rack_status_cache_lock:
        _rack_status_registry.add(key)
    return key


def _cached_rack_status_copy(log_dir: Path) -> dict | None:
    """Compatibility reader backed by SQLite, including background-disabled mode."""
    repository = _cluster_snapshot_repository()
    snapshot = repository.get(
        _cluster_snapshot_identity(),
        _rack_status_cache_key(log_dir),
        repository.configuration_generation(),
    )
    return dict(snapshot.payload) if snapshot is not None and snapshot.payload is not None else None


def _ai_live_inventory_block() -> str:
    """Compact live cluster-size facts for the AI prompt (cached snapshot only, no SSH).

    The catalog deployment envelope only carries per-rack platform *geometry* (e.g. 72 GPUs/rack);
    it cannot know how many racks are actually deployed. Here we derive the DEPLOYED size — compute
    racks, Slurm nodes, GPUs — from the same cached ``/api/status`` snapshot the dashboard uses, by
    summing each rack's ``sinfo`` cell (e.g. ``idle=17,drain=1`` -> 18). Returns "" when the snapshot
    is empty or sinfo hasn't populated yet, so we never feed the model misleading zeros.
    """
    try:
        base = _cached_rack_status_copy(_default_log_dir())
    except Exception:
        _logger.debug("_ai_live_inventory_block: ignoring error", exc_info=True)
        base = None
    if not isinstance(base, dict):
        return ""
    rows = [r for r in (base.get("rows") or []) if isinstance(r, dict)]
    if not rows:
        return ""

    def _nodes_from_sinfo(cell: str) -> int:
        cell = (cell or "").strip()
        if not cell or cell in ("?", "-"):
            return 0
        total = 0
        for part in cell.split(","):
            if "=" in part:
                try:
                    total += int(part.split("=", 1)[1])
                except ValueError:
                    _logger.debug("_ai_live_inventory_block: unparsable sinfo part %r", part)
        return total

    compute_racks = 0
    total_nodes = 0
    for r in rows:
        n = _nodes_from_sinfo(str(r.get("sinfo") or ""))
        if n > 0:
            compute_racks += 1
            total_nodes += n
    if total_nodes <= 0:
        return ""  # sinfo not populated yet — avoid a misleading "0 nodes"

    all_racks = len({str(r.get("rack") or "").strip() for r in rows if r.get("rack")})
    gpus_per_node = _eget_int("GPUS_PER_NODE", 0) or _eget_int("RACK_CLUSTER_GPU_PER_TRAY", 0)
    total_gpus = total_nodes * gpus_per_node if gpus_per_node else 0
    summary = base.get("summary") if isinstance(base.get("summary"), dict) else {}
    updated = base.get("updated_at")

    lines = [
        (
            "Live cluster inventory (from the current rack-status snapshot — authoritative for "
            "DEPLOYED size; sinfo-derived, not the naming-pattern capacity):"
        )
    ]
    rack_line = f"- Compute racks (with Slurm nodes): {compute_racks}"
    if all_racks and all_racks != compute_racks:
        rack_line += f" ({all_racks} racks shown in the table, incl. non-compute switch/mgmt racks)"
    lines.append(rack_line)
    lines.append(f"- Compute nodes (Slurm): {total_nodes}")
    if total_gpus:
        lines.append(f"- GPUs: {total_gpus} ({total_nodes} nodes x {gpus_per_node} GPUs/node)")
    if isinstance(summary, dict) and any(k in summary for k in ("done", "fail", "ongoing", "pending")):
        lines.append(
            f"- Validation progress (compute racks): done {int(summary.get('done', 0) or 0)}, "
            f"fail {int(summary.get('fail', 0) or 0)}, ongoing {int(summary.get('ongoing', 0) or 0)}, "
            f"pending {int(summary.get('pending', 0) or 0)}"
        )
    if updated:
        lines.append(f"- Snapshot updated: {updated}")
    lines.append(
        "Use these deployed counts for cluster-size questions; do NOT infer the size from the "
        "rack naming pattern (that is only naming capacity)."
    )
    return "\n".join(lines)


# The snapshot cache lives here, so app.py is what can build the block; ai_chat only needs to be
# able to call it. Registered once at import so every prompt builder gets it, not just /api/ai/chat.
ai_chat.set_live_inventory_provider(_ai_live_inventory_block)


def _ensure_rack_status_background_thread() -> None:
    """Start the periodic rack_status.sh refresh thread once per process (no-op if disabled)."""
    if not _rack_status_background_enabled():
        return
    global _rack_status_bg_thread
    with _rack_status_bg_thread_lock:
        if _rack_status_bg_thread is not None and _rack_status_bg_thread.is_alive():
            return
        try:
            seed = _rack_status_cache_key(_default_log_dir())
        except Exception:
            _logger.debug("_ensure_rack_status_background_thread: ignoring error", exc_info=True)
            seed = ""
        if seed:
            with _rack_status_cache_lock:
                _rack_status_registry.add(seed)
        _rack_status_bg_thread = threading.Thread(
            target=_rack_status_background_loop,
            name="rack-status-bg",
            daemon=True,
        )
        _rack_status_bg_thread.start()
    _start_cmsh_rack_list_warm_thread()


def _rack_status_background_loop() -> None:
    """Periodically refresh shared snapshots; SQLite leases coordinate workers."""
    while True:
        if not _rack_status_background_enabled():
            return
        interval = max(5, _rack_status_background_interval_sec())
        time.sleep(float(interval))
        if not _rack_status_background_enabled():
            return
        # Skip slow inventory collects during SSH/VPN outages (local mode is always reachable).
        if not host_exec.cluster_reachable():
            continue
        try:
            with _rack_status_cache_lock:
                dirs = sorted(_rack_status_registry)
            if not dirs:
                dirs = [_rack_status_cache_key(_default_log_dir())]
            for d in dirs:
                p = Path(d)
                try:
                    get_cluster_snapshot(p, force_refresh=True)
                except Exception:
                    _logger.exception("cluster snapshot background refresh failed")
        except Exception:
            _logger.exception("cluster snapshot background loop failed")


def _nemo_done_rack_ids_from_rack_status(log_dir: Path) -> frozenset[str] | None:
    """Racks whose **nemo** cell is ``done`` in the current status table (native or shell).

    Returns ``None`` if status could not be loaded (caller treats as: do not apply this filter).
    Returns an empty frozenset if the table loaded but no rack is ``done``.
    """
    try:
        snapshot = get_cluster_snapshot(log_dir)
        if snapshot.payload is None:
            return None
        data = snapshot.payload
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    except Exception:
        _logger.debug("_nemo_done_rack_ids_from_rack_status: ignoring error", exc_info=True)
        return None
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) == 0:
        return None
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rack_raw = (row.get("rack") or "").strip()
        nemo_raw = (row.get("nemo") or "").strip()
        if not rack_raw:
            continue
        first = nemo_raw.split()[0].lower() if nemo_raw else ""
        if first != "done":
            continue
        try:
            rk = _normalize_rack_id(rack_raw)
        except ValueError:
            continue
        out.add(rk)
    return frozenset(out)






def _prepare_power_reset(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Shared parse for sync and async power reset; returns ({\"specs\": [...]}, None) or (None, err)."""
    if os.environ.get("RACK_POWER_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "power-on disabled (RACK_POWER_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    ctv = body.get("ct_down_unk")
    specs: list[str]
    if specs_raw is not None:
        if not isinstance(specs_raw, list):
            return None, (jsonify({"ok": False, "error": "specs must be an array of strings"}), 400)
        if not specs_raw:
            return None, (jsonify({"ok": False, "error": "specs must be a non-empty array"}), 400)
        try:
            specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        if not specs:
            return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
    elif isinstance(ctv, str):
        specs = power_specs_from_ct_down_unk(ctv)
        if not specs:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": "no valid pNN-rMM-ct[digits/ranges] tokens in ct_down_unk",
                    }
                ),
                400,
            )
    else:
        return None, (
            jsonify(
                {
                    "ok": False,
                    "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]} or {"ct_down_unk": "…"}',
                }
            ),
            400,
        )
    return {"specs": specs}, None


@app.route("/api/power-on", methods=["POST"])
def api_power_on():
    """POST JSON {\"specs\": [\"pNN-rMM-ct[id]\", …]} or {\"ct_down_unk\": \"…\"} — cmsh power off/sleep/on per tray."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_power_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    specs = prep["specs"]
    assert isinstance(specs, list)
    sleep_sec = max(0, _env_int("RACK_POWER_RESET_SLEEP_SEC", 10))
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "power", _operator_from_request()):
            results = run_cmsh_power_reset(specs)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e), "results": []}), 500
    ok_all = all(r.get("returncode") == 0 for r in results)
    payload: dict = {
        "ok": ok_all,
        "results": results,
        "reset_sleep_sec": sleep_sec,
    }
    if not ok_all:
        payload["error"] = "one or more cmsh power reset steps failed (see results)"
    return jsonify(payload), (200 if ok_all else 502)


@app.route("/api/power-on-async", methods=["POST"])
def api_power_on_async():
    """Start power reset in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_power_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    specs = prep["specs"]
    assert isinstance(specs, list)
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "power",
            "operator": _operator_from_request(),
            "rack": rk,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_power_reset_worker,
        args=(job_id, specs),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "power", "rack": rk})


def _gpu_reset_script_path() -> Path:
    raw = (_eget("RACK_GPU_RESET_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return _rpath(SCRIPTS_DIR / "gpu_reset.sh")


def run_gpu_reset(pdsh_w: str) -> dict:
    """Run gpu_reset.sh once; argument is the pdsh -w host pattern (may contain commas)."""
    script = _gpu_reset_script_path()
    if not _script_exists(script):
        raise OSError(f"gpu_reset script not found: {script}")
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    timeout = max(30, _env_int("RACK_GPU_RESET_TIMEOUT", 3600))
    dry = os.environ.get("RACK_GPU_RESET_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["bash", str(script), w]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": "dry-run (RACK_GPU_RESET_DRY_RUN)",
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError("bash executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "pdsh_w": w,
    }


def _prepare_gpu_reset(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Shared parse for sync and async GPU reset."""
    if os.environ.get("RACK_GPU_RESET_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "GPU reset disabled (RACK_GPU_RESET_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
        return None, (jsonify({"ok": False, "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]}'}), 400)
    try:
        specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
    pdsh_w = ",".join(specs)
    return {"pdsh_w": pdsh_w}, None


@app.route("/api/gpu-reset", methods=["POST"])
def api_gpu_reset():
    """POST JSON {\"specs\": [\"pNN-rMM-ct[id]\", …]} — merged tray pattern(s) passed to gpu_reset.sh."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_gpu_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "gpu", _operator_from_request()):
            result = run_gpu_reset(pdsh_w)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = result.get("returncode") == 0
    payload: dict = {"ok": ok, **result}
    if not ok:
        payload["error"] = "gpu_reset.sh exited non-zero (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


@app.route("/api/gpu-reset-async", methods=["POST"])
def api_gpu_reset_async():
    """Start GPU reset in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_gpu_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "gpu",
            "operator": _operator_from_request(),
            "rack": rk,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_gpu_reset_worker,
        args=(job_id, pdsh_w),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "gpu", "rack": rk})


# ---------------------------------------------------------------------------
# BMC recovery and MAC change
#
# Both target a single head node where BCM (cmsh / pythoncm) + ipmitool live.
# Invocation always routes through host_exec so the same code path serves
# local-on-head and dashboard-in-container deployments. Each call writes its
# stdout/stderr/returncode into the shared _reset_jobs table; the frontend
# polls /api/reset-job-status?job_id=… (no new polling endpoint needed).
# ---------------------------------------------------------------------------

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_IFACE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SINGLE_NODE_SPEC_RE = re.compile(r"^([a-z0-9-]+-ct)\[(\d+)\]$", re.IGNORECASE)
_HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE)


def _bmc_recovery_script_path() -> Path:
    raw = (_eget("RACK_BMC_RECOVERY_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return _rpath(SCRIPTS_DIR / "bmc-recovery.sh")


def _macswap_script_path() -> Path:
    raw = (_eget("RACK_MACSWAP_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return _rpath(SCRIPTS_DIR / "macswap" / "macswap")


def _macswap_python_module() -> str:
    """Environment Modules name to load before invoking macswap (default ``python3``)."""
    raw = (_eget("RACK_MACSWAP_PYTHON_MODULE") or "").strip()
    return raw or "python3"


def _spec_to_hostname(spec: str) -> str | None:
    """Parse a single-node spec like ``pNN-rMM-ct[01]`` → ``pNN-rMM-ct01``.

    Returns ``None`` for range forms (e.g. ``pNN-rMM-ct[01-04]``) so the
    caller can fan-out only across explicit single-node selections.
    """
    m = _SINGLE_NODE_SPEC_RE.match(spec.strip())
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}".lower()


def run_bmc_recovery_one(node: str, iface: str) -> dict:
    """Run ``bmc-recovery.sh <node> <iface>`` on the head node and capture output."""
    script = _bmc_recovery_script_path()
    if not _script_exists(script):
        raise OSError(f"bmc-recovery script not found: {script}")
    if not _HOSTNAME_RE.match(node):
        raise ValueError(f"invalid node hostname: {node!r}")
    if not _IFACE_NAME_RE.match(iface):
        raise ValueError(f"invalid interface name: {iface!r}")
    timeout = max(30, _env_int("RACK_BMC_RECOVERY_TIMEOUT", 120))
    dry = os.environ.get("RACK_BMC_RECOVERY_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["bash", str(script), node, iface]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": f"dry-run (RACK_BMC_RECOVERY_DRY_RUN)\n{cmd_display}",
            "stderr": "",
            "command_display": cmd_display,
            "node": node,
            "iface": iface,
        }
    try:
        proc = host_exec.run_on_host(cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError("bash executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "node": node,
            "iface": iface,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "node": node,
        "iface": iface,
    }


def _prepare_bmc_recovery(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate the request body; return ({nodes, iface}, None) or (None, (response, code))."""
    if os.environ.get("RACK_BMC_RECOVERY_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "bmc recovery disabled (RACK_BMC_RECOVERY_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
        return None, (jsonify({"ok": False, "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]}'}), 400)
    nodes: list[str] = []
    for raw in specs_raw:
        host = _spec_to_hostname(str(raw))
        if host is None:
            return None, (
                jsonify({"ok": False, "error": f"spec {raw!r} is not a single-node form pNN-rMM-ct[NN]"}),
                400,
            )
        if host not in nodes:
            nodes.append(host)
    iface_raw = body.get("iface")
    iface = "rf0" if iface_raw is None else str(iface_raw).strip()
    if not _IFACE_NAME_RE.match(iface):
        return None, (jsonify({"ok": False, "error": f"invalid interface name: {iface!r}"}), 400)
    return {"nodes": nodes, "iface": iface}, None


def _bmc_recovery_worker(job_id: str, nodes: list[str], iface: str) -> None:
    """Run bmc-recovery.sh against each selected node serially; aggregate into _reset_jobs."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    results: list[dict] = []
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        for node in nodes:
            try:
                res = run_bmc_recovery_one(node, iface)
            except (OSError, ValueError) as e:
                res = {
                    "returncode": -1,
                    "stdout": "",
                    "stderr": str(e),
                    "command_display": "",
                    "node": node,
                    "iface": iface,
                }
            results.append(res)
            _patch(results=list(results))
        ok_all = all(r.get("returncode") == 0 for r in results)
        _patch(
            phase="done" if ok_all else "error",
            ok=ok_all,
            results=results,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=None if ok_all else "bmc-recovery.sh failed on one or more nodes (see results)",
        )
    except Exception as e:
        _logger.debug("_bmc_recovery_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            results=results,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/bmc-recovery-async", methods=["POST"])
def api_bmc_recovery_async():
    """Start BMC recovery in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_bmc_recovery(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    nodes = prep["nodes"]
    iface = prep["iface"]
    assert isinstance(nodes, list) and isinstance(iface, str)
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "bmc",
            "operator": _operator_from_request(),
            "rack": rk,
            "nodes": list(nodes),
            "iface": iface,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_bmc_recovery_worker,
        args=(job_id, list(nodes), iface),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "bmc", "rack": rk})


def run_mac_change(
    node: str,
    node_mac: str | None,
    ifspecs: list[tuple[str, str]],
    dry_run: bool,
) -> dict:
    """Invoke macswap in change mode through ``bash -lc 'module load python3 && macswap …'``."""
    script = _macswap_script_path()
    if not _script_exists(script):
        raise OSError(f"macswap script not found: {script}")
    if not _HOSTNAME_RE.match(node):
        raise ValueError(f"invalid node hostname: {node!r}")
    if node_mac is not None and not _MAC_RE.match(node_mac):
        raise ValueError(f"invalid node MAC: {node_mac!r}")
    for name, mac in ifspecs:
        if not _IFACE_NAME_RE.match(name):
            raise ValueError(f"invalid interface name: {name!r}")
        if not _MAC_RE.match(mac):
            raise ValueError(f"invalid MAC for interface {name!r}: {mac!r}")
    timeout = max(30, _env_int("RACK_MAC_CHANGE_TIMEOUT", 600))
    dry_env = os.environ.get("RACK_MAC_CHANGE_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    inner_argv: list[str] = ["python3", str(script), node]
    if node_mac:
        inner_argv += ["--node-mac", node_mac]
    for name, mac in ifspecs:
        inner_argv += ["--interface", f"{name}={mac}"]
    if dry_run or dry_env:
        inner_argv.append("--dry-run")
    module_name = _macswap_python_module()
    inner_cmd = f"module load {shlex.quote(module_name)} && {shlex.join(inner_argv)}"
    cmd = ["bash", "-lc", inner_cmd]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = host_exec.run_on_host(cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError("bash executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "node": node,
            "dry_run": bool(dry_run or dry_env),
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "node": node,
        "dry_run": bool(dry_run or dry_env),
    }


def _prepare_mac_change(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate the request body; return ({node, node_mac, ifspecs, dry_run}, None) or (None, err)."""
    if os.environ.get("RACK_MAC_CHANGE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "mac change disabled (RACK_MAC_CHANGE_DISABLED)"}), 403)
    node_raw = body.get("node")
    if not isinstance(node_raw, str) or not node_raw.strip():
        return None, (jsonify({"ok": False, "error": '"node" is required (single hostname)'}), 400)
    node = node_raw.strip().lower()
    if not _HOSTNAME_RE.match(node):
        return None, (jsonify({"ok": False, "error": f"invalid node hostname: {node!r}"}), 400)

    node_mac_raw = body.get("node_mac")
    node_mac: str | None
    if node_mac_raw is None or (isinstance(node_mac_raw, str) and not node_mac_raw.strip()):
        node_mac = None
    elif not isinstance(node_mac_raw, str):
        return None, (jsonify({"ok": False, "error": '"node_mac" must be a string'}), 400)
    elif not _MAC_RE.match(node_mac_raw.strip()):
        return None, (jsonify({"ok": False, "error": f"invalid node MAC: {node_mac_raw!r}"}), 400)
    else:
        node_mac = node_mac_raw.strip().lower()

    ifaces_raw = body.get("interfaces") or []
    if not isinstance(ifaces_raw, list):
        return None, (jsonify({"ok": False, "error": '"interfaces" must be an array of {name, mac} objects'}), 400)
    ifspecs: list[tuple[str, str]] = []
    for idx, row in enumerate(ifaces_raw):
        if not isinstance(row, dict):
            return None, (jsonify({"ok": False, "error": f"interfaces[{idx}] must be an object"}), 400)
        name_raw = row.get("name")
        mac_raw = row.get("mac")
        if (name_raw is None or not str(name_raw).strip()) and (mac_raw is None or not str(mac_raw).strip()):
            continue
        if not isinstance(name_raw, str) or not _IFACE_NAME_RE.match(name_raw.strip()):
            return None, (jsonify({"ok": False, "error": f'interfaces[{idx}].name is missing or invalid'}), 400)
        if not isinstance(mac_raw, str) or not _MAC_RE.match(mac_raw.strip()):
            return None, (jsonify({"ok": False, "error": f'interfaces[{idx}].mac is missing or invalid'}), 400)
        ifspecs.append((name_raw.strip(), mac_raw.strip().lower()))

    if node_mac is None and not ifspecs:
        return None, (
            jsonify({"ok": False, "error": "provide --node-mac or at least one interface"}),
            400,
        )

    dry_raw = body.get("dry_run")
    dry_run = bool(dry_raw) if dry_raw is not None else False

    return {"node": node, "node_mac": node_mac, "ifspecs": ifspecs, "dry_run": dry_run}, None


def _mac_change_worker(
    job_id: str,
    node: str,
    node_mac: str | None,
    ifspecs: list[tuple[str, str]],
    dry_run: bool,
) -> None:
    """Run macswap once (single node) and aggregate into _reset_jobs."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            result = run_mac_change(node, node_mac, ifspecs, dry_run)
        except (OSError, ValueError) as e:
            result = {
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
                "command_display": "",
                "node": node,
                "dry_run": dry_run,
            }
        ok = result.get("returncode") == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            results=[result],
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=None if ok else "macswap exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_mac_change_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            results=[],
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/mac-change-async", methods=["POST"])
def api_mac_change_async():
    """Start a MAC change in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_mac_change(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    node = str(prep["node"])
    node_mac = prep["node_mac"]
    assert node_mac is None or isinstance(node_mac, str)
    ifspecs = prep["ifspecs"]
    assert isinstance(ifspecs, list)
    dry_run = bool(prep["dry_run"])
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "mac",
            "operator": _operator_from_request(),
            "rack": rk,
            "node": node,
            "node_mac": node_mac,
            "interfaces": [{"name": n, "mac": m} for n, m in ifspecs],
            "dry_run": dry_run,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_mac_change_worker,
        args=(job_id, node, node_mac, list(ifspecs), dry_run),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "mac", "rack": rk})


_DEFAULT_BUG_REPORT_REMOTE_DIR = "/home/cmsupport/reports"


def _bug_report_remote_dir() -> str:
    """Base dir on each node (pdsh) and on this host (inventory). Override with ``RACK_BUG_REPORT_REMOTE_DIR``."""
    d = (os.environ.get("RACK_BUG_REPORT_REMOTE_DIR") or "").strip()
    if not d:
        d = _DEFAULT_BUG_REPORT_REMOTE_DIR
    out = os.path.expanduser(d).strip()
    return out or _DEFAULT_BUG_REPORT_REMOTE_DIR


def run_bug_report_pdsh(pdsh_w: str) -> dict:
    """Run nvidia-bug-report.sh on each host via pdsh (same -w pattern as GPU reset)."""
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    remote = _bug_report_remote_dir()
    rq = shlex.quote(remote)
    inner = f"mkdir -p {rq}/$HOSTNAME && cd {rq}/$HOSTNAME && nvidia-bug-report.sh"
    timeout = max(120, _env_int("RACK_BUG_REPORT_TIMEOUT", 10800))
    dry = os.environ.get("RACK_BUG_REPORT_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    plat = _get_platform()
    fan = max(1, _env_int("RACK_BUG_REPORT_PDSH_FANOUT", 32))
    cmd = plat.pdsh_cmd(w, inner, fanout=fan)
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": "dry-run (RACK_BUG_REPORT_DRY_RUN)\n" + cmd_display,
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError("pdsh executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "pdsh_w": w,
    }


_BUG_REPORT_PDSH_PATH_RE = re.compile(
    r"^([a-z0-9][a-z0-9.-]*):\s*(/\S+\.log\.gz)\s*$",
    re.IGNORECASE,
)

_BUG_REPORT_BASENAME_RE = re.compile(r"^nvidia-bug-report.*\.log\.gz$", re.IGNORECASE)
# NVLMAPPER output zips under reports/<rack>/ (staged + ./run_nvlmapper.sh from app.py).
_NVLMAPPER_ZIP_BASENAME_RE = re.compile(r"^nvlmapper\.[A-Za-z0-9][A-Za-z0-9._-]*\.zip$", re.IGNORECASE)
_NVLMAPPER_TABLE_BASENAME_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._\-]*\.(csv|xls)$", re.IGNORECASE
)
# Parser Excel copies (e.g. rack-parser-export-*.xlsx) and other workbooks under reports/.
_REPORT_EXCEL_BASENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]*\.xlsx$", re.IGNORECASE)
# Reserved inventory ``host`` for ``*.xlsx`` files sitting directly under the reports base dir (not in a subfolder).
_BUG_REPORT_EXCEL_ROOT_HOST = "excel-root"


def _parse_bug_report_paths_from_pdsh(stdout: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in (stdout or "").splitlines():
        m = _BUG_REPORT_PDSH_PATH_RE.match(line.strip())
        if not m:
            continue
        host = m.group(1).strip()
        path = m.group(2).strip()
        key = (host.casefold(), path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"host": host, "path": path})
    return out


def run_bug_report_list_pdsh(pdsh_w: str) -> dict:
    """List newest nvidia-bug-report *.log.gz per host under ``<remote>/$HOSTNAME/``."""
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    remote = _bug_report_remote_dir()
    rq = shlex.quote(remote)
    inner = f"ls -t {rq}/$HOSTNAME/nvidia-bug-report*.log.gz 2>/dev/null | head -1"
    timeout = max(30, _env_int("RACK_BUG_REPORT_LIST_TIMEOUT", 600))
    plat = _get_platform()
    fan = max(1, _env_int("RACK_BUG_REPORT_PDSH_FANOUT", 32))
    cmd = plat.pdsh_cmd(w, inner, fanout=fan)
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    dry = os.environ.get("RACK_BUG_REPORT_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    if dry:
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError("pdsh executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "pdsh_w": w,
    }


_BUG_REPORT_SUBDIR_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _bug_report_local_base_path() -> Path:
    return _rpath(Path(_bug_report_remote_dir()))


def _ensure_bug_report_local_base_dir() -> tuple[Path, str | None]:
    """Create ``RACK_BUG_REPORT_REMOTE_DIR`` on the execution host when missing."""
    base = _bug_report_local_base_path()
    if host_exec.is_dir(base):
        return base, None
    err = host_exec.makedirs(base)
    if err:
        return base, f"could not create reports directory {base}: {err}"
    return base, f"created reports directory: {base}"


def _local_bug_report_inventory() -> tuple[list[dict[str, str]], str | None]:
    """Scan ``<RACK_BUG_REPORT_REMOTE_DIR>/<subdir>/`` for bug-report ``*.log.gz``, NVLMapper ``*.csv`` / ``*.xls``, ``nvlmapper.*.zip``, and ``*.xlsx``."""
    base, ensure_notice = _ensure_bug_report_local_base_dir()
    if ensure_notice and ensure_notice.startswith("not a directory"):
        return [], ensure_notice
    if ensure_notice and ensure_notice.startswith("could not create"):
        return [], ensure_notice
    # One listing of the whole reports tree on the execution host. This replaced
    # five globs per hostname subdirectory, each of which was a round trip and a
    # local-filesystem call against a path that lives on the remote host.
    base_p = PurePosixPath(str(base))
    entries = host_exec.stat_dir(
        base_p,
        patterns=("nvidia-bug-report*.log.gz", "nvlmapper.*.zip", "*.csv", "*.xls", "*.xlsx"),
        maxdepth=2,
    )

    def _wanted(name: str) -> bool:
        if name.startswith("nvidia-bug-report") and name.endswith(".log.gz"):
            return True
        if name.endswith(".zip"):
            return bool(_NVLMAPPER_ZIP_BASENAME_RE.fullmatch(name))
        if name.endswith((".csv", ".xls")):
            return bool(_NVLMAPPER_TABLE_BASENAME_RE.fullmatch(name))
        if name.endswith(".xlsx"):
            return bool(_REPORT_EXCEL_BASENAME_RE.fullmatch(name))
        return False

    items: list[dict[str, str]] = []
    for path_s in entries:
        f = PurePosixPath(path_s)
        if not _wanted(f.name):
            continue
        if f.parent == base_p:
            # Workbooks placed directly under the reports base, not under a hostname subdir.
            if not f.name.endswith(".xlsx"):
                continue
            items.append({"host": _BUG_REPORT_EXCEL_ROOT_HOST, "name": f.name})
            continue
        # Containment: exactly one level down, under a well-formed hostname dir.
        if f.parent.parent != base_p or not _BUG_REPORT_SUBDIR_RE.fullmatch(f.parent.name):
            continue
        items.append({"host": f.parent.name, "name": f.name})
    items.sort(key=lambda x: (x["host"].lower(), x["name"].lower()))
    notice = ensure_notice if ensure_notice and ensure_notice.startswith("created ") else None
    return items, notice


def _bug_report_ssh_user() -> str:
    u = (os.environ.get("RACK_BUG_REPORT_SSH_USER") or "root").strip()
    return u or "root"


def _bug_report_ssh_argv_prefix() -> list[str]:
    raw = (os.environ.get("RACK_BUG_REPORT_SSH_OPTS") or "").strip()
    if raw:
        return ["ssh", *shlex.split(raw)]
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"]


def _ssh_cat_remote_bytes(host: str, remote_path: str) -> bytes:
    """Read a remote file over ssh (same user/options as bug-report bundle)."""
    if ".." in remote_path or not remote_path.startswith("/"):
        raise ValueError("invalid remote path")
    base = _bug_report_remote_dir().rstrip("/")
    if not (remote_path == base or remote_path.startswith(base + "/")):
        raise ValueError("remote path outside reports directory")
    user = _bug_report_ssh_user()
    inner = f"cat {shlex.quote(remote_path)}"
    argv = [*_bug_report_ssh_argv_prefix(), f"{user}@{host}", inner]
    timeout = max(30, _env_int("RACK_BUG_REPORT_SSH_CAT_TIMEOUT", 3600))
    proc = subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"")[:2000].decode("utf-8", errors="replace")
        raise OSError(f"ssh cat failed on {host}: {err or proc.returncode}")
    return proc.stdout or b""


def _prepare_bug_report_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    if os.environ.get("RACK_BUG_REPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "Bug report disabled (RACK_BUG_REPORT_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
        return None, (jsonify({"ok": False, "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]}'}), 400)
    try:
        specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
    pdsh_w = ",".join(specs)
    return {"pdsh_w": pdsh_w}, None


def _bug_report_worker(job_id: str, pdsh_w: str) -> None:
    """Run pdsh nvidia-bug-report.sh, then discover .log.gz paths per host."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        result = run_bug_report_pdsh(pdsh_w)
        ok = result.get("returncode") == 0
        files: list[dict[str, str]] = []
        list_out = ""
        list_stderr = ""
        if ok:
            lst = run_bug_report_list_pdsh(pdsh_w)
            list_out = (lst.get("stdout") or "").strip()
            list_stderr = (lst.get("stderr") or "").strip()
            files = _parse_bug_report_paths_from_pdsh(list_out)
        err_msg = None if ok else (
            result.get("stderr")
            or "bug report (pdsh) exited non-zero (see stdout/stderr)"
        )
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=result.get("returncode"),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            command_display=result.get("command_display", ""),
            pdsh_w=result.get("pdsh_w", ""),
            bug_report_list_stdout=list_out,
            bug_report_list_stderr=list_stderr,
            bug_report_files=files,
            error=err_msg,
        )
    except Exception as e:
        _logger.debug("_bug_report_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display="",
            pdsh_w=pdsh_w,
            bug_report_files=[],
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/bug-report-async", methods=["POST"])
def api_bug_report_async():
    """POST JSON like GPU reset: ``{"specs": ["pNN-rMM-ct[dd]", …], "rack": "pNN-rMM"}`` — pdsh nvidia-bug-report.sh."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_bug_report_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "bug_report",
            "operator": _operator_from_request(),
            "rack": rk,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_bug_report_worker,
        args=(job_id, pdsh_w),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "bug_report", "rack": rk})


@app.route("/api/bug-report-bundle", methods=["GET"])
def api_bug_report_bundle():
    """GET ``?job_id=…`` — zip of ``bug_report_files`` from a completed bug_report job (ssh cat per host)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _reset_jobs_lock:
        row = _reset_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        if str(row.get("kind") or "") != "bug_report":
            return jsonify({"ok": False, "error": "not a bug_report job"}), 400
        if str(row.get("phase") or "") != "done" or not row.get("ok"):
            return jsonify({"ok": False, "error": "bug report job is not finished successfully"}), 400
        files = row.get("bug_report_files")
        rack = str(row.get("rack") or "rack")
        if not isinstance(files, list) or not files:
            return jsonify({"ok": False, "error": "no bug report files recorded for this job"}), 400
        snap = list(files)
        rk_safe = re.sub(r"[^\w.-]+", "_", rack)[:80]

    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for ent in snap:
                if not isinstance(ent, dict):
                    continue
                host = str(ent.get("host") or "").strip()
                path = str(ent.get("path") or "").strip()
                if not host or not path:
                    continue
                try:
                    raw = _ssh_cat_remote_bytes(host, path)
                except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                    return jsonify({"ok": False, "error": f"failed to read {host}:{path}: {e}"}), 502
                arc = f"{host}_{os.path.basename(path)}"
                zf.writestr(arc, raw)
    except zipfile.BadZipFile:
        return jsonify({"ok": False, "error": "failed to build zip"}), 500

    buf.seek(0)
    fname = f"nvidia-bug-reports-{rk_safe}-{job_id[:8]}.zip"
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/api/bug-report-inventory", methods=["GET"])
def api_bug_report_inventory():
    """List bug-report ``*.log.gz``, NVLMapper ``*.csv`` / ``*.xls``, ``nvlmapper.*.zip``, and ``*.xlsx`` under ``RACK_BUG_REPORT_REMOTE_DIR`` (no pdsh)."""
    if os.environ.get("RACK_BUG_REPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "Bug report disabled (RACK_BUG_REPORT_DISABLED)"}), 403
    base = _bug_report_local_base_path()
    items, scan_err = _local_bug_report_inventory()
    return jsonify(
        {
            "ok": True,
            "base_dir": str(base),
            "items": items,
            "notice": scan_err or "",
        }
    )


@app.route("/api/bug-report-file", methods=["GET"])
def api_bug_report_file():
    """Download one local file from reports dir (subdir layout or ``excel-root`` for base-level ``*.xlsx``)."""
    if os.environ.get("RACK_BUG_REPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "Bug report disabled (RACK_BUG_REPORT_DISABLED)"}), 403
    host = (request.args.get("host") or "").strip()
    name = (request.args.get("name") or "").strip()
    if not host or not name:
        return jsonify({"ok": False, "error": "host and name are required"}), 400
    if not _BUG_REPORT_SUBDIR_RE.fullmatch(host):
        return jsonify({"ok": False, "error": "invalid host (subdir name)"}), 400
    if not (
        _BUG_REPORT_BASENAME_RE.fullmatch(name)
        or _NVLMAPPER_ZIP_BASENAME_RE.fullmatch(name)
        or _NVLMAPPER_TABLE_BASENAME_RE.fullmatch(name)
        or _REPORT_EXCEL_BASENAME_RE.fullmatch(name)
    ):
        return jsonify({"ok": False, "error": "invalid file name"}), 400
    base = _bug_report_local_base_path()
    # Reports live on the execution host, so containment is checked on the
    # normalised path text (which still collapses "..") rather than by resolving
    # against this container's filesystem, and existence is asked of that host.
    if host == _BUG_REPORT_EXCEL_ROOT_HOST:
        path = _rpath(base / name)
    else:
        path = _rpath(base / host / name)
    try:
        path.relative_to(_rpath(base))
    except ValueError:
        return jsonify({"ok": False, "error": "path outside reports directory"}), 400
    if not host_exec.is_file(path):
        return jsonify({"ok": False, "error": "file not found"}), 404
    is_zip = bool(_NVLMAPPER_ZIP_BASENAME_RE.fullmatch(name))
    is_xlsx = bool(_REPORT_EXCEL_BASENAME_RE.fullmatch(name))
    is_csv = name.lower().endswith(".csv") and bool(_NVLMAPPER_TABLE_BASENAME_RE.fullmatch(name))
    is_xls = name.lower().endswith(".xls") and bool(_NVLMAPPER_TABLE_BASENAME_RE.fullmatch(name))
    mime = (
        "application/zip"
        if is_zip
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if is_xlsx
        else "text/csv; charset=utf-8"
        if is_csv
        else "application/vnd.ms-excel"
        if is_xls
        else "application/gzip"
    )
    dl_host = host if host != _BUG_REPORT_EXCEL_ROOT_HOST else "reports"
    return send_file(
        path,
        mimetype=mime,
        as_attachment=True,
        download_name=f"{dl_host}_{name}",
    )


def _prepare_services_restart(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Same JSON as GPU reset: merged tray specs → one pdsh -w pattern."""
    if os.environ.get("RACK_SERVICES_RESTART_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "services restart disabled (RACK_SERVICES_RESTART_DISABLED)"}),
            403,
        )
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
        return None, (jsonify({"ok": False, "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]}'}), 400)
    try:
        specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
    pdsh_w = ",".join(specs)
    return {"pdsh_w": pdsh_w}, None


@app.route("/api/services-restart", methods=["POST"])
def api_services_restart():
    """POST JSON {\"specs\": [\"pNN-rMM-ct[id]\", …]} — pdsh mount + systemd restarts on selected hosts."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_services_restart(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "services", _operator_from_request()):
            result = run_services_restart(pdsh_w)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = result.get("returncode") == 0
    payload: dict = {"ok": ok, **result}
    if not ok:
        payload["error"] = "services restart (pdsh) exited non-zero (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


@app.route("/api/services-restart-async", methods=["POST"])
def api_services_restart_async():
    """Start services restart in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_services_restart(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "services",
            "operator": _operator_from_request(),
            "rack": rk,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_services_restart_worker,
        args=(job_id, pdsh_w),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "services", "rack": rk})


_HW_CHECK_PRESETS: dict[str, list[str]] = {
    "dry_run": ["-d"],
    "default": [],
    "no_dmesg": ["-e", "0"],
    "with_imex": ["-i", "1"],
    "with_spx": ["-x", "1"],
    "with_imex_spx": ["-i", "1", "-x", "1"],
    "with_process": ["-p", "1"],
    "sys_only": ["-e", "0", "-s", "1", "-g", "0", "-i", "0", "-b", "0", "-x", "0", "-p", "0"],
    # --- NscaleTX validation presets (MOP GB300 §7.1.1 / FastTrack §7) ---
    # dcgmi diag standardised on -r 3 (2026-07-26). Long-running: minutes per node.
    "dcgm_diag": ["-e", "0", "-s", "0", "-g", "0", "-i", "0", "-b", "0", "-x", "0",
                  "-p", "0", "-D", "1", "-L", "3"],
    "nvbandwidth": ["-e", "0", "-s", "0", "-g", "0", "-i", "0", "-b", "0", "-x", "0",
                    "-p", "0", "-N", "1"],
    # BOM / inventory: nvidia-smi -L, ECC, nvlink -s, fabric state, topo -p2p n
    "bom_inventory": ["-e", "0", "-s", "0", "-g", "1", "-i", "0", "-b", "0", "-x", "0", "-p", "0"],
    # Job-launch stack: slurmd, munge, Prolog/Epilog (incl. a dry execution), pyxis SPANK
    # plugin, enroot runtime dirs. Added 2026-07-28 after job 63850 was CANCELLED with
    # Reason=Prolog while every GPU/NVLink/CUDA check on the tray passed — nothing here
    # covered the launch path, so a tray that could not start a job still reported healthy.
    "slurm_stack": ["-e", "0", "-s", "0", "-g", "0", "-i", "0", "-b", "0", "-x", "0",
                    "-p", "0", "-S", "1"],
    # Full MOP pre-flight: BOM + dcgmi diag -r 3 + nvbandwidth + launch stack
    "mop_preflight": ["-e", "1", "-s", "1", "-g", "1", "-i", "0", "-b", "1", "-x", "0",
                      "-p", "0", "-D", "1", "-L", "3", "-N", "1", "-S", "1"],
}

_HW_CHECK_ARG_TOKEN_RE = re.compile(r"^[-a-zA-Z0-9_=.,]+$")
_HW_CHECK_ARGS_MAX_LEN = 512
_HW_CHECK_ARGS_MAX_TOKENS = 48


def _hw_check_validate_argv_tokens(tokens: list[str]) -> list[str]:
    if len(tokens) > _HW_CHECK_ARGS_MAX_TOKENS:
        raise ValueError(f"too many checker_args tokens (max {_HW_CHECK_ARGS_MAX_TOKENS})")
    for p in tokens:
        if not p:
            raise ValueError("empty checker_args token")
        if not _HW_CHECK_ARG_TOKEN_RE.fullmatch(p):
            raise ValueError(
                f"disallowed character in checker_args token {p!r}; "
                "allowed: letters, digits, hyphen, underscore, period, comma, equals"
            )
    return list(tokens)


def _hw_check_argv_from_custom_string(raw: str) -> list[str]:
    """Split user-supplied checker flags with shlex (POSIX); reject shell metacharacters per token."""
    s = raw.strip()
    if not s:
        raise ValueError("empty checker_args")
    if len(s) > _HW_CHECK_ARGS_MAX_LEN:
        raise ValueError(f"checker_args too long (max {_HW_CHECK_ARGS_MAX_LEN} characters)")
    try:
        parts = shlex.split(s, comments=False, posix=True)
    except ValueError as e:
        raise ValueError(f"invalid checker_args quoting: {e}") from e
    return _hw_check_validate_argv_tokens(parts)


def _hw_check_pdsh_script_path() -> Path:
    raw = (_eget("RACK_HW_CHECK_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return _rpath(SCRIPTS_DIR / "hw_check_pdsh.sh")


def _hw_check_argv_for_preset(preset: str) -> list[str]:
    key = (preset or "").strip().lower() or "default"
    if key not in _HW_CHECK_PRESETS:
        allowed = ", ".join(sorted(_HW_CHECK_PRESETS))
        raise ValueError(f"unknown preset {preset!r}; use one of: {allowed}")
    return list(_HW_CHECK_PRESETS[key])


def run_hw_check(pdsh_w: str, checker_argv: list[str]) -> dict:
    """Run hw_check_pdsh.sh once; checker_argv is passed after -- to checker.sh on each node."""
    script = _hw_check_pdsh_script_path()
    if not _script_exists(script):
        raise OSError(f"hw_check_pdsh.sh not found: {script}")
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    timeout = max(60, _env_int("RACK_HW_CHECK_TIMEOUT", 3600))
    dry = os.environ.get("RACK_HW_CHECK_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["bash", str(script), w, "--", *checker_argv]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": "dry-run (RACK_HW_CHECK_DRY_RUN)",
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    try:
        # Route through the privileged script profile (root@slogin) like sysinfo:
        # hw_check_pdsh.sh pdsh's to compute nodes and needs root on each node, and
        # the script lives on the cluster filesystem (not the dashboard host).
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
            env=_subprocess_env(),
            profile="script",
        )
    except FileNotFoundError:
        raise OSError("bash executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "pdsh_w": w,
    }


def _prepare_hw_check(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    if os.environ.get("RACK_HW_CHECK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "HW check disabled (RACK_HW_CHECK_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    pdsh_w = ""
    if isinstance(specs_raw, list) and specs_raw:
        try:
            specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        if not specs:
            return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
        pdsh_w = ",".join(specs)
    else:
        # Fallback: derive the whole-rack node list from live inventory (real Slurm
        # NodeNames) when the client sent no tray specs. Keeps hw-check working on
        # sites where the dashboard tray parse is naming-template dependent (e.g.
        # Boston's bos01-aRR-NN-s1-dgx-GG-cNN hostnames).
        rack = (body.get("rack") or "").strip()
        hosts = _rack_node_hostnames_from_inventory(rack) if rack else []
        if hosts:
            pdsh_w = ",".join(hosts)
        else:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            'send JSON {"specs": [...], "preset": "…"} and/or optional "checker_args": "-d …" or ["-d","…"]'
                        ),
                    }
                ),
                400,
            )

    checker_args_display: str | None = None
    argv: list[str]
    preset: str

    custom = body.get("checker_args")
    if isinstance(custom, list) and custom:
        try:
            parts = [str(x).strip() for x in custom]
        except Exception:
            _logger.debug("_prepare_hw_check: ignoring error", exc_info=True)
            return None, (jsonify({"ok": False, "error": "checker_args list must contain strings"}), 400)
        parts = [p for p in parts if p]
        if not parts:
            preset_raw = body.get("preset")
            preset = str(preset_raw).strip().lower() if preset_raw is not None else "default"
            try:
                argv = _hw_check_argv_for_preset(preset)
            except ValueError as e:
                return None, (jsonify({"ok": False, "error": str(e)}), 400)
        else:
            try:
                argv = _hw_check_validate_argv_tokens(parts)
            except ValueError as e:
                return None, (jsonify({"ok": False, "error": str(e)}), 400)
            preset = "custom"
            checker_args_display = " ".join(shlex.quote(p) for p in argv)[:400]
    elif isinstance(custom, str) and custom.strip():
        try:
            argv = _hw_check_argv_from_custom_string(custom)
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        preset = "custom"
        checker_args_display = custom.strip()[:400]
    else:
        preset_raw = body.get("preset")
        preset = str(preset_raw).strip().lower() if preset_raw is not None else "default"
        try:
            argv = _hw_check_argv_for_preset(preset)
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)

    return {
        "pdsh_w": pdsh_w,
        "preset": preset,
        "checker_argv": argv,
        "checker_args_display": checker_args_display,
    }, None


def _hw_check_worker(job_id: str, pdsh_w: str, preset: str, checker_argv: list[str]) -> None:
    """Run hw_check_pdsh.sh in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        result = run_hw_check(pdsh_w, checker_argv)
        ok = result.get("returncode") == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=result.get("returncode"),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            command_display=result.get("command_display", ""),
            pdsh_w=result.get("pdsh_w", ""),
            preset=preset,
            error=None if ok else "hw_check_pdsh.sh exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_hw_check_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display="",
            pdsh_w=pdsh_w,
            preset=preset,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/hw-check-async", methods=["POST"])
def api_hw_check_async():
    """POST JSON {\"specs\": [...], \"preset\": \"…\", \"rack\": \"…\", \"checker_args\": \"…\"} — optional checker_args string (or JSON list) overrides preset."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_hw_check(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    preset = str(prep["preset"])
    argv = prep["checker_argv"]
    assert isinstance(argv, list)
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "hw_check",
            "operator": _operator_from_request(),
            "rack": rk,
            "preset": preset,
            "checker_args": prep.get("checker_args_display"),
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_hw_check_worker,
        args=(job_id, pdsh_w, preset, list(argv)),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "hw_check", "rack": rk})


def _checker_script_path() -> Path:
    """Bundled checker.sh (same directory as hw_check_pdsh.sh)."""
    return _rpath(SCRIPTS_DIR / "checker.sh")


@app.route("/api/hw-check-presets", methods=["GET"])
def api_hw_check_presets():
    """Named checker argument bundles for ``/api/hw-check-async`` (``preset`` vs custom ``checker_args``)."""
    if os.environ.get("RACK_HW_CHECK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "HW check disabled (RACK_HW_CHECK_DISABLED)"}), 403
    presets = {k: list(v) for k, v in sorted(_HW_CHECK_PRESETS.items())}
    return jsonify({"ok": True, "presets": presets})


@app.route("/api/hw-check-help", methods=["GET"])
def api_hw_check_help():
    """checker.sh --help on the dashboard host (RACK_WEB_PRINT_HELP=1 skips root check)."""
    if os.environ.get("RACK_HW_CHECK_HELP_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_HW_CHECK_HELP_DISABLED)", "help": ""}), 403
    script = _checker_script_path()
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"checker.sh not found: {script}", "help": ""}), 404
    env = _subprocess_env({"RACK_WEB_PRINT_HELP": "1"})
    timeout = max(5, _env_int("RACK_HW_CHECK_HELP_TIMEOUT", 20))
    try:
        # The script and its working directory belong to the scripts role, which may
        # be a different host from the app. Running this locally only worked while
        # the two happened to coincide.
        #
        # RACK_WEB_PRINT_HELP goes through env_extra, not env: env is a local-only
        # subprocess argument, so over ssh the flag that skips checker.sh's root
        # check would be dropped and --help would fail with "Please run as root".
        proc = host_exec.run_on_host(
            ["bash", str(script), "--help"],
            cwd=_scripts_dir(),
            env=env,
            env_extra={"RACK_WEB_PRINT_HELP": "1"},
            timeout=timeout,
            role="scripts",
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "checker.sh --help timed out", "help": ""}), 504
    except OSError as e:
        return jsonify({"ok": False, "error": str(e), "help": ""}), 500
    help_out = (proc.stdout or "").strip()
    err_out = (proc.stderr or "").strip()
    help_text = (help_out + ("\n" + err_out if err_out else "")).strip()
    ok = proc.returncode == 0
    return jsonify({"ok": ok, "help": help_text, "returncode": proc.returncode}), (200 if ok else 502)


def _prepare_nvswitch_fix_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NMXC cycle (``nvswitches_rack_audit.sh --fix RACK``)."""
    if os.environ.get("RACK_NVSWITCH_AUDIT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "nvswitch audit disabled (RACK_NVSWITCH_AUDIT_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvswitches_rack_audit_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"nvswitch audit script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), "--no-color", "--fix", rack]
    display_cmd = "./" + script.name + " --fix " + shlex.quote(rack)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _nvswitch_fix_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``nvswitches_rack_audit.sh --fix`` in a background thread (cwd = log_dir)."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # A cluster script with the log dir as cwd: both belong to the execution
        # host, so this goes through the connection on the privileged route (see
        # docs/EXECUTION_ARCHITECTURE.md 1.2) rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            timeout=timeout_sec,
            role="script",
        )
        ok = proc.returncode == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            command_display=display_cmd,
            error=None if ok else "nvswitches_rack_audit.sh --fix exited non-zero (see stdout/stderr)",
        )
    except subprocess.TimeoutExpired:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            command_display=display_cmd,
            error=f"nvswitch fix timed out after {timeout_sec}s",
        )
    except Exception as e:
        _logger.debug("_nvswitch_fix_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/nvswitch-fix-async", methods=["POST"])
def api_nvswitch_fix_async():
    """POST JSON ``{\"rack\": \"pNN-rMM\", \"log_dir\": \"…\"}`` — rack-level ``--fix`` (NMXC cycle); poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_fix_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    # PurePosixPath is NOT a subclass of Path, and ``_resolve_log_dir()`` returns one whenever the
    # log dir lives on a remote host. Asserting Path alone raised AssertionError -> HTTP 500 on
    # every call to this route and the six sibling async routes below. Accept either flavour.
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nmxc_cycle",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_fix_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nmxc_cycle", "rack": rack})


def _prepare_nvswitch_sdn_reset_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVLink SDN reset (``nvswitch_sdn_reset.sh RACK``)."""
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvswitch_sdn_reset_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NVLink SDN reset script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), rack]
    display_cmd = "./" + script.name + " " + shlex.quote(rack)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _nvswitch_sdn_reset_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``nvswitch_sdn_reset.sh RACK`` in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_SDN_RESET_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # profile="script" routes to the privileged node-operational SSH target
        # (RACK_SCRIPT_SSH_*, root+nmxc) when configured, else falls back to control.
        hp = host_exec.popen_on_host(
            cmd, cwd=_host_job_cwd(log_dir), env=_subprocess_env(), profile="script"
        )
        try:
            out, err = hp.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="error",
                ok=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout_sec}s",
                command_display=display_cmd,
                error=f"NVLink SDN reset timed out after {timeout_sec}s",
            )
            _reset_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            command_display=display_cmd,
            error=None if ok else "nvswitch_sdn_reset.sh exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_nvswitch_sdn_reset_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/nvswitch-sdn-reset-async", methods=["POST"])
def api_nvswitch_sdn_reset_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async ``./nvswitch_sdn_reset.sh RACK``; poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_sdn_reset_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvswitch_sdn_reset",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_sdn_reset_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvswitch_sdn_reset", "rack": rack})


def _prepare_nvswitch_system_reset_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVLink system reset (``nvswitch_system_reset.sh RACK NVSWITCH_HOST``)."""
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        nvswitch_host = _normalize_nvswitch_host_for_rack(rack, body.get("nvswitch_host"))
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvswitch_system_reset_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NVLink system reset script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), rack, nvswitch_host]
    display_cmd = "./" + script.name + " " + shlex.quote(rack) + " " + shlex.quote(nvswitch_host)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "nvswitch_host": nvswitch_host,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _nvswitch_system_reset_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``nvswitch_system_reset.sh RACK NVSWITCH_HOST`` in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_SYSTEM_RESET_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # profile="script" routes to the privileged node-operational SSH target
        # (RACK_SCRIPT_SSH_*, root+nmxc) when configured, else falls back to control.
        hp = host_exec.popen_on_host(
            cmd, cwd=_host_job_cwd(log_dir), env=_subprocess_env(), profile="script"
        )
        try:
            out, err = hp.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="error",
                ok=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout_sec}s",
                command_display=display_cmd,
                error=f"NVLink system reset timed out after {timeout_sec}s",
            )
            _reset_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            command_display=display_cmd,
            error=None if ok else "nvswitch_system_reset.sh exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_nvswitch_system_reset_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/nvswitch-hosts", methods=["GET"])
def api_nvswitch_hosts():
    """GET query ``rack=pNN-rMM`` — list NV switch hostnames for the system-reset picker (``nodeset`` when available)."""
    try:
        rack = _normalize_rack_id((request.args.get("rack") or "").strip())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e), "hosts": []}), 400
    hosts = nvswitch_hosts_for_rack_list(rack)
    return jsonify({"ok": True, "rack": rack, "hosts": hosts})


@app.route("/api/nvswitch-system-reset-async", methods=["POST"])
def api_nvswitch_system_reset_async():
    """POST JSON ``{"rack": "pNN-rMM", "nvswitch_host": "pNN-rMM-nvsw-01", "log_dir": "…"}`` — async system reset on one switch; poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_system_reset_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    nvswitch_host = str(prep.get("nvswitch_host") or "")
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvswitch_system_reset",
            "operator": _operator_from_request(),
            "rack": rack,
            "nvswitch_host": nvswitch_host,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_system_reset_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "kind": "nvswitch_system_reset",
            "rack": rack,
            "nvswitch_host": nvswitch_host,
        }
    )


def _prepare_imex_reset_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async IMEX reset (``imex_reset.sh RACK``)."""
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _imex_reset_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"IMEX reset script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), rack]
    display_cmd = "./" + script.name + " " + shlex.quote(rack)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _imex_reset_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``imex_reset.sh RACK`` in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(60, _env_int("RACK_IMEX_RESET_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # profile="script" routes to the privileged node-operational SSH target
        # (RACK_SCRIPT_SSH_*, root+nmxc) when configured, else falls back to control.
        hp = host_exec.popen_on_host(
            cmd, cwd=_host_job_cwd(log_dir), env=_subprocess_env(), profile="script"
        )
        try:
            out, err = hp.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="error",
                ok=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout_sec}s",
                command_display=display_cmd,
                error=f"IMEX reset timed out after {timeout_sec}s",
            )
            _reset_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            command_display=display_cmd,
            error=None if ok else "imex_reset.sh exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_imex_reset_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/imex-reset-async", methods=["POST"])
def api_imex_reset_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async ``./imex_reset.sh RACK``; poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_imex_reset_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    # `_resolve_log_dir()` returns a **PurePosixPath** in ssh mode (the log dir lives on the head
    # node), and PurePosixPath is NOT a subclass of Path — so `assert isinstance(log_dir, Path)`
    # raised AssertionError -> HTTP 500 on EVERY call. This route has never worked in ssh mode.
    # Same bug class as the log readers fixed earlier (see the comment at api_rack_log_content);
    # this handler never got the memo. Accept either flavour.
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "imex_reset",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_imex_reset_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "imex_reset", "rack": rack})


def _nvlmapper_reports_base() -> Path:
    # An execution-host directory: expanduser/resolve would answer with this
    # process's home and filesystem, which is not where the reports are written.
    raw = (_eget("RACK_NVLMAPPER_REPORTS_BASE") or "/home/cmsupport/reports").strip()
    return _rpath(raw or "/home/cmsupport/reports")


def _prepare_nvlmapper_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVLMAPPER (stage under reports/<rack>, run ``./run_nvlmapper.sh <rack>``)."""
    if os.environ.get("RACK_NVLMAPPER_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "NVLMAPPER disabled (RACK_NVLMAPPER_DISABLED)"}), 403)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    nvlmapper_bin = SCRIPTS_DIR / "nvlmapper"
    if not _script_exists(nvlmapper_bin):
        return None, (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        f"nvlmapper binary not found ({nvlmapper_bin}); "
                        "copy from aihpc_val/tools/nvlmapper/dist_amd64"
                    ),
                }
            ),
            500,
        )
    runner = SCRIPTS_DIR / "run_nvlmapper.sh"
    if not _script_exists(runner):
        return None, (jsonify({"ok": False, "error": f"run_nvlmapper.sh not found ({runner})"}), 500)
    return {"rack": rack}, None


def _nvlmapper_zip_path_allowed(path: Path, rack: str) -> bool:
    """Legacy: allow only a finished-job zip under ``reports/<rack>/`` (new runs no longer create zips)."""
    base = _rpath(_nvlmapper_reports_base() / rack)
    try:
        _rpath(path).relative_to(base)
    except ValueError:
        return False
    name = path.name
    if not name.startswith(f"nvlmapper.{rack}.") or not name.lower().endswith(".zip"):
        return False
    return host_exec.is_file(path)


def _nvlmapper_stage_cleanup_keep_outputs(work: Path, rack: str) -> None:
    """Remove staged ``nvlmapper`` binary and ``run_nvlmapper.sh``; drop generated YAML with credentials.

    CSV / XLS / XLSX (and other outputs) remain under *work* for operators to copy from ``reports/<rack>/``.

    Runs on the execution host: the staging directory is wherever the tool ran,
    which is also the only place the credential-bearing YAML exists. Deleting it
    locally would leave the real one in place.
    """
    doomed = [
        f"{work}/{name}"
        for name in ("nvlmapper", "run_nvlmapper.sh", f"config-{rack}.yaml", f"config_{rack}.yaml")
    ]
    # -f so an absent file is not an error; the YAML only exists on some runs.
    host_exec.run_on_host(
        "rm -f " + " ".join(shlex.quote(p) for p in doomed),
        timeout=60,
        role="scripts",
    )


def _nvlmapper_async_worker(job_id: str, rack: str) -> None:
    """Stage ``nvlmapper`` + ``run_nvlmapper.sh``, run ``./run_nvlmapper.sh``, then clean staging only (keep outputs)."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(120, _env_int("RACK_NVLMAPPER_TIMEOUT", 14400))
    work = _rpath(_nvlmapper_reports_base() / rack)
    display_cmd = f"(cd {shlex.quote(str(work))} && ./run_nvlmapper.sh {shlex.quote(rack)})"
    env = _subprocess_env()
    work_dir_s = str(work)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
            nvlmapper_work_dir=work_dir_s,
        )
        # Stage on the execution host, not here: the nvlmapper binary lives in the
        # scripts tree over there and the work dir is where the tool must run, so
        # a local mkdir/copy staged the files onto the wrong machine and then ran
        # nothing. One command keeps the copy on the far side of the connection.
        scripts_root = _scripts_dir()
        staged = [f"{work_dir_s}/nvlmapper", f"{work_dir_s}/run_nvlmapper.sh"]
        stage_cmd = " && ".join(
            [
                f"mkdir -p {shlex.quote(work_dir_s)}",
                f"cp -f {shlex.quote(scripts_root + '/nvlmapper')} {shlex.quote(staged[0])}",
                f"cp -f {shlex.quote(scripts_root + '/run_nvlmapper.sh')} {shlex.quote(staged[1])}",
                "chmod 755 " + " ".join(shlex.quote(p) for p in staged),
            ]
        )
        stage_proc = host_exec.run_on_host(stage_cmd, timeout=180, role="scripts")
        if stage_proc.returncode != 0:
            raise OSError(
                f"could not stage nvlmapper in {work_dir_s}: "
                f"{(stage_proc.stderr or '').strip()[:300]}"
            )
        proc = host_exec.run_on_host(
            ["./run_nvlmapper.sh", rack],
            cwd=work_dir_s,
            env=env,
            timeout=timeout_sec,
            role="scripts",
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        ok = proc.returncode == 0
        _nvlmapper_stage_cleanup_keep_outputs(work, rack)
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=proc.returncode,
            stdout=out,
            stderr=err,
            command_display=display_cmd,
            nvlmapper_zip="",
            nvlmapper_work_dir=work_dir_s,
            error=None
            if ok
            else "NVLMAPPER run exited non-zero (see stdout/stderr); CSV/XLS outputs may still be present under the work directory",
        )
    except subprocess.TimeoutExpired:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            command_display=display_cmd,
            nvlmapper_zip="",
            nvlmapper_work_dir=work_dir_s,
            error=f"NVLMAPPER timed out after {timeout_sec}s",
        )
    except Exception as e:
        _logger.debug("_nvlmapper_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            nvlmapper_zip="",
            nvlmapper_work_dir=work_dir_s,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/nvlmapper-async", methods=["POST"])
def api_nvlmapper_async():
    """POST JSON ``{"rack": "pNN-rMM"}`` — stage, ``./run_nvlmapper.sh``; outputs stay under ``reports/<rack>/`` (no zip). Poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvlmapper_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    rack = str(prep["rack"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvlmapper",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
            "nvlmapper_zip": "",
            "nvlmapper_work_dir": str(_rpath(_nvlmapper_reports_base() / rack)),
        }
    threading.Thread(
        target=_nvlmapper_async_worker,
        args=(job_id, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvlmapper", "rack": rack})


@app.route("/api/nvlmapper-zip", methods=["GET"])
def api_nvlmapper_zip():
    """GET ``?job_id=…`` — legacy zip download (NVLMAPPER no longer builds zips; outputs live under ``nvlmapper_work_dir``)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _reset_jobs_lock:
        row = _reset_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    if str(payload.get("kind") or "") != "nvlmapper":
        return jsonify({"ok": False, "error": "not an nvlmapper job"}), 400
    ph = str(payload.get("phase") or "").lower()
    if ph not in ("done", "error"):
        return jsonify({"ok": False, "error": "job not finished yet"}), 400
    z = str(payload.get("nvlmapper_zip") or "").strip()
    if not z:
        wd = str(payload.get("nvlmapper_work_dir") or "").strip()
        msg = (
            "NVLMAPPER no longer produces a zip; copy CSV/XLS from the rack directory under the reports base."
            + (f" Work dir: {wd}" if wd else "")
        )
        return jsonify({"ok": False, "error": msg}), 400
    path = Path(z).resolve()
    rack = str(payload.get("rack") or "").strip().lower()
    if not rack or not _nvlmapper_zip_path_allowed(path, rack):
        return jsonify({"ok": False, "error": "invalid or missing zip path"}), 400
    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=path.name,
    )


def _prepare_precheck_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async Precheck (sysinfo.sh)."""
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return None, err
    assert prep is not None
    return {
        "log_dir": prep["log_dir"],
        "rack": prep["rack"],
        "target": prep["target"],
        "cmd": list(prep["cmd"]),
        "display_cmd": prep["display_cmd"],
    }, None


def _precheck_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run async precheck via sysinfo.sh in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # A cluster script with the log dir as cwd: both belong to the execution
        # host, so this goes through the connection on the privileged route (see
        # docs/EXECUTION_ARCHITECTURE.md 1.2) rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            timeout=timeout_sec,
            role="script",
        )
        ok = proc.returncode == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            command_display=display_cmd,
            error=None if ok else "sysinfo.sh exited non-zero (see stdout/stderr)",
        )
    except subprocess.TimeoutExpired:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            command_display=display_cmd,
            error=f"Precheck timed out after {timeout_sec}s",
        )
    except Exception as e:
        _logger.debug("_precheck_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/precheck-async", methods=["POST"])
def api_precheck_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async Precheck (sysinfo.sh)."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_precheck_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "precheck",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_precheck_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "precheck", "rack": rack})


def _prepare_nvswitch_check_async(
    body: dict,
) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVSwitch check (nvswitch audit without --fix)."""
    body2 = dict(body)
    body2["fix"] = False
    prep, err = _parse_nvswitch_audit_request(body2)
    if err is not None:
        return None, err
    assert prep is not None
    return {
        "log_dir": prep["log_dir"],
        "rack": prep["rack"],
        "cmd": list(prep["cmd"]),
        "display_cmd": prep["display_cmd"],
    }, None


def _nvswitch_check_async_worker(
    job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str
) -> None:
    """Run async nvswitch check (nvswitches_rack_audit.sh without --fix)."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # A cluster script with the log dir as cwd: both belong to the execution
        # host, so this goes through the connection on the privileged route (see
        # docs/EXECUTION_ARCHITECTURE.md 1.2) rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            timeout=timeout_sec,
            role="script",
        )
        ok = proc.returncode == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            command_display=display_cmd,
            error=None if ok else "nvswitch check exited non-zero (see stdout/stderr)",
        )
    except subprocess.TimeoutExpired:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            command_display=display_cmd,
            error=f"NVSWICTH check timed out after {timeout_sec}s",
        )
    except Exception as e:
        _logger.debug("_nvswitch_check_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/nvswitch-check-async", methods=["POST"])
def api_nvswitch_check_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async nvswitch check."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_check_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvswitch_check",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_check_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvswitch_check", "rack": rack})


def _prepare_imex_check_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async IMEX check (imex-related checks from sysinfo only)."""
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return None, err
    assert prep is not None
    script = _imex_check_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"IMEX check script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), str(prep["target"])]
    display_cmd = "./" + script.name + " " + shlex.quote(str(prep["target"]))
    return {
        "log_dir": prep["log_dir"],
        "rack": prep["rack"],
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _imex_check_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run async IMEX check in a background thread."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # A cluster script with the log dir as cwd: both belong to the execution
        # host, so this goes through the connection on the privileged route (see
        # docs/EXECUTION_ARCHITECTURE.md 1.2) rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            timeout=timeout_sec,
            role="script",
        )
        ok = proc.returncode == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            command_display=display_cmd,
            error=None if ok else "imex_check.sh exited non-zero (see stdout/stderr)",
        )
    except subprocess.TimeoutExpired:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            command_display=display_cmd,
            error=f"IMEX check timed out after {timeout_sec}s",
        )
    except Exception as e:
        _logger.debug("_imex_check_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/imex-check-async", methods=["POST"])
def api_imex_check_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async IMEX check."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_imex_check_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "imex_check",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_imex_check_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "imex_check", "rack": rack})


def _nvlink_preflight_script_path() -> Path:
    raw = (_eget("RACK_NVLINK_PREFLIGHT_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return _rpath(SCRIPTS_DIR / "nvlink_preflight.sh")


def run_nvlink_preflight_pdsh(pdsh_w: str, *, quick: bool) -> dict:
    """pdsh ``nvlink_preflight.sh`` across *pdsh_w*; returns per-node rows plus raw output.

    The script runs ON each node, so the deployed copy must be readable there (it lives under
    the shared RACK_REMOTE_BASE_PATH, which is NFS on BCM). Unlike hw-check there is no wrapper
    script doing the fan-out, so the pdsh command is built here.
    """
    script = _nvlink_preflight_script_path()
    if not _script_exists(script):
        raise OSError(f"nvlink_preflight.sh not found: {script}")
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    # Expand brackets rather than splitting on commas: this site's tray range CONTAINS a comma
    # (``[11-18,28-37]``), and scontrol expansion above is best-effort, so ``w`` may still be
    # bracketed. A naive split produced "r-[11-18" and "28-37]" and reported a healthy 18-node
    # rack as "2 nodes did not respond".
    expected = nvlink_preflight.expand_pdsh_w(w)
    # The functional probe alone allows 120s per node; pdsh fans out in parallel, so the wall
    # clock is roughly one node's worth plus scheduling. Quick mode skips it entirely.
    default_timeout = 600 if quick else 1800
    timeout = max(60, _env_int("RACK_NVLINK_PREFLIGHT_TIMEOUT", default_timeout))
    remote = f"bash {shlex.quote(str(script))}" + (" --quick" if quick else " --full")
    plat = _get_platform()
    cmd = plat.pdsh_cmd(w, remote)
    # Force the ssh transport. pdsh on this fleet defaults to rsh, which the compute nodes refuse
    # with "rcmd: socket: Permission denied" — every pdsh script in bin/scripts sets both of these
    # for that reason, but Platform.pdsh_cmd() does not, so a direct caller has to.
    if "pdsh" in (cmd[0] if cmd else "") and "-R" not in cmd:
        cmd = [cmd[0], "-R", "ssh", *cmd[1:]]
    env = _subprocess_env()
    env["PDSH_RCMD_TYPE"] = "ssh"
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
            env=env,
            profile="script",
        )
    except FileNotFoundError as e:
        raise OSError(f"pdsh not available on the execution host: {e}") from e
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    rows = nvlink_preflight.parse_nvlink_preflight_output(out, err, expected)
    summary = nvlink_preflight.summarize(rows)
    # pdsh exits non-zero when ANY node's command failed, and nvlink_preflight.sh exits 1 on a
    # FAIL verdict — so a non-zero rc here is the normal "some node is not ready" case, not a
    # tooling error. The verdict comes from the parsed rows, never from the exit code.
    return {
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "command_display": cmd_display,
        "pdsh_w": w,
        "quick": quick,
        "rows": rows,
        "summary": summary,
    }


def _nvlink_preflight_worker(job_id: str, pdsh_w: str, quick: bool, rack: str) -> None:
    """Run the NVLink preflight in a background thread; patches ``_reset_jobs[job_id]``."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(phase="running", started_running_at=datetime.now(timezone.utc).isoformat())
        result = run_nvlink_preflight_pdsh(pdsh_w, quick=quick)
        summary = result.get("summary") or {}
        ready = bool(summary.get("ready"))
        _patch(
            # "done" means the check completed and every node is ready; anything else is
            # surfaced as an error phase so the UI cannot paint an un-ready rack green.
            #
            # ``phase`` and ``summary.ready`` are the verdict. Do NOT read ``ok`` from
            # /api/reset-job-status: that route sets ok=True to mean "status query succeeded"
            # and overwrites whatever the worker stored here.
            phase="done" if ready else "error",
            ok=ready,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=result.get("returncode"),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            command_display=result.get("command_display", ""),
            pdsh_w=result.get("pdsh_w", ""),
            quick=quick,
            rows=result.get("rows", []),
            summary=summary,
            error=None if ready else _nvlink_preflight_error_text(summary),
        )
    except (OSError, ValueError) as e:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    finally:
        _reset_job_purge_later(job_id)


def _nvlink_preflight_error_text(summary: dict) -> str:
    """One line naming what is not ready, so the operator does not have to read the table.

    Only blocking conditions appear here. Skipped checks and warnings are reported in the payload
    (``nodes_with_skips`` / ``nodes_with_warnings``) but never make a rack not-ready.
    """
    bits: list[str] = []
    if summary.get("failed"):
        bits.append(f"{summary['failed']} node(s) FAILED")
    if summary.get("incomplete"):
        bits.append(f"{summary['incomplete']} incomplete")
    if summary.get("no_response"):
        bits.append(f"{summary['no_response']} did not respond")
    return "NVLink preflight: " + (", ".join(bits) if bits else "rack not ready")


@app.route("/api/nvlink-preflight-async", methods=["POST"])
def api_nvlink_preflight_async():
    """POST ``{"rack": …, "specs"|"target": …, "quick": bool}`` — NVLink/NVLS readiness gate.

    Async because the full gate runs a 120s forced-NVLink probe per node. Poll the shared
    ``/api/reset-job-status?job_id=…``; the finished job carries ``rows`` and ``summary``.
    """
    if os.environ.get("RACK_NVLINK_PREFLIGHT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "NVLink preflight disabled (RACK_NVLINK_PREFLIGHT_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    rack_raw = body.get("rack")
    if not isinstance(rack_raw, str) or not rack_raw.strip():
        return jsonify({"ok": False, "error": "rack is required"}), 400
    try:
        rack = _normalize_rack_id(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # Prefer the operator's node selection; fall back to the whole rack.
    pdsh_w = ""
    target_raw = body.get("target")
    if isinstance(target_raw, str) and target_raw.strip():
        pdsh_w = target_raw.strip()
    else:
        specs_raw = body.get("specs")
        if isinstance(specs_raw, list) and specs_raw:
            pdsh_w = ",".join(str(x).strip() for x in specs_raw if str(x).strip())
    if not pdsh_w:
        try:
            pdsh_w = validation_target_for_rack(rack, None)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    quick = bool(body.get("quick", False))
    script = _nvlink_preflight_script_path()
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"nvlink_preflight.sh not found: {script}"}), 500

    job_id = str(uuid.uuid4())
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvlink_preflight",
            "operator": _operator_from_request(),
            "rack": rack,
            "quick": quick,
            "phase": "starting",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    threading.Thread(
        target=_nvlink_preflight_worker,
        args=(job_id, pdsh_w, quick, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvlink_preflight", "rack": rack, "pdsh_w": pdsh_w})


@app.route("/api/rack-uptime", methods=["POST"])
def api_rack_uptime():
    """POST JSON ``{"rack": "pNN-rMM"}`` — run ``pdsh -w pNN-rMM-ct[RANGE] uptime``; returns table rows + raw output."""
    if os.environ.get("RACK_UPTIME_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "rack uptime disabled (RACK_UPTIME_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    rack_raw = body.get("rack")
    if not isinstance(rack_raw, str) or not rack_raw.strip():
        return jsonify({"ok": False, "error": "rack is required"}), 400
    try:
        with _track_sync_op(rack_raw, "uptime", _operator_from_request()):
            result = run_rack_uptime_pdsh(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


# last_job / scancel: plain id or job array id (e.g. 12345_0)
_SLURM_SCANCEL_JOB_ID_RE = core.SLURM_JOB_ID_RE  # one definition, in core








# Terminal (finished) job states — what the Scheduler "recent jobs" panel shows so a
# job that has left squeue (success OR failure) is still visible with its log.
























def _demo_parser_results():
    """Return synthetic parser results for demo mode (no parser.sh needed)."""
    import hashlib as _hl
    import math
    rack = (request.args.get("rack") or "demo-rack").strip()
    job_id = (request.args.get("job_id") or "0").strip()
    seed = int(_hl.md5(f"{rack}:{job_id}".encode()).hexdigest()[:8], 16)
    rng = __import__("random").Random(seed)

    sizes = [2**i for i in range(10, 31)]
    nccl_series = [
        {"size_bytes": s, "bus_bw_gbps": round(min(180, 10 + 170 * math.log2(s) / 30) * rng.uniform(0.85, 1.05), 2)}
        for s in sizes
    ]
    nccl_rows = [{
        "job_id": job_id,
        "rack": rack,
        "nccl_test": "all_reduce_perf_mpi",
        "dtype": "float32",
        "peak_busbw_gbps": round(max(p["bus_bw_gbps"] for p in nccl_series), 2),
        "all_reduce_busbw_series": nccl_series,
    }]

    n_iters = rng.randint(80, 200)
    base_step = rng.uniform(1.2, 2.5)
    step_series = [
        {"iteration": i + 1, "step_time": round(base_step * rng.uniform(0.9, 1.15) + (5.0 if i < 3 else 0), 3)}
        for i in range(n_iters)
    ]
    nemotron_rows = [{
        "job_id": job_id,
        "rack": rack,
        "model": "Nemotron-5H-56B",
        "median_step_time": round(base_step, 3),
        "step_time_series": step_series,
    }]

    return jsonify({
        "ok": True,
        "returncode": 0,
        "rack": rack,
        "log_file": f"root-NEMO-1Rack-{rack}-18N-{job_id}.txt",
        "data": {
            "nccl_peak_out_of_place_busbw_gbps": nccl_rows,
            "nemotron_5h_56b_train_window": nemotron_rows,
            "sn_llama_nemotron": [],
            "parser_flags": [{"flag": "demo_mode", "value": "true"}],
        },
        "grafana_embed": None,
    })


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

# Head+tail caps mirror universal_parser's own DEFAULT_HEAD_BYTES / DEFAULT_TAIL_BYTES so a
# remote read produces the same text the module would have produced locally.
# Extra extensions the legacy discovery path (*.txt / *.out only) can never see.

# Intermediate artefacts that are SUPERSEDED by an aggregated result log and must not be
# ingested as runs of their own.
#
# llmb-run writes raw sbatch stdout to ``llmb-nccl_nodes_<jobid>.out`` and a collector trace to
# ``llmb-autocollect_<jobid>.log``; ``collect_llmb_nccl_log.sh`` then aggregates the real result
# into ``cmods-NCCL-1Rack-<rack>-<N>N-<jobid>.txt``. Ingesting the raw stdout too produced TWO
# history rows per job, and the raw one is routinely marked ``fail`` because it still contains
# srun/pmix noise from launch attempts that llmb-run retried successfully — so a passing rack
# surfaced as failed in the dashboard. Judge a run by its aggregated log, never by sbatch stdout.
#
# Override with RACK_UNIVERSAL_SCAN_IGNORE_RE (a full-match regex over the basename); set it to
# something that matches nothing (e.g. ``$^``) to restore the previous behaviour.


_truthy_arg = core.truthy_arg




# --- "did parser.sh actually parse anything?" ----------------------------------------
_PARSER_PLACEHOLDER_TOKENS = {"", "-", "--", "---", "?", "n/a", "na", "none", "null", "unk"}


def _parser_scalar_is_real(v: object) -> bool:
    """True when a parser.sh table cell holds a genuine number (not ``-`` / ``---`` / ``?``)."""
    if v is None or isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return math.isfinite(float(v))
    s = str(v).strip().replace(",", "")
    if s.lower() in _PARSER_PLACEHOLDER_TOKENS:
        return False
    try:
        return math.isfinite(float(s))
    except (TypeError, ValueError):
        return False


def _parser_json_has_usable_rows(parsed: object) -> bool:
    """True when a ``parser.sh --json`` object carries at least one real measurement.

    parser.sh runs every extractor on every input file unconditionally, so it emits a
    fully-formed placeholder row (``'---'`` / ``'?'`` / status ``FAIL``) even for a log the
    extractor did not apply to. "The tables are non-empty" is therefore NOT evidence that
    anything was parsed — this predicate is what decides whether the universal fallback runs.
    """
    if not isinstance(parsed, dict):
        return False
    tables = (
        ("nccl_peak_out_of_place_busbw_gbps",
         ("all_reduce_gbps", "all_gather_gbps", "alltoall_gbps"),
         ("all_reduce_busbw_series",)),
        ("nemotron_5h_56b_train_window",
         ("step_s", "median_step_time", "tflops_per_gpu", "step_time_std", "perf_std", "mfu_percent"),
         ("step_series",)),
        ("mnubergemm_stress",
         ("gflops_max", "gflops_median", "nodes", "ranks"),
         ()),
        ("sn_llama_nemotron",
         ("step_s", "median_step_time", "tflops_per_gpu"),
         ("step_series",)),
    )
    for key, scalars, series_keys in tables:
        rows = parsed.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if any(_parser_scalar_is_real(row.get(f)) for f in scalars):
                return True
            for sk in series_keys:
                ser = row.get(sk)
                if isinstance(ser, list) and ser:
                    return True
    return False


# --- host-aware capped log read ------------------------------------------------------




#: universal_parser metric name -> where the same number lives in an nvis_logscan record.




# --- history ingest ------------------------------------------------------------------




# ---------------------------------------------------------------------------------------------
# Universal action capture: EVERY API call lands in the Parquet lake.
#
# User requirement, verbatim: *"every fucking thing - along with a flag"* — the click, the request,
# the script execution and the output. _lake_push_job covers async workers and sync tracked ops;
# this after_request hook is the catch-all so a route nobody remembered to instrument is still
# recorded. Provenance you have to remember to add is provenance you will forget to add.
#
# RACK_LAKE_CAPTURE:
#   actions  (default) every mutating call (POST/PUT/PATCH/DELETE) + explicit action GETs
#   all                every API call, including read-only polls (very chatty — debugging only)
#   off                nothing
# ---------------------------------------------------------------------------------------------
_LAKE_CAPTURE_SKIP = frozenset({
    # High-frequency SPA polls with no operator intent — they would swamp the lake and bury the
    # actions that matter. Still captured under RACK_LAKE_CAPTURE=all.
    "/api/rack-locks", "/api/status", "/api/cluster-overview", "/api/reset-job-status",
    "/api/metrics/health", "/api/lake/health", "/api/observability/health",
})
# Read-only by verb but a deliberate operator action, so worth keeping even in "actions" mode.
_LAKE_CAPTURE_GET_ALLOW = ("/api/hw-check", "/api/slurm-", "/api/history/capture-job",
                           "/api/rack-log-content", "/api/parser-results", "/api/tools/")
_LAKE_BODY_MAX = 64 * 1024


def _lake_capture_mode() -> str:
    return (os.environ.get("RACK_LAKE_CAPTURE") or "actions").strip().lower()


def _lake_should_capture(path: str, method: str) -> bool:
    mode = _lake_capture_mode()
    if mode == "off" or not path.startswith("/api/"):
        return False
    if mode == "all":
        return True
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return path not in _LAKE_CAPTURE_SKIP
    return any(path.startswith(pfx) for pfx in _LAKE_CAPTURE_GET_ALLOW) and path not in _LAKE_CAPTURE_SKIP


@app.before_request
def _lake_capture_start():
    try:
        g._lake_t0 = time.time()
        # Read the body here: after the view runs it may already be consumed.
        if _lake_should_capture(request.path, request.method):
            g._lake_body = (request.get_data(cache=True) or b"")[:_LAKE_BODY_MAX]
    except Exception:
        _logger.debug("_lake_capture_start: ignoring error", exc_info=True)


@app.after_request
def _lake_capture_request(response):
    try:
        if lake_sink is None or lake_sink.is_disabled():
            return response
        if not _lake_should_capture(request.path, request.method):
            return response
        # Streaming responses (SSE log tails) must not be materialised — reading .data would
        # consume the generator and break the stream in the browser. A generator-backed
        # Response leaves direct_passthrough False, so is_streamed is the load-bearing check:
        # without it a `tail -f` body is swallowed here and the viewer hangs on "Connecting…".
        if response.direct_passthrough or response.is_streamed:
            body = "<streaming response — not captured>"
        else:
            body = (response.get_data() or b"")[:_LAKE_BODY_MAX].decode("utf-8", "replace")
        raw = getattr(g, "_lake_body", b"") or b""
        rack = ""
        try:
            payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
            if isinstance(payload, dict):
                rack = str(payload.get("rack") or "")
        except Exception:  # noqa: BLE001
            payload = {}
        rack = rack or (request.args.get("rack") or "")
        action = request.path.replace("/api/", "").strip("/").replace("/", "_") or "root"
        record = {
            "kind": "rackstatus_request",
            "status": "pass" if response.status_code < 400 else "fail",
            "action": action,
            "method": request.method,
            "path": request.path,
            "query": dict(request.args),
            "request_body": raw.decode("utf-8", "replace"),
            "http_status": response.status_code,
            "duration_ms": round((time.time() - getattr(g, "_lake_t0", time.time())) * 1000, 1),
            "operator": _operator_from_request(),
            "remote_addr": request.headers.get("X-Forwarded-For") or request.remote_addr,
            "rack": rack,
        }
        lake_sink.push_run(
            run_id=lake_sink.make_run_id(f"req-{action}", rack, uuid.uuid4().hex[:12]),
            workload="rackstatus_request", rack=rack, job_id=None, nodes=[],
            status=record["status"], parsed=record,
            log_path=f"synthesized:{action}.response.json", log_text=body,
        )
    except Exception as e:  # noqa: BLE001 - never break a response
        _logger.warning("lake request capture failed: %s", e)
    return response






def _history_ingest_parser_json(
    parsed: object,
    log_dir: Path,
    log_path_s: str,
    *,
    rack_hint: str | None = None,
    source: str = "parser.sh",
) -> list[str]:
    """Persist a ``parser.sh --json`` payload. Returns run_ids (empty list on any problem)."""
    if not isinstance(parsed, dict) or not core.history_enabled():
        return []
    try:
        mt, sz = _log_stat_for_path(log_dir, log_path_s)
        return list(
            results_history.ingest_parser_json(
                parsed,
                source_log_path=str(log_path_s),
                log_mtime=mt,
                log_size=sz,
                rack_hint=rack_hint,
                source=source,
            )
            or []
        )
    except Exception as e:  # noqa: BLE001 - history is best-effort
        _logger.warning("results_history parser ingest failed for %s: %s", log_path_s, e)
        return []




# --- whole-directory inventory (every extension, every depth) ------------------------




# --- history read routes -------------------------------------------------------------


# Alias kept because TRACK-A's integration note documents this spelling.


# --- universal ingest routes ---------------------------------------------------------










@app.route("/api/parser-results")
def api_parser_results():
    """Run parser.sh --json on the log for rack (same cwd convention as rack_status log_dir).

    Optional ``log_file``: basename under log_dir (or failed/) for multirack / explicit logs (must
    contain NEMO or NCCL in the basename). When set, ``rack`` is optional (used for Grafana context).

    Optional ``full_rack=1``: when resolving by rack (no ``log_file``/``path``), only consider logs
    whose basename encodes the full-rack node count (``-18N-`` by default; override with
    ``full_rack_nodes`` or ``RACK_FULL_RACK_NODES``). A row ``job_id`` that points at a partial
    (e.g. ``-1N-``) run is ignored so the newest full-rack log is used instead.

    Optional ``display_timezone`` query (IANA, e.g. ``Europe/Berlin``): zone for naive wall-clock
    timestamps in the NEMO log when deriving the LLM/Grafana time window (same as Charts TZ in the UI).

    Response ``data`` may include ``sn_llama_nemotron``: Nemotron-style rows from a second parser pass
    over ``log_dir/sn-llama/*.txt`` matching the rack (when that directory has LLAMA logs). Empty list
    when none; UI hides the LLAMA tab when empty.
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        return _demo_parser_results()
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    log_fn = (request.args.get("log_file") or "").strip()
    path_arg = (request.args.get("path") or "").strip()
    rack = (request.args.get("rack") or "").strip()
    job_raw = (request.args.get("job_id") or "").strip()
    job_id = job_raw if job_raw and job_raw != "-" else None
    full_rack = (request.args.get("full_rack") or "").strip().lower() in ("1", "true", "yes", "on")
    fr_nodes_raw = (request.args.get("full_rack_nodes") or "").strip()
    full_rack_nodes: int | None = None
    if fr_nodes_raw.isdigit():
        full_rack_nodes = int(fr_nodes_raw, 10) if int(fr_nodes_raw, 10) > 0 else None
    # Populated only when resolving by rack; an explicit ``path``/``log_file`` request means the
    # caller named the exact file it wants, so it keeps the single-log behaviour untouched.
    stage_paths: dict[str, Path] = {}
    if path_arg:
        # Explicit allowlisted path (e.g. a Travis bring-up node log
        # results/<node>/allreduce.txt) — bypasses the NEMO/NCCL basename filter but
        # must live under RACK_STATUS_ALLOWED_PREFIXES (validated by the shared resolver).
        try:
            log_path = _resolve_slurm_job_stdout_path(path_arg, request.args.get("log_dir"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        rk = rack
    elif log_fn:
        log_path = _resolve_nemo_log_basename_in_dir(log_dir, log_fn)
        if log_path is None:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"log file not found or basename must contain NEMO/NCCL: {Path(log_fn).name!r}"
                    ),
                }
            ), 404
        rk = rack
        if not rk:
            rk = multirack_display_rack_from_basename(log_path.name)
    else:
        try:
            rk = _normalize_rack_id(rack)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        # One log per stage: NCCL and NeMo are separate jobs since llmb-run, so a single file
        # can only carry one stage's evidence. The primary keeps every downstream consumer
        # (grafana window, sn-llama, history ingest, adoption) on exactly one log as before;
        # a distinct second stage is parsed after and merged in below.
        stage_paths = pick_logs_for_rack(
            log_dir, rack, job_id, full_rack=full_rack, full_rack_nodes=full_rack_nodes
        )
        log_path = _primary_stage_log(stage_paths, job_id)
        if log_path is None:
            need_n = full_rack_nodes if full_rack_nodes and full_rack_nodes > 0 else _full_rack_expected_nodes()
            detail = (
                f"no matching *-{need_n}N-*.txt for rack {rk!r} under {log_dir} "
                f"(full-rack filter on; uncheck Full rack validation to use the latest partial log)"
                if full_rack
                else (
                    f"no matching *.txt for rack {rk!r} under {log_dir} or {log_dir / 'failed'} "
                    f"(NEMO/NCCL basename, or ClusterKit root-CK-/clusterkit with one rack id)"
                )
            )
            return jsonify({"ok": False, "error": detail}), 404
    adopt_rk = (rk or rack or multirack_display_rack_from_basename(log_path.name) or "").strip()
    if adopt_rk:
        try:
            adopt_rk = _normalize_rack_id(adopt_rk)
        except ValueError:
            pass
    _maybe_adopt_validation_log(log_path, log_dir, adopt_rk)
    parser = _parser_path()
    if not _script_exists(parser):
        return jsonify({"ok": False, "error": f"parser.sh not found: {parser}"}), 500
    env = _subprocess_env({"NO_COLOR": "1"})
    # Route parser.sh through host_exec (SSH when remote) and don't local-resolve a remote path.
    lp = str(_rpath(log_path))
    cmd = [
        "bash",
        str(parser),
        *_parser_extra_args(),
        "--json",
        lp,
    ]
    try:
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(log_dir),
            env=env,
            timeout=max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "parser.sh timed out"}), 504
    except Exception as e:
        _logger.debug("api_parser_results: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    err_tail = (proc.stderr or "").strip()
    raw_out = proc.stdout or ""
    try:
        parsed = json.loads(raw_out) if raw_out.strip() else None
    except json.JSONDecodeError:
        return jsonify(
            {
                "ok": False,
                "error": "parser did not emit valid JSON on stdout",
                "stderr": err_tail,
                "stdout_head": raw_out[:4000],
                "returncode": proc.returncode,
                "log_file": lp,
            }
        ), 502
    if not isinstance(parsed, dict):
        return jsonify({"ok": False, "error": "parser JSON root must be an object"}), 502
    display_cmd = "./parser.sh --json " + shlex.quote(log_path.name)
    sn_nemo: list[object] = []
    # Secondary sn-llama pass. This used to be skipped unless the app ran on the
    # target host, because it shelled out locally against local files; going
    # through the logs role runs it wherever the logs actually are, so the
    # sn-llama series is no longer missing from the charts on a remote deployment.
    root = _rpath(log_dir)
    sn_paths = _sn_llama_txt_paths_for_rack(log_dir, rk, exclude=log_path)
    if sn_paths:
        try:
            rel_sn = [str(PurePosixPath(str(p)).relative_to(root)) for p in sn_paths]
            cmd_sn = ["bash", str(parser), *_parser_extra_args(), "--json", *rel_sn]
            proc_sn = host_exec.run_on_host(
                cmd_sn,
                cwd=str(root),
                env=env,
                timeout=max(60, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
                role="logs",
            )
            if proc_sn.returncode == 0 and (proc_sn.stdout or "").strip():
                sn_parsed = json.loads(proc_sn.stdout)
                if isinstance(sn_parsed, dict):
                    raw_sn = sn_parsed.get("nemotron_5h_56b_train_window")
                    if isinstance(raw_sn, list):
                        sn_nemo = list(raw_sn)
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
            _logger.warning("parser sn-llama bundle failed: %s", e)
    merged_data: dict[str, object] = {**parsed, "sn_llama_nemotron": sn_nemo}

    # --- second stage (2026-08-03) ----------------------------------------------------
    # Stage 1 (NCCL) and stage 2 (NeMo) are separate jobs writing separate logs, so the file
    # parsed above holds evidence for only one of them. Parse the other stage's own log and take
    # ONLY that stage's table from it. Rows are self-describing (each carries rack and job_id),
    # so this is a concatenation. Best-effort: a missing second stage just means one empty tab.
    stage_logs: dict[str, str] = {}
    if stage_paths:
        stage_logs = {stage: str(_rpath(p)) for stage, p in stage_paths.items()}
    for stage, spath in _parser_secondary_stages(stage_paths, log_path):
        table = _PARSER_STAGE_TABLES.get(stage)
        if not table:
            continue
        slp = str(_rpath(spath))
        try:
            proc_s = host_exec.run_on_host(
                ["bash", str(parser), *_parser_extra_args(), "--json", slp],
                cwd=str(log_dir),
                env=env,
                timeout=max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
            )
            if proc_s.returncode != 0 or not (proc_s.stdout or "").strip():
                _logger.warning("parser %s stage returned nothing for %s", stage, slp)
                continue
            parsed_s = json.loads(proc_s.stdout)
            if not isinstance(parsed_s, dict):
                continue
            rows_s = parsed_s.get(table)
            if isinstance(rows_s, list) and rows_s:
                # Replace, not append: parser.sh emits an all-"---" placeholder row for an
                # explicitly-named file whatever its content, so the primary log contributes a
                # junk row to the table it is not the authority for. This stage owns the table.
                merged_data[table] = list(rows_s)
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
            _logger.warning("parser %s stage failed for %s: %s", stage, slp, e)

    # The replace-the-table step above only fires when the other stage HAS its own log. A rack that
    # never ran a stage (NCCL-only racks such as j15/j16 on 2026-08-07) keeps the placeholder row
    # the primary parse produced for the table it is not the authority for. Drop it by content, so
    # "stage never ran" reads as an empty table rather than a UNK row wearing the other stage's
    # job id. Content-based on purpose: a combined-format log legitimately yields both tables from
    # one parse and must not be stripped.
    dropped_placeholders = _drop_parser_placeholder_rows(merged_data)
    if dropped_placeholders:
        _logger.info("parser placeholder rows dropped for %s: %s", rk or "?", dropped_placeholders)

    log_tz_q = _sanitize_display_timezone_query(request.args.get("display_timezone"))
    # Grafana time-window context is optional; never let it 500 the parser charts (it may touch
    # log files, which over SSH can fail). Degrade to no embed on error.
    try:
        g_embed = _grafana_parser_embed_context(
            log_path, rk, job_raw, parsed, log_timestamp_tz=log_tz_q
        )
    except Exception as e:
        _logger.warning("grafana embed context failed: %s", e, exc_info=True)
        g_embed = None
    pdisp = _grafana_url_timezone_param()
    if isinstance(g_embed, dict) and g_embed.get("display_timezone"):
        pdisp = str(g_embed["display_timezone"])

    # --- universal ingest + durable history (2026-07-28) -----------------------------
    # Additive only: the four parser.sh tables in ``data`` are untouched, so the NCCL /
    # Nemotron / MNUBERGEMM charts keep rendering exactly what they rendered before. When
    # parser.sh produced nothing usable (a foreign tool, or the stale head-node v6 that
    # returns rack='?' and '---' for a real MNUBERGEMM log) a ``data.universal`` key is
    # added with the normalised result so the log still surfaces. Never fails the request.
    universal_res = None
    try:
        if not _parser_json_has_usable_rows(parsed):
            universal_res = _universal_fallback_for_log(log_dir, lp, rack_hint=rk or None)
            if universal_res is not None:
                merged_data = {**merged_data, "universal": universal_res}
    except Exception as e:  # noqa: BLE001 - never break the charts for this
        _logger.warning("universal fallback failed for %s: %s", lp, e)
    try:
        if universal_res is None:
            _history_ingest_parser_json(parsed, log_dir, lp, rack_hint=rk or None,
                                        source="api/parser-results")
    except Exception as e:  # noqa: BLE001 - history is best-effort
        _logger.warning("results_history ingest failed for %s: %s", lp, e)

    return jsonify(
        {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "rack": rk,
            "log_file": lp,
            # Which file fed each tab. Absent keys mean that stage has no log for this rack yet.
            "stage_logs": stage_logs,
            "job_id": _jobid_from_nemo_log_basename(Path(lp).name) or job_raw or "",
            "full_rack": bool(full_rack) if not (path_arg or log_fn) else False,
            "full_rack_nodes": (
                (full_rack_nodes if full_rack_nodes and full_rack_nodes > 0 else _full_rack_expected_nodes())
                if full_rack and not (path_arg or log_fn)
                else None
            ),
            "command_display": display_cmd,
            "data": merged_data,
            # {table: [rack…]} for parser.sh placeholder rows removed above. Non-empty means that
            # stage produced no run for this rack, as opposed to the tab having failed to load.
            "dropped_placeholder_rows": dropped_placeholders,
            "stderr": err_tail,
            "grafana_uri": _rack_web_grafana_uri(),
            "grafana_port": _rack_web_grafana_port(),
            "grafana_embed": g_embed,
            "parser_display_timezone": pdisp,
        }
    )


@app.route("/api/nvloom-heatmaps-log")
def api_nvloom_heatmaps_log():
    """Return the newest Slurm stdout for nvloom singlerack (``nvloom/*NVLOOM-<rack>-*.txt``) for heatmap UI.

    Query: ``log_dir``, ``rack`` (same normalization as the rest of rack web).
    Response JSON: ``ok``, ``path`` (resolved file or empty), ``content`` (UTF-8 text, size-capped).
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        demo_txt = (
            "Process  0 (p1-r01-ct01): device 0: NVIDIA GB300 (00000008:06:00); rackGuid: 111\n"
            "Process  1 (p1-r01-ct01): device 1: NVIDIA GB300 (00000009:06:00); rackGuid: 222\n"
            "Running demo_pair\n"
            "A | B\n"
            "0 100 200\n"
            "1 150 220\n"
            "Min demo_pair 90 GB/s\n"
        )
        return jsonify({"ok": True, "path": "(demo)", "content": demo_txt})
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack_raw = (request.args.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    cands = find_nvloom_log_paths_for_rack(log_dir, rk)
    if not cands:
        return jsonify(
            {
                "ok": False,
                "error": f"no nvloom log *NVLOOM-{rk}-*.txt under {log_dir / 'nvloom'}",
            }
        ), 404
    log_path = cands[0]
    st = host_exec.stat(log_path)
    if st is None:
        return jsonify({"ok": False, "error": f"NVLOOM log not readable: {log_path}"}), 404
    sz = st.size
    if sz > _NVLOOM_HEATMAPS_LOG_MAX_BYTES:
        return jsonify(
            {
                "ok": False,
                "error": f"NVLOOM log too large ({sz} bytes; max {_NVLOOM_HEATMAPS_LOG_MAX_BYTES})",
            }
        ), 413
    text, rmeta = host_exec.read_text(
        log_path,
        head_bytes=_NVLOOM_HEATMAPS_LOG_MAX_BYTES,
        tail_bytes=0,
        whole_bytes=_NVLOOM_HEATMAPS_LOG_MAX_BYTES,
    )
    if rmeta.get("read_error"):
        return jsonify({"ok": False, "error": str(rmeta["read_error"])}), 500
    return jsonify({"ok": True, "path": str(_rpath(log_path)), "content": text})


_CLUSTERKIT_JSON_MAX_BYTES = 80 * 1024 * 1024


def _clusterkit_rack_dir(log_dir: Path, rk: str) -> Path:
    return log_dir / "clusterkit" / rk


def _clusterkit_get_cell(links: list[object], i: int, j: int) -> float | None:
    """Upper-triangular storage: same convention as clusterkit ``bwResultAnalyzer.getCell``."""
    if i == j:
        return 0.0
    try:
        if i < j:
            row = links[i]
            if not isinstance(row, list) or j - i >= len(row):
                return None
            return float(row[j - i])
        row = links[j]
        if not isinstance(row, list) or i - j >= len(row):
            return None
        return float(row[i - j])
    except (IndexError, TypeError, ValueError):
        return None


def _clusterkit_parse_bandwidth_payload(
    data: dict[str, object],
) -> tuple[list[str], list[list[float | None]], dict[str, object]] | None:
    """Return ``(labels, matrix, meta)`` from ClusterKit ``bandwidth.json``."""
    nodes = data.get("Nodes")
    links = data.get("Links")
    if not isinstance(nodes, dict) or not isinstance(links, list):
        return None
    try:
        pairs = sorted(((int(v), str(k)) for k, v in nodes.items()), key=lambda x: x[0])
    except (TypeError, ValueError):
        return None
    labels = [p[1] for p in pairs]
    n = len(labels)
    if n < 2 or len(links) < n - 1:
        return None
    for i in range(n - 1):
        row = links[i]
        if not isinstance(row, list) or len(row) != n - i:
            return None
    matrix: list[list[float | None]] = []
    for i in range(n):
        row_out: list[float | None] = []
        for j in range(n):
            if i == j:
                row_out.append(0.0)
            else:
                row_out.append(_clusterkit_get_cell(links, i, j))
        matrix.append(row_out)
    meta: dict[str, object] = {
        "cluster": data.get("Cluster"),
        "user": data.get("User"),
        "testname": data.get("Testname"),
        "date_and_time": data.get("Date_and_Time"),
        "jobid": data.get("JOBID"),
        "ppn": data.get("PPN"),
        "bidirectional": data.get("Bidirectional"),
        "hca_tag": data.get("HCA_Tag"),
        "technology": data.get("Technology"),
        "units": data.get("Units"),
    }
    return labels, matrix, meta


def _clusterkit_pairwise_positive_summary(data: dict[str, object]) -> dict[str, object] | None:
    """Min / max / avg over off-diagonal positive finite cells (bandwidth or latency JSON)."""
    parsed = _clusterkit_parse_bandwidth_payload(data)
    if parsed is None:
        return None
    _labels, matrix, _meta = parsed
    vals: list[float] = []
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if i == j:
                continue
            if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0:
                vals.append(float(v))
    n = len(matrix)
    if not vals:
        return {"node_count": n, "link_count": 0}
    return {
        "node_count": n,
        "link_count": len(vals),
        "min": min(vals),
        "max": max(vals),
        "avg": sum(vals) / len(vals),
    }


def _clusterkit_bandwidth_summary_from_data(data: dict[str, object]) -> dict[str, object] | None:
    return _clusterkit_pairwise_positive_summary(data)


@app.route("/api/clusterkit-results")
def api_clusterkit_results():
    """List ClusterKit ``bandwidth.json`` runs under ``log_dir/clusterkit/<rack>/``; optional full matrix for one run.

    Query: ``log_dir``, ``rack``; optional ``run`` = subdirectory name (basename only) with ``bandwidth.json``.
    ``runs`` are sorted newest first by ``bandwidth.json`` mtime. When ``run`` matches, ``detail`` includes
    ``labels``, ``matrix``, ``meta`` (bandwidth). If ``latency.json`` exists beside it, ``detail["latency"]`` has
    the same shape for pairwise latency (see ``bwResultAnalyzer``).
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        run_q = (request.args.get("run") or "").strip()
        demo_runs: list[dict[str, object]] = [
            {
                "dir": "demo_mlx5_0",
                "rel_path": "clusterkit/p1-r01/demo_mlx5_0",
                "mtime": 0,
                "meta": {"hca_tag": "mlx5_0", "units": "MB/s", "date_and_time": "demo"},
                "summary": {"node_count": 3, "link_count": 6, "min": 100.0, "max": 200.0, "avg": 150.0},
                "has_latency": True,
            }
        ]
        out_demo: dict[str, object] = {"ok": True, "rack": "p1-r01", "runs": demo_runs}
        if run_q == "demo_mlx5_0":
            out_demo["detail"] = {
                "dir": "demo_mlx5_0",
                "labels": ["node-a", "node-b", "node-c"],
                "matrix": [[0.0, 100.0, 150.0], [100.0, 0.0, 200.0], [150.0, 200.0, 0.0]],
                "meta": {
                    "cluster": "demo-cluster",
                    "user": "demo",
                    "testname": "ib_write_bw",
                    "date_and_time": "demo",
                    "jobid": "999",
                    "ppn": 1,
                    "bidirectional": "True",
                    "hca_tag": "mlx5_0",
                    "technology": "IB",
                    "units": "MB/s",
                },
                "summary": {"node_count": 3, "link_count": 6, "min": 100.0, "max": 200.0, "avg": 150.0},
                "latency": {
                    "labels": ["node-a", "node-b", "node-c"],
                    "matrix": [[0.0, 1.25, 1.48], [1.25, 0.0, 2.05], [1.48, 2.05, 0.0]],
                    "meta": {
                        "cluster": "demo-cluster",
                        "testname": "ib_write_lat",
                        "date_and_time": "demo",
                        "jobid": "999",
                        "hca_tag": "mlx5_0",
                        "technology": "IB",
                        "units": "usec",
                    },
                    "summary": {"node_count": 3, "link_count": 6, "min": 1.25, "max": 2.05, "avg": 1.593},
                },
            }
        return jsonify(out_demo)
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack_raw = (request.args.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    root = _rpath(log_dir)
    base = _rpath(_clusterkit_rack_dir(root, rk))
    # One listing covers every run directory under this rack. These files live on
    # the execution host, so the previous iterdir()/stat()/read_text() walk was
    # both a local-filesystem call on a remote path and a round trip per run.
    found = host_exec.stat_dir(
        base, patterns=("bandwidth.json", "latency.json"), maxdepth=2
    )
    if not found:
        return jsonify({"ok": True, "rack": rk, "runs": []})

    def _rel_to_log_dir(p: PurePosixPath) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    with_latency = {
        PurePosixPath(s).parent for s in found if PurePosixPath(s).name == "latency.json"
    }
    runs_out: list[dict[str, object]] = []
    for path_s, st in found.items():
        bw = PurePosixPath(path_s)
        # Only <rack>/<run>/bandwidth.json; skip a stray file directly under the rack dir.
        if bw.name != "bandwidth.json" or bw.parent == base:
            continue
        entry: dict[str, object] = {
            "dir": bw.parent.name,
            "rel_path": _rel_to_log_dir(bw),
            "mtime": int(st.mtime),
        }
        if st.size > _CLUSTERKIT_JSON_MAX_BYTES:
            entry["error"] = f"bandwidth.json too large ({st.size} bytes)"
            runs_out.append(entry)
            continue
        raw, read_meta = host_exec.read_text(bw, whole_bytes=_CLUSTERKIT_JSON_MAX_BYTES)
        if read_meta.get("read_error"):
            entry["error"] = str(read_meta["read_error"])
            runs_out.append(entry)
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            entry["error"] = str(e)
            runs_out.append(entry)
            continue
        if not isinstance(data, dict):
            continue
        entry["meta"] = {
            "hca_tag": data.get("HCA_Tag"),
            "units": data.get("Units"),
            "date_and_time": data.get("Date_and_Time"),
            "jobid": data.get("JOBID"),
            "bidirectional": data.get("Bidirectional"),
            "cluster": data.get("Cluster"),
        }
        entry["summary"] = _clusterkit_bandwidth_summary_from_data(data) or {}
        entry["has_latency"] = bw.parent in with_latency
        runs_out.append(entry)
    runs_out.sort(key=lambda r: int(r.get("mtime") or 0), reverse=True)
    out: dict[str, object] = {"ok": True, "rack": rk, "runs": runs_out}
    run_q = (request.args.get("run") or "").strip()
    if run_q and run_q == PurePosixPath(run_q).name and re.fullmatch(r"[A-Za-z0-9_.\-]+", run_q):
        # Containment comes from the listing: `found` only holds paths under base,
        # so a traversal attempt simply misses instead of needing a resolve() check.
        sub = base / run_q

        def _load_json(p: PurePosixPath) -> dict | None:
            st = found.get(str(p))
            if st is None or st.size > _CLUSTERKIT_JSON_MAX_BYTES:
                return None
            text, meta = host_exec.read_text(p, whole_bytes=_CLUSTERKIT_JSON_MAX_BYTES)
            if meta.get("read_error"):
                return None
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                return None
            return loaded if isinstance(loaded, dict) else None

        data = _load_json(sub / "bandwidth.json")
        parsed = _clusterkit_parse_bandwidth_payload(data) if data is not None else None
        if parsed:
            labels, matrix, bw_meta = parsed
            detail: dict[str, object] = {
                "dir": run_q,
                "labels": labels,
                "matrix": matrix,
                "meta": bw_meta,
                "summary": _clusterkit_bandwidth_summary_from_data(data) or {},
            }
            lat_data = _load_json(sub / "latency.json")
            lat_parsed = (
                _clusterkit_parse_bandwidth_payload(lat_data) if lat_data is not None else None
            )
            if lat_parsed:
                ll, lm, lmeta = lat_parsed
                detail["latency"] = {
                    "labels": ll,
                    "matrix": lm,
                    "meta": lmeta,
                    "summary": _clusterkit_pairwise_positive_summary(lat_data) or {},
                }
            out["detail"] = detail
    return jsonify(out)


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


# Metrics worth a fleet baseline, per workload kind. Names match what
# ``results_history`` stores from parser.sh (records_from_parser_json) and from
# ``universal_parser`` — both write the same lower-cased metric_name.
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


@app.route("/api/analytics/history", methods=["GET"])
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


@app.route("/api/analytics/last", methods=["GET"])
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


@app.route("/api/analytics/aggregate", methods=["POST"])
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


@app.route("/api/analytics/aggregate-async", methods=["POST"])
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


@app.route("/api/analytics/aggregate-status", methods=["GET"])
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


#: Report kind -> the formats it can render, first entry being the default.
#:
#: The Nscale report answers a customer's JSON Schema, so JSON is the only honest format for it —
#: a CSV of that document would not validate against the contract it exists to satisfy. The Rack
#: Status report is ours and carries the same dataset in whichever shape the reader needs.

#: Filename extension and MIME type per format.
















#: Small on purpose: each finished job pins a multi-MB document until it is collected.
#: How long a "running" job stays eligible for idempotent reuse. Comfortably longer than a
#: full sweep (~4 minutes at 2000 nodes) and short enough that a dead worker frees its key.










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
        _logger.debug("api_analytics_aggregate: ignoring error", exc_info=True)
        app.logger.warning("analytics: NCCL rack discovery failed: %s", _e)

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
    scalar_rows: list[dict[str, object]] = []
    nemotron_rows: list[dict[str, object]] = []
    scaling_entries: dict[int, list[dict[str, object]]] = {}
    # Successful runs whose step/TFLOPs could not be averaged, keyed the same way. They get a
    # row with null means instead of being dropped: a 50-step multirack NeMo run used to pass
    # every gate, contribute its NCCL series to the chart, and then vanish from the tables with
    # no indication it had ever been considered.
    scaling_no_metrics: dict[int, list[str]] = {}

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

    size_to_vals: dict[int, list[float]] = {}
    for ser in series_by_rack.values():
        for p in ser:
            sz = int(p["size_bytes"])
            size_to_vals.setdefault(sz, []).append(float(p["bus_bw_gbps"]))
    sizes_sorted = sorted(size_to_vals.keys())
    median_bw = {sz: statistics.median(size_to_vals[sz]) for sz in sizes_sorted if size_to_vals[sz]}

    per_rack_series: list[dict[str, object]] = []
    for rack in sorted(series_by_rack.keys()):
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
        lf = next((r["log_file"] for r in scalar_rows if r["rack"] == rack), "")
        per_rack_series.append({"rack": rack, "log_file": lf, "bus_bw_gbps": bws, "pct_vs_median": pcts})

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
        nm = _pick_nemotron_row(parsed, "")
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
            "nccl_ar_series": {
                "sizes_bytes": sizes_sorted,
                "median_bus_bw_gbps": [round(median_bw[s], 4) for s in sizes_sorted],
                "per_rack": per_rack_series,
            },
            "nemotron": nemotron_rows,
        },
        "multirack": {
            "nccl_series_by_log": nccl_multirack,
            "nemotron_scaling": nemotron_scaling,
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


def _probe_log_json_via_rack_status(abs_path: Path) -> dict[str, object] | None:
    """Run ``rack_status.sh --probe-json`` for one log file (same nemo/nccl/squeue heuristics as the table)."""
    script = _script_path()
    if not _script_exists(script):
        return None
    ap = _rpath(abs_path)
    mvt = (_eget("RACK_MULTIRACK_VALIDATION_TEST") or "nccl_all_reduce").strip() or "nccl_all_reduce"
    vt_if_unset = mvt if _is_multirack_log_basename(ap.name) else "nccl_all_reduce,llm"
    env = _subprocess_env({"NO_COLOR": "1"}, validation_test_if_unset=vt_if_unset)
    _ensure_cmsh_rack_list_env_for_subprocess(env)
    # ``env=`` is LOCAL ONLY -- run_on_host drops it for an SSH target, which is how this
    # deployment runs the script. Without env_extra the remote never sees VALIDATION_TEST and
    # validation_test_env.sh falls back to its strictest suite
    # (all_reduce + alltoall + allgather + llm) applied to whichever single log is newest,
    # so racks that passed every stage still render red. Forward the acceptance knobs
    # explicitly; env_extra bypasses the FORWARD_ENV_PREFIXES allowlist.
    env_extra = {"NO_COLOR": "1", "VALIDATION_TEST": env.get("VALIDATION_TEST", vt_if_unset)}
    for _k in ("NEMO_TARGET_STEPS", "RACK_VALIDATION_CT_RANGE", "NVIS_RACK_ID"):
        _v = (env.get(_k) or "").strip()
        if _v:
            env_extra[_k] = _v
    try:
        proc = host_exec.run_on_host(
            [str(script), "--probe-json", str(ap)],
            cwd=str(ap.parent),
            env=env,
            env_extra=env_extra,
            timeout=max(60, _env_int("RACK_MULTIRACK_PROBE_TIMEOUT", 180)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        out = json.loads(raw.splitlines()[-1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


_LOG_ANALYSIS_CACHE_VERSION = 2
_LOG_ANALYSIS_CACHE_PATH = REPO_ROOT / "var" / "cache" / "log_analysis_cache.json"


def _glob_one_rack_nemo_logs_for_rack_status(log_dir: Path) -> list[Path]:
    """NEMO single-rack validation logs in log_dir root (not failed/, not NCCL-only, no Llama stdout)."""
    root = _rpath(log_dir)
    naming = _nvis_naming()
    out: list[Path] = []
    listing = [PurePosixPath(s) for s in host_exec.stat_dir(root, patterns=("*.txt",))]
    for p in sorted(listing, key=lambda x: x.name.lower()):
        if not _nemo_single_rack_log_basename(p.name):
            continue
        if naming.is_llama_slurm_stdout_basename(p.name):
            continue
        if _is_multirack_log_basename(p.name):
            continue
        out.append(p)
    return out


def _log_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    return (int(mtime_ns), int(st.st_size))


def _load_log_analysis_cache_entries() -> dict[str, dict[str, object]]:
    p = _LOG_ANALYSIS_CACHE_PATH
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            locked = False
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                pass
            try:
                raw = json.load(f)
            except Exception:
                _logger.debug("_load_log_analysis_cache_entries: ignoring error", exc_info=True)
                raw = {}
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") != _LOG_ANALYSIS_CACHE_VERSION:
        return {}
    ent = raw.get("entries")
    if not isinstance(ent, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    for k, v in ent.items():
        if isinstance(v, dict) and isinstance(k, str):
            out[k] = v
    return out


def _save_log_analysis_cache_entries(entries: dict[str, dict[str, object]]) -> None:
    p = _LOG_ANALYSIS_CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(
        {"version": _LOG_ANALYSIS_CACHE_VERSION, "entries": entries},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
            except OSError:
                pass
            try:
                f.write(blob)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            finally:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass
        os.replace(str(tmp), str(p))
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _slurm_running_job_ids_for_rack_status() -> set[str]:
    """Slurm job ids for SQUEUE_USER or USER (same idea as rack_status load_squeue_jobs)."""
    user = (os.environ.get("SQUEUE_USER") or os.environ.get("USER") or "root").strip()
    plat = _get_platform()
    try:
        proc = host_exec.run_slurm_on_host(
            plat.squeue_cmd(f"-u {shlex.quote(user)} -h -o %i"),
            timeout=max(5, _env_int("RACK_LOG_ANALYSIS_SQUEUE_TIMEOUT_SEC", 60)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    out: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        j = line.strip()
        if j.isdigit():
            out.add(j)
    return out


def _tmp_status_row_sort_key(row: tuple[str, str, int, int, int]) -> tuple[int, int, int, str, str]:
    rack, jid, _on, _nc, _ll = row
    m = re.match(r"^p(\d+)-r(\d+)$", rack.strip().lower())
    if m:
        return (int(m.group(1)), int(m.group(2)), int(jid) if jid.isdigit() else 999_999, rack, jid)
    if rack.strip() == "?":
        return (99_999, 99_999, int(jid) if jid.isdigit() else 999_999, rack, jid)
    return (50_000, 50_000, int(jid) if jid.isdigit() else 999_999, rack, jid)


def _maybe_build_tmp_status_from_log_cache(log_dir: Path) -> Path | None:
    """Build a tmp_status TSV and return its path for ``rack_status.sh --tmp-status``, or None."""
    if _truthy_env("RACK_LOG_ANALYSIS_CACHE_DISABLED"):
        return None
    extra = _extra_script_args()
    if any(a == "--tmp-status" for a in extra):
        return None
    paths = _glob_one_rack_nemo_logs_for_rack_status(log_dir)
    if not paths:
        return None
    max_files = max(0, _env_int("RACK_LOG_ANALYSIS_CACHE_MAX_FILES", 200))
    if len(paths) > max_files:
        return None

    running = _slurm_running_job_ids_for_rack_status()
    entries = _load_log_analysis_cache_entries()
    dirty = False
    rows: list[tuple[str, str, int, int, int]] = []
    alive: set[str] = set()

    for p in paths:
        key = str(p.resolve())
        alive.add(key)
        fp = _log_fingerprint(p)
        if fp is None:
            continue
        mtime_ns, size = fp
        hit = entries.get(key)
        nc: int | None = None
        ll: int | None = None
        if isinstance(hit, dict):
            try:
                if int(hit["mtime_ns"]) == mtime_ns and int(hit["size"]) == size:
                    nc = int(hit["nccl_ok"]) & 1
                    ll = int(hit["llm_ok"]) & 1
            except (KeyError, TypeError, ValueError):
                nc = ll = None
        if nc is None or ll is None:
            probe = _probe_log_json_via_rack_status(p) or {}
            nc = 1 if probe.get("ok") and probe.get("nccl_ok") else 0
            ll = 1 if probe.get("ok") and probe.get("llm_ok") else 0
            entries[key] = {"mtime_ns": mtime_ns, "size": size, "nccl_ok": nc, "llm_ok": ll}
            dirty = True
        jid = _jobid_from_nemo_log_basename(p.name) or "?"
        rack = _rack_id_from_log_basename(p.name) or "?"
        ongoing = 1 if jid in running else 0
        rows.append((rack, jid, ongoing, nc, ll))

    pruned = {k: v for k, v in entries.items() if k in alive}
    if len(pruned) != len(entries):
        entries = pruned
        dirty = True

    if dirty:
        _save_log_analysis_cache_entries(entries)

    rows.sort(key=_tmp_status_row_sort_key)
    _LOG_ANALYSIS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tpath_s = tempfile.mkstemp(
            prefix="rack_tmp_status_",
            suffix=".tsv",
            dir=str(_LOG_ANALYSIS_CACHE_PATH.parent),
            text=True,
        )
    except OSError:
        fd, tpath_s = tempfile.mkstemp(prefix="rack_tmp_status_", suffix=".tsv", text=True)
    os.close(fd)
    tpath = Path(tpath_s)
    try:
        with open(tpath, "w", encoding="utf-8") as wf:
            for rack, jid, on, nc, ll in rows:
                wf.write(f"{rack}\t{jid}\t{on}\t{nc}\t{ll}\n")
    except OSError:
        try:
            tpath.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return tpath


@app.route("/api/multirack-status")
def api_multirack_status():
    """List multirack NEMO/NCCL logs: async sinfo/scontrol + log probes (same pattern as single-rack native)."""
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    # Always jsonify failures: an uncaught AttributeError (e.g. PurePosixPath.resolve in
    # ssh/container mode) becomes Flask's HTML 500 page, and the SPA surfaces
    # "Unexpected token '<!DOCTYPE' is not valid JSON" in Results & logs on disk.
    try:
        payload = native_status.build_multirack_status(
            log_dir,
            _probe_log_json_via_rack_status,
            multirack_display_rack_from_basename,
            _multirack_physical_rack_ids,
        )
    except Exception as e:
        app.logger.exception("multirack-status failed for log_dir=%s", log_dir)
        return jsonify({"ok": False, "error": f"multirack-status failed: {e}"}), 500
    return jsonify(payload)






@app.route("/api/multirack-campaign")
def api_multirack_campaign():
    try:
        return jsonify(_multirack_campaign_snapshot())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        app.logger.exception("multirack campaign snapshot failed")
        return jsonify({"ok": False, "active": False, "error": str(exc)}), 503


_EXPORT_RACK_RE = re.compile(r"^(p\d+-r)(\d+)$", re.IGNORECASE)


def _export_float_cell(v: object) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _export_nodes_per_rack() -> int:
    n = _eget_int("RACK_EXPORT_NODES_PER_RACK", 18)
    return max(1, min(n, 65535))


def _export_parse_rack(rack: str) -> tuple[str, int] | None:
    m = _EXPORT_RACK_RE.match((rack or "").strip())
    if not m:
        return None
    pref = m.group(1).lower()
    try:
        num = int(m.group(2), 10)
    except ValueError:
        return None
    return pref, num


def _export_nccl_coalesce_max(nccl: list[dict], field: str) -> dict[str, float]:
    """One peak value per rack (max across duplicate job logs)."""
    out: dict[str, float] = {}
    for row in nccl:
        if not isinstance(row, dict):
            continue
        rk = str(row.get("rack") or "").strip()
        if not rk:
            continue
        v = _export_float_cell(row.get(field))
        if v is None:
            continue
        out[rk] = max(out.get(rk, float("-inf")), v)
    return {k: v for k, v in out.items() if v > float("-inf")}


def _export_llm_coalesce_latest_job(llm: list[dict]) -> dict[str, dict]:
    """Keep the row per rack with the highest numeric job_id."""
    best: dict[str, tuple[int, dict]] = {}
    for row in llm:
        if not isinstance(row, dict):
            continue
        rk = str(row.get("rack") or "").strip()
        if not rk:
            continue
        jid_raw = str(row.get("job_id") or "").strip()
        try:
            jk = int(jid_raw, 10)
        except ValueError:
            jk = -1
        prev = best.get(rk)
        if prev is None or jk >= prev[0]:
            best[rk] = (jk, row)
    return {k: v[1] for k, v in best.items()}


def _export_title_racks_flat(rack_ids: object) -> str:
    """Title listing racks that appear in the export (no synthetic multi-node spans)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for rid in rack_ids:
        s = str(rid).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        ordered.append(s)

    def sort_key(rid: str) -> tuple[str, int]:
        pr = _export_parse_rack(rid)
        if pr:
            return (pr[0].lower(), pr[1])
        return (rid.lower(), 0)

    ordered.sort(key=sort_key)
    if not ordered:
        return "Racks"
    return "Racks " + ", ".join(ordered)


def _export_formatted_metric_rows(values_by_rack: dict[str, float]) -> list[tuple[str, int, float]]:
    """One row per rack (single-rack logs only)."""
    parsed: list[tuple[str, int, str, float]] = []
    for rid, val in values_by_rack.items():
        pr = _export_parse_rack(rid)
        if pr:
            parsed.append((pr[0].lower(), pr[1], rid, val))
    parsed.sort(key=lambda x: (x[0], x[1]))
    if not parsed:
        return []
    npr = _export_nodes_per_rack()
    return [(rid, npr, val) for _a, _b, rid, val in parsed]


def _export_formatted_llm_rows(by_rack: dict[str, dict]) -> list[tuple[str, int, float, float, float]]:
    """One row per rack: Rack, Nodes, step_s, tflops, mfu (numeric for MFU cell formatting)."""
    tmp: dict[str, tuple[float, float, float]] = {}
    for rid, row in by_rack.items():
        st = _export_float_cell(row.get("step_s"))
        tf = _export_float_cell(row.get("tflops_per_gpu"))
        mf = _export_float_cell(row.get("mfu_percent"))
        if st is None and tf is None and mf is None:
            continue
        tmp[rid] = (st or 0.0, tf or 0.0, mf or 0.0)
    parsed: list[tuple[str, int, str, tuple[float, float, float]]] = []
    for rid, tup in tmp.items():
        pr = _export_parse_rack(rid)
        if pr:
            parsed.append((pr[0].lower(), pr[1], rid, tup))
    parsed.sort(key=lambda x: (x[0], x[1]))
    if not parsed:
        return []
    npr = _export_nodes_per_rack()
    return [(rid, npr, t[0], t[1], t[2]) for _a, _b, rid, t in parsed]


def _workbook_style_and_write_table(
    ws,
    *,
    title: str,
    headers: list[str],
    data_rows: list[list[object]],
    merge_to_col: int,
) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    title_fill = PatternFill("solid", fgColor="2F4F2F")
    hdr_fill = PatternFill("solid", fgColor="375623")
    data_fill = PatternFill("solid", fgColor="3A3D40")
    font_title = Font(color="FFFFFF", bold=True, size=12)
    font_hdr = Font(color="FFFFFF", bold=True, size=11)
    font_data = Font(color="E6EDF3", size=11)
    thin = Side(style="thin", color="5C6B7A")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center")

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=merge_to_col)
    c1 = ws.cell(row=1, column=1, value=title)
    c1.fill = title_fill
    c1.font = font_title
    c1.alignment = left
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col, value=h)
        cell.fill = hdr_fill
        cell.font = font_hdr
        cell.alignment = center
        cell.border = border
    for ri, row in enumerate(data_rows, start=3):
        for ci, val in enumerate(row, start=1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.fill = data_fill
            cell.font = font_data
            cell.border = border
            cell.alignment = center if ci > 1 else left


def _workbook_bytes_from_parser_export(parsed: dict) -> bytes:
    """Build .xlsx: NCCL sheets + llm match rack summary table layout (title + Rack / Nodes / …)."""
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError(
            "openpyxl is required for Excel export (pip install -r requirements.txt)"
        ) from e

    wb = Workbook()
    ws_ar = wb.active
    ws_ar.title = "nccl all_reduce"
    ws_aa = wb.create_sheet("nccl all_to_all")
    ws_ag = wb.create_sheet("nccl all_gather")
    ws_llm = wb.create_sheet("llm")

    nccl = parsed.get("nccl_peak_out_of_place_busbw_gbps")
    if not isinstance(nccl, list):
        nccl = []

    ar_map = _export_nccl_coalesce_max(nccl, "all_reduce_gbps")
    aa_map = _export_nccl_coalesce_max(nccl, "alltoall_gbps")
    ag_map = _export_nccl_coalesce_max(nccl, "all_gather_gbps")

    all_nccl_racks = sorted(set(ar_map) | set(aa_map) | set(ag_map))
    title_nccl = _export_title_racks_flat(all_nccl_racks)

    def nccl_body(rows: list[tuple[str, int, float]]) -> list[list[object]]:
        return [[a, b, round(c, 2)] for a, b, c in rows]

    ar_rows = _export_formatted_metric_rows(ar_map)
    aa_rows = _export_formatted_metric_rows(aa_map)
    ag_rows = _export_formatted_metric_rows(ag_map)

    _workbook_style_and_write_table(
        ws_ar,
        title=title_nccl,
        headers=["Rack", "Nodes", "NCCL All Reduce GB/s"],
        data_rows=nccl_body(ar_rows),
        merge_to_col=3,
    )
    _workbook_style_and_write_table(
        ws_aa,
        title=title_nccl,
        headers=["Rack", "Nodes", "NCCL All to All GB/s"],
        data_rows=nccl_body(aa_rows),
        merge_to_col=3,
    )
    _workbook_style_and_write_table(
        ws_ag,
        title=title_nccl,
        headers=["Rack", "Nodes", "NCCL All Gather GB/s"],
        data_rows=nccl_body(ag_rows),
        merge_to_col=3,
    )

    llm = parsed.get("nemotron_5h_56b_train_window")
    if not isinstance(llm, list):
        llm = []
    llm_by = _export_llm_coalesce_latest_job(llm)
    _llm_base = _export_title_racks_flat(llm_by.keys())
    _ct_inner = _validation_ct_inner()
    llm_title_str = (
        f"{_llm_base} · ct[{_ct_inner}]" if _llm_base != "Racks" else f"Racks ct[{_ct_inner}]"
    )
    llm_rows_fmt = _export_formatted_llm_rows(llm_by)
    llm_data: list[list[object]] = []
    for rack, nodes, st, tf, mf in llm_rows_fmt:
        mf_s = f"{mf:.1f}%" if mf is not None else ""
        llm_data.append([rack, nodes, round(st, 2), round(tf, 2), mf_s])

    _workbook_style_and_write_table(
        ws_llm,
        title=llm_title_str,
        headers=["Rack", "Nodes", "Steps [s]", "Per GPU TFLOPs", "MFU [%]"],
        data_rows=llm_data,
        merge_to_col=5,
    )

    for ws in (ws_ar, ws_aa, ws_ag, ws_llm):
        for col in range(1, ws.max_column + 1):
            letter = get_column_letter(col)
            maxlen = 10
            for row in range(1, ws.max_row + 1):
                v = ws.cell(row=row, column=col).value
                if v is not None:
                    maxlen = max(maxlen, min(52, len(str(v))))
            ws.column_dimensions[letter].width = maxlen + 2.0

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _parser_excel_report_copy_dir() -> Path:
    """Directory for an on-disk copy of each successful parser Excel export.

    Override with ``RACK_PARSER_EXCEL_REPORT_DIR`` (absolute or ``~`` path; directory is created).

    Default: ``<RACK_BUG_REPORT_REMOTE_DIR>/parser-excel`` (typically ``/home/cmsupport/reports/parser-excel``).
    """
    raw = (_eget("RACK_PARSER_EXCEL_REPORT_DIR") or "").strip()
    if raw:
        # An exec-host path: expanduser/resolve would answer from this process's
        # filesystem and home directory, which is not where the reports live.
        return _rpath(raw)
    return _bug_report_local_base_path() / "parser-excel"


@app.route("/api/parser-export-excel")
def api_parser_export_excel():
    """Run parser.sh --json on export-selected logs (log root only; no ``failed/``; one successful 1Rack log per rack with nemo=done), then .xlsx."""
    if os.environ.get("RACK_PARSER_EXPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "export disabled (RACK_PARSER_EXPORT_DISABLED)"}), 403
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    paths = list_log_txt_paths_for_export(log_dir, include_multirack_under_failed=False)
    if not paths:
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"no exportable .txt logs matching {_parser_export_glob()!r} under {log_dir} "
                    "(Excel: log_dir root only, no failed/; NEMO 1Rack: one successful log per rack with "
                    "nemo=done in rack_status, unless RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER is set)"
                ),
            }
        ), 404
    parser = _parser_path()
    if not _script_exists(parser):
        return jsonify({"ok": False, "error": f"parser.sh not found: {parser}"}), 500
    root = _rpath(log_dir)
    rel_args: list[str] = []
    for p in paths:
        # Already absolute exec-host paths from the listing; .resolve() would ask
        # the *local* filesystem about a directory that may live elsewhere.
        try:
            rel_args.append(str(PurePosixPath(str(p)).relative_to(root)))
        except ValueError:
            rel_args.append(str(p))
    env = _subprocess_env({"NO_COLOR": "1"})
    cmd = ["bash", str(parser), *_parser_extra_args(), "--json", *rel_args]
    timeout = max(60, _env_int("RACK_PARSER_EXPORT_TIMEOUT", 900))
    try:
        # parser.sh reads the logs, so it has to run where they are: the logs role's
        # host, with the log dir as cwd. A local subprocess found neither.
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(root),
            env=env,
            timeout=timeout,
            role="logs",
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"parser.sh --json timed out after {timeout}s"}), 504
    except Exception as e:
        _logger.debug("api_parser_export_excel: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    if proc.returncode != 0:
        return jsonify(
            {
                "ok": False,
                "error": "parser.sh --json exited non-zero",
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip(),
                "stdout_head": (proc.stdout or "")[:4000],
            }
        ), 502
    raw_out = (proc.stdout or "").strip()
    if not raw_out:
        return jsonify(
            {
                "ok": False,
                "error": "parser.sh produced empty JSON on stdout",
                "stderr": (proc.stderr or "").strip(),
            }
        ), 502
    try:
        parsed = json.loads(raw_out)
    except json.JSONDecodeError:
        return jsonify(
            {
                "ok": False,
                "error": "parser did not emit valid JSON on stdout",
                "stderr": (proc.stderr or "").strip(),
                "stdout_head": raw_out[:4000],
            }
        ), 502
    if not isinstance(parsed, dict):
        return jsonify({"ok": False, "error": "parser JSON root must be an object"}), 502
    try:
        body = _workbook_bytes_from_parser_export(parsed)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fname = f"rack-parser-export-{stamp}.xlsx"
    hdrs: dict[str, str] = {
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "X-Export-Log-Count": str(len(paths)),
    }
    try:
        dest = _parser_excel_report_copy_dir() / fname
        # The reports directory belongs to the execution host, so the copy travels
        # through the connection; a local write would land inside the container.
        err = host_exec.write_bytes(dest, body)
        if err:
            raise OSError(err)
        hdrs["X-Export-Report-Copy"] = str(dest)
    except OSError as e:
        _logger.warning("parser Excel: could not write report copy: %s", e)
        hdrs["X-Export-Report-Copy-Error"] = str(e)[:500]
    return Response(
        body,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=hdrs,
    )


@app.route("/api/parser-export-csv")
def api_parser_export_csv_legacy():
    """Backward-compatible redirect; exports are now multi-sheet Excel."""
    return redirect(url_for("api_parser_export_excel", **request.args), code=307)


@app.route("/api/export-logs-zip")
def api_export_logs_zip():
    """Zip exportable matching .txt (same rules as parser Excel export: root + multirack under failed/)."""
    if os.environ.get("RACK_LOG_EXPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "log zip export disabled (RACK_LOG_EXPORT_DISABLED)"}), 403
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    paths = list_log_txt_paths_for_export(log_dir)
    if not paths:
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"no exportable .txt logs matching {_parser_export_glob()!r} under {log_dir} "
                    "(root + multirack under failed/; NEMO 1Rack: one successful log per rack with "
                    "nemo=done in rack_status, unless RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER is set)"
                ),
            }
        ), 404
    root = _rpath(log_dir)
    # The logs live on the execution host, so each one is pulled through the
    # connection and written from memory: zipfile.write() resolves and opens the
    # path locally, which finds nothing when the log dir is on the head node.
    cap = max(1, _env_int("RACK_LOG_EXPORT_MAX_BYTES", 64 * 1024 * 1024))
    bio = io.BytesIO()
    written = 0
    skipped: list[str] = []
    try:
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                text, meta = host_exec.read_text(p, whole_bytes=cap)
                if meta.get("read_error"):
                    skipped.append(p.name)
                    continue
                try:
                    arc = str(PurePosixPath(str(p)).relative_to(root))
                except ValueError:
                    arc = p.name
                zf.writestr(arc, text)
                written += 1
    except (OSError, zipfile.BadZipFile) as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not written:
        return jsonify(
            {"ok": False, "error": f"could not read any of the {len(paths)} matching logs"}
        ), 500
    body = bio.getvalue()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fname = f"rack-logs-export-{stamp}.zip"
    return Response(
        body,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Export-Log-Count": str(written),
            "X-Export-Skipped": str(len(skipped)),
        },
    )


_NCCL_DEBUG_LEVELS = ("VERSION", "WARN", "INFO", "TRACE")

# Custom launch env vars (UI "Extra environment variables" field). A key must be a valid
# POSIX-ish env name; values are single-line and length-capped; the count is bounded. These
# guardrails keep a well-formed KEY=VALUE map without allowing injection of newlines / huge
# payloads into the job environment. Forwarded as env_extra (bypasses the SSH allowlist) and
# chained to the container via each launcher's --export=ALL.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_MAX_VARS = 32
_ENV_MAX_VALUE_LEN = 2048

# Upper bound for the launch dialog's NEMO step selector. The UI offers 50 (perf) and 2010
# (soak); the cap only rejects absurd hand-crafted payloads, it does not pin the choices.
_NEMO_MAX_STEPS_LIMIT = 1_000_000


def _custom_env_from_body(body: dict) -> dict[str, str]:
    """Validated user-supplied ``env`` map from the POST body ({"KEY": "VALUE", ...}).

    Silently drops entries with an invalid key, a multi-line/oversized value, or beyond the
    per-launch cap (the UI validates too, so a bad row just doesn't get applied).
    """
    extra = body.get("env")
    if not isinstance(extra, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in extra.items():
        key = str(k).strip()
        if not _ENV_KEY_RE.match(key):
            continue
        val = str(v)
        if "\n" in val or "\r" in val or len(val) > _ENV_MAX_VALUE_LEN:
            continue
        out[key] = val
        if len(out) >= _ENV_MAX_VARS:
            break
    return out


def _launch_env_from_body(body: dict) -> dict[str, str]:
    """Per-launch env knobs parsed from the POST body: NCCL debug level + custom env vars.

    ``nccl_debug`` accepts VERSION/WARN/INFO/TRACE (case-insensitive). ``off``/``default``/
    empty/absent means "leave the script default" (no override). We set BOTH
    ``NVIS_NCCL_DEBUG`` (honored by the NEMO launchers, which otherwise hard-code the
    level) and raw ``NCCL_DEBUG`` (read directly by ``nccl_allreduce.sbatch`` and the
    portable Stage-1 launcher) so the toggle reaches every NCCL/NEMO launch path.

    ``env`` is a user-supplied KEY=VALUE map from the launch dialog (e.g.
    ``OMPI_MCA_coll_hcoll_enable=0``). Explicit keys win over the NCCL-debug defaults.

    All of these are threaded to the worker as ``env_extra`` so they reach the remote
    cluster over SSH (the forward allowlist is bypassed for explicit ``env_extra`` keys)
    and are forwarded into the container by each launcher's ``--export=ALL``.
    """
    out: dict[str, str] = {}
    raw = body.get("nccl_debug")
    if raw is not None:
        lvl = str(raw).strip().upper()
        if lvl and lvl not in ("OFF", "DEFAULT", "NONE") and lvl in _NCCL_DEBUG_LEVELS:
            out["NVIS_NCCL_DEBUG"] = lvl
            out["NCCL_DEBUG"] = lvl
    # NEMO training length. NVIS_NEMO_MAX_STEPS is what the job runs; NEMO_TARGET_STEPS is the
    # minimum DECLARED total the log labeller requires before calling a run finished
    # (lib/llm_label.inc.sh, default 2010). They MUST move together — a 50-step run left at the
    # 2010 threshold completes but is never labelled done.
    steps = body.get("max_steps")
    if isinstance(steps, bool):
        steps = None
    if isinstance(steps, (int, float)) and 1 <= int(steps) <= _NEMO_MAX_STEPS_LIMIT:
        out["NVIS_NEMO_MAX_STEPS"] = str(int(steps))
        out["NEMO_TARGET_STEPS"] = str(int(steps))
    # User-supplied vars win over the debug defaults above.
    custom = _custom_env_from_body(body)
    out.update(custom)
    # Advertise the exact set of user-supplied keys so multi-node launchers can forward
    # precisely these to remote ranks. nccl_allreduce.sbatch only propagates an explicit
    # -x allowlist to orted on nodes other than the mpirun host; without this, a custom
    # var (e.g. NCCL_NET_PLUGIN=none) would apply to local ranks only.
    if custom:
        out["NVIS_EXTRA_ENV_KEYS"] = " ".join(sorted(custom))
    return out


def _nccl_version_cli(body: dict) -> list[str] | tuple[object, int]:
    """Validate ``body['nccl_version']`` → ``["--nccl-version", v]`` for run_nccl_mpi.sh.

    Shared by the direct NCCL launch surfaces (single-rack Stage-1 + cross-rack) so the
    version selector behaves identically. Returns ``[]`` when unset/default, or an
    ``(jsonify(error), 400)`` tuple on an invalid token. "system"/"default" = host build
    (2.28.9); "X.Y.Z" = a vetted NFS build loaded via LD_LIBRARY_PATH.
    """
    ver = (body.get("nccl_version") or _eget("RACK_NCCL_MPI_VERSION") or "").strip()
    if not ver:
        return []
    if not re.match(r"^(system|default|[0-9]+(\.[0-9]+){1,3})$", ver):
        return (jsonify({"ok": False, "error": f"invalid nccl_version: {ver!r}"}), 400)
    return ["--nccl-version", ver]


def _nccl_rounds_from_body(body: dict, default: int | None) -> int | None:
    """NCCL sweep rounds from the launch body (``iters``), clamped to 1..50.

    ``default`` is returned when the body omits ``iters`` (single-rack Stage-1 passes 5 so the
    acceptance sweep runs 5 times; the x-rack path passes ``None`` to leave the sbatch default).
    """
    raw = body.get("iters")
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, 50))


def _prepare_run_validation(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for sync and async validation. Returns (prep, None) or (None, (jsonify(...), code))."""
    if os.environ.get("RACK_VALIDATION_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "run-validation disabled (RACK_VALIDATION_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _validation_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"validation script not found: {script}"}), 500)
    # Pass the resolved rack id as $2 so the script names the log …-1Rack-<rack>-… correctly for any
    # naming convention (Bright p<N>-r<N> or Boston bos01-b05); the script's own regex is Bright-only.
    cmd = ["bash", str(script), target, rk]
    display_cmd = "./" + script.name + " " + shlex.quote(target) + " " + shlex.quote(rk)
    timeout = max(30, _job_env_int("RACK_VALIDATION_TIMEOUT", 600))
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            # Settings -> job env values must reach run_llmb_nccl.sh, which otherwise falls back
            # to its built-in defaults (/home/nvis/dgxc-benchmarking, /home/nvis/llmb_venv) —
            # the wrong user on this cluster, and paths that do not exist. Mirrors how the
            # ClusterKit prepare pulls its RACK_CLUSTERKIT_* keys via _eget.
            "env_updates": {
                **{
                    k: v
                    for k in ("LLMB_INSTALL", "LLMB_REPO", "LLMB_VENV", "NVIS_WORKSPACE_DIR")
                    if (v := (_eget(k) or "").strip())
                },
                # Launch-dialog vars win over the stored Settings values.
                **_launch_env_from_body(body),
            },
        },
        None,
    )


@app.route("/api/run-validation", methods=["POST"])
def api_run_validation():
    """Run singlerack validation script with one target, e.g. p26-r05-ct[01-18] (cwd = log_dir)."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_validation(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    target = str(prep["target"])
    rk = str(prep["rk"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    try:
        with _track_sync_op(rk, "validation", _operator_from_request()):
            # Privileged "script" target (root@slogin): pdsh-rsync to root-owned /raid/data
            # + sbatch. Routes via host_exec so SSH mode runs on the cluster, not locally.
            proc = host_exec.run_on_host(
                cmd,
                timeout=timeout,
                cwd=str(log_dir),
                env=_subprocess_env(),
                profile="script",
            )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "validation script timed out", "target": target}), 504
    except Exception as e:
        _logger.debug("api_run_validation: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e), "target": target}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@app.route("/api/run-validation-async", methods=["POST"])
def api_run_validation_async():
    """Start validation in a background thread; poll GET /api/run-validation-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_validation(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) and _eu else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    val_tool, val_stage = _validation_tool_and_stage()
    _record_validation_activity(
        val_tool,
        stage=val_stage,
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    if env_merged is None:
        env_merged = {}
    env_merged = {**env_merged, "NVIS_ACTIVITY_ID": job_id}
    with _validation_jobs_lock:
        _validation_jobs[job_id] = {
            "job_id": job_id,
            "kind": "validation",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_validation_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"env_updates": env_merged},
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
        }
    )


def _prepare_run_nemo_xrack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Parse POST body for ``run-nemo-xrack.sh`` (comma-separated rack-ct[inner] specs)."""
    if os.environ.get("RACK_NEMO_XRACK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "multinode NEMO launch disabled (RACK_NEMO_XRACK_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    racks_raw = body.get("racks")
    if not isinstance(racks_raw, list) or len(racks_raw) < 1:
        return None, (jsonify({"ok": False, "error": "racks must be a non-empty array of rack ids"}), 400)
    inner = _validation_ct_inner()
    naming = _nvis_naming()
    specs: list[str] = []
    seen: set[str] = set()
    for item in racks_raw:
        rk = _normalize_rack_id(str(item).strip())
        if rk in seen:
            continue
        seen.add(rk)
        # Build the tray spec from NVIS_CT_ID, never a literal "-ct". The joiner is a naming
        # property: Bright is <rack>-ct<NN>, but Nscale TX is <rack>-<NN>, so the hardcoded form
        # produced ink50-dh01-h02-ct[11-18,...] — hosts that exist in neither DNS nor Slurm.
        # pdsh then failed ssh 255 on every node and sbatch rejected the list with
        # "Invalid node name specified". Every other tray-spec site already uses ct_blob.
        specs.append(naming.ct_blob(rk, inner))
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid rack ids"}), 400)
    script = _nemo_xrack_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"run-nemo-xrack script not found: {script}"}), 500)
    arg = ",".join(specs)
    cmd = ["bash", str(script), arg]
    display_cmd = "./" + script.name + " " + shlex.quote(arg)
    timeout = max(300, _job_env_int("RACK_NEMO_XRACK_TIMEOUT", 7200))
    return (
        {
            "log_dir": log_dir,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "arg": arg,
            "racks": sorted(seen),
            "env_updates": _launch_env_from_body(body),
        },
        None,
    )


@app.route("/api/run-nemo-xrack-async", methods=["POST"])
def api_run_nemo_xrack_async():
    """Start ``run-nemo-xrack.sh`` in the background; poll ``/api/run-nemo-xrack-status``.

    Process cwd is *log_dir* (same as ``/api/run-validation``) so Slurm batch stdout files land there.
    """
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_nemo_xrack(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    arg = str(prep["arg"])
    racks = [str(r) for r in prep.get("racks") or []]
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) and _eu else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    # Same as run-validation: cwd=log_dir so Slurm --output=… writes under the UI log dir.
    cwd = _host_job_cwd(log_dir)
    with _xrack_jobs_lock:
        _xrack_jobs[job_id] = {
            "job_id": job_id,
            "kind": "xrack",
            "operator": operator,
            "rack": "multinode",
            "racks": racks,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
            "xrack_arg": arg,
        }
    _record_validation_activity(
        "nemo-xrack",
        stage="multirack",
        scope="multirack",
        rack=racks[0] if racks else "",
        racks=racks,
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    threading.Thread(
        target=_xrack_worker,
        args=(job_id, cmd, cwd, timeout, display_cmd),
        kwargs={"env_updates": env_merged},
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "command_display": display_cmd,
        }
    )


def _prepare_run_llama(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Parse POST body for ``run-llama.sh`` (nodeset string, cwd = log_dir)."""
    if os.environ.get("RACK_LLAMA_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "LLAMA launch disabled (RACK_LLAMA_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        nodes_spec = _normalize_llama_nodes_spec(body.get("nodes_spec"))
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _run_llama_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"run-llama.sh not found: {script}"}), 500)
    inner = f"module load slurm && exec bash {shlex.quote(str(script))} {shlex.quote(nodes_spec)}"
    cmd = ["bash", "-lc", inner]
    display_cmd = f"module load slurm && ./{script.name} {shlex.quote(nodes_spec)}"
    timeout = max(120, _job_env_int("RACK_LLAMA_TIMEOUT", 7200))
    return (
        {
            "log_dir": log_dir,
            "rack": rk,
            "nodes_spec": nodes_spec,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
        },
        None,
    )


def _prepare_run_exemplar_llama(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Build the exact 512-GPU GB300 Llama 3.1 405B Exemplar launch.

    Each selected NVL72 rack contributes 16 tray hosts (64 GPUs), so an accepted
    request is exactly eight racks / 128 hosts / 512 GPUs. The wrapper repeats the
    node-count and Slurm-state checks immediately before ``llmb-run submit``.
    """
    if _truthy_env("RACK_EXEMPLAR_LLAMA_DISABLED"):
        return None, (
            jsonify({"ok": False, "error": "Exemplar Llama launch disabled (RACK_EXEMPLAR_LLAMA_DISABLED)"}),
            403,
        )

    racks_raw = body.get("racks")
    if not isinstance(racks_raw, list):
        return None, (jsonify({"ok": False, "error": "racks must be an array of eight rack ids"}), 400)
    racks: list[str] = []
    seen: set[str] = set()
    try:
        for item in racks_raw:
            rack = _normalize_rack_id(str(item).strip())
            if rack not in seen:
                seen.add(rack)
                racks.append(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if len(racks) != 8:
        return None, (
            jsonify({"ok": False, "error": f"the 512-GPU Exemplar gate requires exactly 8 racks; got {len(racks)}"}),
            400,
        )

    dtype = str(body.get("dtype") or "nvfp4").strip().lower()
    if dtype not in ("fp8", "nvfp4"):
        return None, (jsonify({"ok": False, "error": "dtype must be fp8 or nvfp4"}), 400)
    image_profile = str(body.get("image_profile") or "nccl2.30.7").strip().lower()
    if image_profile not in ("nccl2.30.7", "stock"):
        return None, (
            jsonify({"ok": False, "error": "image_profile must be nccl2.30.7 or stock"}),
            400,
        )
    tuning = str(body.get("tuning") or "baseline").strip().lower()
    if tuning not in ("baseline", "gb300"):
        return None, (jsonify({"ok": False, "error": "tuning must be baseline or gb300"}), 400)

    inner = (_eget("RACK_EXEMPLAR_LLAMA_CT_RANGE", "11-18,28-35") or "11-18,28-35").strip()
    if not re.fullmatch(r"\d{1,3}(?:-\d{1,3})?(?:,\d{1,3}(?:-\d{1,3})?)*", inner):
        return None, (
            jsonify({"ok": False, "error": f"invalid RACK_EXEMPLAR_LLAMA_CT_RANGE: {inner!r}"}),
            500,
        )
    naming = _nvis_naming()
    nodelist = ",".join(naming.ct_blob(rack, inner) for rack in sorted(racks))

    script = _run_exemplar_llama_script_path()
    if not _script_exists(script):
        return None, (
            jsonify({"ok": False, "error": f"run-exemplar-llama script not found: {script}"}),
            500,
        )
    dry_run = bool(body.get("dry_run"))
    cmd = [
        "bash",
        str(script),
        "--dtype",
        dtype,
        "--nodelist",
        nodelist,
        "--image",
        image_profile,
        "--tuning",
        tuning,
    ]
    if dry_run:
        cmd.append("--dry-run")
    display_cmd = shlex.join(cmd)
    llmb_install = (_eget("RACK_LLMB_INSTALL", "/home/cmods/workspace/llmb") or "/home/cmods/workspace/llmb").strip()
    log_dir = f"{llmb_install.rstrip('/')}/workloads/pretrain_llama3.1/experiments"
    timeout = max(120, _job_env_int("RACK_EXEMPLAR_LLAMA_TIMEOUT", 900))
    return (
        {
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "cwd": llmb_install,
            "log_dir": log_dir,
            "racks": sorted(racks),
            "nodelist": nodelist,
            "dtype": dtype,
            "image_profile": image_profile,
            "tuning": tuning,
            "dry_run": dry_run,
        },
        None,
    )


@app.route("/api/run-exemplar-llama-async", methods=["POST"])
def api_run_exemplar_llama_async():
    """Submit Llama 3.1 405B Exemplar pretraining across eight selected racks."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_exemplar_llama(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    racks = [str(r) for r in prep["racks"]]
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    display_cmd = str(prep["display_cmd"])
    log_dir = str(prep["log_dir"])
    dry_run = bool(prep["dry_run"])
    _record_validation_activity(
        "llama-exemplar",
        stage="stage2",
        scope="multirack",
        rack=racks[0],
        racks=racks,
        log_dir=log_dir,
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
        dry_run=dry_run,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "llama-exemplar",
            "operator": operator,
            "rack": "8 racks",
            "racks": racks,
            "nodelist": prep["nodelist"],
            "gpu_count": 512,
            "dtype": prep["dtype"],
            "image_profile": prep["image_profile"],
            "tuning": prep["tuning"],
            "dry_run": dry_run,
            "log_dir": log_dir,
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    env_updates = {"NVIS_ACTIVITY_ID": job_id}
    threading.Thread(
        target=_stress_worker,
        args=(job_id, prep["cmd"], str(prep["cwd"]), int(prep["timeout"]), display_cmd),
        kwargs={
            "error_label": "run-exemplar-llama script",
            "profile": "script",
            "persist": True,
            "env_updates": env_updates,
        },
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "racks": racks,
            "nodelist": prep["nodelist"],
            "gpu_count": 512,
            "dtype": prep["dtype"],
            "image_profile": prep["image_profile"],
            "tuning": prep["tuning"],
            "dry_run": dry_run,
            "command_display": display_cmd,
        }
    )


@app.route("/api/run-llama-async", methods=["POST"])
def api_run_llama_async():
    """Start ``run-llama.sh`` in the background; poll ``/api/run-llama-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_llama(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    rk = str(prep["rack"])
    nodes_spec = str(prep["nodes_spec"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    cwd = _host_job_cwd(log_dir)
    _record_validation_activity(
        "llama",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=cwd,
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _llama_jobs_lock:
        _llama_jobs[job_id] = {
            "job_id": job_id,
            "kind": "llama",
            "operator": operator,
            "rack": rk,
            "log_dir": cwd,
            "nodes_spec": nodes_spec,
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_llama_worker,
        args=(job_id, cmd, cwd, timeout, display_cmd),
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "command_display": display_cmd,
        }
    )


def _sn_llama_txt_paths_for_rack(log_dir: Path, rack: str, *, exclude: Path | None = None) -> list[Path]:
    """``log_dir/sn-llama/*.txt`` whose names suggest LLAMA and match *rack* (case-insensitive substring on rack id tokens)."""
    d = _rpath(log_dir) / "sn-llama"
    needles: list[str] = list(_nvis_naming().find_rack_ids_in_string(rack or ""))
    if not needles:
        t = (rack or "").strip().lower()
        if t:
            needles = [t]
    ex = str(_rpath(exclude)) if exclude is not None else None
    out: list[Path] = []
    # One listing on the execution host; mtime arrives with it, so the newest-first
    # ordering below needs no per-file stat.
    entries = host_exec.stat_dir(d, patterns=("*.txt",))
    paths = [
        PurePosixPath(s)
        for s in sorted(entries, key=lambda s: entries[s].mtime, reverse=True)
    ]
    cap = max(1, _env_int("RACK_PARSER_SN_LLAMA_MAX_FILES", 25))
    for p in paths:
        if len(out) >= cap:
            break
        if ex is not None and str(p) == ex:
            continue
        nl = p.name.casefold()
        if "llama" not in nl:
            continue
        # Needles from ``find_rack_ids_in_string`` keep canonical casing (e.g. CBR…); basename is matched case-insensitively.
        if needles and not any(n.casefold() in nl for n in needles):
            continue
        out.append(p)
    return list(reversed(out))


def _prepare_run_mnubergemm_stress(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for async mnubergemm stress. Returns (prep, None) or (None, (jsonify(...), code))."""
    if os.environ.get("RACK_MNUBERGEMM_STRESS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "run-stress disabled (RACK_MNUBERGEMM_STRESS_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _mnubergemm_stress_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"stress script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(120, _env_int("RACK_MNUBERGEMM_STRESS_TIMEOUT", 86400))
    ws = _nvis_workspace_dir_effective()
    cuda_m = (_eget("RACK_MNUBERGEMM_CUDA_MAJOR") or _eget("RACK_NVLOOM_CUDA_MAJOR") or _eget("CUDA_MAJ") or "13").strip()
    if cuda_m not in ("12", "13"):
        cuda_m = "13"
    env_updates: dict[str, str] = {
        "WORKSPACE_DIR": ws,
        "NVIS_WORKSPACE_DIR": ws,
        "TOPDIR": ws,
        "NVIS_BIN_SCRIPTS": str(_rpath(SCRIPTS_DIR)),
        "CUDA_MAJ": cuda_m,
        "NVIS_SYSINFO_SH": str(_sysinfo_script_path()),
        "NVIS_CHECKER_SH": str(_rpath(SCRIPTS_DIR / "checker.sh")),
    }
    for k in ("NVIS_HPCX_ROOT", "NVIS_NCCL_PATH", "RACK_MNUBERGEMM_PARTITION"):
        v = (_eget(k) or "").strip()
        if v:
            env_updates[k] = v
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _llmb_env_updates(body: dict) -> dict[str, str]:
    """Env for the llmb-run launchers.

    ``_eget()`` reads Settings out of ``var/cache/job_env_overrides.json``, which is NOT part
    of ``os.environ`` and therefore does NOT cross the SSH hop to the head node. Without this,
    ``run_llmb_nccl.sh`` silently falls back to its built-in ``/home/nvis/...`` defaults and
    cannot find llmb-run. Forward the workspace keys explicitly.
    """
    out: dict[str, str] = {}
    for key in ("LLMB_INSTALL", "LLMB_REPO", "LLMB_VENV", "LLMB_RUN", "NVIS_WORKSPACE_DIR"):
        val = (_eget(key) or "").strip()
        if val:
            out[key] = val
    out.update(_launch_env_from_body(body))
    return out


def _prepare_run_nccl_fullrack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for the standalone Stage-1 NCCL benchmark.

    Converged onto the SAME direct path as the cross-rack test (``run_nccl_mpi.sh`` →
    ``nccl_allreduce.sbatch``: host nccl-tests + mpirun, NCCL over the IB rails, library
    version selectable via LD_LIBRARY_PATH). ``--app-log-rack`` names the Slurm output
    ``<user>-NCCL-1Rack-<rack>-<n>N-<jobid>.txt`` so the dashboard row + parser flip the
    rack's Stage-1 NCCL status, exactly as the legacy portable launcher did. Uses the
    ``script`` profile (root@slogin) like the other NCCL launchers.
    """
    if os.environ.get("RACK_NCCL_FULLRACK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "NCCL benchmark disabled (RACK_NCCL_FULLRACK_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    tgt_ov_s = tgt_ov.strip() if isinstance(tgt_ov, str) else ""
    # Resolve the node target. When the UI sends an explicit node selection we submit those
    # exact trays via ``--nodelist`` (so e.g. a rack minus a down/faulted node runs on 17);
    # with no selection we pass the rack id and let run_nccl_mpi.sh resolve the full rack.
    try:
        target = validation_target_for_rack(rack, tgt_ov_s or None)
    except ValueError:
        target = rk
    # Stage-1 runs through llmb-run (run_llmb_nccl.sh), not raw sbatch. Operator ask
    # 2026-07-28: "fix stage 1 NCCL to use llmb run - no more sbatch - only for MNUberGEMM
    # everything else is llmbrun". One llmb submit runs the whole pre-generated testset
    # (24 tests: all_reduce, all_gather, reduce_scatter, alltoall, sendrecv), so the
    # per-collective knobs below (--size-max / --nccl-version / --rounds) do not apply --
    # the sweep is defined by llmb_repo/nccl/config/testset.toml and baked into launch.sh.
    # Set RACK_NCCL_FULLRACK_LAUNCHER=mpi to fall back to run_nccl_mpi.sh + nccl_allreduce.sbatch.
    launcher = (_eget("RACK_NCCL_FULLRACK_LAUNCHER") or "llmb").strip().lower()
    use_llmb = launcher not in ("mpi", "sbatch", "direct", "nccl_mpi")
    script = _run_llmb_nccl_script_path() if use_llmb else _run_nccl_mpi_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NCCL launcher not found: {script}"}), 500)
    env_updates: dict[str, str] = {"RACK_JOB_LOG_DIR": str(log_dir), "NVIS_RACK": rk}

    if use_llmb:
        cmd = ["bash", str(script)]
        reservation = (body.get("reservation") or _eget("RACK_LLMB_NCCL_RESERVATION") or "").strip()
        if reservation:
            cmd += ["--reservation", reservation]
        if bool(body.get("dry_run")):
            cmd.append("--dry-run")
        # Explicit tray selection -> --nodelist; otherwise let the script expand the rack id.
        if tgt_ov_s:
            cmd += ["--nodelist", target]
            node_arg_display = " --nodelist " + shlex.quote(target)
        else:
            cmd.append(rk)
            node_arg_display = " " + shlex.quote(rk)
        display_cmd = "./" + script.name + (
            (" --reservation " + shlex.quote(reservation)) if reservation else ""
        ) + (" --dry-run" if body.get("dry_run") else "") + node_arg_display
        timeout = max(120, _job_env_int("RACK_LLMB_NCCL_TIMEOUT", 14400))
        env_updates.update(_llmb_env_updates(body))
    else:
        # Cap the all_reduce sweep. Accept the legacy `end`/RACK_NCCL_FULLRACK_END knobs too.
        size_max = (
            body.get("size_max") or body.get("end") or _eget("RACK_NCCL_MPI_SIZE_MAX")
            or _eget("RACK_NCCL_FULLRACK_END") or "16G"
        ).strip()
        cmd = ["bash", str(script), "--app-log-rack", rk]
        if size_max:
            cmd += ["--size-max", size_max]
        vc = _nccl_version_cli(body)
        if isinstance(vc, tuple):
            return None, vc
        cmd += vc
        nccl_version = vc[1] if vc else ""
        # Stage-1 acceptance runs the sweep 5× by default; the UI modal can override via ``iters``.
        rounds = _nccl_rounds_from_body(body, 5) or 5
        cmd += ["--rounds", str(rounds)]
        if tgt_ov_s:
            cmd += ["--nodelist", target]
            node_arg_display = " --nodelist " + shlex.quote(target)
        else:
            cmd.append(rk)
            node_arg_display = " " + shlex.quote(rk)
        display_cmd = "./" + script.name + " --app-log-rack " + shlex.quote(rk) + (
            (" --size-max " + shlex.quote(size_max)) if size_max else ""
        ) + (
            (" --nccl-version " + shlex.quote(nccl_version)) if nccl_version else ""
        ) + " --rounds " + str(rounds) + node_arg_display
        timeout = max(120, _job_env_int("RACK_NCCL_FULLRACK_TIMEOUT", 7200))
        # run_nccl_mpi.sh writes its Slurm --output under RACK_JOB_LOG_DIR (defaults to cwd; the
        # worker cwd is log_dir, but set it explicitly so the app log always lands in log_dir).
        env_updates.update(_launch_env_from_body(body))
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _prepare_run_nvloom_singlerack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for async nvloom singlerack Slurm job. Returns (prep, None) or (None, (jsonify(...), code))."""
    if os.environ.get("RACK_NVLOOM_SINGLERACK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "nvloom singlerack disabled (RACK_NVLOOM_SINGLERACK_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvloom_singlerack_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"nvloom script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(120, _env_int("RACK_NVLOOM_SINGLERACK_TIMEOUT", 10800))
    ws = _nvis_workspace_dir_effective()
    cuda_m = (_eget("RACK_NVLOOM_CUDA_MAJOR") or _eget("CUDA_MAJ") or "13").strip()
    if cuda_m not in ("12", "13"):
        cuda_m = "13"
    env_updates: dict[str, str] = {
        "NVIS_SYSINFO_SH": str(_sysinfo_script_path()),
        "RACK_SAMPLES": str(max(1, min(256, _env_int("RACK_NVLOOM_RACK_SAMPLES", 5)))),
        "RACK_NVLOOM_SINGLERACK_RACK": rk,
        "WORKSPACE_DIR": ws,
        "NVIS_WORKSPACE_DIR": ws,
        "TOPDIR": ws,
        "NVIS_BIN_SCRIPTS": str(_rpath(SCRIPTS_DIR)),
        "CUDA_MAJ": cuda_m,
    }
    for k in ("NVIS_NVLOOM_HPCX_INIT", "NVIS_NVLOOM_CLI", "NVIS_NVLOOM_UCX_TLS", "RACK_NVLOOM_PARTITION"):
        v = (_eget(k) or "").strip()
        if v:
            env_updates[k] = v
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _prepare_run_clusterkit_singlerack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for async ClusterKit singlerack Slurm job."""
    if os.environ.get("RACK_CLUSTERKIT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "clusterkit disabled (RACK_CLUSTERKIT_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _clusterkit_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"clusterkit script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(120, _job_env_int("RACK_CLUSTERKIT_TIMEOUT", 14400))
    ws = _nvis_workspace_dir_effective()
    cuda_m = (_eget("RACK_CLUSTERKIT_CUDA_MAJOR") or _eget("RACK_NVLOOM_CUDA_MAJOR") or _eget("CUDA_MAJ") or "13").strip()
    if cuda_m not in ("12", "13"):
        cuda_m = "13"
    env_updates: dict[str, str] = {
        "WORKSPACE_DIR": ws,
        "NVIS_WORKSPACE_DIR": ws,
        "TOPDIR": ws,
        "NVIS_BIN_SCRIPTS": str(_rpath(SCRIPTS_DIR)),
        "CUDA_MAJ": cuda_m,
        "RACK_CLUSTERKIT_RACK": rk,
    }
    pl = _clusterkit_product_line_for_job(body)
    if pl:
        env_updates["RACK_CLUSTER_PRODUCT_LINE"] = pl
    for k in (
        "NVIS_HPCX_ROOT",
        "NVIS_CLUSTERKIT_HPCX_INIT",
        "RACK_CLUSTERKIT_PARTITION",
        "RACK_CLUSTERKIT_EXTRA_OPTS",
        "NVIS_RDMA_LINK_TECH",
        "RACK_CLUSTERKIT_LINK_TECH",
        "RACK_CLUSTERKIT_HCA_PROFILE",
        "RACK_CLUSTERKIT_UCX_TLS",
    ):
        v = (_eget(k) or "").strip()
        if v:
            env_updates[k] = v
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _prepare_run_llmb_nccl_container(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Prepare the audited containerized LLMB 24-test NCCL workflow."""
    try:
        log_raw = body.get("log_dir")
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)
    storage = str(body.get("storage") or "local").strip().lower()
    if storage not in ("local", "nfs"):
        return None, (jsonify({"ok": False, "error": "storage must be local or nfs"}), 400)
    rsync = body.get("rsync_to_local", body.get("rsync", storage == "local")) is True
    if rsync and storage != "local":
        return None, (jsonify({"ok": False, "error": "rsync requires storage=local"}), 400)
    local_dir = str(body.get("local_container_dir") or "/tmp/containers").rstrip("/")
    if local_dir != "/tmp/containers":
        return None, (jsonify({"ok": False, "error": "local_container_dir is fixed to /tmp/containers"}), 400)

    nodes_raw = body.get("nodes")
    racks_raw = body.get("racks")
    if racks_raw is None and body.get("rack"):
        racks_raw = [body.get("rack")]
    if nodes_raw is not None and (not isinstance(nodes_raw, list) or not nodes_raw):
        return None, (jsonify({"ok": False, "error": "nodes must be a non-empty array"}), 400)
    if racks_raw is not None and (not isinstance(racks_raw, list) or not racks_raw):
        return None, (jsonify({"ok": False, "error": "racks must be a non-empty array"}), 400)

    racks: list[str] = []
    nodes: list[str] = []
    try:
        for raw in racks_raw or []:
            rack = _normalize_rack_id(str(raw).strip())
            if rack not in racks:
                racks.append(rack)
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)
    if isinstance(nodes_raw, list):
        for raw in nodes_raw:
            node = str(raw).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", node):
                return None, (jsonify({"ok": False, "error": f"invalid node name: {raw!r}"}), 400)
            nodes.append(node)
    target = body.get("target")
    if not nodes and isinstance(target, str) and target.strip():
        nodes = _scontrol_hostnames_preserving_case(target)
        if not nodes:
            return None, (jsonify({"ok": False, "error": "target did not resolve to any Slurm nodes"}), 400)

    campaign_id = ""
    if not nodes and racks:
        try:
            campaign = _multirack_campaign_snapshot()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            return None, (jsonify({"ok": False, "error": f"exact-node campaign unavailable: {exc}"}), 503)
        campaign_id = str(campaign.get("id") or "")
        requested = str(body.get("campaign") or "").strip()
        if requested and requested != campaign_id:
            return None, (jsonify({"ok": False, "error": "stale or unknown exact-node campaign id"}), 409)
        indexed = {str(row.get("rack") or ""): row for row in campaign.get("racks", []) if isinstance(row, dict)}
        for rack in racks:
            if campaign.get("active"):
                row = indexed.get(rack)
                if row is None or not row.get("eligible"):
                    reason = row.get("reason") if row else "outside exact-node campaign"
                    return None, (jsonify({"ok": False, "error": f"rack {rack}: {reason}"}), 409)
                nodes.extend(str(node) for node in row.get("nodes", []) if str(node).strip())
            else:
                resolved = _rack_node_hostnames_from_inventory(rack)
                if not resolved:
                    return None, (jsonify({"ok": False, "error": f"no Slurm inventory nodes found for rack {rack}"}), 409)
                nodes.extend(resolved)
    nodes = _dedupe_hostnames_preserve_spelling(nodes)
    if not nodes:
        return None, (jsonify({"ok": False, "error": "nodes, target, rack, or racks is required"}), 400)
    inventory = _all_slurm_nodenames()
    if inventory:
        known = {node.casefold() for node in inventory}
        unknown = [node for node in nodes if node.casefold() not in known]
        if unknown:
            return None, (jsonify({"ok": False, "error": f"nodes not in current Slurm inventory: {', '.join(unknown)}"}), 409)
    try:
        states, ineligible = _validation_node_eligibility(nodes)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return None, (jsonify({"ok": False, "error": f"unable to verify exact-node scheduler eligibility: {exc}"}), 503)
    if ineligible:
        return None, (jsonify({"ok": False, "error": f"exact target is not idle/eligible: {', '.join(ineligible[:12])}"}), 409)

    script = Path(_scripts_dir()) / "run-llmb-nccl-container.sh"
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"container LLMB NCCL launcher not found: {script}"}), 500)
    cmd = ["bash", str(script), "--nodelist", ",".join(nodes), "--storage", storage,
           "--local-container-dir", local_dir, "--log-dir", str(log_dir)]
    cmd.append("--rsync" if rsync else "--no-rsync")
    dry_run = bool(body.get("dry_run"))
    if dry_run:
        cmd.append("--dry-run")
    scope = "node" if not racks else ("rack" if len(racks) == 1 else "multirack")
    return ({
        "workflow": "llmb-container-24test", "log_dir": log_dir, "racks": racks,
        "nodes": nodes, "node_states": states, "target": ",".join(nodes), "scope": scope,
        "storage": storage, "rsync_to_local": rsync, "cmd": cmd,
        "container_runtime": {"nccl": "2.29.3", "cuda": "13.1"},
        "recipe_contract": "job147392-shape (24 tests, 4 ranks/GPUs per node, PMIx)",
        "image_provenance": {
            "source": "/cluster/containers/nvidia+nemo+26.04.01-arm.sqsh",
            "size": 42245001216,
            "sha256": "55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc",
            "status": (
                "fleet-staged squashfs differs from the original job 147392 image digest; "
                "embedded NCCL 2.29.3 matches the proven runtime"
            ),
        },
        "pmix_gds": {"value": "hash", "status": "requested scale workaround; not a proven result claim"},
        "display_cmd": " ".join(shlex.quote(part) for part in cmd),
        "timeout": max(120, _job_env_int("RACK_LLMB_NCCL_CONTAINER_TIMEOUT", 43200)),
        "dry_run": dry_run,
        "env_updates": {**_launch_env_from_body(body), "NVIS_PREPARED_NODE_COUNT": str(len(nodes))},
        "campaign": campaign_id,
    }, None)


def _prepare_run_llmb_nccl(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for the LLMB NCCL microbenchmark (MOP §7.3).

    Accepts EITHER a single ``rack`` (single-rack component) OR a list ``racks``
    (multi/cross-rack component). The wrapper (``run_llmb_nccl.sh``) takes NVIS rack
    ids and resolves the real Slurm nodes via ``sinfo`` itself, so we pass the
    canonical rack id(s) — not the ``-ct[...]`` validation target.
    """
    if os.environ.get("RACK_LLMB_NCCL_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "NCCL Microbenchmark (LLMB) disabled (RACK_LLMB_NCCL_DISABLED)"}),
            403,
        )
    workflow = str(body.get("workflow") or "").strip().lower()
    if body.get("containerized") is True or workflow in ("container", "llmb-container", "llmb-container-24test"):
        return _prepare_run_llmb_nccl_container(body)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)

    # Accept a single rack ("rack") or a list ("racks"); normalize + de-dupe.
    racks_in: list[str] = []
    racks_raw = body.get("racks")
    if racks_raw is not None:
        if not isinstance(racks_raw, list) or len(racks_raw) < 1:
            return None, (jsonify({"ok": False, "error": "racks must be a non-empty array of rack ids"}), 400)
        racks_in = [str(x) for x in racks_raw]
    else:
        single = (body.get("rack") or "").strip()
        if not single:
            return None, (jsonify({"ok": False, "error": "rack or racks is required"}), 400)
        racks_in = [single]

    rack_ids: list[str] = []
    seen: set[str] = set()
    for item in racks_in:
        try:
            rk = _normalize_rack_id(str(item).strip())
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        if rk in seen:
            continue
        seen.add(rk)
        rack_ids.append(rk)
    if not rack_ids:
        return None, (jsonify({"ok": False, "error": "no valid rack ids"}), 400)

    # When an exact-node campaign is configured it is authoritative: excluded or
    # unhealthy trays must never be silently reintroduced by rack-name expansion.
    try:
        campaign_snapshot = _multirack_campaign_snapshot()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return None, (jsonify({"ok": False, "error": f"exact-node campaign unavailable: {exc}"}), 503)
    campaign_id = str(campaign_snapshot.get("id") or "")
    requested_campaign = str(body.get("campaign") or "").strip()
    if requested_campaign and requested_campaign != campaign_id:
        return None, (jsonify({"ok": False, "error": "stale or unknown exact-node campaign id"}), 409)
    exact_nodes: list[str] = []
    if campaign_snapshot.get("active"):
        indexed = {
            str(row.get("rack") or ""): row
            for row in campaign_snapshot.get("racks", [])
            if isinstance(row, dict)
        }
        for rack in rack_ids:
            row = indexed.get(rack)
            if row is None:
                return None, (jsonify({"ok": False, "error": f"rack {rack} is outside exact-node campaign {campaign_id}"}), 409)
            if not row.get("eligible"):
                return None, (jsonify({"ok": False, "error": f"rack {rack}: {row.get('reason') or 'not eligible'}"}), 409)
            exact_nodes.extend(str(node) for node in row.get("nodes", []) if str(node).strip())

    # Multi-rack NCCL now runs the DIRECT path (run_nccl_mpi.sh → nccl_allreduce.sbatch:
    # host nccl-tests + mpirun, NCCL over the IB rails). This replaces the llmb-run
    # pyxis/enroot container, whose UCX could not see the RDMA verbs devices inside the
    # container ("network devices 'mlx5_*:1' are not available" → no active transport →
    # job killed). Set RACK_LLMB_NCCL_USE_CONTAINER=1 to fall back to the old container.
    use_container = _truthy_env("RACK_LLMB_NCCL_USE_CONTAINER")
    script = _run_llmb_nccl_script_path() if use_container else _run_nccl_mpi_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NCCL launcher not found: {script}"}), 500)

    cmd: list[str] = ["bash", str(script)]
    reservation = (body.get("reservation") or _eget("RACK_LLMB_NCCL_RESERVATION") or "").strip()
    if reservation:
        cmd += ["--reservation", reservation]
    dry_run = bool(body.get("dry_run"))
    if dry_run:
        cmd.append("--dry-run")
    # Direct path only: cap the all_reduce sweep (nccl-tests -e). Default 16G so the
    # x-rack test exercises large messages; overridable per-request or via Settings.
    size_max = ""
    nccl_version = ""
    if not use_container:
        size_max = (body.get("size_max") or _eget("RACK_NCCL_MPI_SIZE_MAX") or "16G").strip()
        if size_max:
            cmd += ["--size-max", size_max]
        # NCCL version selector (direct path only): which vetted libnccl the sbatch loads
        # via LD_LIBRARY_PATH. Shared validator with the single-rack Stage-1 path.
        _vc = _nccl_version_cli(body)
        if isinstance(_vc, tuple):
            return None, _vc
        cmd += _vc
        nccl_version = _vc[1] if _vc else ""
    # Sweep rounds: honor an explicit ``iters`` from the launch modal; else leave the sbatch
    # default (1) so existing cross-rack behavior is unchanged when the knob isn't sent.
    rounds = _nccl_rounds_from_body(body, None) if not use_container else None
    if rounds:
        cmd += ["--rounds", str(rounds)]
    if exact_nodes:
        cmd += ["--nodelist", ",".join(exact_nodes)]
    else:
        cmd += rack_ids

    display_cmd = "./" + script.name + (
        (" --reservation " + shlex.quote(reservation)) if reservation else ""
    ) + (" --dry-run" if dry_run else "") + (
        (" --size-max " + shlex.quote(size_max)) if size_max else ""
    ) + (
        (" --nccl-version " + shlex.quote(nccl_version)) if nccl_version else ""
    ) + (
        (" --rounds " + str(rounds)) if rounds else ""
    ) + ((" --nodelist " + shlex.quote(",".join(exact_nodes))) if exact_nodes else (
        " " + " ".join(shlex.quote(r) for r in rack_ids)
    ))
    timeout = max(120, _job_env_int("RACK_LLMB_NCCL_TIMEOUT", 14400))
    return (
        {
            "log_dir": log_dir,
            "racks": rack_ids,
            "campaign": campaign_id if exact_nodes else "",
            "nodes": exact_nodes,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "dry_run": dry_run,
            "env_updates": _launch_env_from_body(body),
        },
        None,
    )






@app.route("/api/validation-presets", methods=["GET"])
def api_validation_presets():
    return jsonify({"ok": True, "presets": validation_presets.catalogue()})


@app.route("/api/validation-presets/prepare", methods=["POST"])
def api_prepare_validation_preset():
    body = request.get_json(silent=True) or {}
    plan, err = _prepare_validation_preset(body)
    if err is not None:
        return err
    assert plan is not None
    public_plan = {key: value for key, value in plan.items() if key not in ("cmd", "env_updates", "timeout", "log_dir")}
    return jsonify({"ok": True, "plan": public_plan})


def _run_validation_preset_async(body: dict):
    plan, err = _prepare_validation_preset(body)
    if err is not None:
        return err
    assert plan is not None
    supplied_hash = str(body.get("plan_hash") or "").strip()
    if not supplied_hash:
        return jsonify({"ok": False, "error": "plan_hash from /api/validation-presets/prepare is required"}), 409
    if not secrets.compare_digest(supplied_hash, str(plan["plan_hash"])):
        return jsonify({"ok": False, "error": "validation plan changed; prepare and review it again", "plan_hash": plan["plan_hash"]}), 409
    job_id = str(uuid.uuid4())
    operator = _operator_from_request()
    tool = {
        "nccl-2.31.2": "nccl-fullrack", "llama31-405b-fp8": "llama-exemplar",
        "gpt-oss-cpu": "external", "exemplar-system-info": "external",
        "nemo-26.04.01": "nemo-xrack", "mnubergemm-26.03-v1.17": "mnubergemm",
    }[str(plan["preset_id"])]
    racks = [str(rack) for rack in plan["racks"]]
    _record_validation_activity(tool, stage="other", scope=str(plan["scope"]),
        rack=racks[0] if racks else "", racks=racks, log_dir=str(plan["log_dir"]),
        command_display=str(plan["command_display"]), activity_id=job_id, operator=operator)
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id, "kind": "validation-preset", "preset_id": plan["preset_id"],
            "operator": operator, "racks": racks, "nodes": plan["nodes"], "node_count": plan["node_count"],
            "log_dir": plan["log_dir"], "phase": "starting", "command_display": plan["command_display"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    threading.Thread(target=_stress_worker,
        args=(job_id, plan["cmd"], plan["log_dir"], plan["timeout"], plan["command_display"]),
        kwargs={"error_label": "validation preset launcher", "env_updates": plan["env_updates"],
                "profile": "script", "persist": True}, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "preset_id": plan["preset_id"],
                    "nodes": plan["nodes"], "node_count": plan["node_count"],
                    "command_display": plan["command_display"], "timeout_sec": plan["timeout"]})


@app.route("/api/run-validation-preset-async", methods=["POST"])
def api_run_validation_preset_async():
    return _run_validation_preset_async(request.get_json(silent=True) or {})


@app.route("/api/run-exemplar-sysinfo-async", methods=["POST"])
def api_run_exemplar_sysinfo_async():
    body = request.get_json(silent=True) or {}
    body["preset_id"] = "exemplar-system-info"
    return _run_validation_preset_async(body)




@app.route("/api/run-validation-preset-status", methods=["GET"])
def api_run_validation_preset_status():
    return _validation_preset_status_payload((request.args.get("job_id") or "").strip())


@app.route("/api/run-exemplar-sysinfo-status", methods=["GET"])
def api_run_exemplar_sysinfo_status():
    return _validation_preset_status_payload((request.args.get("job_id") or "").strip())


@app.route("/api/run-mnubergemm-stress-async", methods=["POST"])
def api_run_mnubergemm_stress_async():
    """Start mnubergemm stress driver; poll GET /api/run-mnubergemm-stress-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_mnubergemm_stress(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    _record_validation_activity(
        "mnubergemm",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "mnubergemm",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"env_updates": env_merged, "persist": True},
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@app.route("/api/run-nccl-fullrack-async", methods=["POST"])
def api_run_nccl_fullrack_async():
    """Start the standalone Stage-1 NCCL benchmark; poll GET /api/run-nccl-fullrack-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_nccl_fullrack(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    _record_validation_activity(
        "nccl-fullrack",
        stage="stage1",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    if env_merged is None:
        env_merged = {}
    env_merged = {**env_merged, "NVIS_ACTIVITY_ID": job_id}
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nccl-fullrack",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={
            "error_label": "nccl benchmark script",
            "env_updates": env_merged,
            "profile": "script",
            "persist": True,
        },
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@app.route("/api/run-nvloom-async", methods=["POST"])
def api_run_nvloom_async():
    """Start nvloom singlerack Slurm driver (sbatch --wait); poll GET /api/run-nvloom-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_nvloom_singlerack(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    _record_validation_activity(
        "nvloom",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvloom",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"error_label": "nvloom singlerack script", "env_updates": env_merged, "persist": True},
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@app.route("/api/run-clusterkit-async", methods=["POST"])
def api_run_clusterkit_async():
    """Start ClusterKit singlerack Slurm driver; poll GET /api/run-clusterkit-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_clusterkit_singlerack(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    _record_validation_activity(
        "clusterkit",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "clusterkit",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"error_label": "clusterkit script", "env_updates": env_merged, "persist": True},
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@app.route("/api/run-llmb-nccl-async", methods=["POST"])
def api_run_llmb_nccl_async():
    """Start the LLMB NCCL microbenchmark (single rack via ``rack`` or cross-rack via
    ``racks``); poll GET /api/run-llmb-nccl-status?job_id=…

    Process cwd is *log_dir* (like clusterkit / nemo-xrack) so Slurm batch stdout lands
    under the UI log dir. Runs on the privileged ``script`` profile (Slurm + llmb-run on
    the head node).
    """
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_run_llmb_nccl(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    racks = [str(r) for r in prep["racks"]]
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    dry_run = bool(prep["dry_run"])
    workflow = str(prep.get("workflow") or "direct-host")
    nodes = [str(node) for node in prep.get("nodes", [])]
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) and _eu else None
    rack_label = racks[0] if len(racks) == 1 else (f"{len(racks)} racks" if racks else f"{len(nodes)} nodes")
    cwd = _host_job_cwd(log_dir)
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    scope = str(prep.get("scope") or ("multirack" if len(racks) > 1 else "rack"))
    _record_validation_activity(
        "llmb-nccl",
        stage="stage1",
        scope=scope,
        rack=racks[0] if racks else rack_label,
        racks=racks,
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
        dry_run=dry_run,
    )
    if env_merged is None:
        env_merged = {}
    env_merged = {**env_merged, "NVIS_ACTIVITY_ID": job_id}
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "llmb-nccl",
            "operator": operator,
            "rack": rack_label,
            "racks": racks,
            "nodes": nodes,
            "workflow": workflow,
            "dry_run": dry_run,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, cwd, timeout, display_cmd),
        kwargs={
            "error_label": "run_llmb_nccl script",
            "profile": "script",
            "persist": True,
            "env_updates": env_merged,
        },
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rack_label,
            "racks": racks,
            "nodes": nodes,
            "workflow": workflow,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@app.route("/api/multirack-jobs", methods=["GET"])
def api_multirack_jobs():
    """List launched multi-rack jobs from the durable state table (newest first).

    Persists across sessions / restarts, so every operator sees the same job list.
    Optional ``?log_dir=`` filters to jobs launched against that log directory.
    """
    log_dir_raw = (request.args.get("log_dir") or "").strip()
    log_dir_filter: str | None = None
    if log_dir_raw:
        try:
            log_dir_filter = str(_resolve_log_dir(log_dir_raw))
        except ValueError:
            log_dir_filter = log_dir_raw
    try:
        limit = int(request.args.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    jobs = validation_activities.list_activities(log_dir=log_dir_filter, scope="multirack", limit=limit)
    _refresh_validation_activities_lazy(jobs)
    return jsonify({"ok": True, "jobs": jobs})


@app.route("/api/multirack-jobs/<job_id>", methods=["DELETE"])
def api_multirack_job_delete(job_id: str):
    """Dismiss (remove) a launched multi-rack job from the durable state table."""
    if validation_activities.delete_activity(job_id):
        return jsonify({"ok": True, "job_id": job_id})
    return jsonify({"ok": False, "error": "unknown job_id"}), 404


def _validation_activities_query_filters() -> tuple[str | None, str | None, str | None, str | None, int]:
    log_dir_raw = (request.args.get("log_dir") or "").strip()
    log_dir_filter: str | None = None
    if log_dir_raw:
        try:
            log_dir_filter = str(_resolve_log_dir(log_dir_raw))
        except ValueError:
            log_dir_filter = log_dir_raw
    rack = (request.args.get("rack") or "").strip() or None
    tool = (request.args.get("tool") or "").strip() or None
    scope = (request.args.get("scope") or "").strip() or None
    try:
        limit = int(request.args.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    return log_dir_filter, rack, tool, scope, limit


@app.route("/api/validation-activities", methods=["GET"])
def api_validation_activities():
    """List validation activities (newest first) with optional rack/log_dir/tool/scope filters."""
    log_dir_filter, rack, tool, scope, limit = _validation_activities_query_filters()
    rows = validation_activities.list_activities(
        log_dir=log_dir_filter,
        rack=rack,
        tool=tool,
        scope=scope,
        limit=limit,
    )
    _refresh_validation_activities_lazy(rows)
    completion = validation_activities.completion_rollup(
        log_dir=log_dir_filter,
        rack=rack,
        tool=tool,
        scope=scope,
    )
    return jsonify({"ok": True, "activities": rows, "completion_rollup": completion})


@app.route("/api/validation-activities/ledger", methods=["GET"])
def api_validation_activities_ledger():
    """Human-readable, append-preserving multi-rack run ledger."""
    return Response(
        validation_activities.read_multirack_ledger_markdown(),
        content_type="text/markdown; charset=utf-8",
    )


@app.route("/api/validation-activities/<activity_id>", methods=["GET"])
def api_validation_activity_detail(activity_id: str):
    aid = (activity_id or "").strip()
    if not aid:
        return jsonify({"ok": False, "error": "activity_id is required"}), 400
    rec = validation_activities.get_activity(aid)
    if rec is None:
        return jsonify({"ok": False, "error": "unknown activity_id"}), 404
    _refresh_validation_activities_lazy([rec])
    rec = validation_activities.get_activity(aid) or rec
    return jsonify({"ok": True, "activity": rec})


@app.route(
    "/api/validation-activities/<activity_id>/corrective-actions",
    methods=["POST"],
)
def api_validation_activity_corrective_action(activity_id: str):
    """Annotate an activity; corrective actions append and are never overwritten."""
    aid = (activity_id or "").strip()
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    issue = body.get("issue_summary")
    outcome = body.get("outcome_summary")
    if not any(str(value or "").strip() for value in (action, issue, outcome)):
        return jsonify(
            {
                "ok": False,
                "error": "action, issue_summary, or outcome_summary is required",
            }
        ), 400
    rec = validation_activities.annotate_activity(
        aid,
        issue_summary=issue,
        outcome_summary=outcome,
        corrective_action=action,
        corrective_action_result=body.get("result"),
        corrective_action_status=body.get("status"),
        operator=_operator_from_request() or body.get("operator") or "operator",
    )
    if rec is None:
        return jsonify({"ok": False, "error": "unknown activity_id"}), 404
    return jsonify({"ok": True, "activity_id": aid, "activity": rec})


@app.route("/api/validation-activities/<activity_id>", methods=["DELETE"])
def api_validation_activity_delete(activity_id: str):
    aid = (activity_id or "").strip()
    if validation_activities.delete_activity(aid):
        return jsonify({"ok": True, "activity_id": aid})
    return jsonify({"ok": False, "error": "unknown activity_id"}), 404


@app.route("/api/validation-activities/adopt", methods=["POST"])
def api_validation_activities_adopt():
    """Register an external validation log under a new durable activity id."""
    body = request.get_json(silent=True) or {}
    path_raw = (body.get("path") or body.get("log_path") or "").strip()
    if not path_raw:
        return jsonify({"ok": False, "error": "path is required"}), 400
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack = (body.get("rack") or "").strip()
    try:
        if rack:
            rack = _normalize_rack_id(rack)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not rack:
        rack = multirack_display_rack_from_basename(os.path.basename(path_raw)) or ""
    try:
        log_path = _resolve_slurm_job_stdout_path(path_raw, str(log_dir))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    aid = validation_activities.adopt_log(
        str(log_path),
        log_dir=str(log_dir),
        rack=rack,
        operator=_operator_from_request() or "external",
    )
    if not aid:
        return jsonify({"ok": False, "error": "could not adopt log"}), 400
    rec = validation_activities.get_activity(aid)
    return jsonify({"ok": True, "activity_id": aid, "activity": rec})


@app.route("/api/rack-grafana-live-charts", methods=["GET"])
def api_rack_grafana_live_charts():
    """Grafana solo context for GPU thermal + power, last 5 minutes, hosts = rack CT expand."""
    try:
        _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack = (request.args.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    log_tz_q = _sanitize_display_timezone_query(request.args.get("display_timezone"))
    g_embed = _grafana_live_5m_embed_context(rk, log_timestamp_tz=log_tz_q)
    pdisp = _grafana_url_timezone_param()
    if isinstance(g_embed, dict) and g_embed.get("display_timezone"):
        pdisp = str(g_embed["display_timezone"])
    return jsonify(
        {
            "ok": True,
            "rack": rk,
            "grafana_uri": _rack_web_grafana_uri(),
            "grafana_port": _rack_web_grafana_port(),
            "grafana_embed": g_embed,
            "parser_display_timezone": pdisp,
        }
    )


def _parse_sysinfo_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Validate sysinfo POST body. On success returns (prep, None); prep has log_dir, rack, target, display_cmd, cmd."""
    if os.environ.get("RACK_SYSINFO_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "run-sysinfo disabled (RACK_SYSINFO_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _sysinfo_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"sysinfo script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    return (
        {
            "log_dir": log_dir,
            "rack": rk,
            "target": target,
            "display_cmd": display_cmd,
            "cmd": cmd,
        },
        None,
    )






def _sysinfo_stream_generator(prep: dict):
    """Yield stdout+stderr bytes from sysinfo.sh until process exit (or timeout)."""
    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    cwd = _REMOTE_BASE if (_REMOTE_BASE and host_exec.exec_kind() == "ssh") else str(prep["log_dir"])
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        cwd,
        prep["display_cmd"],
        timeout_sec,
        linebuffer_env_var="RACK_SYSINFO_LINEBUFFER",
        linebuffer_default="1",
        privileged=True,
    )


def _parse_nvswitch_audit_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Validate POST body for NVSwitch rack audit stream; prep has log_dir, rack, display_cmd, cmd."""
    if os.environ.get("RACK_NVSWITCH_AUDIT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "nvswitch audit disabled (RACK_NVSWITCH_AUDIT_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    fix_raw = body.get("fix")
    fix = fix_raw is True or (
        isinstance(fix_raw, str) and fix_raw.strip().lower() in ("1", "true", "yes")
    )
    script = _nvswitches_rack_audit_script_path()
    if not _script_exists(script):
        return None, (
            jsonify({"ok": False, "error": f"nvswitch audit script not found: {script}"}),
            500,
        )
    cmd: list[str] = ["bash", str(script), "--no-color"]
    if fix:
        cmd.append("--fix")
    cmd.append(rk)
    if fix:
        display_cmd = "./" + script.name + " --fix " + shlex.quote(rk)
    else:
        display_cmd = "./" + script.name + " " + shlex.quote(rk)
    return (
        {
            "log_dir": log_dir,
            "rack": rk,
            "fix": fix,
            "display_cmd": display_cmd,
            "cmd": cmd,
        },
        None,
    )


def _nvswitch_audit_stream_generator(prep: dict):
    """Stream nvswitches_rack_audit.sh (check-only or --fix)."""
    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    cwd = _REMOTE_BASE if (_REMOTE_BASE and host_exec.exec_kind() == "ssh") else str(prep["log_dir"])
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        cwd,
        prep["display_cmd"],
        timeout_sec,
        linebuffer_env_var="RACK_NVSWITCH_AUDIT_LINEBUFFER",
        linebuffer_default="1",
        privileged=True,
    )


@app.route("/api/run-sysinfo", methods=["POST"])
def api_run_sysinfo():
    """Run sysinfo.sh with one NODELIST target, e.g. p22-r05-ct[01-18] (cwd = log_dir)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    try:
        with _track_sync_op(prep.get("rack") or "", "sysinfo", _operator_from_request()):
            proc = subprocess.run(
                prep["cmd"],
                cwd=str(prep["log_dir"]),
                env=_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800)),
                check=False,
            )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "sysinfo.sh timed out", "target": prep["target"]}), 504
    except Exception as e:
        _logger.debug("api_run_sysinfo: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e), "target": prep["target"]}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "rack": prep["rack"],
            "target": prep["target"],
            "command_display": prep["display_cmd"],
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


























































@app.route("/api/run-sysinfo-stream", methods=["POST"])
def api_run_sysinfo_stream():
    """Stream merged stdout/stderr from sysinfo.sh as text/plain (UTF-8)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None

    rk_for_lock = str(prep.get("rack") or "")
    operator = _operator_from_request()

    def _tracked_sysinfo_stream() -> Iterator[bytes]:
        with _track_sync_op(rk_for_lock, "sysinfo", operator):
            yield from _sysinfo_stream_generator(prep)

    return Response(
        stream_with_context(_tracked_sysinfo_stream()),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/run-nvswitch-audit-stream", methods=["POST"])
def api_run_nvswitch_audit_stream():
    """Stream nvswitches_rack_audit.sh for one rack (optional body.fix true => --fix). cwd = log_dir."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_nvswitch_audit_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None

    rk_for_lock = str(prep.get("rack") or "")
    operator = _operator_from_request()

    def _tracked_nvswitch_audit_stream() -> Iterator[bytes]:
        with _track_sync_op(rk_for_lock, "nvswitch_audit", operator):
            yield from _nvswitch_audit_stream_generator(prep)

    return Response(
        stream_with_context(_tracked_nvswitch_audit_stream()),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )












@app.route("/")
def index():
    """Serve the React SPA."""
    return _serve_spa_asset("index.html")


def _cluster_env_pick(*keys: str, default: str = "") -> str:
    for k in keys:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v.replace("\n", " ").replace("\r", " ").strip()
    return default


# Strip ``module load <mod> &&`` from command strings for cluster-dashboard display only.
_CLUSTER_SLURM_MODULE_PREFIX_RE = re.compile(
    r"^\s*module\s+load\s+(?:'[^']+'|\"[^\"]+\"|\S+)\s+&&\s*",
    re.IGNORECASE,
)


def _cluster_slurm_command_display_public(cmd: str) -> str:
    """Hide ``module load slurm &&`` in UI; execution still uses the real shell command."""
    s = (cmd or "").strip()
    s = _CLUSTER_SLURM_MODULE_PREFIX_RE.sub("", s, count=1)
    return s.strip() or (cmd or "").strip()


_SLURM_VERSION_JUNK_RE = re.compile(
    r"node\s+status|running\s+in\s+active\s+master",
    re.IGNORECASE,
)


def _pick_slurm_version_line(stdout: str, stderr: str) -> str:
    """``scontrol -V`` often prints the version on *stderr*; stdout may contain unrelated lines."""
    lines: list[str] = []
    for block in (stdout or "", stderr or ""):
        for ln in block.splitlines():
            t = ln.strip()
            if t:
                lines.append(t)
    if not lines:
        return ""
    for ln in lines:
        if _SLURM_VERSION_JUNK_RE.search(ln):
            continue
        if re.search(r"slurm\s*[\d.]|^slurm\b", ln, re.IGNORECASE):
            return ln
    for ln in lines:
        if _SLURM_VERSION_JUNK_RE.search(ln):
            continue
        if re.search(r"\d+\.\d+", ln):
            return ln
    for ln in lines:
        if not _SLURM_VERSION_JUNK_RE.search(ln):
            return ln
    return ""


def _cluster_probe_slurm_version() -> tuple[str, str]:
    plat = _get_platform()
    # ``scontrol -V`` only (with platform Slurm module prefix), inside one login shell.
    shell_cmd = plat.scontrol_cmd("-V")
    timeout = max(5, _env_int("RACK_CLUSTER_SLURM_PROBE_TIMEOUT", 40))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        return "", "scontrol not found"
    except subprocess.TimeoutExpired:
        return "", "slurm version probe timed out"
    out = proc.stdout or ""
    err = proc.stderr or ""
    line = _pick_slurm_version_line(out, err)
    if proc.returncode != 0:
        return "", (err.strip() or out.strip() or f"exit {proc.returncode}")
    if not line:
        return "", err.strip() or out.strip() or "empty scontrol -V output"
    return line, ""


def _cluster_probe_bcm_cmsh_version() -> tuple[str, str]:
    """BCM / Bright Cluster Manager build from ``cmsh -c 'main; versioninfo'`` (head / dashboard host only)."""
    if _truthy_env("RACK_CLUSTER_BCM_CMSH_DISABLED"):
        return "", "disabled (RACK_CLUSTER_BCM_CMSH_DISABLED)"
    cmsh = (_eget("RACK_CMSH") or "cmsh").strip() or "cmsh"
    cmd = [cmsh, "-c", "main; versioninfo"]
    timeout = max(10, _env_int("RACK_CLUSTER_BCM_CMSH_TIMEOUT", 60))
    if not host_exec.which(cmsh):
        return "", "cmsh not in PATH"
    try:
        proc = host_exec.run_on_host(cmd, timeout=timeout)
    except FileNotFoundError:
        return "", "cmsh executable not found"
    except subprocess.TimeoutExpired:
        return "", "cmsh versioninfo timed out"
    out = (proc.stdout or "").strip()
    err_tail = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return "", err_tail or f"exit {proc.returncode}"
    if not out:
        return "", err_tail or "empty stdout"
    # cmsh versioninfo pads columns with runs of spaces ("Cluster Manager          11.0")
    # and emits 4+ build lines. Joining them raw produced an unreadable blob in the
    # Compute overview header. Collapse whitespace and show the Cluster Manager line only.
    lines = [re.sub(r"\s+", " ", ln.strip()) for ln in out.splitlines() if ln.strip()]
    cap = max(40, _env_int("RACK_CLUSTER_BCM_CMSH_DISPLAY_MAX", 120))
    n_lines = max(1, _env_int("RACK_CLUSTER_BCM_CMSH_DISPLAY_LINES", 1))
    snippet = " | ".join(lines[:n_lines]) if lines else out
    if len(snippet) > cap:
        snippet = snippet[: cap - 3].rstrip() + "…"
    return snippet, ""


def _cluster_squeue_overview_block() -> dict[str, object]:
    """Head-only ``squeue`` (same as ``/api/slurm-squeue``); never targets compute nodes."""
    if os.environ.get("RACK_SLURM_SQUEUE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "command_display": "",
            "returncode": -1,
            "error": "squeue disabled (RACK_SLURM_SQUEUE_DISABLED)",
        }
    try:
        result = _run_squeue()
    except OSError as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(e),
            "command_display": "",
            "returncode": -1,
            "error": str(e),
        }
    out = dict(result)
    rc = int(out.get("returncode") if out.get("returncode") is not None else -1)
    ok = bool(out.get("ok")) and rc == 0
    stdout = str(out.get("stdout") or "")
    cap = max(4096, _env_int("RACK_CLUSTER_SQUEUE_OVERVIEW_MAX_BYTES", 262144))
    if len(stdout) > cap:
        stdout = stdout[:cap] + "\n… (truncated; increase RACK_CLUSTER_SQUEUE_OVERVIEW_MAX_BYTES)\n"
    raw_cmd = str(out.get("command_display") or "")
    return {
        "ok": ok,
        "stdout": stdout,
        "stderr": str(out.get("stderr") or ""),
        "command_display": _cluster_slurm_command_display_public(raw_cmd),
        "returncode": rc,
        "error": None if ok else str(out.get("error") or "squeue exited non-zero (see stderr)"),
    }


_CLUSTER_DM_KEYS: tuple[str, ...] = ("product_line", "hw_vendor", "customer_name")


def _cluster_dashboard_metadata_path() -> Path:
    raw = (os.environ.get("RACK_CLUSTER_DASHBOARD_METADATA_FILE") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return (REPO_ROOT / "var" / "cache" / "cluster_dashboard_metadata.json").resolve()


def read_cluster_dashboard_metadata() -> dict[str, str]:
    """Load dashboard-saved cluster labels (product line, HW vendor, project/customer).

    Returns ``product_line``, ``hw_vendor``, ``customer_name`` (empty string when unset).
    """
    defaults = {k: "" for k in _CLUSTER_DM_KEYS}
    p = _cluster_dashboard_metadata_path()
    if not p.is_file():
        return dict(defaults)
    max_len = max(1, _env_int("RACK_CLUSTER_DASHBOARD_META_MAX_LEN", 512))
    try:
        with open(p, "r", encoding="utf-8") as f:
            locked = False
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                pass
            try:
                try:
                    raw = json.load(f)
                except json.JSONDecodeError:
                    raw = {}
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except (OSError, UnicodeDecodeError, TypeError):
        return dict(defaults)
    if not isinstance(raw, dict):
        return dict(defaults)
    out = dict(defaults)
    for k in _CLUSTER_DM_KEYS:
        v = raw.get(k)
        if isinstance(v, str):
            t = v.replace("\n", " ").replace("\r", " ").strip()
            if t:
                out[k] = t[:max_len]
    return out


def update_cluster_dashboard_metadata(updates: dict[str, object]) -> dict[str, str]:
    """Merge *updates* into the on-disk metadata file (empty string or null clears a key)."""
    max_len = max(1, _env_int("RACK_CLUSTER_DASHBOARD_META_MAX_LEN", 512))
    p = _cluster_dashboard_metadata_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("{}\n", encoding="utf-8")
    with open(p, "r+", encoding="utf-8") as f:
        locked = False
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass
        try:
            f.seek(0)
            blob = f.read()
            try:
                cur = json.loads(blob) if blob.strip() else {}
            except json.JSONDecodeError:
                cur = {}
            if not isinstance(cur, dict):
                cur = {}
            for k in _CLUSTER_DM_KEYS:
                if k not in updates:
                    continue
                v = updates[k]
                if v is None:
                    cur.pop(k, None)
                    continue
                if not isinstance(v, str):
                    continue
                t = v.replace("\n", " ").replace("\r", " ").strip()
                if not t:
                    cur.pop(k, None)
                else:
                    cur[k] = t[:max_len]
            new_obj = {k: cur[k] for k in _CLUSTER_DM_KEYS if isinstance(cur.get(k), str) and str(cur[k]).strip()}
            new_blob = json.dumps(new_obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            f.seek(0)
            f.truncate()
            f.write(new_blob)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass
    return read_cluster_dashboard_metadata()


def build_cluster_overview(
    log_dir: Path,
    snapshot: cluster_snapshot.ClusterSnapshot | None = None,
) -> dict[str, object]:
    """Aggregate cluster facts for the React dashboard (env + head-only Slurm; no node SSH/pdsh)."""
    current = snapshot or get_cluster_snapshot(log_dir)
    cached = dict(current.payload) if isinstance(current.payload, dict) else None
    rows = list(cached.get("rows") or []) if cached is not None else []
    rack_ids = sorted({str(r.get("rack") or "").strip() for r in rows if r.get("rack")})
    rack_count = len(rack_ids) if cached is not None else None
    summary = dict(cached.get("summary") or {}) if cached is not None else None
    if summary is not None:
        for k in ("done", "fail", "ongoing", "pending"):
            if k not in summary:
                summary[k] = 0
    phys = dict(cached.get("physical_rf0") or {}) if cached is not None else {}
    tray_hosts = len(phys) if cached is not None else None
    gpus_per_tray = max(1, _eget_int("RACK_CLUSTER_GPU_PER_TRAY", 8))
    estimated_gpus = tray_hosts * gpus_per_tray if tray_hosts is not None else None
    validation_total = None
    if summary is not None:
        validation_total = int(summary.get("done", 0) or 0) + int(summary.get("fail", 0) or 0)
        validation_total += int(summary.get("ongoing", 0) or 0) + int(summary.get("pending", 0) or 0)

    cluster_name = _cluster_env_pick("RACK_CLUSTER_NAME", "CLUSTER_NAME")
    if not cluster_name:
        try:
            cluster_name = socket.gethostname()
        except Exception:
            _logger.debug("build_cluster_overview: ignoring error", exc_info=True)
            cluster_name = "—"

    customer = _cluster_env_pick("RACK_CLUSTER_CUSTOMER_NAME", "RACK_CLUSTER_PROJECT") or _project_name() or "—"
    product_line = _cluster_env_pick("RACK_CLUSTER_PRODUCT_LINE", "RACK_PRODUCT_LINE") or "—"
    hw_vendor = _cluster_env_pick("RACK_CLUSTER_HW_VENDOR", "RACK_HW_VENDOR")
    dash_meta = read_cluster_dashboard_metadata()
    # Dashboard file fills gaps only (does not override env / hostname-derived values).
    if product_line == "—" and dash_meta.get("product_line"):
        product_line = dash_meta["product_line"]
    if (not hw_vendor or hw_vendor == "—") and dash_meta.get("hw_vendor"):
        hw_vendor = dash_meta["hw_vendor"]
    if customer == "—" and dash_meta.get("customer_name"):
        customer = dash_meta["customer_name"]
    bcm_version = _cluster_env_pick("RACK_CLUSTER_BCM_VERSION", "RACK_BCM_VERSION")
    slurm_version = _cluster_env_pick("RACK_CLUSTER_SLURM_VERSION", "RACK_SLURM_VERSION")
    if slurm_version and _SLURM_VERSION_JUNK_RE.search(slurm_version):
        slurm_version = ""
    nemo_container = _cluster_env_pick(
        "RACK_CLUSTER_NEMO_CONTAINER",
        "RACK_CLUSTER_NEMO_IMAGE",
        "RACK_NEMO_CONTAINER_IMAGE",
        "NEMO_SQSH_SRC",
    )

    # Live BCM/Slurm/queue probes run only via ``/api/cluster-overview/live``.
    # Keep env-configured versions here; queue is a non-live placeholder for FE compat.
    # Never probe SSH here — cold/stale reachability must not run ``ssh true``.
    reachable, reach_detail = host_exec.cached_reachability_state()
    probes: dict[str, object] = {}
    squeue_block: dict[str, object] = {
        "ok": False,
        "stdout": "",
        "stderr": "",
        "command_display": "",
        "returncode": -1,
        "error": "live details load separately",
    }

    if hw_vendor == "":
        hw_vendor = "—"
    if bcm_version == "":
        bcm_version = "—"
    if slurm_version == "":
        slurm_version = "—"
    if nemo_container == "":
        nemo_container = "—"

    cuda_nccl_build_host_candidates = sorted(phys.keys(), key=str.casefold)

    out: dict[str, object] = {
        "ok": True,
        "cluster_name": cluster_name,
        "customer_name": customer,
        "product_line": product_line,
        "hw_vendor": hw_vendor,
        "bcm_version": bcm_version,
        "slurm_version": slurm_version,
        "nemo_container": nemo_container,
        "cuda_nccl_build_host_candidates": cuda_nccl_build_host_candidates,
        "tools_download_destination_default": _tools_download_destination_default(),
        "tools_workspace_default": _nvis_workspace_dir_effective(),
        "rack_count": rack_count,
        "tray_hosts": tray_hosts,
        "estimated_gpu_count": estimated_gpus,
        "validation_summary": summary,
        "validation_cells_total": validation_total,
        "status_updated_at": cached.get("updated_at") if cached is not None else None,
        "snapshot": current.metadata(),
        "probes": probes,
        "app_version": _app_version_mod.app_version(),
        "squeue": squeue_block,
        "cluster_dashboard_metadata": dash_meta,
        "cluster_unreachable_detail": "",
        "exec_mode": host_exec.exec_kind(),
    }
    # Unknown cached reachability: leave unset/None rather than inventing down/up.
    if reachable is None:
        out["cluster_reachable"] = None
    else:
        out["cluster_reachable"] = reachable
        out["cluster_unreachable_detail"] = (
            "" if reachable else (reach_detail or "head node not reachable over SSH")
        )
    return out


@app.route("/api/cluster-dashboard-metadata", methods=["GET", "PUT"])
def api_cluster_dashboard_metadata():
    """GET/PUT JSON ``product_line``, ``hw_vendor``, ``customer_name`` (saved under REPO_ROOT/var when unset)."""
    if request.method == "GET":
        meta = read_cluster_dashboard_metadata()
        return jsonify({"ok": True, **meta})
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected JSON object"}), 400
    updates: dict[str, object] = {}
    for k in _CLUSTER_DM_KEYS:
        if k not in body:
            continue
        v = body[k]
        if v is None:
            updates[k] = None
        elif isinstance(v, str):
            updates[k] = v
        else:
            return jsonify({"ok": False, "error": f"{k} must be a string or null"}), 400
    try:
        meta = update_cluster_dashboard_metadata(updates)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, **meta})


def _job_environment_settings_payload() -> dict[str, object]:
    """Same JSON shape for GET /api/settings/job-environment and successful PUT (avoids multi-worker GET races)."""
    ovr = _job_env_overrides_load()
    eff = _job_env_effective_values()
    variables: list[dict[str, object]] = []
    for d in JOB_ENV_UI_DEFINITIONS:
        k = d["key"]
        variables.append(
            {
                **d,
                "value": eff.get(k, ""),
                "has_override": k in ovr,
            }
        )
    sections: list[dict[str, object]] = []
    for sec_id, title in JOB_ENV_SECTION_ORDER:
        sec_vars: list[dict[str, object]] = []
        for d in JOB_ENV_UI_DEFINITIONS:
            if d.get("section") != sec_id:
                continue
            k = d["key"]
            sec_vars.append(
                {
                    **d,
                    "value": eff.get(k, ""),
                    "has_override": k in ovr,
                }
            )
        sections.append({"id": sec_id, "title": title, "variables": sec_vars})
    return {
        "ok": True,
        "sections": sections,
        "variables": variables,
        "path": str(_job_env_overrides_path()),
    }


@app.route("/api/grafana/deeplinks")
def api_grafana_deeplinks():
    """Config for 'Open in Grafana' deep-links (full dashboards scoped by rack/host, opened in a new tab).

    Unlike the /d-solo/ parser embeds, these need no embedding config — they link to the existing
    BCM/NMC dashboards. Response: ``enabled``, ``base`` (may be empty -> resolve to same host),
    ``port``, ``var_rack`` / ``var_host`` (template var names), ``timezone`` and ``dashboards``.
    """
    return jsonify(
        {
            "ok": True,
            "enabled": _grafana_deeplinks_enabled(),
            "base": _rack_web_grafana_uri(),
            "port": _rack_web_grafana_port(),
            "var_rack": _grafana_var_rack(),
            "var_host": _grafana_var_host(),
            "timezone": _grafana_url_timezone_param(),
            "dashboards": _grafana_deeplink_dashboards(),
        }
    )


@app.route("/api/settings/job-environment", methods=["GET", "PUT"])
def api_settings_job_environment():
    """GET effective job/validation env (with dashboard overrides); PUT merges ``overrides`` (empty value clears)."""
    if request.method == "GET":
        return jsonify(_job_environment_settings_payload())

    body = request.get_json(silent=True) or {}
    raw_updates = body.get("overrides")
    if raw_updates is None:
        return jsonify({"ok": False, "error": 'expected JSON body with "overrides" object'}), 400
    if not isinstance(raw_updates, dict):
        return jsonify({"ok": False, "error": '"overrides" must be an object'}), 400

    before_snapshot = dict(_job_env_overrides_load())
    cur = dict(before_snapshot)
    for k, v in raw_updates.items():
        if not isinstance(k, str) or k not in _JOB_ENV_ALLOWED_KEYS:
            return jsonify({"ok": False, "error": f"unknown or disallowed key: {k!r}"}), 400
        if v is None or (isinstance(v, str) and not v.strip()):
            cur.pop(k, None)
            continue
        if not isinstance(v, str):
            return jsonify({"ok": False, "error": f"{k}: value must be a string"}), 400
        t = v.strip()
        if len(t) > 8192:
            return jsonify({"ok": False, "error": f"{k}: value too long (max 8192)"}), 400
        if k in ("NVIS_RACK_ID", "NVIS_CT_ID", "NVIS_NVSW_ID") and t:
            err = nvis_id_templates.validate_nvis_template(t, label=k)
            if err:
                return jsonify({"ok": False, "error": err}), 400
        if k == "RACK_VALIDATION_CT_RANGE" and not _CT_RANGE_INNER_RE.match(t):
            return jsonify(
                {
                    "ok": False,
                    "error": "RACK_VALIDATION_CT_RANGE must contain only digits, commas, and hyphens (e.g. 01-18)",
                }
            ), 400
        if k == "RACK_STATUS_BACKGROUND_INTERVAL_SEC":
            try:
                iv = int(t, 10)
            except ValueError:
                return jsonify(
                    {"ok": False, "error": "RACK_STATUS_BACKGROUND_INTERVAL_SEC must be an integer"}
                ), 400
            if iv < 5 or iv > 86400:
                return jsonify(
                    {
                        "ok": False,
                        "error": "RACK_STATUS_BACKGROUND_INTERVAL_SEC must be between 5 and 86400",
                    }
                ), 400
        if k == "RACK_WEB_GRAFANA_PORT":
            try:
                pv = int(t, 10)
            except ValueError:
                return jsonify({"ok": False, "error": "RACK_WEB_GRAFANA_PORT must be an integer"}), 400
            if pv < 1 or pv > 65535:
                return jsonify(
                    {"ok": False, "error": "RACK_WEB_GRAFANA_PORT must be between 1 and 65535"}
                ), 400
        cur[k] = t

    rack_n = (cur.get("NVIS_RACK_ID") or os.environ.get("NVIS_RACK_ID") or "").strip()
    ct_n = (cur.get("NVIS_CT_ID") or os.environ.get("NVIS_CT_ID") or "").strip()
    nvsw_n = (cur.get("NVIS_NVSW_ID") or os.environ.get("NVIS_NVSW_ID") or "").strip()
    if rack_n or ct_n or nvsw_n:
        err_nvis = native_status.validate_nvis_naming_settings(rack_n, ct_n, nvsw_n)
        if err_nvis:
            return jsonify({"ok": False, "error": err_nvis}), 400
        for legacy_k in (
            "RACK_CMSH_DS_NODE_PATTERN",
            "RACK_CMSH_DS_NODE_TOKEN_PATTERN",
            "RACK_CMSH_DS_NODE_FIND_PATTERN",
            "RACK_CMSH_DS_NODE_PARSE_PATTERN",
        ):
            cur.pop(legacy_k, None)

    bust = _job_env_status_payload_needs_rebuild(before_snapshot, cur)
    on_disk = _job_env_overrides_write(cur, bust_status_cache=bust)
    nvftp_cache_sync.ensure_background_thread()
    payload = dict(_job_environment_settings_payload())
    payload["saved"] = on_disk
    return jsonify(payload)


@app.route("/api/cluster-overview", methods=["GET"])
def api_cluster_overview():
    """Cluster-wide facts whose inventory fields use the canonical shared snapshot."""
    _ensure_rack_status_background_thread()
    nvftp_cache_sync.ensure_background_thread()
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    client_id = (request.args.get("client_id") or "").strip()
    connected_users = _touch_app_client(client_id)
    try:
        force_refresh = (request.args.get("refresh") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        # Demo inventory is instant — wait so clients never see an initializing flash.
        # Real clusters return immediately and refresh in the background.
        snapshot = get_cluster_snapshot(
            log_dir,
            force_refresh=force_refresh,
            wait=_truthy_env("RACK_STATUS_DEMO"),
        )
        payload = build_cluster_overview(log_dir, snapshot)
        payload["connected_users"] = connected_users
        return jsonify(payload)
    except Exception as e:
        _logger.debug("api_cluster_overview: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/cluster-overview/live", methods=["GET"])
def api_cluster_overview_live():
    """Non-blocking BCM / Slurm / queue live probes with per-section cache state."""
    force_refresh = (request.args.get("refresh") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        reachable, reach_detail = host_exec.reachability_state()
        cluster_id = _cluster_snapshot_identity()
        snapshot = _get_cluster_overview_live_service().get_snapshot(
            cluster_id,
            force_refresh=force_refresh,
            allow_refresh=reachable,
        )
        return jsonify(
            {
                "ok": True,
                "cluster_id": cluster_id,
                "reachable": reachable,
                "unreachable_detail": (
                    "" if reachable else (reach_detail or "head node not reachable over SSH")
                ),
                "sections": snapshot.get("sections") or {},
            }
        )
    except Exception as e:
        _logger.debug("api_cluster_overview_live: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": cluster_overview_live.safe_error(e)}), 500


@app.route("/api/app-stats", methods=["GET"])
def api_app_stats():
    """Lightweight app metrics: active users, version, and running async commands."""
    client_id = (request.args.get("client_id") or "").strip()
    connected_users = _touch_app_client(client_id)
    return jsonify(
        {
            "ok": True,
            "connected_users": connected_users,
            "app_version": _app_version_mod.app_version(),
            "async_running": _count_running_async_commands(),
            "active_client_ttl_sec": _app_client_ttl_sec(),
        }
    )


def _merge_project_name_into_status_payload(payload: dict) -> None:
    """SPA payload: optional ``RACK_WEB_PROJECT_NAME`` / ``PROJECT_NAME`` label."""
    payload["project_name"] = _project_name()


def _merge_naming_into_status_payload(payload: dict) -> None:
    """Expose ``NVIS_*`` naming hints for the SPA (validation targets, node id parsing).

    Also emits a backend-authoritative ``hierarchy`` (section -> row -> rack) built
    from the same naming module that parses/sorts rack ids, so the SPA floor view
    and any other hierarchy-aware surface consume one source of truth instead of
    re-parsing rack ids client-side.
    """
    naming = _nvis_naming()
    payload["naming"] = naming.schema_dict()
    try:
        rows = payload.get("rows") or []
        rack_ids = [r.get("rack", "") for r in rows if isinstance(r, dict) and r.get("rack")]
        payload["hierarchy"] = naming.build_hierarchy(rack_ids)
    except Exception:  # pragma: no cover - hierarchy is additive, never break status
        _logger.exception("failed to build rack hierarchy for status payload")


def _merge_cluster_reachable_into_status_payload(out: dict, reachable: bool, detail: str = "") -> None:
    """Tag a /api/status payload with cluster link state for the SPA connection banner."""
    out["cluster_reachable"] = reachable
    out["cluster_unreachable_detail"] = "" if reachable else (detail or "head node not reachable over SSH")
    out["exec_mode"] = host_exec.exec_kind()


def _degraded_status_payload(log_dir: Path, detail: str) -> dict:
    """Minimal payload returned when the cluster link is down and no cache exists yet.

    Keeps the request fast (no blocking SSH) and lets the SPA show its last-known UI
    plus a reconnecting banner instead of spinning on a dead connection.
    """
    out: dict = {
        "ok": True,
        "degraded": True,
        "served_from_cache": False,
        "rows": [],
        "summary": {"done": 0, "fail": 0, "ongoing": 0, "pending": 0},
        "footnotes": [],
        "legend": "",
        "stderr": "",
        "updated_at": None,
        "log_dir": str(log_dir),
        "rack_status_background_interval_sec": _rack_status_background_interval_sec(),
    }
    try:
        out["comments"] = read_comments()
    except Exception:
        _logger.debug("_degraded_status_payload: ignoring error", exc_info=True)
        out["comments"] = {}
    _merge_project_name_into_status_payload(out)
    _merge_naming_into_status_payload(out)
    _merge_cluster_reachable_into_status_payload(out, False, detail)
    return out


@app.route("/api/status")
def api_status():
    """Rack status from the canonical snapshot.

    ``raw=1`` remains a diagnostic collector bypass. It is never persisted and
    therefore can never become the source of Overview inventory counts.
    """
    _start_cmsh_rack_list_warm_thread()
    _ensure_rack_status_background_thread()
    nvftp_cache_sync.ensure_background_thread()
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    want_raw = request.args.get("raw") == "1"
    interval = _rack_status_background_interval_sec()
    reachable, reach_detail = host_exec.reachability_state()
    if want_raw:
        try:
            data = run_rack_status(log_dir, include_raw=True)
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "error": "rack_status.sh timed out"}), 504
        except Exception as e:
            _logger.debug("api_status: ignoring error", exc_info=True)
            return jsonify({"ok": False, "error": str(e)}), 500
        _merge_live_native_workers_into_status(data, log_dir)
        data["snapshot_bypass"] = True
        _merge_fresh_comments_into_status_payload(data)
        _merge_project_name_into_status_payload(data)
        _merge_naming_into_status_payload(data)
        _merge_cluster_reachable_into_status_payload(data, reachable, reach_detail)
        return jsonify(data)

    try:
        force_refresh = (request.args.get("refresh") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        # Demo inventory is instant — wait so clients never see an initializing flash.
        # Real clusters return immediately and refresh in the background.
        snapshot = get_cluster_snapshot(
            log_dir,
            force_refresh=force_refresh,
            wait=_truthy_env("RACK_STATUS_DEMO"),
        )
    except Exception as e:
        _logger.debug("api_status: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

    if snapshot.payload is not None:
        out = dict(snapshot.payload)
    else:
        out = {
            "ok": True,
            "returncode": 0,
            "updated_at": None,
            "log_dir": str(log_dir),
            "script": "",
            "stderr": "",
            "legend": "",
            "rows": [],
            "summary": {"done": 0, "fail": 0, "ongoing": 0, "pending": 0},
            "footnotes": [],
            "comments": {},
            "physical_rf0": {},
            "native_workers": {"mode": "snapshot_unavailable"},
        }
    out["snapshot"] = snapshot.metadata()
    out["served_from_cache"] = snapshot.payload is not None
    out["rack_status_background_interval_sec"] = interval
    _merge_fresh_comments_into_status_payload(out)
    _merge_project_name_into_status_payload(out)
    _merge_naming_into_status_payload(out)
    _merge_cluster_reachable_into_status_payload(out, reachable, reach_detail)
    return jsonify(out)


@app.route("/spa/")
@app.route("/spa/<path:path>")
def serve_spa(path: str = "index.html"):
    """Serve the React SPA from static/spa/ (built by Vite)."""
    return _serve_spa_asset(path)


@app.route("/<path:path>")
def serve_root_spa(path: str):
    """Serve React SPA for all non-API routes (client-side routing)."""
    return _serve_spa_asset(path)


# ---------------------------------------------------------------------------
# Available tools/scripts for workflow automation
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# AI Chat routes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Help / knowledge content routes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Workflow routes
# ---------------------------------------------------------------------------
















@app.route("/api/history/capture-job", methods=["GET", "POST"])
def api_capture_job_attempt():
    """``?job_id=`` — record a Slurm attempt (even one that produced no log) to history + lake."""
    jid = (request.args.get("job_id") or (request.get_json(silent=True) or {}).get("job_id") or "").strip()
    if not jid or not core.SLURM_JOB_ID_RE.fullmatch(jid):
        return jsonify({"ok": False, "error": "valid job_id required"}), 400
    return jsonify(capture_job_attempt(jid))


def capture_job_attempt(job_id: str) -> dict:
    """Record one Slurm job attempt (state, reason, exit code, nodes) to history + the lake.

    Works whether or not the job produced output — that is the entire point.
    """
    res: dict[str, object] = {"ok": False, "job_id": job_id, "error": None}
    try:
        raw = _run_scontrol_show_job(job_id)
        text = raw.get("stdout") if isinstance(raw, dict) else str(raw)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"scontrol failed: {e}"
        return res

    f = _parse_scontrol_job(text or "")
    if not f.get("JobId"):
        # scontrol only remembers a job for MinJobAge after it ends. A 40-minute soak checked an
        # hour later is simply GONE — which silently missed three completed MNUBERGEMM runs.
        # sacct reads the accounting DB and retains them, so fall back to it.
        try:
            sa = host_exec.run_slurm_on_host(
                _get_platform().sacct_cmd(
                    f"-j {shlex.quote(job_id)} -X -P -n "
                    "-o JobID,JobName,State,Elapsed,ExitCode,Start,End,NodeList,User"
                )
            )
            line = (sa.stdout or "").strip().splitlines()
            if line:
                c = line[0].split("|")
                if len(c) >= 8:
                    f = {
                        "JobId": c[0], "JobName": c[1], "JobState": c[2].split()[0],
                        "RunTime": c[3], "ExitCode": c[4], "StartTime": c[5],
                        "EndTime": c[6], "NodeList": c[7],
                        "Reason": "(from sacct — not in scontrol)",
                    }
                    res["source"] = "sacct"
        except Exception as e:  # noqa: BLE001
            res["error"] = f"sacct fallback failed: {e}"
            return res
    if not f.get("JobId"):
        res["error"] = "job not found in scontrol or sacct"
        return res

    state = f.get("JobState", "UNKNOWN")
    reason = f.get("Reason", "")
    nodelist = f.get("NodeList", "")
    rack, nodes = _rack_and_nodes_from_nodelist(nodelist)
    # A job cancelled by the prolog reports ExitCode 0:<sig> and RunTime 00:00:00 — treat it as a
    # FAILED attempt, never as a pass, even though Slurm's own ExitCode field starts with 0.
    rt = f.get("RunTime", "00:00:00")
    ran = rt not in ("00:00:00", "")
    # A soak workload (MNUBERGEMM) is EXPECTED to hit the walltime — the soak deliberately outruns
    # it. TIMEOUT after substantial runtime is a PASS on MOP 7.2 criteria; only a TIMEOUT with no
    # runtime is a real failure. Mislabelling it cost h03's successful 75.7 kW run a "fail" record.
    status = {"COMPLETED": "pass", "RUNNING": "running", "PENDING": "pending"}.get(state)
    if status is None:
        if state == "TIMEOUT":
            status = "timeout" if ran else "fail"
        elif not ran or state in ("CANCELLED", "FAILED", "NODE_FAIL", "BOOT_FAIL", "DEADLINE"):
            status = "fail"
        else:
            status = "unknown"

    record = {
        "kind": "job_attempt",
        "status": status,
        "job_id": job_id,
        "rack": rack,
        "nodes": nodes,
        "scontrol": f,
        "state": state,
        "reason": reason,
        "exit_code": f.get("ExitCode", ""),
        "run_time": f.get("RunTime", ""),
        "produced_log": False,
        "tail": text[:4000],
    }
    res["state"], res["reason"], res["status"] = state, reason, status

    try:
        rid = results_history.ingest_run({
            "run_id": f"attempt-{job_id}",
            # sacct does NOT return StdOut, so a job recovered from the accounting DB has no
            # log path. results_history requires one, and without a fallback every sacct-sourced
            # run landed in Parquet but silently failed the SQLite ingest ("source_log_path is
            # required") — i.e. half-captured, which is worse than not captured because the lake
            # looks complete. Synthesise a stable identifier instead.
            "source_log_path": f.get("StdOut") or f"sacct:{job_id}",
            "log_basename": (f.get("StdOut", "") or "").rsplit("/", 1)[-1] or f"job-{job_id}",
            "workload_kind": "job_attempt",
            "rack": rack,
            "nodes": record["nodes"],
            "job_id": job_id,
            "started_at": f.get("StartTime", ""),
            "ended_at": f.get("EndTime", ""),
            "status": status,
            "metrics": [],
            "extra": record,
        })
        res["history_run_id"] = rid
    except Exception as e:  # noqa: BLE001
        res["error"] = f"history: {e}"

    if lake_sink is not None and not lake_sink.is_disabled():
        out = lake_sink.push_run(
            run_id=lake_sink.make_run_id("jobattempt", rack, job_id),
            workload="job_attempt", rack=rack, job_id=job_id,
            nodes=record["nodes"], status=status, parsed=record,
            log_path=f.get("StdOut", ""), log_text=text or "",
        )
        res["lake"] = out
        if not out.get("ok"):
            _logger.warning("lake job-attempt push failed for %s: %s", job_id, out.get("error"))

    res["ok"] = True
    return res

# Per-domain API modules under backend/api/, discovered automatically. Drop a module in that
# package and it registers itself; nothing here changes. See backend/api/__init__.py.
import api as _api_package

_api_package.register(app)

# After all routes and middleware are registered (avoid circular import via nvftp_cache_sync._eget → app).
_bootstrap_catalog_at_startup()
_bootstrap_nvftp_sync_at_startup()
_bootstrap_validation_activities_refresh_at_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)

# ---------------------------------------------------------------------------------------------
# Job-attempt capture — a run that never produces a log MUST still be recorded.
#
# Job 63850 (MNUBERGEMM, h01) was CANCELLED in the Slurm prolog: RunTime 00:00:00, no stdout file
# ever created. It therefore left NO trace in the log dir, in results_history, or in the lake — the
# attempt was invisible even though it consumed an 18-node allocation and drained a tray. Capturing
# only successful runs is the same blank-read-as-success failure this platform exists to catch.
# ---------------------------------------------------------------------------------------------




def _rack_and_nodes_from_nodelist(nodelist: str) -> tuple[str, list[str]]:
    """Attribute a Slurm ``NodeList`` to ``(rack id, node hostnames)``.

    Two things make this less trivial than it looks:
      * Slurm reports a *compact* list (``<rack>-[01-18]``), so it has to be expanded before
        the hostnames mean anything, and ``(null)`` / ``None assigned`` for a job that never
        got an allocation.
      * Rack attribution goes through the naming engine (``NVIS_RACK_ID`` / ``NVIS_CT_ID``),
        so every site's convention works. This used to hardcode one cluster's ``ink50``
        prefix, which silently produced an empty rack and no nodes everywhere else.
    """
    spec = (nodelist or "").strip()
    if not spec or spec.lower() in ("(null)", "none assigned"):
        return "", []
    if _uparse is not None:
        hosts = _uparse.expand_nodelist(spec)
    else:  # no normaliser: bracket ranges stay unexpanded, but do not lose the plain names
        hosts = [h for h in re.split(r"[,\s]+", spec) if h]
    naming = _nvis_naming()
    # Prefer recognised trays, but never drop everything: an unrecognised convention should
    # still surface its nodes rather than report the job as having run nowhere.
    nodes = [h for h in hosts if naming.is_tray_hostname(h)] or hosts
    for h in nodes:
        hit = naming.sinfo_rack_from_host(h)
        if hit:
            return (naming.canonical_rack_id(hit[0]) or hit[0]), nodes
    return "", nodes
