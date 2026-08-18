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
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
import warnings
import zipfile
from datetime import datetime, timezone, tzinfo
from pathlib import Path, PurePosixPath

from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    request,
    send_file,
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
import validation_activities
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
from cluster_metadata import (
    _CLUSTER_DM_KEYS,
    _cluster_dashboard_metadata_path,
    read_cluster_dashboard_metadata,
)
from grafana_embed import (
    _grafana_dash_slug,
    _grafana_dash_uid,
    _grafana_deeplink_dashboards,
    _grafana_deeplinks_enabled,
    _grafana_live_5m_embed_context,
    _grafana_url_timezone_param,
    _grafana_var_host,
    _grafana_var_rack,
    _rack_web_grafana_port,
    _rack_web_grafana_uri,
    _sanitize_display_timezone_query,
)
from log_time_window import _zoneinfo_for_log_parsing
from naming import (
    _multirack_site_prefix,
    _normalize_rack_id,
    _nvis_naming,
)
from paths import (
    SCRIPTS_DIR,
    _bug_report_local_base_path,
    _bug_report_remote_dir,
    _clusterkit_script_path,
    _default_log_dir,
    _hw_check_pdsh_script_path,
    _imex_check_script_path,
    _imex_reset_script_path,
    _mnubergemm_stress_script_path,
    _nemo_xrack_script_path,
    _nvlmapper_reports_base,
    _nvloom_singlerack_script_path,
    _nvswitch_sdn_reset_script_path,
    _nvswitch_system_reset_script_path,
    _nvswitches_rack_audit_script_path,
    _parser_path,
    _rack_status_script_path,
    _resolve_log_dir,
    _run_exemplar_llama_script_path,
    _run_llama_script_path,
    _run_llmb_nccl_script_path,
    _sysinfo_script_path,
    _validation_script_path,
)
from rack_status_engine import (
    _cluster_snapshot_db_path,
    _cluster_snapshot_identity,
    _cluster_snapshot_repository,
    _extra_script_args,
    _rack_status_background_interval_sec,
    _rack_status_cache_key,
    _rack_status_cache_lock,
    _rack_status_registry,
    _try_populate_cmsh_rack_list_file,
    get_cluster_snapshot,
    run_rack_status,
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
    _reset_job_purge_later,
    _reset_jobs,
    _reset_jobs_lock,
    _stress_jobs,
    _stress_jobs_lock,
    _validation_jobs,
    _validation_jobs_lock,
)

_rack_status_exec_lock = threading.Lock()
_rack_status_cache: dict[str, dict] = {}
_rack_status_bg_thread_lock = threading.Lock()
_rack_status_bg_thread: threading.Thread | None = None
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


_APP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")




































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


































# One implementation, in the module the API packages can also import. Aliased rather than
# re-exported under a new name so the 38 call sites here stay untouched.
_truthy_env = core.truthy_env
_SLURM_STDOUT_RE = core.SLURM_STDOUT_RE
_slurm_job_base_id = core.slurm_job_base_id
from activity_hydration import (
    _hydrate_activity_task_logs,
    _task_needs_slurm_hydration,
)
from app_clients import _app_client_ttl_sec, _touch_app_client
from node_targets import (
    _CT_RANGE_INNER_RE,
    _expand_pdsh_w_slurm_nodenames,
    _validation_ct_inner,
)
from results_ingest import (
    _lake_push_job,
    _parser_export_glob,
    _parser_extra_args,
)
from slurm_query import (
    _run_squeue,
)
from validation_launch import (
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




































# Which parser.sh table each stage is the authority for. A stage's rows are taken from that
# stage's own log and nowhere else, so a NeMo log can never contribute NCCL numbers.

# Scalar/series fields that carry an actual measurement, per stage table. A row holding none of
# these is parser.sh's all-"---" placeholder rather than a result.































# --- whole-file scanner -----------------------------------------------------------------------
# The scanner client and its caches live in logscan_cache.py, imported under these names above.










def _log_ts_zone() -> tzinfo:
    return _zoneinfo_for_log_parsing(None)


























# --- Grafana deep-links (open full BCM/NMC dashboards in a new tab, scoped by rack/host) --------
#
# Unlike the /d-solo/ *embeds* (which need GF_SECURITY_ALLOW_EMBEDDING and a homegrown 2-panel
# dashboard), deep-links open a full Grafana dashboard in a new tab. On a BCM 11 / Mission Control
# (NMC) head node these dashboards already exist and are scoped by the ``$Rack`` / ``$hostname``
# template variables — so we just build ``/d/<uid>/<slug>?var-<Rack>=..&var-<hostname>=..`` URLs.
# The catalog below defaults to the stock NMC GB200 dashboards but is fully overridable via
# ``RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS`` (JSON) so this is not hardwired to any one site.

























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














def _nccl_fullrack_script_path() -> Path:
    p = _eget("RACK_NCCL_FULLRACK_SCRIPT", str(SCRIPTS_DIR / "nccl_fullrack_rounds.sh")) or str(
        SCRIPTS_DIR / "nccl_fullrack_rounds.sh"
    )
    return _rpath(Path(p))




_nvis_workspace_dir_effective = config_env._nvis_workspace_dir_effective




















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
        "RACK_STATUS_SCRIPT": str(_rack_status_script_path()),
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






_CMSH_RACK_LIST_WARM_THREAD_STARTED = False
_CMSH_RACK_WARM_START_LOCK = threading.Lock()


def _rack_sort_key_for_cmsh_list(rid: str) -> tuple[int, int, str]:
    m = re.match(r"^p(\d+)-r(\d+)$", rid.strip().lower())
    if not m:
        return (10**9, 10**9, rid)
    return (int(m.group(1)), int(m.group(2)), rid)






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




def _rack_status_background_enabled() -> bool:
    return not _truthy_env("RACK_STATUS_BACKGROUND_DISABLED")










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






def _rack_status_cache_clear_all() -> None:
    """Shared invalidation entry point for status-affecting configuration changes."""
    _cluster_snapshot_repository().bump_configuration_generation()
    with _rack_status_cache_lock:
        _rack_status_cache.clear()






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
























# ---------------------------------------------------------------------------
# BMC recovery and MAC change
#
# Both target a single head node where BCM (cmsh / pythoncm) + ipmitool live.
# Invocation always routes through host_exec so the same code path serves
# local-on-head and dashboard-in-container deployments. Each call writes its
# stdout/stderr/returncode into the shared _reset_jobs table; the frontend
# polls /api/reset-job-status?job_id=… (no new polling endpoint needed).
# ---------------------------------------------------------------------------































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







































































































# last_job / scancel: plain id or job array id (e.g. 12345_0)
_SLURM_SCANCEL_JOB_ID_RE = core.SLURM_JOB_ID_RE  # one definition, in core








# Terminal (finished) job states — what the Scheduler "recent jobs" panel shows so a
# job that has left squeue (success OR failure) is still visible with its log.


























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










# --- whole-directory inventory (every extension, every depth) ------------------------




# --- history read routes -------------------------------------------------------------


# Alias kept because TRACK-A's integration note documents this spelling.


# --- universal ingest routes ---------------------------------------------------------








































































# Metrics worth a fleet baseline, per workload kind. Names match what
# ``results_history`` stores from parser.sh (records_from_parser_json) and from
# ``universal_parser`` — both write the same lower-cased metric_name.
















#: Report kind -> the formats it can render, first entry being the default.
#:
#: The Nscale report answers a customer's JSON Schema, so JSON is the only honest format for it —
#: a CSV of that document would not validate against the contract it exists to satisfy. The Rack
#: Status report is ours and carries the same dataset in whichever shape the reader needs.

#: Filename extension and MIME type per format.
















#: Small on purpose: each finished job pins a multi-MB document until it is collected.
#: How long a "running" job stays eligible for idempotent reuse. Comfortably longer than a
#: full sweep (~4 minutes at 2000 nodes) and short enough that a dead worker frees its key.









































































# Custom launch env vars (UI "Extra environment variables" field). A key must be a valid
# POSIX-ish env name; values are single-line and length-capped; the count is bounded. These
# guardrails keep a well-formed KEY=VALUE map without allowing injection of newlines / huge
# payloads into the job environment. Forwarded as env_extra (bypasses the SSH allowlist) and
# chained to the container via each launcher's --export=ALL.

# Upper bound for the launch dialog's NEMO step selector. The UI offers 50 (perf) and 2010
# (soak); the cap only rejects absurd hand-crafted payloads, it does not pin the choices.




























































































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




