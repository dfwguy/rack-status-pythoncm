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
"""The dashboard core: rack status, cluster overview, app stats, and the settings writer.

What is left of the original ``app.py`` once every other domain moved out. These are the routes the SPA
calls on load, and they are the ones that must stay cheap:

* ``api_status`` and ``api_cluster_overview`` are two renderings of the shared snapshot from
  ``rack_status_engine``; neither collects inventory itself. ``raw=1`` on status is a diagnostic
  collector bypass and is deliberately never persisted, so it cannot become the source of Overview's
  counts.
* The background warmers (``_ensure_rack_status_background_thread``,
  ``_start_cmsh_rack_list_warm_thread``) are started lazily BY these routes rather than at boot, so a
  deployment nobody is looking at does not scan the fleet on a timer.
* ``api_cluster_overview_live`` is the opt-in probe path: version and queue probes that talk to the
  cluster, kept separate from the cached overview precisely so the cached one never pays for them.
* ``_job_env_overrides_write`` is the settings WRITER, the counterpart to the readers in ``config_env``.
  It busts the rack-status cache and rebuilds the transport connection, because settings like
  ``RACK_SCRIPT_SUDO`` change how commands run and must take effect without an app restart.

``_merge_*_into_status_payload`` is a deliberate pattern: the snapshot is machine-collected, and comments,
naming, project name and live worker state are layered on top at read time. That keeps operator-authored
data out of the cached artifact, so editing a comment does not invalidate a fleet scan.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import app_version as _app_version_mod
import cluster_overview_live
import cluster_snapshot
import host_exec
import native_status
import nvftp_cache_sync
import nvis_id_templates
from app_clients import _app_client_ttl_sec, _touch_app_client
from audit.actions import action
from cluster_metadata import (
    _CLUSTER_DM_KEYS,
    _cluster_dashboard_metadata_path,
    read_cluster_dashboard_metadata,
)
from comments_store import read_comments
from config_env import (
    _JOB_ENV_ALLOWED_KEYS,
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
    _nvis_workspace_dir_effective,
)
from core import rpath as _rpath
from core import truthy_env as _truthy_env
from flask import Blueprint, jsonify, request
from grafana_embed import (
    _grafana_dash_slug,
    _grafana_dash_uid,
    _grafana_var_host,
    _grafana_var_rack,
    _rack_web_grafana_port,
    _rack_web_grafana_uri,
)
from job_registry import (
    _reset_jobs,
    _reset_jobs_lock,
    _stress_jobs,
    _stress_jobs_lock,
    _validation_jobs,
    _validation_jobs_lock,
)
from naming import _multirack_site_prefix, _nvis_naming
from node_targets import _CT_RANGE_INNER_RE, _validation_ct_inner
from paths import (
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
from platforms import get_platform as _get_platform
from rack_status_engine import (
    _cluster_snapshot_db_path,
    _cluster_snapshot_identity,
    _extra_script_args,
    _rack_status_background_interval_sec,
    _rack_status_cache_clear_all,
    _rack_status_cache_key,
    _rack_status_cache_lock,
    _rack_status_registry,
    _try_populate_cmsh_rack_list_file,
    get_cluster_snapshot,
    run_rack_status,
)
from results_ingest import _parser_export_glob, _parser_extra_args
from slurm_query import _run_squeue

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("dashboard", __name__, url_prefix="")


_rack_status_bg_thread_lock = threading.Lock()


_rack_status_bg_thread: threading.Thread | None = None


_cluster_overview_live_service_lock = threading.Lock()


_cluster_overview_live_service_cache: (
    tuple[str, cluster_overview_live.ClusterOverviewLiveService] | None
) = None


# Dashboard Settings → job / validation env overrides (merged into subprocess env and ``_eget``
# reads). ``section`` groups rows in Settings, in this order. Note left behind in app.py when the
# constant moved here during the api/ migration.
JOB_ENV_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard & charts"),
    ("external", "External connections"),
    ("nvftp", "NVFTP cache sync"),
    ("native_cmsh", "Native status, parser & cmsh"),
    ("jobs_cluster", "Validation, jobs & cluster tools"),
)


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


def _cluster_env_pick(*keys: str, default: str = "") -> str:
    for k in keys:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v.replace("\n", " ").replace("\r", " ").strip()
    return default


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


@bp.route("/api/cluster-dashboard-metadata", methods=["GET", "PUT"])
@action("settings:update_dashboard")
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


@bp.route("/api/settings/job-environment", methods=["GET", "PUT"])
@action("settings:update_job_env")
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


@bp.route("/api/cluster-overview", methods=["GET"])
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


@bp.route("/api/cluster-overview/live", methods=["GET"])
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


@bp.route("/api/app-stats", methods=["GET"])
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


@bp.route("/api/status")
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
