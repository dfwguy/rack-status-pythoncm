#!/usr/bin/env python3
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
import hashlib
import io
import json
import logging
import math
import os
import pty
import queue
import re
import secrets
import select
import signal
import shlex
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import uuid
import time
import warnings
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
    send_file,
    session,
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

import native_status  # noqa: E402 — after sys.path so ``import native_status`` works when cwd != APP_DIR
import nvis_id_templates  # noqa: E402


def _nvis_naming() -> nvis_id_templates.NvisNaming:
    return nvis_id_templates.get_nvis_naming()
import host_exec  # noqa: E402
import ai_chat  # noqa: E402
import help_content  # noqa: E402
import workflow_engine  # noqa: E402
import rack_locks  # noqa: E402
import token_vault  # noqa: E402
import nvftp_cache_sync  # noqa: E402
from platforms import get_platform as _get_platform  # noqa: E402

# Catalog (platforms/tools/projects). Optional at import time so the app still boots if
# the store is unavailable; all call sites degrade to the legacy env behavior.
try:
    from catalog import context as catalog_context  # noqa: E402
    from catalog import seed as catalog_seed  # noqa: E402
    from catalog import store as catalog_store  # noqa: E402
    from catalog.api import catalog_bp  # noqa: E402

    _CATALOG_AVAILABLE = True
except Exception as _catalog_exc:  # pragma: no cover - defensive
    logging.getLogger(__name__).warning("catalog unavailable: %s", _catalog_exc)
    catalog_context = None  # type: ignore[assignment]
    catalog_seed = None  # type: ignore[assignment]
    catalog_store = None  # type: ignore[assignment]
    catalog_bp = None  # type: ignore[assignment]
    _CATALOG_AVAILABLE = False

NVFTP_TOKENS_URL = (
    "https://nvftp.nvidia.com/workspace/1099ff0b-621c-490f-a662-c69583ccabad/tokens"
)

_REMOTE_BASE = os.environ.get("RACK_REMOTE_BASE_PATH", "").strip()
if _REMOTE_BASE:
    SCRIPTS_DIR = Path(_REMOTE_BASE) / "bin" / "scripts"
    DEFAULT_LOG_DIR = Path(_REMOTE_BASE) / "var" / "logs"
else:
    SCRIPTS_DIR = REPO_ROOT / "bin" / "scripts"
    DEFAULT_LOG_DIR = REPO_ROOT / "var" / "logs"

DEFAULT_SCRIPT = SCRIPTS_DIR / "rack_status.sh"
# Output basename for Tools → 05-download-containers.sh (must match script).
_TOOLS_NEMO_CONTAINER_SQSH = "nemo-25.11.01.sqsh"


def _script_exists(path: Path) -> bool:
    """Check if a script file exists. In SSH mode, skip the check (file is on the remote host)."""
    if host_exec.is_remote():
        return True
    return path.is_file()

app = Flask(__name__, static_folder=str(REPO_ROOT / "static"), static_url_path="/static")
_logger = logging.getLogger(__name__)

try:
    from flask_sock import Sock
    _sock = Sock(app)
except ImportError:
    _sock = None
    _logger.warning("flask-sock not installed; /ws/terminal will be unavailable")

if _CATALOG_AVAILABLE and catalog_bp is not None:
    app.register_blueprint(catalog_bp)

_validation_jobs_lock = threading.Lock()
_validation_jobs: dict[str, dict[str, object]] = {}

_stress_jobs_lock = threading.Lock()
_stress_jobs: dict[str, dict[str, object]] = {}

_xrack_jobs_lock = threading.Lock()
_xrack_jobs: dict[str, dict[str, object]] = {}

_llama_jobs_lock = threading.Lock()
_llama_jobs: dict[str, dict[str, object]] = {}

_physical_rf0_lock = threading.Lock()
_physical_rf0_map: dict[str, str] = {}
_physical_rf0_loaded = False
_app_clients_lock = threading.Lock()
_app_clients_last_seen: dict[str, float] = {}

_rack_status_cache_lock = threading.Lock()
_rack_status_exec_lock = threading.Lock()
_rack_status_cache: dict[str, dict] = {}
_rack_status_registry: set[str] = set()
_rack_status_bg_thread_lock = threading.Lock()
_rack_status_bg_thread: threading.Thread | None = None

# Dashboard Settings → job / validation env overrides (merged into subprocess env and ``_eget`` reads).
# ``section`` groups rows in Settings (see ``JOB_ENV_SECTION_ORDER``).
JOB_ENV_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard & charts"),
    ("external", "External connections"),
    ("nvftp", "NVFTP cache sync"),
    ("native_cmsh", "Native status, parser & cmsh"),
    ("jobs_cluster", "Validation, jobs & cluster tools"),
)
JOB_ENV_UI_DEFINITIONS: list[dict[str, str]] = [
    {
        "section": "dashboard",
        "key": "RACK_STATUS_BACKGROUND_INTERVAL_SEC",
        "label": "Status background refresh (seconds)",
        "description": "How often the server refreshes cached native rack status (min 5). The background loop re-reads this after each sleep, so saves apply within roughly one interval (restart for an immediate first tick).",
    },
    {
        "section": "dashboard",
        "key": "RACK_WEB_PROJECT_NAME",
        "label": "Page title suffix",
        "description": "Optional short label appended to HTML titles (shell PROJECT_NAME is still read if this is unset).",
    },
    {
        "section": "nvftp",
        "key": "RACK_PROJECT_TOKEN",
        "label": "NVFTP project folder name",
        "description": (
            "Top-level folder on the NVFTP remote path for this site (e.g. mistral-p2). "
            "var/cache and rack_comments.json mirror to /nvis-project-view/<token>/cache/ every RACK_NVFTP_SYNC_INTERVAL_SEC. "
            "Leave empty to disable uploads."
        ),
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_SFTP_USER",
        "label": "NVFTP SFTP username",
        "description": (
            "SFTP user from your NVFTP workspace. "
            "Password is the token saved via Overview → Set Project View."
        ),
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_REMOTE_BASE",
        "label": "NVFTP remote base path",
        "description": (
            "Optional POSIX prefix before /nvis-project-view/{RACK_PROJECT_TOKEN}/ on transfer.nvftp.nvidia.com. "
            "Leave empty for the default /nvis-project-view/… layout."
        ),
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_SYNC_INTERVAL_SEC",
        "label": "NVFTP cache sync interval (seconds)",
        "description": "How often var/cache is uploaded when sync is enabled (default 7200 = 2 hours, min 300).",
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_SFTP_HOST",
        "label": "NVFTP SFTP host",
        "description": "SFTP hostname (default transfer.nvftp.nvidia.com).",
    },
    {
        "section": "external",
        "key": "RACK_PROMETHEUS_LOG_TIMESTAMP_TZ",
        "label": "Log timestamp timezone (IANA)",
        "description": "Zone for naive timestamps in NEMO logs and Grafana axis defaults when RACK_WEB_GRAFANA_TIMEZONE is unset (e.g. UTC, America/Los_Angeles).",
    },
    {
        "section": "external",
        "key": "RACK_PROMETHEUS_BASIC_AUTH_USER",
        "label": "Prometheus basic auth username",
        "description": "Same variable as USER in ~/.prometheusrc (Bright Prometheus). Used by Utility → launch Grafana script when set together with the password; leave both empty to use PROMETHEUS_RC / default rc paths on the server.",
    },
    {
        "section": "external",
        "key": "RACK_PROMETHEUS_BASIC_AUTH_PASSWORD",
        "label": "Prometheus basic auth password",
        "description": "Same variable as PASSWORD in ~/.prometheusrc. Stored in server overrides like other settings; restrict file permissions on var/cache/job_env_overrides.json.",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_URI",
        "label": "Grafana base URL",
        "description": "Full origin without trailing slash (e.g. https://grafana.example). Empty uses same host as the app.",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_PORT",
        "label": "Grafana port",
        "description": "Port when Grafana is on the same host as the browser (default 3000).",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_DASH_UID",
        "label": "Grafana dashboard UID",
        "description": "UID path segment for embedded GPU / thermal panels (default bright-gpu-per-host).",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_DASH_SLUG",
        "label": "Grafana dashboard slug",
        "description": "Slug in Grafana URLs for the embedded dashboard (default bright-gpu-thermal-and-power).",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_TIMEZONE",
        "label": "Grafana timezone query",
        "description": "browser, utc, or an IANA zone. When empty, the server uses the same zone as RACK_PROMETHEUS_LOG_TIMESTAMP_TZ.",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_STATUS_SCRIPT",
        "label": "rack_status.sh path",
        "description": "Main rack status script (native table and probe-json).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_STATUS_EXTRA_ARGS",
        "label": "rack_status.sh extra args",
        "description": "Extra arguments appended to rack_status.sh (shell-split).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_SCRIPT",
        "label": "parser.sh path",
        "description": "Log parser script for /api/parser-results.",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_EXTRA_ARGS",
        "label": "parser.sh extra args",
        "description": "Extra arguments before --json (shell-split).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_EXPORT_GLOB",
        "label": "Parser export glob",
        "description": "Basename glob under log dir for exports (default *.txt).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_TIMEOUT",
        "label": "Parser timeout (seconds)",
        "description": "Timeout for parser.sh --json in the parser API.",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_EXCEL_REPORT_DIR",
        "label": "Excel report directory",
        "description": "Output directory for Excel exports (optional).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_MULTIRACK_SITE_PREFIX",
        "label": "Multirack site prefix",
        "description": "Pod id for multirack log names without pN- (default p5).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_MULTIRACK_VALIDATION_TEST",
        "label": "Multirack probe VALIDATION_TEST",
        "description": "Default VALIDATION_TEST for multirack probe-json (e.g. nccl_all_reduce).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_SLURM_SKIP_MODULE_LOAD",
        "label": "Skip module load for Slurm",
        "description": (
            "Set to 1 if squeue/sinfo/scontrol are already on PATH (avoids ``module load slurm`` and "
            "bash-completion errors on the head node)."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "RACK_MODULES_INIT",
        "label": "Environment modules init script",
        "description": (
            "Optional path to source before ``module load`` (e.g. "
            "/cm/local/apps/environment-modules/current/init/bash). Leave empty for auto-detect."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "RACK_CMSH",
        "label": "cmsh executable",
        "description": "Bright cmsh command (power reset, probes, cluster overview).",
    },
    {
        "section": "native_cmsh",
        "key": "NVIS_RACK_ID",
        "label": "Rack id pattern",
        "description": (
            "How a rack row is named in the dashboard. Default: [\\d+] (digits as in cmsh). "
            "Bright default: p[\\d+]-r[\\d+] (e.g. p5-r03). Optional [01-99] for zero-padded two-digit fields. "
            "CBR default: CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99]."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "NVIS_CT_ID",
        "label": "Compute tray hostname pattern",
        "description": (
            "Full compute node name from cmsh ds (must start with the rack pattern and add tray slot(s) at the end). "
            "Bright default: p[\\d+]-r[\\d+]-ct[\\d+] (e.g. p5-r03-ct12). "
            "CBR default: CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99][01-99]."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "NVIS_NVSW_ID",
        "label": "NVSwitch hostname pattern (optional)",
        "description": (
            "Naming pattern for NVSwitch devices (reserved for future UI). "
            "Bright default: p[\\d+]-r[\\d+]-nvsw-[\\d+] (e.g. p03-r13-nvsw-9). "
            "CBR default: CBR[\\d+]-DH[\\d+]-NSW-NV[01-99][01-99][01-99]."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "VALIDATION_TEST",
        "label": "Validation test suite (parser + rack status)",
        "description": (
            "Comma-separated tokens for NCCL/LLM pass (see bin/scripts/validation_test_env.sh). "
            "Typical single-rack NEMO logs: nccl_all_reduce,llm. Full NCCL sweep: "
            "nccl_all_reduce,nccl_alltoall,nccl_allgather,llm. When unset, the server defaults to "
            "nccl_all_reduce,llm for singlerack runs (multirack probe-json uses RACK_MULTIRACK_VALIDATION_TEST only)."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_CT_RANGE",
        "label": "Validation CT range",
        "description": "Tray indices inside pNN-rMM-ct[…] for singlerack validation (e.g. 01-18).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_SCRIPT",
        "label": "Singlerack validation script",
        "description": "Path to run_singlerack.sh (or site equivalent).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_TIMEOUT",
        "label": "Validation timeout (seconds)",
        "description": "Max wall time for synchronous / async singlerack validation.",
    },
    {
        "section": "jobs_cluster",
        "key": "NEMO_SQSH_SRC",
        "label": "NEMO container .sqsh path",
        "description": "Enroot image path or URI fragment; used for Setup defaults and skip checks.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NEMO_XRACK_SCRIPT",
        "label": "Multinode NEMO script",
        "description": "Path to run-nemo-xrack.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NEMO_XRACK_TIMEOUT",
        "label": "NEMO xrack timeout (seconds)",
        "description": "Background job timeout for multinode NEMO.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_LLAMA_SCRIPT",
        "label": "Llama script",
        "description": "Path to run-llama.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_LLAMA_TIMEOUT",
        "label": "Llama timeout (seconds)",
        "description": "Background job timeout for Llama runs.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_MNUBERGEMM_STRESS_SCRIPT",
        "label": "MNU Bergemm stress script",
        "description": "Path to run_mnubergemm_stress.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_MNUBERGEMM_CUDA_MAJOR",
        "label": "MNUBERGEMM stress CUDA major",
        "description": "12 or 13; selects HPC-X unpack under NVIS_WORKSPACE_DIR (default 13). Falls back to RACK_NVLOOM_CUDA_MAJOR.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_MNUBERGEMM_PARTITION",
        "label": "MNUBERGEMM stress Slurm partition",
        "description": "If set, run_mnubergemm_stress.sh passes sbatch -p … (omit for site default).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_SINGLERACK_SCRIPT",
        "label": "nvloom singlerack driver script",
        "description": "Path to run_nvloom_singlerack.sh (Slurm sbatch --wait; logs under log_dir/nvloom/).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_SINGLERACK_TIMEOUT",
        "label": "nvloom singlerack timeout (seconds)",
        "description": "Background job timeout for nvloom Slurm wrapper (sbatch --wait).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_RACK_SAMPLES",
        "label": "nvloom gpuToRackSamples",
        "description": "Integer 1–256 for --gpuToRackSamples (default 5). Passed as RACK_SAMPLES.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_NVLOOM_UCX_TLS",
        "label": "nvloom UCX_TLS override",
        "description": (
            "Optional UCX_TLS string for the nvloom srun step (Spectrum-X / RoCE). "
            "When unset, nvloom_singlerack.sbatch uses rc,cuda_copy,cuda_ipc,cuda,self only if UCX_TLS is not already set."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_SCRIPT",
        "label": "ClusterKit singlerack driver script",
        "description": "Path to run_clusterkit.sh (Slurm; logs under log_dir/clusterkit/).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_TIMEOUT",
        "label": "ClusterKit singlerack timeout (seconds)",
        "description": "Background job timeout for ClusterKit Slurm wrapper (default 14400).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_PARTITION",
        "label": "ClusterKit Slurm partition",
        "description": "If set, run_clusterkit.sh passes sbatch -p … (omit for site default).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_CUDA_MAJOR",
        "label": "ClusterKit CUDA major (HPC-X bundle)",
        "description": "12 or 13; passed as CUDA_MAJ (default 13). Falls back to RACK_NVLOOM_CUDA_MAJOR.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_RDMA_LINK_TECH",
        "label": "RDMA link technology (global)",
        "description": (
            "auto | mlx | ib | roce | spectrumx | spx. Default auto: InfiniBand-style mlx5 sysfs when usable, else RoCE/Spectrum-X. "
            "Other validation scripts may source nvis-rdma-common.inc.sh; ClusterKit uses this when RACK_CLUSTERKIT_LINK_TECH is unset."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_LINK_TECH",
        "label": "ClusterKit interconnect mode",
        "description": (
            "Same values as NVIS_RDMA_LINK_TECH; overrides the global default for singlerack ClusterKit only. "
            "Use roce or spx on Spectrum-X; use mlx or ib for classic IB mlx5 local_cpulist mapping."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_HCA_PROFILE",
        "label": "ClusterKit HCA preset profile",
        "description": (
            "Preset rail table: mlx — gb300_mlx4 (default), b300_mlx8; roce — gb300_roce_spx_quad (default), "
            "gb300_roce_spx_rail, b300_roce_dual_8. Must match RACK_CLUSTERKIT_LINK_TECH / NVIS_RDMA_LINK_TECH family."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_UCX_TLS",
        "label": "ClusterKit UCX_TLS (RoCE branch)",
        "description": "Optional. When interconnect is RoCE/Spectrum-X, clusterkit.sbatch sets UCX_TLS to this value (default rc,cuda).",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_SLURM_HOSTLIST",
        "label": "Slurm sbatch -w override (comma-separated)",
        "description": "Optional. If set, run_clusterkit.sh / run_nvloom_singlerack.sh / run_mnubergemm_stress.sh use this exact NodeName list for sbatch -w instead of expanding the validation target. Use when Slurm NodeNames differ from NVIS hostnames (FQDN, prefix, or casing). Leave empty to auto-expand via scontrol.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_WORKSPACE_DIR",
        "label": "Workspace root (HPC-X / CUDA / nvloom)",
        "description": "Absolute path; default /home/cmsupport/workspace. Used by nvloom singlerack, MNUBERGEMM stress, and install scripts (TOPDIR).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_WORKSPACE_DIR",
        "label": "Workspace root (alias)",
        "description": "Optional; if NVIS_WORKSPACE_DIR is unset, this sets the same workspace path.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_CUDA_MAJOR",
        "label": "nvloom CUDA major (HPC-X bundle)",
        "description": '12 or 13; selects hpcx unpack name under the workspace (default 13). Passed as CUDA_MAJ.',
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SYSINFO_SCRIPT",
        "label": "sysinfo / Precheck script",
        "description": "Path to sysinfo.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SYSINFO_TIMEOUT",
        "label": "sysinfo timeout (seconds)",
        "description": "Timeout for Precheck / sysinfo streams.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_HW_CHECK_SCRIPT",
        "label": "Hardware check (pdsh) script",
        "description": "Path to hw_check_pdsh.sh when set; default uses bundled script.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_GPU_RESET_SCRIPT",
        "label": "GPU reset script",
        "description": "Site GPU reset script path.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVSWITCH_AUDIT_SCRIPT",
        "label": "NVSwitch audit script",
        "description": "Path to nvswitches_rack_audit.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVSWITCH_SDN_RESET_SCRIPT",
        "label": "NVSwitch SDN reset script",
        "description": "Path to nvswitch_sdn_reset.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT",
        "label": "NVSwitch system reset script",
        "description": "Path to nvswitch_system_reset.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_IMEX_RESET_SCRIPT",
        "label": "IMEX reset script",
        "description": "Path to imex_reset.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_IMEX_CHECK_SCRIPT",
        "label": "IMEX check script",
        "description": "Path to imex_check.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLMAPPER_REPORTS_BASE",
        "label": "NVLMapper reports base",
        "description": "Directory under which NVLMapper stages per-rack work.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVLMAPPER_RC",
        "label": "NVLMapper credentials file",
        "description": (
            "Path to key=value file (username= / user=, password=) merged into generated YAML by run_nvlmapper.sh. "
            "Default /root/.nvlmapperrc. Ignored when NVLMAPPER_USERNAME and NVLMAPPER_PASSWORD are both set."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "NVLMAPPER_USERNAME",
        "label": "NVLMapper username",
        "description": (
            "BMC/switch user for nvlmapper when not using a credentials file alone. When set together with "
            "NVLMAPPER_PASSWORD, overrides NVLMAPPER_RC file contents for the merge step."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "NVLMAPPER_PASSWORD",
        "label": "NVLMapper password",
        "description": "Password paired with NVLMAPPER_USERNAME. Stored in server overrides like Prometheus auth; restrict permissions on var/cache/job_env_overrides.json.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SLURM_DRAIN_REASON",
        "label": "Slurm drain reason",
        "description": "Reason string passed to scontrol drain (default admin).",
    },
]
_JOB_ENV_ALLOWED_KEYS: frozenset[str] = frozenset(d["key"] for d in JOB_ENV_UI_DEFINITIONS)
# Snapshot of process env for allowed keys at import (used when an override is cleared in Settings).
_JOB_ENV_SHELL_BASELINE: dict[str, str | None] = {k: os.environ.get(k) for k in _JOB_ENV_ALLOWED_KEYS}
# Changes to these keys affect the cached native / shell ``/api/status`` payload; others skip a full bust.
_STATUS_ROW_ENV_KEYS: frozenset[str] = frozenset(
    {
        "RACK_STATUS_SCRIPT",
        "RACK_STATUS_EXTRA_ARGS",
        "RACK_CMSH",
        "NVIS_RACK_ID",
        "NVIS_CT_ID",
        "NVIS_NVSW_ID",
        "RACK_SLURM_SKIP_MODULE_LOAD",
        "RACK_MODULES_INIT",
        "RACK_MULTIRACK_SITE_PREFIX",
        "RACK_MULTIRACK_VALIDATION_TEST",
        "VALIDATION_TEST",
        "RACK_PARSER_SCRIPT",
        "RACK_PARSER_EXTRA_ARGS",
        "RACK_PARSER_EXPORT_GLOB",
        "RACK_PARSER_TIMEOUT",
    }
)
_job_env_file_lock = threading.Lock()
_JOB_ENV_OVERRIDES_CACHE: dict[str, str] | None = None
_JOB_ENV_OVERRIDES_MTIME: float | None = None
_JOB_ENV_BOOTSTRAP_DONE = False


def _materialize_job_env_overrides_into_os_environ(ov: dict[str, str]) -> None:
    """Copy dashboard JSON overrides into ``os.environ`` so ``native_status`` / ``host_exec`` inherit them."""
    for k in _JOB_ENV_ALLOWED_KEYS:
        if k in ov and ov[k].strip():
            os.environ[k] = ov[k].strip()[:8192]
        else:
            base = _JOB_ENV_SHELL_BASELINE.get(k)
            if base is not None:
                os.environ[k] = base
            else:
                os.environ.pop(k, None)
    # This strips Settings-managed keys not present in the overrides; re-apply the active
    # catalog overlay so platform/cluster-derived values (e.g. RACK_VALIDATION_CT_RANGE)
    # survive. Only runs on cache-miss materialization, so this is infrequent.
    _apply = globals().get("_apply_catalog_active_to_environ")
    if callable(_apply):
        try:
            _apply()
        except Exception:
            pass


def _job_env_status_payload_needs_rebuild(before: dict[str, str], after: dict[str, str]) -> bool:
    for k in _STATUS_ROW_ENV_KEYS:
        if (before.get(k) or "").strip() != (after.get(k) or "").strip():
            return True
    return False


def _job_env_overrides_path() -> Path:
    return (REPO_ROOT / "var" / "cache" / "job_env_overrides.json").resolve()


def _job_env_flock_path() -> Path:
    """Cross-process lock so Gunicorn workers / NFS do not read partially updated overrides."""
    return (_job_env_overrides_path().parent / "job_env_overrides.flock").resolve()


@contextmanager
def _job_env_flock(shared: bool = True):
    """``shared=True`` (LOCK_SH) for readers, ``False`` (LOCK_EX) for writers."""
    if _truthy_env("RACK_STATUS_DEMO"):
        yield
        return
    p = _job_env_flock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+", encoding="utf-8")
    locked = False
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            locked = True
        except (OSError, AttributeError):
            pass
        yield
    finally:
        if locked:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            f.close()
        except OSError:
            pass


_TOOL_EXCLUSIVE_LOCK_FILES: dict[str, str] = {
    "download_containers": "tools_download_containers.flock",
    "install_hpcx_workspace": "tools_install_hpcx_workspace.flock",
    "install_cuda_nccl_workspace": "tools_install_cuda_nccl_workspace.flock",
    "install_nvloom_workspace": "tools_install_nvloom_workspace.flock",
    "launch_grafana_bright_prometheus": "tools_launch_grafana_bright_prometheus.flock",
}

_TOOL_EXCLUSIVE_LOCK_BUSY_MSG: dict[str, str] = {
    "download_containers": (
        "Another session is already running the NEMO container download. Wait for it to finish."
    ),
    "install_hpcx_workspace": (
        "Another session is already running the Mellanox HPC-X workspace install. Wait for it to finish."
    ),
    "install_cuda_nccl_workspace": (
        "Another session is already running the CUDA toolkit / NCCL workspace install. Wait for it to finish."
    ),
    "install_nvloom_workspace": (
        "Another session is already running the nvloom workspace install. Wait for it to finish."
    ),
    "launch_grafana_bright_prometheus": (
        "Another session is already running the Grafana + Prometheus launch script. Wait for it to finish."
    ),
}


class _ToolsExclusiveLockBusyError(Exception):
    """Non-blocking flock(2) LOCK_EX failed — another process/session holds this Utility lock."""


def _tools_exclusive_lock_path(lock_name: str) -> Path:
    fname = _TOOL_EXCLUSIVE_LOCK_FILES.get(lock_name)
    if not fname:
        raise ValueError(f"unknown tools lock: {lock_name!r}")
    p = (REPO_ROOT / "var" / "cache" / fname).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _tools_exclusive_lock(lock_name: str):
    """Hold an advisory exclusive flock for one Utility script (cross-process / multi-worker)."""
    path = _tools_exclusive_lock_path(lock_name)
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise _ToolsExclusiveLockBusyError(_TOOL_EXCLUSIVE_LOCK_BUSY_MSG.get(lock_name, "This tool is already running."))
    try:
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


def _tools_try_acquire_exclusive_flock_for_stream(lock_name: str):
    """Return ``(fh, None)`` on success, or ``(None, busy_message)`` if another holder has LOCK_EX."""
    path = _tools_exclusive_lock_path(lock_name)
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            fh.close()
        except OSError:
            pass
        return None, _TOOL_EXCLUSIVE_LOCK_BUSY_MSG.get(lock_name, "This tool is already running.")
    return fh, None


def _tools_release_exclusive_flock_fh(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def _read_job_env_file_raw(p: Path) -> dict[str, str]:
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k not in _JOB_ENV_ALLOWED_KEYS:
            continue
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:8192]
        elif v is not None and str(v).strip():
            out[k] = str(v).strip()[:8192]
    return out


def _job_env_overrides_load() -> dict[str, str]:
    """Effective overrides saved from Settings (merged on top of process env for jobs)."""
    global _JOB_ENV_OVERRIDES_CACHE, _JOB_ENV_OVERRIDES_MTIME
    p = _job_env_overrides_path()
    # Keep native_status on the same file as this process (covers forked workers / odd env).
    os.environ["NVIS_JOB_ENV_OVERRIDES_FILE"] = str(p)
    with _job_env_flock(shared=True):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            with _job_env_file_lock:
                _JOB_ENV_OVERRIDES_CACHE = {}
                _JOB_ENV_OVERRIDES_MTIME = None
            _materialize_job_env_overrides_into_os_environ({})
            return {}
        with _job_env_file_lock:
            if _JOB_ENV_OVERRIDES_CACHE is not None and _JOB_ENV_OVERRIDES_MTIME == mtime:
                return dict(_JOB_ENV_OVERRIDES_CACHE)
        data = _read_job_env_file_raw(p)
        with _job_env_file_lock:
            _JOB_ENV_OVERRIDES_CACHE = data
            _JOB_ENV_OVERRIDES_MTIME = mtime
        _materialize_job_env_overrides_into_os_environ(data)
        return dict(data)


def _job_env_overrides_invalidate_cache() -> None:
    global _JOB_ENV_OVERRIDES_CACHE, _JOB_ENV_OVERRIDES_MTIME
    with _job_env_file_lock:
        _JOB_ENV_OVERRIDES_CACHE = None
        _JOB_ENV_OVERRIDES_MTIME = None


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
        _materialize_job_env_overrides_into_os_environ(clean)
    try:
        native_status.invalidate_job_env_file_cache()
    except Exception:
        pass
    if bust_status_cache:
        _rack_status_cache_clear_all()
    return clean


def _eget(name: str, default: str | None = None) -> str | None:
    """Environment getter with catalog + dashboard Settings overrides.

    Precedence (highest first): active cluster identity/naming/connection (catalog
    ``high`` overlay) > dashboard Settings overrides (``job_env_overrides.json``) >
    process env > platform geometry/tool defaults (catalog ``low`` overlay) > *default*.
    """
    high: dict[str, str] = {}
    low: dict[str, str] = {}
    if _CATALOG_AVAILABLE and catalog_context is not None:
        try:
            high, low = catalog_context.overlays_for_active()
        except Exception:
            high, low = {}, {}
    if name in high and high[name].strip():
        return high[name].strip()
    ov = _job_env_overrides_load()
    if name in ov and ov[name].strip():
        return ov[name].strip()
    v = os.environ.get(name)
    if v is not None:
        return v
    if name in low and low[name].strip():
        return low[name].strip()
    return default


def _eget_int(name: str, default: int) -> int:
    """Like ``_env_int`` but honors catalog overlays + Settings overrides via ``_eget``.

    Used for platform-geometry-derived knobs (CT range count, GPUs/tray, nodes/rack)
    so the active platform's geometry applies unless explicitly overridden.
    """
    raw = _eget(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def _subprocess_env(
    updates: dict[str, str] | None = None,
    *,
    validation_test_if_unset: str = "nccl_all_reduce,llm",
) -> dict[str, str]:
    high: dict[str, str] = {}
    low: dict[str, str] = {}
    if _CATALOG_AVAILABLE and catalog_context is not None:
        try:
            high, low = catalog_context.overlays_for_active()
        except Exception:
            high, low = {}, {}
    # Precedence (later wins): low platform defaults < process env < Settings overrides
    # < active cluster identity < per-call updates.
    out: dict[str, str] = {}
    out.update(low)
    out.update(os.environ)
    out.update(_job_env_overrides_load())
    out.update(high)
    if updates:
        out.update(updates)
    # validation_test_env.sh treats unset as a very strict suite (all NCCL collectives + LLM), which
    # often marks racks pending when logs only have all-reduce + Nemotron. Callers may pass a
    # different *validation_test_if_unset* (e.g. multirack probe uses RACK_MULTIRACK_VALIDATION_TEST).
    if not (out.get("VALIDATION_TEST") or "").strip():
        d = (validation_test_if_unset or "nccl_all_reduce,llm").strip() or "nccl_all_reduce,llm"
        out["VALIDATION_TEST"] = d
    return out


_PHYSICAL_RF0_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_APP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")


def _parse_physical_rf0_stdout(text: str) -> dict[str, str]:
    """hostname(lower) -> first IPv4 after that line (skips interfaces / noise until get ip)."""
    out: dict[str, str] = {}
    raw_lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines: list[str] = []
    for ln in raw_lines:
        t = ln.strip()
        if not t or t.startswith(">") or t.startswith("#"):
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
        if not host_exec.is_remote() and not shutil.which(cmd[0]):
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


def _validation_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    """Drop finished job metadata after a delay (avoids unbounded memory)."""

    def _purge() -> None:
        time.sleep(float(delay_sec))
        with _validation_jobs_lock:
            _validation_jobs.pop(job_id, None)

    threading.Thread(target=_purge, daemon=True).start()


def _validation_worker(job_id: str, cmd: list[str], cwd: str, timeout: int, display_cmd: str) -> None:
    """Run validation bash in a background thread; update _validation_jobs[job_id]."""

    def _patch(**kwargs: object) -> None:
        with _validation_jobs_lock:
            row = _validation_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env())
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error="validation script timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _validation_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else "validation script exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _validation_job_purge_later(job_id)


def _stress_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    def _purge() -> None:
        time.sleep(float(delay_sec))
        with _stress_jobs_lock:
            _stress_jobs.pop(job_id, None)

    threading.Thread(target=_purge, daemon=True).start()


def _stress_worker(
    job_id: str,
    cmd: list[str],
    cwd: str,
    timeout: int,
    display_cmd: str,
    *,
    error_label: str = "mnubergemm stress script",
    env_updates: dict[str, str] | None = None,
) -> None:
    """Run a stress-style bash driver (mnubergemm, nvloom, …) in a background thread; update _stress_jobs[job_id]."""

    def _patch(**kwargs: object) -> None:
        with _stress_jobs_lock:
            row = _stress_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env(env_updates))
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error=f"{error_label} timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _stress_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else f"{error_label} exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _stress_job_purge_later(job_id)


def _xrack_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    def _purge() -> None:
        time.sleep(float(delay_sec))
        with _xrack_jobs_lock:
            _xrack_jobs.pop(job_id, None)

    threading.Thread(target=_purge, daemon=True).start()


def _xrack_worker(job_id: str, cmd: list[str], cwd: str, timeout: int, display_cmd: str) -> None:
    """Run run-nemo-xrack.sh in a background thread; update _xrack_jobs[job_id]."""

    def _patch(**kwargs: object) -> None:
        with _xrack_jobs_lock:
            row = _xrack_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env())
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error="run-nemo-xrack.sh timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _xrack_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else "run-nemo-xrack.sh exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _xrack_job_purge_later(job_id)


def _llama_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    def _purge() -> None:
        time.sleep(float(delay_sec))
        with _llama_jobs_lock:
            _llama_jobs.pop(job_id, None)

    threading.Thread(target=_purge, daemon=True).start()


def _llama_worker(job_id: str, cmd: list[str], cwd: str, timeout: int, display_cmd: str) -> None:
    """Run ``run-llama.sh`` (via bash -lc module load slurm) in a background thread."""

    def _patch(**kwargs: object) -> None:
        with _llama_jobs_lock:
            row = _llama_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env())
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error="run-llama.sh timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _llama_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else "run-llama.sh exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _llama_job_purge_later(job_id)


_reset_jobs_lock = threading.Lock()
_reset_jobs: dict[str, dict[str, object]] = {}


def _reset_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    def _purge() -> None:
        time.sleep(float(delay_sec))
        with _reset_jobs_lock:
            _reset_jobs.pop(job_id, None)

    threading.Thread(target=_purge, daemon=True).start()


def _power_reset_worker(job_id: str, specs: list[str]) -> None:
    """Run cmsh power cycle in a background thread."""

    def _patch(**kwargs: object) -> None:
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def _job_env_int(name: str, default: int) -> int:
    """Like ``_env_int`` but prefers values saved under Settings → job environment."""
    ov = _job_env_overrides_load()
    if name in ov:
        raw = ov[name]
        if raw and str(raw).strip():
            try:
                return int(str(raw).strip(), 10)
            except ValueError:
                return default
    return _env_int(name, default)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _project_name() -> str:
    """Optional label appended to page titles (``PROJECT_NAME`` or ``RACK_WEB_PROJECT_NAME``)."""
    v = (_eget("RACK_WEB_PROJECT_NAME") or "").strip()
    if v:
        return v.replace("\n", " ").replace("\r", " ").strip()
    v = (os.environ.get("PROJECT_NAME") or "").strip()
    if v:
        return v.replace("\n", " ").replace("\r", " ").strip()
    return ""


def _app_client_ttl_sec() -> int:
    return max(30, _env_int("RACK_WEB_ACTIVE_CLIENT_TTL_SEC", 120))


def _app_version() -> str:
    return (os.environ.get("RACK_WEB_VERSION") or "dev").strip() or "dev"


def _touch_app_client(client_id: str, now_ts: float | None = None) -> int:
    """Track active dashboard clients and return currently-active count."""
    cid = (client_id or "").strip()
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - float(_app_client_ttl_sec())
    with _app_clients_lock:
        stale = [k for k, ts in _app_clients_last_seen.items() if float(ts) < cutoff]
        for k in stale:
            _app_clients_last_seen.pop(k, None)
        if _APP_CLIENT_ID_RE.fullmatch(cid):
            _app_clients_last_seen[cid] = now
        return len(_app_clients_last_seen)


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


def _operator_from_request() -> str:
    """Operator label from the ``X-Rack-Operator`` header. Advisory metadata only -- never
    trusted for authorization. Empty when the SPA hasn't set a display name."""
    try:
        raw = request.headers.get("X-Rack-Operator", "")
    except RuntimeError:
        # Outside a request context (worker thread): fall back to empty label.
        raw = ""
    return rack_locks.sanitize_operator(raw)


def _client_id_from_request() -> str:
    """Stable browser id from the ``X-Rack-Client-Id`` header (also accepted as
    ``?client_id=`` for backward compatibility with the existing useRackStatus poll)."""
    try:
        raw = request.headers.get("X-Rack-Client-Id", "") or request.args.get("client_id", "")
    except RuntimeError:
        return ""
    if not isinstance(raw, str):
        return ""
    cid = raw.strip()[:80]
    return cid if _APP_CLIENT_ID_RE.fullmatch(cid) else ""


def _iter_active_jobs_for_locks() -> list[dict]:
    """Flatten the five in-memory job dicts into the shape ``rack_locks`` expects:
    ``{job_id, rack | racks, kind, operator, phase, started_at, sync}``.

    Called by ``GET /api/rack-locks`` only; cheap (≤ tens of entries in practice)."""
    rows: list[dict] = []
    for src_lock, src_dict, default_kind in (
        (_reset_jobs_lock, _reset_jobs, "reset"),
        (_validation_jobs_lock, _validation_jobs, "validation"),
        (_stress_jobs_lock, _stress_jobs, "stress"),
        (_xrack_jobs_lock, _xrack_jobs, "xrack"),
        (_llama_jobs_lock, _llama_jobs, "llama"),
    ):
        with src_lock:
            snapshot = list(src_dict.items())
        for jid, row in snapshot:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "job_id": str(jid),
                    "rack": row.get("rack"),
                    "racks": row.get("racks"),
                    "kind": str(row.get("kind") or default_kind),
                    "operator": str(row.get("operator") or ""),
                    "phase": str(row.get("phase") or ""),
                    "started_at": str(row.get("started_at") or row.get("created_at") or ""),
                    "sync": bool(row.get("sync")),
                }
            )
    return rows


def _active_clients_snapshot() -> dict[str, float]:
    """Copy of the heartbeat map for ``rack_locks`` (offline-claim detection)."""
    with _app_clients_lock:
        return dict(_app_clients_last_seen)


@contextmanager
def _track_sync_op(rack: str | object, kind: str, operator: str = ""):
    """Register a synthetic entry in ``_reset_jobs`` so long-running sync handlers and
    write-y SSE streams show up in ``GET /api/rack-locks`` for their duration.

    Accepts a single rack id or a list/tuple. ``ValueError`` from a bad rack id is logged
    and swallowed -- the wrapped handler runs untracked rather than breaking the route."""
    racks: list[str] = []
    if isinstance(rack, (list, tuple)):
        candidates = rack
    else:
        candidates = [rack] if rack else []
    for raw_rack in candidates:
        if not isinstance(raw_rack, str) or not raw_rack:
            continue
        try:
            racks.append(rack_locks.normalize_rack_id(raw_rack))
        except ValueError:
            continue
    if not racks:
        # Nothing to lock against; still yield so the route runs.
        yield None
        return
    job_id = "sync-" + uuid.uuid4().hex[:12]
    entry = {
        "job_id": job_id,
        "rack": racks[0] if len(racks) == 1 else None,
        "racks": racks,
        "kind": str(kind or "operation"),
        "operator": rack_locks.sanitize_operator(operator),
        "phase": "running",
        "sync": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with _reset_jobs_lock:
        _reset_jobs[job_id] = entry
    try:
        yield job_id
    finally:
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row["phase"] = "done"
                row["finished_at"] = datetime.now(timezone.utc).isoformat()
        _reset_job_purge_later(job_id, delay_sec=15)


def _auth_enabled() -> bool:
    """Dashboard login via PAM or token unless RACK_WEB_AUTH_DISABLED is set."""
    return not _truthy_env("RACK_WEB_AUTH_DISABLED")


def _auth_mode() -> str:
    """Return ``pam`` or ``token``.  Defaults to ``token`` when inside a container (SSH mode)."""
    explicit = (os.environ.get("RACK_WEB_AUTH_MODE") or "").strip().lower()
    if explicit in ("pam", "token"):
        return explicit
    return "token" if host_exec.is_remote() else "pam"


def _auth_token() -> str | None:
    """Token string when RACK_WEB_AUTH_MODE=token."""
    t = (os.environ.get("RACK_WEB_AUTH_TOKEN") or "").strip()
    return t or None


def _auth_user() -> str:
    return (os.environ.get("RACK_WEB_AUTH_USER") or "root").strip() or "root"


def _pam_service() -> str:
    return (os.environ.get("RACK_WEB_PAM_SERVICE") or "login").strip() or "login"


def _session_lifetime() -> timedelta:
    h = _env_int("RACK_WEB_SESSION_HOURS", 8)
    if h < 1:
        h = 1
    if h > 24 * 30:
        h = 24 * 30
    return timedelta(hours=h)


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
    if not (_CATALOG_AVAILABLE and catalog_context is not None):
        return
    try:
        _catalog_restore_environ_baseline()
        high, low = catalog_context.overlays_for_active()
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
            pass
        try:
            native_status.invalidate_job_env_file_cache()
        except Exception:
            pass
        _rack_status_cache_clear_all()
    except Exception:
        _logger.exception("catalog: applying active context to environ failed")


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


def _session_ok() -> bool:
    return session.get("rack_web_auth") == "1"


def _safe_next(raw: str | None) -> str:
    s = (raw or "").strip().split("#", 1)[0]
    if not s.startswith("/") or s.startswith("//"):
        return ""
    return s


def _pam_helper_python_exe() -> str:
    return (os.environ.get("RACK_WEB_PAM_HELPER_PYTHON") or "/usr/bin/python3").strip() or "/usr/bin/python3"


def _pam_verify_subprocess(username: str, password: str, service: str) -> bool:
    """Authenticate via pam_helper.py under a Python that can load ctypes (e.g. /usr/bin/python3)."""
    exe = _pam_helper_python_exe()
    helper = APP_DIR / "rack_web" / "pam_helper.py"
    if not helper.is_file():
        raise RuntimeError(f"Internal error: missing {helper}")
    payload = json.dumps({"user": username, "password": password, "service": service})
    try:
        proc = subprocess.run(
            [exe, str(helper)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=90,
            env=_subprocess_env(),
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"PAM helper interpreter not found: {exe!r}. Set RACK_WEB_PAM_HELPER_PYTHON to a "
            "Python whose ctypes module works (often /usr/bin/python3), or recreate the venv with "
            "that interpreter instead of a mismatched module-loaded Python."
        ) from None
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("PAM authentication subprocess timed out") from e
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    err_tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"PAM helper process failed (exit {proc.returncode}). "
            f"Try RACK_WEB_PAM_HELPER_PYTHON=/usr/bin/python3. Stderr: {err_tail[-1200:]}"
        )
    if not out:
        raise RuntimeError(
            "PAM helper produced no output. "
            f"Try RACK_WEB_PAM_HELPER_PYTHON=/usr/bin/python3. Stderr: {err_tail[-1200:]}"
        )
    line = out.splitlines()[-1].strip()
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PAM helper returned invalid JSON ({line[:200]!r}): {e}") from e
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return bool(data.get("ok"))


def _token_verify(password: str) -> bool:
    """Verify password against RACK_WEB_AUTH_TOKEN (simple shared-secret)."""
    token = _auth_token()
    if not token:
        return False
    return secrets.compare_digest(password, token)


def _pam_verify(username: str, password: str, service: str) -> bool:
    if password is None:
        return False
    if _auth_mode() == "token":
        return _token_verify(password)
    try:
        from rack_web.pam_ctypes import authenticate as pam_authenticate
    except (ImportError, OSError):
        return _pam_verify_subprocess(username, password, service)
    return bool(pam_authenticate(username, password, service=service))


def _is_public_spa_static_path() -> bool:
    """Vite build assets and icons must be served without a session.

    The login screen is part of the React SPA; if ``/assets/*.js`` is redirected to
  ``/login``, the browser receives HTML instead of JavaScript and the page stays blank.
    """
    p = request.path
    if p in ("/login", "/favicon.svg", "/icons.svg"):
        return True
    if p.startswith("/assets/") or p.startswith("/spa/"):
        return True
    return False


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
    if request.endpoint in ("login", "api_login", "api_auth_check", "static"):
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
        return redirect(url_for("login", next=nxt))
    return redirect(url_for("login"))


@app.after_request
def _api_json_no_store(response):
    """Avoid stale dashboard JSON behind browsers or reverse proxies."""
    if request.path.startswith("/api/") and response.content_type and "application/json" in response.content_type:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _script_path() -> Path:
    p = _eget("RACK_STATUS_SCRIPT", str(DEFAULT_SCRIPT)) or str(DEFAULT_SCRIPT)
    return _resolve_path(Path(p))


def _parser_path() -> Path:
    p = _eget("RACK_PARSER_SCRIPT", str(SCRIPTS_DIR / "parser.sh")) or str(SCRIPTS_DIR / "parser.sh")
    return _resolve_path(Path(p))


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


def _nemo_nccl_in_log_basename(name: str) -> bool:
    """Logs with NEMO or NCCL in the basename (multirack tab, exports, legacy shell table)."""
    low = name.lower()
    if "nemo" in low or "nccl" in low:
        return True
    return _nvis_naming().auxiliary_single_rack_log_basename(name)


def _nemo_single_rack_log_basename(name: str) -> bool:
    """Single-rack NEMO validation logs for the dashboard table / Open Log (not NCCL-only sweeps)."""
    low = name.lower()
    if "nemo" in low:
        return True
    return _nvis_naming().auxiliary_single_rack_log_basename(name)


_MULTIRACK_N_RACK_RE = re.compile(r"(?:[2-9]|[1-9]\d{1,3})rack", re.IGNORECASE)


def _jobid_from_nemo_log_basename(name: str) -> str | None:
    """Slurm job id from NEMO/NCCL/LLAMA log basenames (aligned with ``rack_status.sh`` ``jobid_from_fname``)."""
    m = re.search(r"NEMO-LLAMA3_8B-1N-.+-(\d+)\.txt$", name, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"N-(\d+)\.txt$", name, re.IGNORECASE)
    return m2.group(1) if m2 else None


def _is_multirack_log_basename(name: str) -> bool:
    """True for multi-rack NEMO/NCCL logs (excluded from rack_status single-rack table; listed in multirack tab)."""
    if not _nemo_nccl_in_log_basename(name):
        return False
    if _MULTIRACK_N_RACK_RE.search(name):
        return True
    low = name.lower()
    # run-nemo-xrack.sh: root-NEMO-p5-r[…]-ct[…],p5-r[…]-ct[…]-<n>N-<jid>.txt
    if (low.startswith("root-nemo-") or low.startswith("root-nccl-")) and _nvis_naming().multirack_comma_after_rack_re().search(
        low
    ):
        return True
    return len(_nvis_naming().find_rack_ids_in_string(name)) >= 2


def _multirack_site_prefix() -> str:
    """Pod id for ``r[01-04]``-style names without ``pN-`` (e.g. ``p5``). See RACK_MULTIRACK_SITE_PREFIX (defaults to p5)."""
    raw = _eget("RACK_MULTIRACK_SITE_PREFIX")
    if raw is None or not str(raw).strip():
        return "p5"
    return str(raw).strip().lower()


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


def _multirack_node_count_suffix(name: str) -> str:
    """Return `` (36N)`` when basename contains ``-36N-`` / ``-36N.`` style node count."""
    m = re.search(r"-(\d+)N-\d", name, re.IGNORECASE)
    if m:
        return f" ({m.group(1)}N)"
    m2 = re.search(r"-(\d+)N\.txt$", name, re.IGNORECASE)
    if m2:
        return f" ({m2.group(1)}N)"
    return ""


def multirack_display_rack_from_basename(name: str) -> str:
    """Human label like ``p5-r01+p5-r02 (36N)`` for multirack log filenames."""
    suffix = _multirack_node_count_suffix(name)
    naming = _nvis_naming()
    racks = sorted(naming.find_rack_ids_in_string(name))
    if len(racks) >= 2:
        return "+".join(racks) + suffix
    pod_re = naming.multirack_pod_r_bracket_re()
    if pod_re:
        m = pod_re.search(name)
        if m:
            expanded = naming.expand_pod_r_inner(m.group(1), m.group(2))
            if expanded:
                return "+".join(expanded) + suffix
    bare_re = naming.multirack_bare_r_bracket_re()
    if bare_re:
        m2 = bare_re.search(name)
        if m2:
            expanded = naming.expand_site_r_inner(_multirack_site_prefix(), m2.group(1))
            if expanded:
                return "+".join(expanded) + suffix
    stem = Path(name).stem
    return (stem[:120] if stem else name) + suffix


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


def _resolve_nemo_log_basename_in_dir(log_dir: Path, basename: str) -> Path | None:
    """Return path to ``basename`` under log_dir root or failed/; None if missing or unsafe."""
    bn = Path(basename).name
    if bn != basename.strip() or ".." in bn or "/" in bn or "\\" in bn:
        return None
    if not _nemo_nccl_in_log_basename(bn):
        return None
    root = log_dir.resolve()
    for sub in (root, root / "failed", root / "sn-llama"):
        if not sub.is_dir():
            continue
        cand = (sub / bn).resolve()
        try:
            cand.relative_to(root)
        except ValueError:
            continue
        if cand.is_file():
            return cand
    return None


def _rack_id_from_nemo_1rack_filename(name: str) -> str | None:
    """Return normalized rack id if basename looks like a NEMO 1Rack log, else None."""
    return _nvis_naming().rack_id_from_nemo_1rack_filename(name)


def _rack_id_from_log_basename(name: str) -> str | None:
    """Rack id from single-rack NEMO validation basename (1Rack-, root-CK-, …); not Llama Slurm stdout."""
    return _nvis_naming().rack_id_from_log_basename(name)


def _nemo_log_file_looks_successful(path: Path) -> bool:
    """Match parser.sh llm_label: exclude Slurm hard-fail; require LLM step markers (OK path)."""
    grep_exe = shutil.which("grep")
    if not grep_exe:
        return True
    lp = str(path.resolve())
    timeout = max(30, _env_int("RACK_EXPORT_SUCCESS_GREP_TIMEOUT", 180))
    try:
        fail = subprocess.run(
            [grep_exe, "-E", "-q", r"srun:.*error:.*Exited|error:.*SIGNAL", lp],
            capture_output=True,
            timeout=timeout,
        )
        if fail.returncode == 0:
            return False
        ok = subprocess.run(
            [
                grep_exe,
                "-E",
                "-q",
                r"train_step_timing.*consumed_samples|Step Time.*MODEL_TFLOP/s/GPU",
                lp,
            ],
            capture_output=True,
            timeout=timeout,
        )
        return ok.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _multirack_txt_paths_under_failed(log_dir: Path, pat: str) -> list[Path]:
    """Multirack NEMO/NCCL *.txt under log_dir/failed/ (same glob as root export).

    Multirack jobs often write logs only under ``failed/``; include them in Excel/zip export so
    parser results match the Multirack tab.
    """
    root = log_dir.resolve()
    failed = root / "failed"
    if not failed.is_dir():
        return []
    out: list[Path] = []
    for p in failed.glob(pat):
        if p.is_file() and _nemo_nccl_in_log_basename(p.name) and _is_multirack_log_basename(p.name):
            out.append(p)
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
    root = log_dir.resolve()
    root_files: list[Path] = []
    if root.is_dir():
        for p in root.glob(pat):
            if p.is_file() and _nemo_nccl_in_log_basename(p.name):
                root_files.append(p)

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
        try:
            best = max(successful, key=lambda x: x.stat().st_mtime)
        except OSError:
            continue
        selected_rack.append(best)

    tail_failed = (
        _multirack_txt_paths_under_failed(log_dir, pat) if include_multirack_under_failed else []
    )
    merged: dict[str, Path] = {}
    for p in others + selected_rack + tail_failed:
        merged[str(p.resolve())] = p
    out = sorted(merged.values(), key=lambda p: p.name.lower())
    return out


def find_log_paths_for_rack(log_dir: Path, rack: str) -> list[Path]:
    """Paths to single-rack validation logs for *rack* under ``log_dir`` and ``failed/`` (newest first).

    Scans ``*.txt`` and keeps files whose basename maps to exactly this rack via
    ``rack_id_from_log_basename`` (agnostic to pod/rack zero-padding in filenames, e.g. ``p5-r01``
    vs dashboard ``p05-r01``). Excludes multirack and Llama Slurm stdout basenames.
    """
    rk = _normalize_rack_id(rack)
    naming = _nvis_naming()
    out: list[Path] = []
    seen: set[str] = set()
    for base in (log_dir, log_dir / "failed"):
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.txt"), key=lambda x: x.name.lower()):
            if not p.is_file() or not _nemo_single_rack_log_basename(p.name):
                continue
            if naming.is_llama_slurm_stdout_basename(p.name):
                continue
            if _is_multirack_log_basename(p.name):
                continue
            if naming.rack_id_from_log_basename(p.name) != rk:
                continue
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                out.append(p)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def find_nvloom_log_paths_for_rack(log_dir: Path, rack: str) -> list[Path]:
    """Newest-first paths to ``*NVLOOM-<rack>-*.txt`` under ``log_dir/nvloom`` (and ``nvloom/failed``)."""
    try:
        rk = _normalize_rack_id(rack)
    except ValueError:
        return []
    pat = f"*NVLOOM-{rk}-*.txt"
    out: list[Path] = []
    for base in (log_dir / "nvloom", log_dir / "nvloom" / "failed"):
        if base.is_dir():
            out.extend(p for p in base.glob(pat) if p.is_file())
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


_NVLOOM_HEATMAPS_LOG_MAX_BYTES = 40 * 1024 * 1024


def pick_log_for_rack(log_dir: Path, rack: str, job_id: str | None) -> Path | None:
    cands = find_log_paths_for_rack(log_dir, rack)
    if not cands:
        return None
    j = (job_id or "").strip()
    if j.isdigit():
        suf = f"N-{j}.txt"
        for p in cands:
            if p.name.endswith(suf):
                return p
    return cands[0]


def _slurm_job_base_id(job_raw: str | None) -> str | None:
    s = (job_raw or "").strip()
    if not s or s == "-":
        return None
    if "_" in s:
        s = s.split("_", 1)[0]
    return s if s.isdigit() else None


def _run_bash_slurm_prefix(cmd: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run Slurm CLI after optional ``module load`` (non-login shell; see ``host_exec.run_slurm_on_host``)."""
    plat = _get_platform()
    return host_exec.run_slurm_on_host(
        plat.slurm_prefix(cmd),
        timeout=timeout,
    )


def _scontrol_hostnames_preserving_case(nodespec: str) -> list[str]:
    """Hostnames from ``scontrol show hostnames``, one per line, preserving Slurm ``NodeName`` spelling."""
    nodespec = nodespec.strip()
    if not nodespec or nodespec.lower() in ("none", "null"):
        return []
    proc = _run_bash_slurm_prefix(
        "scontrol show hostnames " + shlex.quote(nodespec),
        timeout=max(30, _env_int("RACK_SCONTROL_HOSTNAMES_TIMEOUT", 120)),
    )
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _dedupe_hostnames_preserve_spelling(hosts: list[str]) -> list[str]:
    """Drop duplicate hosts case-insensitively; keep the first spelling seen (prefer Slurm output order)."""
    out_map: dict[str, str] = {}
    for h in hosts:
        t = (h or "").strip()
        if not t:
            continue
        k = t.casefold()
        if k not in out_map:
            out_map[k] = t
    return sorted(out_map.values(), key=str.casefold)


def _split_pdsh_w_top_level_commas(w: str) -> list[str]:
    """Split a pdsh ``-w`` comma list without splitting commas inside ``[…]`` host ranges."""
    w = (w or "").strip()
    if not w:
        return []
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in w:
        if ch == "[":
            depth += 1
            cur.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            s = "".join(cur).strip()
            if s:
                out.append(s)
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _expand_pdsh_w_slurm_nodenames(w: str) -> str:
    """Expand each Slurm nodeset fragment via ``scontrol show hostnames`` so pdsh uses NodeName casing.

    pdsh bracket expansion can lower-case host tokens, which breaks ssh/DNS on some sites. When Slurm
    expansion fails for a fragment, the original fragment is kept. Set ``RACK_PDSH_W_SKIP_SLURM_EXPAND``
    to skip this step entirely.
    """
    w = (w or "").strip()
    if not w:
        return w
    if os.environ.get("RACK_PDSH_W_SKIP_SLURM_EXPAND", "").strip().lower() in ("1", "true", "yes"):
        return w
    parts_out: list[str] = []
    for seg in _split_pdsh_w_top_level_commas(w):
        hosts = _scontrol_hostnames_preserving_case(seg)
        if hosts:
            parts_out.extend(hosts)
        else:
            parts_out.append(seg)
    return ",".join(parts_out)


def _hostnames_for_slurm_job(job_raw: str | None) -> tuple[list[str], str]:
    bid = _slurm_job_base_id(job_raw)
    if not bid:
        return [], "no_job_id"
    proc = _run_bash_slurm_prefix(
        f"sacct -j {shlex.quote(bid)} -X -P -n -o NodeList%2000 --noheader",
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
            pass
    raw = (_eget("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ") or "UTC").strip() or "UTC"
    if raw.upper() in ("UTC", "GMT", "Z", "UCT"):
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(raw)
    except Exception:
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
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _llm_wall_times_from_log(path: Path, tz_override: str | None = None) -> list[float]:
    tz = _zoneinfo_for_log_parsing(tz_override)
    times: list[float] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not _LLM_LINE_MARK_RE.search(line):
                    continue
                m = _LOG_TS_SEARCH_RE.search(line)
                if not m:
                    continue
                u = _parse_ts_unix(m.group("ts"), tz)
                if u is not None:
                    times.append(u)
    except OSError:
        return []
    return times


def _median_float(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


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


def _iso_utc_z(ts: float) -> str:
    """RFC3339 UTC with Z suffix for Grafana ``from`` / ``to`` URL params."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    s = dt.isoformat(timespec="milliseconds")
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    return s


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


_CT_RANGE_INNER_RE = re.compile(r"^[0-9,\-]+$")


def _validation_ct_inner() -> str:
    s = (_eget("RACK_VALIDATION_CT_RANGE", "01-18") or "").strip()
    if not s or not _CT_RANGE_INNER_RE.match(s):
        return "01-18"
    return s


def _resolve_path(p: Path) -> Path:
    """Resolve a filesystem path; skip .resolve() in SSH mode (paths refer to the remote host)."""
    p = p.expanduser()
    if host_exec.is_remote():
        return p
    return p.resolve()


def _validation_script_path() -> Path:
    p = _eget("RACK_VALIDATION_SCRIPT", str(SCRIPTS_DIR / "run_singlerack.sh")) or str(
        SCRIPTS_DIR / "run_singlerack.sh"
    )
    return _resolve_path(Path(p))


def _mnubergemm_stress_script_path() -> Path:
    p = _eget("RACK_MNUBERGEMM_STRESS_SCRIPT", str(SCRIPTS_DIR / "run_mnubergemm_stress.sh")) or str(
        SCRIPTS_DIR / "run_mnubergemm_stress.sh"
    )
    return _resolve_path(Path(p))


def _nvloom_singlerack_script_path() -> Path:
    p = _eget("RACK_NVLOOM_SINGLERACK_SCRIPT", str(SCRIPTS_DIR / "run_nvloom_singlerack.sh")) or str(
        SCRIPTS_DIR / "run_nvloom_singlerack.sh"
    )
    return _resolve_path(Path(p))


def _clusterkit_script_path() -> Path:
    p = _eget("RACK_CLUSTERKIT_SCRIPT", str(SCRIPTS_DIR / "run_clusterkit.sh")) or str(SCRIPTS_DIR / "run_clusterkit.sh")
    return _resolve_path(Path(p))


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
        pass
    return (_eget("RACK_CLUSTER_PRODUCT_LINE") or _eget("RACK_PRODUCT_LINE") or "").strip()


def _nvis_workspace_dir_effective() -> str:
    """Default workspace root for HPC-X / CUDA / nvloom install layout (same as bin/scripts/workspace-dir-common.sh)."""
    return (_eget("NVIS_WORKSPACE_DIR") or _eget("RACK_WORKSPACE_DIR") or "").strip() or "/home/cmsupport/workspace"


def _sysinfo_script_path() -> Path:
    p = _eget("RACK_SYSINFO_SCRIPT", str(SCRIPTS_DIR / "sysinfo.sh")) or str(SCRIPTS_DIR / "sysinfo.sh")
    return _resolve_path(Path(p))


def _nvswitches_rack_audit_script_path() -> Path:
    p = _eget("RACK_NVSWITCH_AUDIT_SCRIPT", str(SCRIPTS_DIR / "nvswitches_rack_audit.sh")) or str(
        SCRIPTS_DIR / "nvswitches_rack_audit.sh"
    )
    return _resolve_path(Path(p))


def _nvswitch_sdn_reset_script_path() -> Path:
    p = _eget("RACK_NVSWITCH_SDN_RESET_SCRIPT", str(SCRIPTS_DIR / "nvswitch_sdn_reset.sh")) or str(
        SCRIPTS_DIR / "nvswitch_sdn_reset.sh"
    )
    return _resolve_path(Path(p))


def _nvswitch_system_reset_script_path() -> Path:
    p = _eget("RACK_NVSWITCH_SYSTEM_RESET_SCRIPT", str(SCRIPTS_DIR / "nvswitch_system_reset.sh")) or str(
        SCRIPTS_DIR / "nvswitch_system_reset.sh"
    )
    return _resolve_path(Path(p))


def _imex_reset_script_path() -> Path:
    p = _eget("RACK_IMEX_RESET_SCRIPT", str(SCRIPTS_DIR / "imex_reset.sh")) or str(
        SCRIPTS_DIR / "imex_reset.sh"
    )
    return _resolve_path(Path(p))


def _imex_check_script_path() -> Path:
    p = _eget("RACK_IMEX_CHECK_SCRIPT", str(SCRIPTS_DIR / "imex_check.sh")) or str(
        SCRIPTS_DIR / "imex_check.sh"
    )
    return _resolve_path(Path(p))


def _nemo_xrack_script_path() -> Path:
    p = _eget("RACK_NEMO_XRACK_SCRIPT", str(SCRIPTS_DIR / "run-nemo-xrack.sh")) or str(
        SCRIPTS_DIR / "run-nemo-xrack.sh"
    )
    return _resolve_path(Path(p))


def _run_llama_script_path() -> Path:
    p = _eget("RACK_LLAMA_SCRIPT", str(SCRIPTS_DIR / "run-llama.sh")) or str(SCRIPTS_DIR / "run-llama.sh")
    return _resolve_path(Path(p))


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
        raise ValueError("nodes_spec must be a string")
    t = raw.strip()
    if not t or len(t) > 800:
        raise ValueError("nodes_spec is empty or too long")
    for c in ";|&$`'\n\r()<>":
        if c in t:
            raise ValueError("nodes_spec contains forbidden characters")
    return t


def validation_target_for_rack(rack: str, override: str | None) -> str:
    """Build validation target for run_singlerack.sh / sysinfo (pattern from NVIS_* templates)."""
    return _nvis_naming().build_validation_target(
        rack, _validation_ct_inner(), override
    )


def _default_log_dir() -> Path:
    p = os.environ.get("RACK_STATUS_LOG_DIR", str(DEFAULT_LOG_DIR))
    path = Path(p)
    if host_exec.is_remote():
        return path
    return path.resolve()


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("RACK_STATUS_ALLOWED_PREFIXES", "")
    if not raw.strip():
        return []
    remote = host_exec.is_remote()
    return [Path(x.strip()) if remote else Path(x.strip()).resolve() for x in raw.split(",") if x.strip()]


def _resolve_log_dir(raw: str | None) -> Path:
    base = Path(raw or _default_log_dir()).expanduser()
    if host_exec.is_remote():
        s = str(base)
        _MACOS_FIRMLINK = "/System/Volumes/Data"
        if s.startswith(_MACOS_FIRMLINK + "/"):
            s = s[len(_MACOS_FIRMLINK):]
        log_dir = Path(s)
    else:
        log_dir = base.resolve()
    roots = _allowed_roots()
    if roots:
        ok = any(str(log_dir).startswith(str(r) + os.sep) or log_dir == r for r in roots)
        if not ok:
            raise ValueError(f"log_dir must be under one of: {[str(r) for r in roots]}")
    if not host_exec.is_remote() and not log_dir.is_dir():
        raise ValueError(f"not a directory: {log_dir}")
    return log_dir


def _tools_check_destination_allowed(p: Path) -> None:
    """Raise ``ValueError`` if *p* is outside allowed prefixes when enforcement is on."""
    if _truthy_env("RACK_TOOLS_ENFORCE_ALLOWED_PREFIXES"):
        roots = _allowed_roots()
        if roots:
            ok = any(str(p).startswith(str(r) + os.sep) or p == r for r in roots)
            if not ok:
                raise ValueError(
                    "destination must be under one of RACK_STATUS_ALLOWED_PREFIXES "
                    f"(enforce: RACK_TOOLS_ENFORCE_ALLOWED_PREFIXES): {[str(r) for r in roots]}"
                )


def _resolve_tools_destination(raw: str) -> Path:
    """Validate an absolute destination **directory** for Tools (e.g. enroot import output).

    May be an existing directory or a non-existent path that ``mkdir -p`` will create. Must not
    be a file path or a path whose last component looks like a ``.sqsh`` file.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("destination is required")
    p = Path(s).expanduser()
    if not p.is_absolute():
        raise ValueError("destination must be an absolute path")
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        p = Path(os.path.abspath(str(p)))
    try:
        if p.is_file():
            raise ValueError("destination must be a directory, not a file")
        if not p.is_dir() and p.suffix.lower() == ".sqsh":
            raise ValueError("destination must be a directory path, not a .sqsh file path")
    except OSError as e:
        raise ValueError(f"destination is not usable: {e}") from e
    _tools_check_destination_allowed(p)
    return p


def _tools_destination_dir_for_status_probe(raw: str) -> Path:
    """Directory to check for an existing NEMO ``.sqsh`` (UI may paste a file path; POST remains directory-only)."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("destination is required")
    p = Path(s).expanduser()
    if not p.is_absolute():
        raise ValueError("destination must be an absolute path")
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        p = Path(os.path.abspath(str(p)))
    try:
        is_dir = p.is_dir()
        is_file = p.is_file()
    except OSError as e:
        raise ValueError(f"destination is not usable: {e}") from e
    if is_file and p.suffix.lower() != ".sqsh":
        raise ValueError("destination must be a directory or a NEMO .sqsh file path")
    if (is_file and p.suffix.lower() == ".sqsh") or (not is_dir and p.suffix.lower() == ".sqsh"):
        dest_dir = p.parent
    else:
        dest_dir = p
    _tools_check_destination_allowed(dest_dir)
    return dest_dir


def _tools_sqsh_nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _tools_find_existing_sqsh_for_skip(dest_dir: Path) -> tuple[Path | None, list[str]]:
    """Return (path, checked_str_paths) if a non-empty .sqsh is already present under *dest_dir*."""
    checked: list[str] = []

    def try_path(p: Path) -> Path | None:
        checked.append(str(p))
        return p if _tools_sqsh_nonempty(p) else None

    hit = try_path(dest_dir / _TOOLS_NEMO_CONTAINER_SQSH)
    if hit:
        return hit, checked

    raw = (_eget("NEMO_SQSH_SRC") or "").strip()
    if raw:
        bn = Path(raw).expanduser().name
        if bn.lower().endswith(".sqsh"):
            hit = try_path(dest_dir / bn)
            if hit:
                return hit, checked
        try:
            nemo = _resolve_path(Path(raw))
        except (OSError, ValueError, RuntimeError):
            nemo = None
        if nemo is not None and nemo.suffix.lower() == ".sqsh":
            try:
                if nemo.parent.resolve() == dest_dir.resolve():
                    hit = try_path(nemo)
                    if hit:
                        return hit, checked
            except OSError:
                pass

    return None, checked


def _tools_download_destination_default() -> str:
    """Directory default for Tools → download containers: parent of ``NEMO_SQSH_SRC`` when it is a .sqsh path."""
    raw = (_eget("NEMO_SQSH_SRC") or "").strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = Path(raw).expanduser()
    if p.suffix.lower() == ".sqsh":
        return str(p.parent) if str(p.parent) not in ("", ".") else ""
    return str(p)


def _extra_script_args() -> list[str]:
    extra = _eget("RACK_STATUS_EXTRA_ARGS", "") or ""
    if not extra.strip():
        return []
    return shlex.split(extra)


def _ct_power_token_re() -> re.Pattern[str]:
    """Tray blob tokens from ``ct_down_unk`` (pattern follows ``NVIS_*`` templates)."""
    naming = _nvis_naming()
    rack_part = nvis_id_templates._template_to_regex(naming.rack_template).replace("(?i)", "")
    join = re.escape(naming.ct_joiner)
    return re.compile(rf"({rack_part}{join}\[[^\]]+\])", re.IGNORECASE)


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


def _ct_power_tray_full_re() -> re.Pattern[str]:
    return _nvis_naming().validation_target_re()


def _ct_power_single_ct_re() -> re.Pattern[str]:
    naming = _nvis_naming()
    rack_part = nvis_id_templates._template_to_regex(naming.rack_template).replace("(?i)", "")
    join = re.escape(naming.ct_joiner)
    return re.compile(rf"^({rack_part}){join}\[(\d+)\]$", re.IGNORECASE)


def merge_ct_individuals_for_power(nodes: list[str]) -> list[str]:
    """
    Merge per-node tokens (``<rack><ct_joiner>[dd]`` with one tray id) into tray specs for cmsh power.
    """
    naming = _nvis_naming()
    single_re = _ct_power_single_ct_re()
    full_re = _ct_power_tray_full_re()
    by_rack: dict[str, list[int]] = {}
    for raw in nodes:
        if not isinstance(raw, str):
            raise ValueError("each entry in specs must be a string")
        token = raw.strip()
        m = single_re.match(token)
        if not m:
            raise ValueError(
                f"invalid node token {raw!r} — expected rack{ naming.ct_joiner }[number] with a single tray id"
            )
        rack_tok = m.group(1).strip()
        rr = naming.canonical_rack_id(rack_tok) or rack_tok
        li = m.lastindex or 0
        if li < 2:
            raise ValueError(
                f"invalid node token {raw!r} — expected rack{ naming.ct_joiner }[number] with a single tray id"
            )
        n = int(m.group(li), 10)
        by_rack.setdefault(rr, []).append(n)
    out: list[str] = []
    for rr in sorted(by_rack.keys(), key=str.lower):
        inner = naming.condense_tray_range_inner(by_rack[rr])
        if inner:
            tok = naming.ct_blob(rr, inner)
            if not full_re.match(tok):
                raise ValueError(f"invalid merged tray spec {tok!r}")
            out.append(tok)
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


def _comments_file() -> Path:
    return Path(os.environ.get("RACK_COMMENTS_FILE", str(REPO_ROOT / "rack_comments.json"))).resolve()


def _normalize_rack_id(rack: str) -> str:
    return _nvis_naming().normalize_rack_id(rack)


def _normalize_nvswitch_host_for_rack(rack: str, raw: object) -> str:
    """Require a full NVSwitch hostname belonging to *rack* (``NVIS_NVSW_ID`` template)."""
    rk = _normalize_rack_id(rack)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("nvswitch_host is required")
    return _nvis_naming().normalize_nvsw_host(rk, raw)


def _nvswitch_host_sort_key(hostname: str) -> tuple[int, str]:
    m = re.search(r"-nvsw-(\d+)$", hostname, flags=re.IGNORECASE)
    if m:
        return (int(m.group(1)), hostname.lower())
    return (0, hostname.lower())


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


def _normalize_multirack_comment_basename(raw: object) -> str | None:
    """Basename of a NEMO/NCCL multirack log file; used as storage suffix under ``mr:<basename>``."""
    if not isinstance(raw, str):
        return None
    bn = raw.strip()
    if not bn or bn != Path(bn).name or ".." in bn or "/" in bn or "\\" in bn:
        return None
    if len(bn) > 512:
        return None
    low = bn.lower()
    if "nemo" not in low and "nccl" not in low:
        return None
    return bn


def _multirack_comment_storage_key(basename: str) -> str | None:
    bn = _normalize_multirack_comment_basename(basename)
    return f"mr:{bn.lower()}" if bn else None


def _split_stored_comments(raw: object) -> tuple[dict[str, str], dict[str, str]]:
    """Split JSON object into rack comments (``pN-rMM``) and multirack log comments (``mr:<basename>``)."""
    racks: dict[str, str] = {}
    multirack: dict[str, str] = {}
    if not isinstance(raw, dict):
        return racks, multirack
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        t = v.strip()
        if not t:
            continue
        lk = k.strip()
        if lk.lower().startswith("mr:"):
            sk = _multirack_comment_storage_key(lk[3:])
            if sk:
                multirack[sk] = t
            continue
        try:
            rk = _normalize_rack_id(lk)
        except ValueError:
            continue
        racks[rk] = t
    return racks, multirack


def read_comments() -> dict[str, str]:
    p = _comments_file()
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
        racks, mr = _split_stored_comments(raw)
        return {**racks, **mr}
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _load_comment_maps_locked(f) -> tuple[dict[str, str], dict[str, str]]:
    blob = f.read()
    try:
        raw = json.loads(blob) if blob.strip() else {}
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return _split_stored_comments(raw)


def set_comment_for_rack(rack: str, comment: str | None) -> dict[str, str]:
    """Set or clear comment for rack. Returns full comments map after update (preserves multirack keys)."""
    rack_key = _normalize_rack_id(rack)
    max_len = _env_int("RACK_COMMENT_MAX_LEN", 4000)
    if max_len < 1:
        max_len = 4000
    p = _comments_file()
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
            racks, mr = _load_comment_maps_locked(f)
            text = (comment or "").strip()
            if not text:
                racks.pop(rack_key, None)
            else:
                racks[rack_key] = text[:max_len]
            merged = {**racks, **mr}
            new_blob = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            f.seek(0)
            f.truncate()
            f.write(new_blob)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
            return merged
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass


def set_comment_for_multirack_log(basename: str, comment: str | None) -> dict[str, str]:
    """Set or clear comment for a multirack log basename (``root-NEMO-…``.txt). Key on disk: ``mr:<basename>``."""
    sk = _multirack_comment_storage_key(basename)
    if not sk:
        raise ValueError("invalid multirack log basename for comment (expected .txt with nemo or nccl in name)")
    max_len = _env_int("RACK_COMMENT_MAX_LEN", 4000)
    if max_len < 1:
        max_len = 4000
    p = _comments_file()
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
            racks, mr = _load_comment_maps_locked(f)
            text = (comment or "").strip()
            if not text:
                mr.pop(sk, None)
            else:
                mr[sk] = text[:max_len]
            merged = {**racks, **mr}
            new_blob = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            f.seek(0)
            f.truncate()
            f.write(new_blob)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
            return merged
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass


def _merge_fresh_comments_into_status_payload(out: dict) -> None:
    """Re-read rack comments from disk. Cached /api/status rows omit updates until rack_status re-runs; comments are edited separately, so always merge."""
    try:
        out["comments"] = read_comments()
    except Exception:
        out.setdefault("comments", {})


def _merge_live_native_workers_into_status(out: dict, log_dir: Path) -> None:
    """Refresh native table rows and ``native_workers`` from in-memory async workers.

    Cached ``/api/status`` JSON can otherwise keep ``sinfo`` / ``ct_down_unk`` as ``?`` forever
    (first snapshot was taken before ds/sinfo threads finished) while only worker metadata was merged.
    """
    if _truthy_env("RACK_STATUS_USE_SHELL_TABLE"):
        return
    ld = log_dir.resolve()
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
        if stripped.startswith("rack; list") or stripped.startswith("cm: rack; list"):
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
        if any(mk in stripped for mk in _FOOTNOTE_MARKERS):
            if stripped not in footnotes:
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
        if not host_exec.is_remote() and not shutil.which(cmd[0]):
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
            out["comments"] = {}
        out["physical_rf0"] = get_physical_rf0_map()
        out["native_workers"] = {
            "mode": "shell_table",
            "note": "Table from rack_status.sh; native async workers are not used.",
        }
        return out

    out = native_status.build_native_rack_status(
        log_dir.resolve(),
        include_raw,
        _probe_log_json_via_rack_status,
    )
    summ = out.get("summary")
    if not isinstance(summ, dict):
        out["summary"] = {"done": 0, "fail": 0, "ongoing": 0, "pending": 0}
    try:
        out["comments"] = read_comments()
    except Exception:
        out["comments"] = {}
    out["physical_rf0"] = get_physical_rf0_map()
    return out


def _rack_status_background_enabled() -> bool:
    return not _truthy_env("RACK_STATUS_BACKGROUND_DISABLED")


def _rack_status_background_interval_sec() -> int:
    return max(5, _job_env_int("RACK_STATUS_BACKGROUND_INTERVAL_SEC", 30))


def _rack_status_cache_clear_all() -> None:
    """Invalidate cached native payloads so the next ``/api/status`` uses current job-env overrides."""
    with _rack_status_cache_lock:
        _rack_status_cache.clear()


def _rack_status_cache_key(log_dir: Path) -> str:
    return str(log_dir.resolve())


def _register_rack_status_log_dir(log_dir: Path) -> str:
    key = _rack_status_cache_key(log_dir)
    with _rack_status_cache_lock:
        _rack_status_registry.add(key)
    return key


def _cached_rack_status_copy(log_dir: Path) -> dict | None:
    """Return a shallow copy of the cached /api/status payload for *log_dir*, or ``None``."""
    if not _rack_status_background_enabled():
        return None
    key = _rack_status_cache_key(log_dir)
    with _rack_status_cache_lock:
        hit = _rack_status_cache.get(key)
    return dict(hit) if hit else None


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
    """Refresh cached rack_status for every registered ``log_dir``; one subprocess at a time."""
    first = True
    while True:
        if not first:
            interval = max(5, _rack_status_background_interval_sec())
            time.sleep(float(interval))
        first = False
        try:
            with _rack_status_cache_lock:
                dirs = sorted(_rack_status_registry)
            if not dirs:
                dirs = [_rack_status_cache_key(_default_log_dir())]
            for d in dirs:
                p = Path(d)
                try:
                    with _rack_status_exec_lock:
                        payload = run_rack_status(p, include_raw=False)
                    with _rack_status_cache_lock:
                        _rack_status_cache[_rack_status_cache_key(p)] = payload
                except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                    continue
                except Exception:
                    continue
        except Exception:
            continue


def _nemo_done_rack_ids_from_rack_status(log_dir: Path) -> frozenset[str] | None:
    """Racks whose **nemo** cell is ``done`` in the current status table (native or shell).

    Returns ``None`` if status could not be loaded (caller treats as: do not apply this filter).
    Returns an empty frozenset if the table loaded but no rack is ``done``.
    """
    try:
        cached = _cached_rack_status_copy(log_dir)
        if cached is not None:
            data = cached
        elif _rack_status_background_enabled():
            with _rack_status_exec_lock:
                data = run_rack_status(log_dir, include_raw=False)
        else:
            data = run_rack_status(log_dir, include_raw=False)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    except Exception:
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


@app.route("/api/comments", methods=["GET"])
def api_comments_list():
    return jsonify({"ok": True, "comments": read_comments()})


@app.route("/api/comments/<rack_id>", methods=["PUT", "DELETE"])
def api_comment_put(rack_id: str):
    """PUT JSON {\"comment\": \"...\"} sets text; empty string clears. DELETE clears.

    *rack_id* is either a rack id (``pN-rMM``) or a multirack log key: ``mr:<basename>`` or
    ``<basename>`` for a ``*.txt`` log name containing ``nemo`` or ``nccl``.
    """
    raw_id = (rack_id or "").strip()
    if request.method == "DELETE":
        payload = ""
    else:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "expected JSON object with \"comment\" string"}), 400
        c = body.get("comment", "")
        if c is not None and not isinstance(c, str):
            return jsonify({"ok": False, "error": "\"comment\" must be a string"}), 400
        payload = c if isinstance(c, str) else ""

    is_multirack = False
    storage_key = ""
    multirack_bn = ""
    try:
        storage_key = _normalize_rack_id(raw_id)
    except ValueError:
        multirack_bn = raw_id[3:].strip() if raw_id.lower().startswith("mr:") else raw_id.strip()
        sk = _multirack_comment_storage_key(multirack_bn)
        if sk is None:
            return jsonify(
                {
                    "ok": False,
                    "error": "invalid rack id or multirack log basename (use pN-rMM or mr:<log>.txt)",
                }
            ),
            400
        is_multirack = True
        storage_key = sk

    try:
        if is_multirack:
            all_comments = set_comment_for_multirack_log(multirack_bn, payload)
        else:
            all_comments = set_comment_for_rack(raw_id, payload)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(
        {
            "ok": True,
            "rack": storage_key,
            "comment": all_comments.get(storage_key, ""),
            "comments": all_comments,
        }
    )


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
        return _resolve_path(Path(raw))
    return _resolve_path(SCRIPTS_DIR / "gpu_reset.sh")


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
        return _resolve_path(Path(raw))
    return _resolve_path(SCRIPTS_DIR / "bmc-recovery.sh")


def _macswap_script_path() -> Path:
    raw = (_eget("RACK_MACSWAP_SCRIPT") or "").strip()
    if raw:
        return _resolve_path(Path(raw))
    return _resolve_path(SCRIPTS_DIR / "macswap" / "macswap")


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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

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
    return _resolve_path(Path(_bug_report_remote_dir()))


def _ensure_bug_report_local_base_dir() -> tuple[Path, str | None]:
    """Create ``RACK_BUG_REPORT_REMOTE_DIR`` when missing (local dashboard host only)."""
    base = _bug_report_local_base_path()
    if host_exec.is_remote():
        return base, None
    if base.exists():
        if not base.is_dir():
            return base, f"not a directory: {base}"
        return base, None
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return base, f"could not create reports directory {base}: {e}"
    return base, f"created reports directory: {base}"


def _local_bug_report_inventory() -> tuple[list[dict[str, str]], str | None]:
    """Scan ``<RACK_BUG_REPORT_REMOTE_DIR>/<subdir>/`` for bug-report ``*.log.gz``, NVLMapper ``*.csv`` / ``*.xls``, ``nvlmapper.*.zip``, and ``*.xlsx``."""
    base, ensure_notice = _ensure_bug_report_local_base_dir()
    if ensure_notice and ensure_notice.startswith("not a directory"):
        return [], ensure_notice
    if ensure_notice and ensure_notice.startswith("could not create"):
        return [], ensure_notice
    items: list[dict[str, str]] = []
    try:
        for sub in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not sub.is_dir():
                continue
            if not _BUG_REPORT_SUBDIR_RE.fullmatch(sub.name):
                continue
            try:
                for f in sorted(
                    sub.glob("nvidia-bug-report*.log.gz"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    if not f.is_file():
                        continue
                    try:
                        f.resolve().relative_to(base)
                    except ValueError:
                        continue
                    items.append({"host": sub.name, "name": f.name})
                for f in sorted(
                    sub.glob("nvlmapper.*.zip"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    if not f.is_file():
                        continue
                    if not _NVLMAPPER_ZIP_BASENAME_RE.fullmatch(f.name):
                        continue
                    try:
                        f.resolve().relative_to(base)
                    except ValueError:
                        continue
                    items.append({"host": sub.name, "name": f.name})
                for f in sorted(
                    sub.glob("*.csv"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    if not f.is_file():
                        continue
                    if not _NVLMAPPER_TABLE_BASENAME_RE.fullmatch(f.name):
                        continue
                    try:
                        f.resolve().relative_to(base)
                    except ValueError:
                        continue
                    items.append({"host": sub.name, "name": f.name})
                for f in sorted(
                    sub.glob("*.xls"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    if not f.is_file():
                        continue
                    if not _NVLMAPPER_TABLE_BASENAME_RE.fullmatch(f.name):
                        continue
                    try:
                        f.resolve().relative_to(base)
                    except ValueError:
                        continue
                    items.append({"host": sub.name, "name": f.name})
                for f in sorted(
                    sub.glob("*.xlsx"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    if not f.is_file():
                        continue
                    if not _REPORT_EXCEL_BASENAME_RE.fullmatch(f.name):
                        continue
                    try:
                        f.resolve().relative_to(base)
                    except ValueError:
                        continue
                    items.append({"host": sub.name, "name": f.name})
            except OSError:
                continue
        # Workbooks placed directly under the reports base (not under a hostname subdir).
        try:
            for f in sorted(
                base.glob("*.xlsx"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                if not f.is_file():
                    continue
                if not _REPORT_EXCEL_BASENAME_RE.fullmatch(f.name):
                    continue
                try:
                    f.resolve().relative_to(base.resolve())
                except ValueError:
                    continue
                items.append({"host": _BUG_REPORT_EXCEL_ROOT_HOST, "name": f.name})
        except OSError:
            pass
    except OSError as e:
        return [], str(e)
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

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
    if host == _BUG_REPORT_EXCEL_ROOT_HOST:
        path = (base / name).resolve()
    else:
        path = (base / host / name).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "path outside reports directory"}), 400
    if not path.is_file():
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
        return _resolve_path(Path(raw))
    return _resolve_path(SCRIPTS_DIR / "hw_check_pdsh.sh")


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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
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


def _prepare_hw_check(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    if os.environ.get("RACK_HW_CHECK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "HW check disabled (RACK_HW_CHECK_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
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
    try:
        specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)

    checker_args_display: str | None = None
    argv: list[str]
    preset: str

    custom = body.get("checker_args")
    if isinstance(custom, list) and custom:
        try:
            parts = [str(x).strip() for x in custom]
        except Exception:
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

    pdsh_w = ",".join(specs)
    return {
        "pdsh_w": pdsh_w,
        "preset": preset,
        "checker_argv": argv,
        "checker_args_display": checker_args_display,
    }, None


def _hw_check_worker(job_id: str, pdsh_w: str, preset: str, checker_argv: list[str]) -> None:
    """Run hw_check_pdsh.sh in a background thread."""

    def _patch(**kwargs: object) -> None:
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

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
    return _resolve_path(SCRIPTS_DIR / "checker.sh")


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
        proc = subprocess.run(
            ["bash", str(script), "--help"],
            cwd=str(SCRIPTS_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
    assert isinstance(log_dir, Path)
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_SDN_RESET_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
            error=None if ok else "nvswitch_sdn_reset.sh exited non-zero (see stdout/stderr)",
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
            error=f"NVLink SDN reset timed out after {timeout_sec}s",
        )
    except Exception as e:
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
    assert isinstance(log_dir, Path)
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_SYSTEM_RESET_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
            error=None if ok else "nvswitch_system_reset.sh exited non-zero (see stdout/stderr)",
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
            error=f"NVLink system reset timed out after {timeout_sec}s",
        )
    except Exception as e:
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
    assert isinstance(log_dir, Path)
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(60, _env_int("RACK_IMEX_RESET_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
            error=None if ok else "imex_reset.sh exited non-zero (see stdout/stderr)",
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
            error=f"IMEX reset timed out after {timeout_sec}s",
        )
    except Exception as e:
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
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, Path)
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
    raw = (_eget("RACK_NVLMAPPER_REPORTS_BASE") or "/home/cmsupport/reports").strip()
    return Path(os.path.expanduser(raw or "/home/cmsupport/reports")).resolve()


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
    base = (_nvlmapper_reports_base() / rack).resolve()
    try:
        path.resolve().relative_to(base)
    except ValueError:
        return False
    name = path.name
    if not name.startswith(f"nvlmapper.{rack}.") or not name.lower().endswith(".zip"):
        return False
    return path.is_file()


def _nvlmapper_stage_cleanup_keep_outputs(work: Path, rack: str) -> None:
    """Remove staged ``nvlmapper`` binary and ``run_nvlmapper.sh``; drop generated YAML with credentials.

    CSV / XLS / XLSX (and other outputs) remain under *work* for operators to copy from ``reports/<rack>/``.
    """
    for name in ("nvlmapper", "run_nvlmapper.sh"):
        p = work / name
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
        except OSError:
            pass
    for yaml_name in (f"config-{rack}.yaml", f"config_{rack}.yaml"):
        p = work / yaml_name
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _nvlmapper_async_worker(job_id: str, rack: str) -> None:
    """Stage ``nvlmapper`` + ``run_nvlmapper.sh``, run ``./run_nvlmapper.sh``, then clean staging only (keep outputs)."""

    def _patch(**kwargs: object) -> None:
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(120, _env_int("RACK_NVLMAPPER_TIMEOUT", 14400))
    work = (_nvlmapper_reports_base() / rack).resolve()
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
        work.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SCRIPTS_DIR / "nvlmapper", work / "nvlmapper")
        shutil.copy2(SCRIPTS_DIR / "run_nvlmapper.sh", work / "run_nvlmapper.sh")
        (work / "nvlmapper").chmod(0o755)
        (work / "run_nvlmapper.sh").chmod(0o755)
        proc = subprocess.run(
            ["./run_nvlmapper.sh", rack],
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
            "nvlmapper_work_dir": str((_nvlmapper_reports_base() / rack).resolve()),
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
    assert isinstance(log_dir, Path)
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
    assert isinstance(log_dir, Path)
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
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
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
    assert isinstance(log_dir, Path)
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


@app.route("/api/reset-job-status", methods=["GET"])
def api_reset_job_status():
    """Poll async jobs: power, GPU reset, services restart, HW check, bug report, NMXC cycle, NVLink SDN reset, NVLink system reset, IMEX reset, NVLMAPPER, Precheck, NVSWICTH check, or IMEX check."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _reset_jobs_lock:
        row = _reset_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    payload["ok"] = True
    return jsonify(payload)


_SLURM_DRAIN_REASON_RE = re.compile(r"^[A-Za-z0-9 _.,:/()\-]{1,256}$")
# last_job / scancel: plain id or job array id (e.g. 12345_0)
_SLURM_SCANCEL_JOB_ID_RE = re.compile(r"^[1-9][0-9]*(_[0-9]+)?$")


def _slurm_nodename_target_from_body(body: dict) -> tuple[str | None, str | None]:
    """Parse rack (+ optional target) into Slurm nodename=… value; (None, err) on error."""
    rack = (body.get("rack") or "").strip()
    if not rack:
        return None, "rack is required"
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, "target must be a string"
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, str(e)
    return target, None


def _run_scontrol_node_update(target: str, state_kv: list[str]) -> dict:
    """Run scontrol update nodename=TARGET …; state_kv e.g. [\"state=drain\", \"reason=admin\"].

    By default runs under ``bash -lc`` after ``module load <RACK_SLURM_MODULE>`` (default *slurm*),
    matching cluster login environments where *scontrol* is not on PATH until modules load.
    Set RACK_SLURM_SKIP_MODULE_LOAD=1 to invoke *scontrol* directly (e.g. full path already on PATH).
    """
    plat = _get_platform()
    nodename_arg = f"nodename={target}"
    tail = " ".join(shlex.quote(a) for a in ["update", nodename_arg, *state_kv])
    shell_cmd = plat.scontrol_cmd(tail)
    cmd_display = shell_cmd

    dry = os.environ.get("RACK_SLURM_NODES_STATE_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    if dry:
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "dry-run (RACK_SLURM_NODES_STATE_DRY_RUN)",
            "stderr": "",
            "command_display": cmd_display,
        }
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(
            shell_cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError(
            "scontrol executable not found; set RACK_SLURM_SCONTROL to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
        }
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
    }


@app.route("/api/slurm-drain", methods=["POST"])
def api_slurm_drain():
    """POST JSON {\"rack\": \"p34-r07\"} — ``module load`` then scontrol update nodename=… drain."""
    if os.environ.get("RACK_SLURM_NODES_STATE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "Slurm drain/undrain disabled (RACK_SLURM_NODES_STATE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    target, err = _slurm_nodename_target_from_body(body)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    raw_reason = body.get("reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        return jsonify({"ok": False, "error": "reason must be a string"}), 400
    r = (raw_reason if isinstance(raw_reason, str) else "").strip()
    if not r:
        r = (_eget("RACK_SLURM_DRAIN_REASON") or "admin").strip() or "admin"
    if not _SLURM_DRAIN_REASON_RE.fullmatch(r):
        return jsonify(
            {
                "ok": False,
                "error": "invalid reason (1–256 chars: letters, digits, space, _.,:/()- only)",
            }
        ), 400
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "slurm_drain", _operator_from_request()):
            result = _run_scontrol_node_update(target, ["state=drain", f"reason={r}"])
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = bool(result.get("ok"))
    payload: dict = {"ok": ok, "rack": _normalize_rack_id((body.get("rack") or "").strip()), "target": target, **result}
    if not ok:
        payload["error"] = "scontrol update failed (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


@app.route("/api/slurm-undrain", methods=["POST"])
def api_slurm_undrain():
    """POST JSON {\"rack\": \"p34-r07\"} — ``module load`` then scontrol update nodename=… state=idle."""
    if os.environ.get("RACK_SLURM_NODES_STATE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "Slurm drain/undrain disabled (RACK_SLURM_NODES_STATE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    target, err = _slurm_nodename_target_from_body(body)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "slurm_undrain", _operator_from_request()):
            result = _run_scontrol_node_update(target, ["state=idle"])
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = bool(result.get("ok"))
    payload: dict = {"ok": ok, "rack": _normalize_rack_id((body.get("rack") or "").strip()), "target": target, **result}
    if not ok:
        payload["error"] = "scontrol update failed (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


def _run_scancel(job_id: str) -> dict:
    """Run ``scancel JOBID`` after optional ``module load`` (same env semantics as *scontrol*)."""
    jid = (job_id or "").strip()
    if not _SLURM_SCANCEL_JOB_ID_RE.fullmatch(jid):
        raise ValueError("job_id must be a Slurm job id (digits, optional _TASK for arrays)")
    plat = _get_platform()
    shell_cmd = plat.scancel_cmd(shlex.quote(jid))
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "scancel executable not found; set RACK_SLURM_SCANCEL to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
        }
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
    }


def _run_squeue() -> dict:
    """Run ``squeue`` after optional ``module load`` (same env semantics as *scancel* / *scontrol*)."""
    plat = _get_platform()
    shell_cmd = plat.squeue_cmd()
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "squeue executable not found; set RACK_SLURM_SQUEUE to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
        }
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
    }


@app.route("/api/slurm-squeue", methods=["GET"])
def api_slurm_squeue():
    """Run ``module load slurm && squeue`` (unless ``RACK_SLURM_SKIP_MODULE_LOAD``) for the dashboard host."""
    if os.environ.get("RACK_SLURM_SQUEUE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "squeue disabled (RACK_SLURM_SQUEUE_DISABLED)"}), 403
    try:
        result = _run_squeue()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


def _run_sinfo_r() -> dict:
    """Run ``sinfo -R --long`` after optional ``module load`` (same env semantics as *squeue* / *scontrol*)."""
    plat = _get_platform()
    shell_cmd = plat.sinfo_cmd("-R --long")
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "sinfo executable not found; set RACK_SLURM_SINFO to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
        }
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
    }


@app.route("/api/slurm-sinfo-r", methods=["GET"])
def api_slurm_sinfo_r():
    """Run ``module load slurm && sinfo -R --long`` (unless ``RACK_SLURM_SKIP_MODULE_LOAD``) for the dashboard host."""
    if os.environ.get("RACK_SLURM_SINFO_R_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "sinfo -R --long disabled (RACK_SLURM_SINFO_R_DISABLED)"}), 403
    try:
        result = _run_sinfo_r()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


@app.route("/api/slurm-scancel", methods=["POST"])
def api_slurm_scancel():
    """POST JSON ``{\"job_id\": \"…\"}`` — ``module load`` then ``scancel`` (optional ``rack`` for context only)."""
    if os.environ.get("RACK_SLURM_SCANCEL_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "scancel disabled (RACK_SLURM_SCANCEL_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    raw_jid = body.get("job_id")
    if isinstance(raw_jid, int):
        jid = str(raw_jid).strip()
    elif isinstance(raw_jid, str):
        jid = raw_jid.strip()
    else:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    if not jid:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    if not _SLURM_SCANCEL_JOB_ID_RE.fullmatch(jid):
        return jsonify(
            {
                "ok": False,
                "error": "job_id must be a Slurm job id (digits, optional _TASK for arrays)",
            }
        ), 400
    rack_raw = (body.get("rack") or "").strip()
    rack_norm = None
    if rack_raw:
        try:
            rack_norm = _normalize_rack_id(rack_raw)
        except ValueError:
            rack_norm = rack_raw
    try:
        with _track_sync_op(rack_raw, "slurm_scancel", _operator_from_request()):
            result = _run_scancel(jid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = bool(result.get("ok"))
    payload: dict = {"ok": ok, "job_id": jid, **result}
    if rack_norm:
        payload["rack"] = rack_norm
    if not ok:
        payload["error"] = "scancel failed (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


def _run_scontrol_show_job(job_id: str) -> dict:
    """Run ``scontrol show job JOBID`` after optional ``module load`` (same env as *scancel*)."""
    jid = (job_id or "").strip()
    if not _SLURM_SCANCEL_JOB_ID_RE.fullmatch(jid):
        raise ValueError("job_id must be a Slurm job id (digits, optional _TASK for arrays)")
    plat = _get_platform()
    tail = " ".join(shlex.quote(a) for a in ["show", "job", jid])
    shell_cmd = plat.scontrol_cmd(tail)
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "scontrol executable not found; set RACK_SLURM_SCONTROL to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
        }
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
    }


def _resolve_slurm_job_stdout_path(path_raw: str, log_dir_raw: str | None) -> Path:
    """Resolve Slurm ``StdOut=`` path for read APIs; must be a file under allowed roots or dashboard log_dir."""
    raw = (path_raw or "").strip()
    if not raw or ".." in raw or "://" in raw:
        raise ValueError("invalid path")
    p = _resolve_path(Path(raw))
    if not p.exists():
        raise ValueError("path does not exist")
    if not p.is_file():
        raise ValueError("path is not a regular file")
    roots = _allowed_roots()
    if roots:
        ok = any(str(p).startswith(str(r) + os.sep) or p == r for r in roots)
        if not ok:
            raise ValueError("job stdout path must be under RACK_STATUS_ALLOWED_PREFIXES")
        return p
    if log_dir_raw and str(log_dir_raw).strip():
        try:
            log_root = _resolve_log_dir(str(log_dir_raw).strip())
            p.relative_to(log_root.resolve())
            return p
        except ValueError:
            pass
    try:
        p.relative_to(_default_log_dir().resolve())
        return p
    except ValueError as e:
        raise ValueError(
            "job stdout path must be under the dashboard log directory (log_dir) or "
            "RACK_STATUS_DEFAULT_LOG_DIR, or configure RACK_STATUS_ALLOWED_PREFIXES"
        ) from e


@app.route("/api/slurm-scontrol-show-job", methods=["GET"])
def api_slurm_scontrol_show_job():
    """``GET ?job_id=`` — ``module load`` then ``scontrol show job JOBID`` (dashboard host)."""
    if os.environ.get("RACK_SLURM_SCONTROL_SHOW_JOB_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_SLURM_SCONTROL_SHOW_JOB_DISABLED)"}), 403
    jid = (request.args.get("job_id") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    if not _SLURM_SCANCEL_JOB_ID_RE.fullmatch(jid):
        return jsonify(
            {
                "ok": False,
                "error": "job_id must be a Slurm job id (digits, optional _TASK for arrays)",
            }
        ), 400
    try:
        result = _run_scontrol_show_job(jid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = bool(result.get("ok"))
    payload: dict = {"ok": ok, "job_id": jid, **result}
    if not ok:
        payload["error"] = "scontrol show job failed (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


@app.route("/api/slurm-job-stdout-content", methods=["GET"])
def api_slurm_job_stdout_content():
    """``GET ?path=&log_dir=`` — read job stdout file if path is allowed (from ``scontrol show job``)."""
    path_raw = (request.args.get("path") or "").strip()
    if not path_raw:
        return jsonify({"ok": False, "error": "path is required"}), 400
    try:
        lp = _resolve_slurm_job_stdout_path(path_raw, request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    max_inline = max(0, _env_int("RACK_LOG_INLINE_MAX_BYTES", 10 * 1024 * 1024))
    try:
        sz = lp.stat().st_size
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if sz > max_inline:
        return jsonify(
            {
                "ok": True,
                "too_large": True,
                "size_bytes": sz,
                "filename": lp.name,
                "max_inline_bytes": max_inline,
                "download_hint": "Use GET /api/slurm-job-stdout-download with the same query parameters.",
            }
        )
    try:
        raw = lp.read_bytes()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    text = raw.decode("utf-8", errors="replace")
    return jsonify(
        {
            "ok": True,
            "too_large": False,
            "size_bytes": sz,
            "filename": lp.name,
            "path": str(lp),
            "content": text,
        }
    )


@app.route("/api/slurm-job-stdout-download", methods=["GET"])
def api_slurm_job_stdout_download():
    """Download job stdout file (same path rules as ``/api/slurm-job-stdout-content``)."""
    path_raw = (request.args.get("path") or "").strip()
    if not path_raw:
        return jsonify({"ok": False, "error": "path is required"}), 400
    try:
        lp = _resolve_slurm_job_stdout_path(path_raw, request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not lp.is_file():
        return jsonify({"ok": False, "error": "not a file"}), 404
    return send_file(
        str(lp),
        as_attachment=True,
        download_name=lp.name,
        mimetype="text/plain; charset=utf-8",
        max_age=0,
    )


def _rack_log_resolve(
    log_dir_raw: str | None,
    rack: str,
    job_id_raw: str | None,
    log_file_raw: str | None = None,
) -> tuple[Path, Path, str] | tuple[Response, int]:
    """Resolve rack log path for API handlers. Returns (log_dir, log_path, rack_key) or (jsonify, status)."""
    try:
        log_dir = _resolve_log_dir(log_dir_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    lf = (log_file_raw or "").strip()
    if lf:
        lp = _resolve_nemo_log_basename_in_dir(log_dir, lf)
        if lp is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"log file not found or basename must contain NEMO/NCCL: {Path(lf).name!r}"
                        ),
                    }
                ),
                404,
            )
        rk = multirack_display_rack_from_basename(lp.name)
        return log_dir, lp, rk
    rack_s = (rack or "").strip()
    try:
        rk = _normalize_rack_id(rack_s)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    job_raw = (job_id_raw or "").strip()
    job_id = job_raw if job_raw and job_raw != "-" else None
    log_path = pick_log_for_rack(log_dir, rack_s, job_id)
    if log_path is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        f"no matching *.txt for rack {rk!r} under {log_dir} or {log_dir / 'failed'} "
                        f"(NEMO/NCCL basename, or ClusterKit root-CK-/clusterkit with one rack id)"
                    ),
                }
            ),
            404,
        )
    root = log_dir.resolve()
    lp = log_path.resolve()
    try:
        lp.relative_to(root)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid log path"}), 400
    if not lp.is_file():
        return jsonify({"ok": False, "error": "log file not found"}), 404
    return log_dir, lp, rk


@app.route("/api/rack-log-download")
def api_rack_log_download():
    """Download the full NEMO rack log file (same pick as parser / tail-log)."""
    out = _rack_log_resolve(
        request.args.get("log_dir"),
        request.args.get("rack") or "",
        request.args.get("job_id"),
        request.args.get("log_file"),
    )
    if not isinstance(out[0], Path):
        return out
    _log_dir, lp, _rk = out
    return send_file(
        str(lp),
        as_attachment=True,
        download_name=lp.name,
        mimetype="text/plain; charset=utf-8",
        max_age=0,
    )


@app.route("/api/rack-log-content")
def api_rack_log_content():
    """Return full log text as JSON if under size limit; otherwise too_large + metadata."""
    max_inline = max(0, _env_int("RACK_LOG_INLINE_MAX_BYTES", 10 * 1024 * 1024))
    out = _rack_log_resolve(
        request.args.get("log_dir"),
        request.args.get("rack") or "",
        request.args.get("job_id"),
        request.args.get("log_file"),
    )
    if not isinstance(out[0], Path):
        return out
    _log_dir, lp, _rk = out
    try:
        sz = lp.stat().st_size
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if sz > max_inline:
        return jsonify(
            {
                "ok": True,
                "too_large": True,
                "size_bytes": sz,
                "filename": lp.name,
                "max_inline_bytes": max_inline,
            }
        )
    try:
        raw = lp.read_bytes()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    text = raw.decode("utf-8", errors="replace")
    return jsonify(
        {
            "ok": True,
            "too_large": False,
            "size_bytes": sz,
            "filename": lp.name,
            "content": text,
        }
    )


def _demo_parser_results():
    """Return synthetic parser results for demo mode (no parser.sh needed)."""
    import math
    import hashlib as _hl
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


@app.route("/api/parser-results")
def api_parser_results():
    """Run parser.sh --json on the log for rack (same cwd convention as rack_status log_dir).

    Optional ``log_file``: basename under log_dir (or failed/) for multirack / explicit logs (must
    contain NEMO or NCCL in the basename). When set, ``rack`` is optional (used for Grafana context).

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
    rack = (request.args.get("rack") or "").strip()
    job_raw = (request.args.get("job_id") or "").strip()
    job_id = job_raw if job_raw and job_raw != "-" else None
    if log_fn:
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
        log_path = pick_log_for_rack(log_dir, rack, job_id)
        if log_path is None:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"no matching *.txt for rack {rk!r} under {log_dir} or {log_dir / 'failed'} "
                        f"(NEMO/NCCL basename, or ClusterKit root-CK-/clusterkit with one rack id)"
                    ),
                }
            ), 404
    parser = _parser_path()
    if not _script_exists(parser):
        return jsonify({"ok": False, "error": f"parser.sh not found: {parser}"}), 500
    env = _subprocess_env({"NO_COLOR": "1"})
    cmd = [
        "bash",
        str(parser),
        *_parser_extra_args(),
        "--json",
        str(log_path.resolve()),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "parser.sh timed out"}), 504
    except Exception as e:
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
                "log_file": str(log_path.resolve()),
            }
        ), 502
    if not isinstance(parsed, dict):
        return jsonify({"ok": False, "error": "parser JSON root must be an object"}), 502
    display_cmd = "./parser.sh --json " + shlex.quote(log_path.name)
    sn_nemo: list[object] = []
    sn_paths = _sn_llama_txt_paths_for_rack(log_dir, rk, exclude=log_path)
    if sn_paths:
        try:
            rel_sn = [str(p.resolve().relative_to(log_dir.resolve())) for p in sn_paths]
            cmd_sn = ["bash", str(parser), *_parser_extra_args(), "--json", *rel_sn]
            proc_sn = subprocess.run(
                cmd_sn,
                cwd=str(log_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=max(60, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
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
    log_tz_q = _sanitize_display_timezone_query(request.args.get("display_timezone"))
    g_embed = _grafana_parser_embed_context(
        log_path, rk, job_raw, parsed, log_timestamp_tz=log_tz_q
    )
    pdisp = _grafana_url_timezone_param()
    if isinstance(g_embed, dict) and g_embed.get("display_timezone"):
        pdisp = str(g_embed["display_timezone"])
    return jsonify(
        {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "rack": rk,
            "log_file": str(log_path.resolve()),
            "command_display": display_cmd,
            "data": merged_data,
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
    try:
        sz = log_path.stat().st_size
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if sz > _NVLOOM_HEATMAPS_LOG_MAX_BYTES:
        return jsonify(
            {
                "ok": False,
                "error": f"NVLOOM log too large ({sz} bytes; max {_NVLOOM_HEATMAPS_LOG_MAX_BYTES})",
            }
        ), 413
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": str(log_path.resolve()), "content": text})


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
    base = _clusterkit_rack_dir(log_dir, rk)
    if not base.is_dir():
        return jsonify({"ok": True, "rack": rk, "runs": []})
    runs_out: list[dict[str, object]] = []
    try:
        subdirs = [p for p in base.iterdir() if p.is_dir()]
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    for sub in subdirs:
        bw = sub / "bandwidth.json"
        if not bw.is_file():
            continue
        try:
            st = bw.stat()
        except OSError:
            continue
        if st.st_size > _CLUSTERKIT_JSON_MAX_BYTES:
            runs_out.append(
                {
                    "dir": sub.name,
                    "rel_path": str(bw.resolve().relative_to(log_dir.resolve())),
                    "mtime": int(st.st_mtime),
                    "error": f"bandwidth.json too large ({st.st_size} bytes)",
                }
            )
            continue
        try:
            raw = bw.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            runs_out.append(
                {
                    "dir": sub.name,
                    "rel_path": str(bw.resolve().relative_to(log_dir.resolve())),
                    "mtime": int(st.st_mtime),
                    "error": str(e),
                }
            )
            continue
        if not isinstance(data, dict):
            continue
        summ = _clusterkit_bandwidth_summary_from_data(data)
        meta_only = {
            "hca_tag": data.get("HCA_Tag"),
            "units": data.get("Units"),
            "date_and_time": data.get("Date_and_Time"),
            "jobid": data.get("JOBID"),
            "bidirectional": data.get("Bidirectional"),
            "cluster": data.get("Cluster"),
        }
        has_lat = (sub / "latency.json").is_file()
        runs_out.append(
            {
                "dir": sub.name,
                "rel_path": str(bw.resolve().relative_to(log_dir.resolve())),
                "mtime": int(st.st_mtime),
                "meta": meta_only,
                "summary": summ or {},
                "has_latency": has_lat,
            }
        )
    runs_out.sort(key=lambda r: int(r.get("mtime") or 0), reverse=True)
    out: dict[str, object] = {"ok": True, "rack": rk, "runs": runs_out}
    run_q = (request.args.get("run") or "").strip()
    if run_q and run_q == Path(run_q).name and re.fullmatch(r"[A-Za-z0-9_.\-]+", run_q):
        target = base / run_q / "bandwidth.json"
        try:
            sub_resolved = (base / run_q).resolve()
            if target.is_file() and sub_resolved.parent == base.resolve():
                sz = target.stat().st_size
                if sz <= _CLUSTERKIT_JSON_MAX_BYTES:
                    data = json.loads(target.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(data, dict):
                        parsed = _clusterkit_parse_bandwidth_payload(data)
                        if parsed:
                            labels, matrix, meta = parsed
                            summ = _clusterkit_bandwidth_summary_from_data(data)
                            detail: dict[str, object] = {
                                "dir": run_q,
                                "labels": labels,
                                "matrix": matrix,
                                "meta": meta,
                                "summary": summ or {},
                            }
                            lat_path = sub_resolved / "latency.json"
                            try:
                                if lat_path.is_file():
                                    lst = lat_path.stat().st_size
                                    if lst <= _CLUSTERKIT_JSON_MAX_BYTES:
                                        lat_data = json.loads(
                                            lat_path.read_text(encoding="utf-8", errors="replace")
                                        )
                                        if isinstance(lat_data, dict):
                                            lat_parsed = _clusterkit_parse_bandwidth_payload(lat_data)
                                            if lat_parsed:
                                                ll, lm, lmeta = lat_parsed
                                                lsumm = _clusterkit_pairwise_positive_summary(lat_data)
                                                detail["latency"] = {
                                                    "labels": ll,
                                                    "matrix": lm,
                                                    "meta": lmeta,
                                                    "summary": lsumm or {},
                                                }
                            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                                pass
                            out["detail"] = detail
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return jsonify(out)


def _parser_json_for_log_path(log_dir: Path, log_path: Path) -> tuple[dict | None, str | None]:
    """Run ``parser.sh --json`` on one log under ``log_dir`` (relative path). Returns ``(parsed, err)``."""
    parser = _parser_path()
    if not _script_exists(parser):
        return None, "parser.sh not found"
    env = _subprocess_env({"NO_COLOR": "1"})
    try:
        rel = str(log_path.resolve().relative_to(log_dir.resolve()))
    except ValueError:
        return None, "log path outside log_dir"
    cmd = ["bash", str(parser), *_parser_extra_args(), "--json", rel]
    timeout = max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "parser.sh timed out"
    except Exception as e:
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
    return (parsed if isinstance(parsed, dict) else None), None


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
    m = _MULTIRACK_N_RACK_RE.search(name)
    if m:
        tok = m.group(0).lower()
        if tok.endswith("rack"):
            try:
                return max(1, int(tok[:-4], 10))
            except ValueError:
                pass
    phy = _multirack_physical_rack_ids(name)
    return len(phy) if phy else 0


def _multirack_done_log_paths(log_dir: Path) -> list[Path]:
    """Multirack NEMO/NCCL logs under log_dir root and ``failed/`` that pass the successful-log heuristic."""
    pat = _parser_export_glob()
    root = log_dir.resolve()
    merged: dict[str, Path] = {}
    if root.is_dir():
        for p in root.glob(pat):
            if not p.is_file() or not _nemo_nccl_in_log_basename(p.name):
                continue
            if not _is_multirack_log_basename(p.name):
                continue
            if not _nemo_log_file_looks_successful(p):
                continue
            merged[str(p.resolve())] = p
    for p in _multirack_txt_paths_under_failed(log_dir, pat):
        if _nemo_log_file_looks_successful(p):
            merged[str(p.resolve())] = p
    return sorted(merged.values(), key=lambda x: x.stat().st_mtime, reverse=True)


def _read_log_text_chunk(path: Path, max_bytes: int = 14 * 1024 * 1024) -> str:
    try:
        with path.open("rb") as fh:
            blob = fh.read(max_bytes)
        return blob.decode("utf-8", errors="replace")
    except OSError:
        return ""


_RE_RUN_GB = re.compile(r"(?<![\w-])-gb\s+(\d+)\b", re.I)
_RE_RUN_NUM_GPUS = re.compile(r"--num_gpus\s+(\d+)\b", re.I)
_RE_RUN_GPUS_PER_NODE = re.compile(r"--gpus_per_node\s+(\d+)\b", re.I)
_RE_RUN_SLURM_NNODES = re.compile(r"SLURM_NNODES=(\d+)\b")
_RE_RUN_NODELIST = re.compile(r"NODELIST:\s*(\S+)")
_RE_SEQ_LEN_CANDIDATES = (
    re.compile(r"(?:^|[\s|])seq(?:uence)?[_-]?length[=:\s]+(\d+)\b", re.I),
    re.compile(r"context[_-]?length[=:\s]+(\d+)\b", re.I),
    re.compile(r"(?<![\w-])-sl\s+(\d+)\b"),
)


def _last_regex_int(pat: re.Pattern[str], text: str) -> int | None:
    last: int | None = None
    for m in pat.finditer(text):
        try:
            last = int(m.group(1))
        except (ValueError, IndexError):
            continue
    return last


def _infer_num_nodes_from_nodelist(hosts: str) -> int | None:
    h = hosts.strip()
    if not h or re.search(r"[\[\]]", h):
        return None
    if "," in h:
        return h.count(",") + 1
    return 1


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


def _mean_finite(vals: list[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not xs:
        return None
    return float(statistics.mean(xs))


def _log_mtime_or_zero(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _nemotron_scaling_append_entry(
    bucket: dict[int, list[dict[str, object]]], n_r: int, lp: Path, tf_f: float, st_f: float
) -> None:
    """Append one log's Nemotron stats + parsed run geometry / throughput for analytics buckets."""
    log_text = _read_log_text_chunk(lp)
    mrun = _nemotron_log_run_hw_tokens(log_text, st_f)
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


_analytics_aggregate_cache_lock = threading.Lock()
_ANALYTICS_AGG_CACHE_PATH = (REPO_ROOT / "var" / "cache" / "analytics_aggregate_cache.json").resolve()


def _analytics_rows_fingerprint(rows_in: object) -> str:
    """Stable hash of rack status rows sent with analytics (same inputs → same key)."""
    if not isinstance(rows_in, list):
        return hashlib.sha256(b"[]").hexdigest()
    pairs: list[tuple[str, str]] = []
    for ent in rows_in:
        if not isinstance(ent, dict):
            continue
        rk = str(ent.get("rack", "")).strip().lower()
        nm = str(ent.get("nemo", "")).strip().lower()
        if rk:
            pairs.append((rk, nm))
    pairs.sort()
    raw = json.dumps(pairs, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _analytics_aggregate_cache_get(log_dir_key: str) -> dict[str, object] | None:
    """Return last stored payload for *log_dir_key* (resolved absolute path string), or ``None``."""
    with _analytics_aggregate_cache_lock:
        try:
            with open(_ANALYTICS_AGG_CACHE_PATH, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return None
    if not isinstance(doc, dict) or int(doc.get("version") or 0) != 1:
        return None
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        return None
    hit = entries.get(log_dir_key)
    return hit if isinstance(hit, dict) else None


def _analytics_aggregate_cache_put(log_dir_key: str, payload: dict[str, object]) -> None:
    """Persist ``analyzed_at``, ``rows_fingerprint``, ``single_rack``, ``multirack`` for a log directory."""
    _ANALYTICS_AGG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    store = {
        k: payload[k]
        for k in ("analyzed_at", "rows_fingerprint", "single_rack", "multirack")
        if k in payload
    }
    with _analytics_aggregate_cache_lock:
        doc: dict[str, object] = {"version": 1, "entries": {}}
        try:
            if _ANALYTICS_AGG_CACHE_PATH.is_file():
                with open(_ANALYTICS_AGG_CACHE_PATH, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                if isinstance(prev, dict) and int(prev.get("version") or 0) == 1 and isinstance(prev.get("entries"), dict):
                    doc["entries"] = dict(prev["entries"])
        except (OSError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
            pass
        ent: dict[str, object] = {}
        prev_entries = doc.get("entries")
        if isinstance(prev_entries, dict):
            ent = dict(prev_entries)
        doc["entries"] = ent
        ent[log_dir_key] = store
        tmp = _ANALYTICS_AGG_CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, _ANALYTICS_AGG_CACHE_PATH)


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
    hit = _analytics_aggregate_cache_get(str(log_dir.resolve()))
    if not hit:
        return jsonify({"ok": False, "error": "no cached analysis for this log directory"}), 404
    out: dict[str, object] = dict(hit)
    out["ok"] = True
    out["cached"] = True
    return jsonify(out)


@app.route("/api/analytics/aggregate", methods=["POST"])
def api_analytics_aggregate():
    """POST JSON ``{log_dir, rows:[{rack,nemo},…]}`` — parser-based NCCL/Nemotron analytics (single-rack + multirack).

    Single-rack: only rows with ``nemo == "done"``; newest *1Rack-*.txt per rack that passes the
    successful-log heuristic. NCCL: fleet median baseline for all_reduce bus_bw series and peak
    scalars. Nemotron: ``median_step_time``, ``tflops_per_gpu``, step-time CV%, deviation vs fleet mean.

    Multirack: successful multirack logs only; NCCL one series per log. Nemotron scaling / throughput / efficiency
    tables group logs by inferred rack count (``…2rack…`` / distinct ``pN-rMM`` tokens), **including bucket Rack=1**
    from successful single-rack (1Rack) logs for done racks (same picks as single-rack Nemotron). Per-bucket means
    for step and TFLOPs; nodes/# GPU shown from the **newest** log in each bucket.
    """
    if os.environ.get("RACK_ANALYTICS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "analytics disabled (RACK_ANALYTICS_DISABLED)"}), 403
    if _truthy_env("RACK_STATUS_DEMO"):
        demo = dict(_demo_analytics_aggregate())
        demo["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        demo["rows_fingerprint"] = "demo"
        demo["cached"] = False
        return jsonify(demo)
    body = request.get_json(silent=True) or {}
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rows_in = body.get("rows")
    if not isinstance(rows_in, list):
        return jsonify({"ok": False, "error": 'send JSON {"log_dir": "…", "rows": [{"rack":"…","nemo":"…"}, …]}'}), 400

    done_racks: list[str] = []
    for ent in rows_in:
        if not isinstance(ent, dict):
            continue
        rk = str(ent.get("rack", "")).strip()
        nemo = str(ent.get("nemo", "")).strip().lower()
        if not rk or nemo != "done":
            continue
        try:
            done_racks.append(_normalize_rack_id(rk))
        except ValueError:
            continue
    done_racks = sorted(set(done_racks))

    series_by_rack: dict[str, list[dict[str, float]]] = {}
    scalar_rows: list[dict[str, object]] = []
    nemotron_rows: list[dict[str, object]] = []
    scaling_entries: dict[int, list[dict[str, object]]] = {}

    for rack in done_racks:
        lp = pick_log_for_rack(log_dir, rack, None)
        if lp is None or not lp.is_file():
            continue
        if not _nemo_log_file_looks_successful(lp):
            continue
        parsed, err = _parser_json_for_log_path(log_dir, lp)
        if parsed is None:
            continue
        nc = _pick_nccl_row(parsed, rack)
        nm = _pick_nemotron_row(parsed, rack)
        lf = lp.name
        ar_series: list[dict[str, float]] = []
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
                }
            )
        if isinstance(nm, dict):
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
                _nemotron_scaling_append_entry(scaling_entries, 1, lp, float(tf), float(med))

    size_to_vals: dict[int, list[float]] = {}
    for _rk, ser in series_by_rack.items():
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
            if n_r < 1:
                n_r = 1
            tf = _parse_gbps_scalar(nm.get("tflops_per_gpu"))
            st = _parse_gbps_scalar(nm.get("median_step_time"))
            if st is None:
                st = _parse_gbps_scalar(nm.get("step_s"))
            if tf is not None and st is not None:
                tf_f = float(tf)
                st_f = float(st)
                _nemotron_scaling_append_entry(scaling_entries, n_r, lp, tf_f, st_f)

    nemotron_scaling: list[dict[str, object]] = []
    nemotron_throughput_scaling: list[dict[str, object]] = []
    for n_r in sorted(scaling_entries.keys()):
        ents = scaling_entries[n_r]
        if not ents:
            continue
        mean_tf = round(statistics.mean(float(e["tf"]) for e in ents), 3)
        mean_st = round(statistics.mean(float(e["st"]) for e in ents), 4)
        n_logs = len(ents)
        nemotron_scaling.append(
            {
                "n_racks": n_r,
                "n_logs": n_logs,
                "mean_tflops_per_gpu": mean_tf,
                "mean_step_s": mean_st,
            }
        )
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
    try:
        _analytics_aggregate_cache_put(str(log_dir.resolve()), payload)
    except OSError:
        _logger.exception("analytics aggregate cache write failed")
    return jsonify(payload)


def _probe_log_json_via_rack_status(abs_path: Path) -> dict[str, object] | None:
    """Run ``rack_status.sh --probe-json`` for one log file (same nemo/nccl/squeue heuristics as the table)."""
    script = _script_path()
    if not _script_exists(script):
        return None
    ap = abs_path.resolve()
    mvt = (_eget("RACK_MULTIRACK_VALIDATION_TEST") or "nccl_all_reduce").strip() or "nccl_all_reduce"
    vt_if_unset = mvt if _is_multirack_log_basename(ap.name) else "nccl_all_reduce,llm"
    env = _subprocess_env({"NO_COLOR": "1"}, validation_test_if_unset=vt_if_unset)
    _ensure_cmsh_rack_list_env_for_subprocess(env)
    try:
        proc = host_exec.run_on_host(
            [str(script), "--probe-json", str(ap)],
            cwd=str(ap.parent),
            env=env,
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


_LOG_ANALYSIS_CACHE_VERSION = 1
_LOG_ANALYSIS_CACHE_PATH = REPO_ROOT / "var" / "cache" / "log_analysis_cache.json"


def _glob_one_rack_nemo_logs_for_rack_status(log_dir: Path) -> list[Path]:
    """NEMO single-rack validation logs in log_dir root (not failed/, not NCCL-only, no Llama stdout)."""
    root = log_dir.resolve()
    naming = _nvis_naming()
    out: list[Path] = []
    for p in sorted(root.glob("*.txt"), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
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
    payload = native_status.build_multirack_status(
        log_dir.resolve(),
        _probe_log_json_via_rack_status,
        multirack_display_rack_from_basename,
        _multirack_physical_rack_ids,
    )
    return jsonify(payload)


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
        return Path(os.path.expanduser(raw)).resolve()
    return (_bug_report_local_base_path() / "parser-excel").resolve()


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
    rel_args: list[str] = []
    for p in paths:
        rel_args.append(str(p.resolve().relative_to(log_dir.resolve())))
    env = _subprocess_env({"NO_COLOR": "1"})
    cmd = ["bash", str(parser), *_parser_extra_args(), "--json", *rel_args]
    timeout = max(60, _env_int("RACK_PARSER_EXPORT_TIMEOUT", 900))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"parser.sh --json timed out after {timeout}s"}), 504
    except Exception as e:
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
        report_dir = _parser_excel_report_copy_dir()
        report_dir.mkdir(parents=True, exist_ok=True)
        dest = report_dir / fname
        dest.write_bytes(body)
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
    root = log_dir.resolve()
    bio = io.BytesIO()
    try:
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                rp = p.resolve()
                arc = str(rp.relative_to(root))
                zf.write(str(rp), arcname=arc)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
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
            "X-Export-Log-Count": str(len(paths)),
        },
    )


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
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(30, _job_env_int("RACK_VALIDATION_TIMEOUT", 600))
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
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
            proc = subprocess.run(
                cmd,
                cwd=str(log_dir),
                env=_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "validation script timed out", "target": target}), 504
    except Exception as e:
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
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _validation_jobs_lock:
        _validation_jobs[job_id] = {
            "job_id": job_id,
            "kind": "validation",
            "operator": _operator_from_request(),
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


@app.route("/api/run-validation-status", methods=["GET"])
def api_run_validation_status():
    """Return state for a job started via /api/run-validation-async (phase, stdout, …)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _validation_jobs_lock:
        row = _validation_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    payload["ok"] = True
    return jsonify(payload)


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
    specs: list[str] = []
    seen: set[str] = set()
    for item in racks_raw:
        rk = _normalize_rack_id(str(item).strip())
        if rk in seen:
            continue
        seen.add(rk)
        specs.append(f"{rk}-ct[{inner}]")
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
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    # Same as run-validation: cwd=log_dir so Slurm --output=… writes under the UI log dir.
    cwd = str(log_dir.resolve())
    with _xrack_jobs_lock:
        _xrack_jobs[job_id] = {
            "job_id": job_id,
            "kind": "xrack",
            "operator": _operator_from_request(),
            "rack": "multinode",
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
            "xrack_arg": arg,
        }
    threading.Thread(
        target=_xrack_worker,
        args=(job_id, cmd, cwd, timeout, display_cmd),
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "command_display": display_cmd,
        }
    )


@app.route("/api/run-nemo-xrack-status", methods=["GET"])
def api_run_nemo_xrack_status():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _xrack_jobs_lock:
        row = _xrack_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    payload["ok"] = True
    return jsonify(payload)


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
    cwd = str(log_dir.resolve())
    with _llama_jobs_lock:
        _llama_jobs[job_id] = {
            "job_id": job_id,
            "kind": "llama",
            "operator": _operator_from_request(),
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


@app.route("/api/run-llama-status", methods=["GET"])
def api_run_llama_status():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _llama_jobs_lock:
        row = _llama_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    payload["ok"] = True
    return jsonify(payload)


def _sn_llama_txt_paths_for_rack(log_dir: Path, rack: str, *, exclude: Path | None = None) -> list[Path]:
    """``log_dir/sn-llama/*.txt`` whose names suggest LLAMA and match *rack* (case-insensitive substring on rack id tokens)."""
    root = log_dir.resolve()
    d = root / "sn-llama"
    if not d.is_dir():
        return []
    needles: list[str] = []
    for rid in _nvis_naming().find_rack_ids_in_string(rack or ""):
        needles.append(rid)
    if not needles:
        t = (rack or "").strip().lower()
        if t:
            needles = [t]
    ex = exclude.resolve() if exclude is not None else None
    out: list[Path] = []
    try:
        paths = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    cap = max(1, _env_int("RACK_PARSER_SN_LLAMA_MAX_FILES", 25))
    for p in paths:
        if len(out) >= cap:
            break
        try:
            if not p.is_file():
                continue
            if ex is not None and p.resolve() == ex:
                continue
        except OSError:
            continue
        nl = p.name.casefold()
        if "llama" not in nl:
            continue
        # Needles from ``find_rack_ids_in_string`` keep canonical casing (e.g. CBR…); basename is matched case-insensitively.
        if needles and not any(n.casefold() in nl for n in needles):
            continue
        out.append(p)
    return list(reversed(out))


@app.route("/api/sn-llama-logs", methods=["GET"])
def api_sn_llama_logs():
    """List ``*.txt`` under ``log_dir/sn-llama/`` (newest first), optional ``rack`` substring filter."""
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack_f = (request.args.get("rack") or "").strip().lower()
    d = log_dir.resolve() / "sn-llama"
    if not d.is_dir():
        return jsonify({"ok": True, "files": []})
    out: list[dict[str, object]] = []
    try:
        paths = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    for p in paths[:200]:
        try:
            if not p.is_file():
                continue
            nm = p.name.lower()
            if rack_f and rack_f not in nm:
                continue
            st = p.stat()
            out.append({"name": p.name, "mtime": int(st.st_mtime)})
        except OSError:
            continue
    return jsonify({"ok": True, "files": out})


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
        "NVIS_BIN_SCRIPTS": str(_resolve_path(SCRIPTS_DIR)),
        "CUDA_MAJ": cuda_m,
        "NVIS_SYSINFO_SH": str(_sysinfo_script_path()),
        "NVIS_CHECKER_SH": str(_resolve_path(SCRIPTS_DIR / "checker.sh")),
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
    if not script.is_file():
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
        "NVIS_BIN_SCRIPTS": str(_resolve_path(SCRIPTS_DIR)),
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
    if not script.is_file():
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
        "NVIS_BIN_SCRIPTS": str(_resolve_path(SCRIPTS_DIR)),
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
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "mnubergemm",
            "operator": _operator_from_request(),
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
        kwargs={"env_updates": env_merged},
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


@app.route("/api/run-mnubergemm-stress-status", methods=["GET"])
def api_run_mnubergemm_stress_status():
    """Return state for a job started via /api/run-mnubergemm-stress-async."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _stress_jobs_lock:
        row = _stress_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    payload["ok"] = True
    return jsonify(payload)


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
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvloom",
            "operator": _operator_from_request(),
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
        kwargs={"error_label": "nvloom singlerack script", "env_updates": env_merged},
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


@app.route("/api/run-nvloom-status", methods=["GET"])
def api_run_nvloom_status():
    """Return state for a job started via /api/run-nvloom-async (same store as mnubergemm stress)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _stress_jobs_lock:
        row = _stress_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    payload["ok"] = True
    return jsonify(payload)


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
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "clusterkit",
            "operator": _operator_from_request(),
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
        kwargs={"error_label": "clusterkit script", "env_updates": env_merged},
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


@app.route("/api/run-clusterkit-status", methods=["GET"])
def api_run_clusterkit_status():
    """Return state for a job started via /api/run-clusterkit-async."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _stress_jobs_lock:
        row = _stress_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    payload["ok"] = True
    return jsonify(payload)


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


def _merged_stdout_stream_generator(
    cmd: list[str],
    cwd: str,
    display_cmd: str,
    timeout_sec: int,
    *,
    linebuffer_env_var: str,
    linebuffer_default: str = "1",
    env_extra: dict[str, str] | None = None,
) -> Iterator[bytes]:
    """Yield merged stdout/stderr from a subprocess until exit (or timeout)."""
    cmd_run = list(cmd)
    remote = host_exec.is_remote()
    if not remote and (_eget(linebuffer_env_var, linebuffer_default) or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        stdbuf = shutil.which("stdbuf")
        if stdbuf:
            cmd_run = [stdbuf, "-oL", "-eL", *cmd_run]
    cmd_run = host_exec.wrap_for_host(cmd_run, cwd=cwd if remote else None)
    env = None if remote else _subprocess_env(env_extra)
    proc = subprocess.Popen(
        cmd_run,
        cwd=None if remote else cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=0,
    )
    outq: queue.Queue[bytes | None] = queue.Queue()

    def feeder() -> None:
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                block = stdout.read(8192)
                if not block:
                    break
                outq.put(block)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            outq.put(None)

    threading.Thread(target=feeder, daemon=True).start()
    header = f"# {display_cmd}\n# cwd: {cwd}\n\n"
    yield header.encode("utf-8")
    deadline = time.monotonic() + timeout_sec
    rc: int | None = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if proc.poll() is None:
                    proc.kill()
                yield b"\n*** timed out ***\n"
                try:
                    rc = proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    rc = -1
                yield f"\n--- exit {rc} ---\n".encode("utf-8")
                return
            try:
                chunk = outq.get(timeout=min(0.5, max(0.05, remaining)))
            except queue.Empty:
                continue
            if chunk is None:
                break
            yield chunk
        try:
            rc = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            rc = proc.poll()
        yield f"\n--- exit {rc if rc is not None else '?'} ---\n".encode("utf-8")
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass


def _sysinfo_stream_generator(prep: dict):
    """Yield stdout+stderr bytes from sysinfo.sh until process exit (or timeout)."""
    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    cwd = _REMOTE_BASE if host_exec.is_remote() and _REMOTE_BASE else str(prep["log_dir"])
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        cwd,
        prep["display_cmd"],
        timeout_sec,
        linebuffer_env_var="RACK_SYSINFO_LINEBUFFER",
        linebuffer_default="1",
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
    cwd = _REMOTE_BASE if host_exec.is_remote() and _REMOTE_BASE else str(prep["log_dir"])
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        cwd,
        prep["display_cmd"],
        timeout_sec,
        linebuffer_env_var="RACK_NVSWITCH_AUDIT_LINEBUFFER",
        linebuffer_default="1",
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
            )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "sysinfo.sh timed out", "target": prep["target"]}), 504
    except Exception as e:
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


@app.route("/api/tools/download-containers", methods=["POST"])
def api_tools_download_containers():
    """Run bin/scripts/05-download-containers.sh with a chosen destination (and optional arch).

    Requires ``NGC_API_KEY`` in the **server** environment (nvapi-…), not in the HTTP body.
    If the expected ``.sqsh`` already exists and is non-empty, returns success with ``skipped`` (no import, no key needed).
    """
    if os.environ.get("RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    dest_raw = body.get("destination")
    arch_raw = body.get("arch")
    try:
        dest_dir = _resolve_tools_destination(str(dest_raw).strip() if dest_raw else "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    found, checked_paths = _tools_find_existing_sqsh_for_skip(dest_dir)
    if found is not None:
        msg = f"File already exists, skipping download: {found}"
        return jsonify(
            {
                "ok": True,
                "skipped": True,
                "destination": str(dest_dir),
                "existing_file": str(found),
                "checked_paths": checked_paths,
                "message": msg,
                "stdout": msg,
                "stderr": "",
                "command_display": "(no command — output file already present)",
            }
        )
    if not (os.environ.get("NGC_API_KEY") or "").strip():
        return jsonify(
            {
                "ok": False,
                "error": "NGC_API_KEY is not set on the dashboard host (export before starting the app)",
            }
        ), 400
    arch = str(arch_raw).strip() if arch_raw else ""
    if arch and arch not in ("aarch64", "x86_64", "arm64"):
        return jsonify({"ok": False, "error": "arch must be aarch64, x86_64, or arm64"}), 400
    if arch == "arm64":
        arch = "aarch64"
    script = SCRIPTS_DIR / "05-download-containers.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(60, _env_int("RACK_TOOLS_DOWNLOAD_CONTAINERS_TIMEOUT", 14400))
    cmd: list[str] = [str(script), str(dest_dir)]
    if arch:
        cmd.append(arch)
    display_cmd = f"{script.name} {shlex.quote(str(dest_dir))}" + (f" {shlex.quote(arch)}" if arch else "")
    try:
        with _tools_exclusive_lock("download_containers"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e), "destination": str(dest_dir)}), 409
    except subprocess.TimeoutExpired:
        return jsonify(
            {"ok": False, "error": f"download script timed out after {timeout}s", "destination": str(dest_dir)}
        ), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "destination": str(dest_dir)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "destination": str(dest_dir),
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


_STREAM_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Accel-Buffering": "no",
}


@app.route("/api/tools/download-containers-stream", methods=["POST"])
def api_tools_download_containers_stream():
    """Stream ``05-download-containers.sh`` merged stdout/stderr (same POST JSON as ``/api/tools/download-containers``)."""
    if os.environ.get("RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    dest_raw = body.get("destination")
    arch_raw = body.get("arch")
    try:
        dest_dir = _resolve_tools_destination(str(dest_raw).strip() if dest_raw else "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    found, _checked = _tools_find_existing_sqsh_for_skip(dest_dir)
    if found is not None:
        msg = f"File already exists, skipping download: {found}"

        def _skip_gen() -> Iterator[bytes]:
            yield f"{msg}\n\n--- exit 0 ---\n".encode("utf-8")

        return Response(
            stream_with_context(_skip_gen()),
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )
    if not (os.environ.get("NGC_API_KEY") or "").strip():

        def _ngc_gen() -> Iterator[bytes]:
            yield (
                "ERROR: NGC_API_KEY is not set on the dashboard host (export before starting the app)\n"
            ).encode("utf-8")

        return Response(
            stream_with_context(_ngc_gen()),
            status=400,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )
    arch = str(arch_raw).strip() if arch_raw else ""
    if arch and arch not in ("aarch64", "x86_64", "arm64"):
        return jsonify({"ok": False, "error": "arch must be aarch64, x86_64, or arm64"}), 400
    if arch == "arm64":
        arch = "aarch64"
    script = SCRIPTS_DIR / "05-download-containers.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(60, _env_int("RACK_TOOLS_DOWNLOAD_CONTAINERS_TIMEOUT", 14400))
    cmd: list[str] = [str(script), str(dest_dir)]
    if arch:
        cmd.append(arch)
    display_cmd = f"{script.name} {shlex.quote(str(dest_dir))}" + (f" {shlex.quote(arch)}" if arch else "")
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("download_containers")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode("utf-8")

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _download_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_download_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


@app.route("/api/tools/download-containers-status", methods=["GET"])
def api_tools_download_containers_status():
    """Return whether a non-empty NEMO ``.sqsh`` is already present for *destination* (same rules as download skip)."""
    if os.environ.get("RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED)"}), 403
    dest_q = (request.args.get("destination") or "").strip()
    if not dest_q:
        return jsonify({"ok": True, "exists": False})
    try:
        dest_dir = _tools_destination_dir_for_status_probe(dest_q)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    found, _checked = _tools_find_existing_sqsh_for_skip(dest_dir)
    if found is not None:
        return jsonify({"ok": True, "exists": True, "existing_file": str(found), "destination_dir": str(dest_dir)})
    return jsonify({"ok": True, "exists": False, "destination_dir": str(dest_dir)})


def _workspace_toolchain_status_payload(ws: Path, cuda_major: str) -> dict[str, object]:
    """Probe CUDA toolkit, HPC-X unpack, NCCL binaries, and nvloom in-tree build under *ws*."""
    maj = cuda_major.strip() if cuda_major else "13"
    cuda_nvcc = (ws / "cuda" / "bin" / "nvcc").is_file() or (ws / f"cuda{maj}" / "bin" / "nvcc").is_file()
    if maj == "12":
        hpcx_unpack = "hpcx-v2.22.1-gcc-doca_ofed-ubuntu24.04-cuda12-aarch64"
    else:
        hpcx_unpack = "hpcx-v2.25.1-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64"
    _hpx = ws / hpcx_unpack
    hpcx_init = (_hpx / "hpcx-init.sh").is_file() or (_hpx / "hpcx-mt-init-ompi.sh").is_file()
    nccl_perf = (ws / "nccl" / "bins" / "all_reduce_perf").is_file()
    all_ready = bool(cuda_nvcc and hpcx_init and nccl_perf)
    _nvloom_dir = ws / "nvloom"
    nvloom_cli = (_nvloom_dir / "nvloom_cli").is_file()
    cmake_nvloom_bundle = (ws / "cmake-3.28.1-linux-aarch64" / "bin" / "cmake").is_file()
    return {
        "cuda_nvcc": cuda_nvcc,
        "hpcx_init": hpcx_init,
        "nccl_all_reduce_perf": nccl_perf,
        "all_ready": all_ready,
        "hpcx_unpack": hpcx_unpack,
        "workspace_dir": str(ws),
        "nvloom_cli": nvloom_cli,
        "cmake_nvloom_bundle": cmake_nvloom_bundle,
    }


@app.route("/api/tools/workspace-toolchain-status", methods=["GET"])
def api_tools_workspace_toolchain_status():
    """Return whether CUDA toolkit, HPC-X, NCCL test binary, and nvloom_cli are present under *workspace*."""
    ws_q = (request.args.get("workspace") or "").strip()
    cuda_m = str(request.args.get("cuda_major") or "13").strip()
    if cuda_m not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    if not ws_q:
        return jsonify(
            {
                "ok": True,
                "cuda_nvcc": False,
                "hpcx_init": False,
                "nccl_all_reduce_perf": False,
                "all_ready": False,
                "workspace_dir": "",
                "nvloom_cli": False,
                "cmake_nvloom_bundle": False,
            }
        )
    try:
        ws = _resolve_tools_destination(ws_q)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    payload = _workspace_toolchain_status_payload(ws, cuda_m)
    return jsonify({"ok": True, **payload})


def _write_prometheus_rc_for_launch(user: str, password: str) -> Path:
    """Create a mode-0600 file with ``USER`` / ``PASSWORD`` assignments like ``~/.prometheusrc`` for ``source``."""
    fd, path_s = tempfile.mkstemp(prefix="nvis-prometheusrc-", suffix=".env", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"USER={shlex.quote(user)}\n")
            f.write(f"PASSWORD={shlex.quote(password)}\n")
        p = Path(path_s)
        p.chmod(0o600)
        return p
    except Exception:
        try:
            os.unlink(path_s)
        except OSError:
            pass
        raise


@app.route("/api/tools/launch-grafana-bright-prometheus", methods=["POST"])
def api_tools_launch_grafana_bright_prometheus():
    """Run ``bin/scripts/launch-grafana-bright-prometheus.sh`` on the dashboard host (Docker Grafana + Prometheus DS).

    When **both** ``RACK_PROMETHEUS_BASIC_AUTH_USER`` and ``RACK_PROMETHEUS_BASIC_AUTH_PASSWORD`` are set
    (Settings → External connections), the server writes a temporary ``PROMETHEUS_RC`` file in the same shape
    as ``~/.prometheusrc`` and passes it to the script. If either is empty, ``PROMETHEUS_RC`` is unset so the
    script resolves ``/cm/cmsupport/.prometheusrc`` or ``~/.prometheusrc`` as documented in the script.
    """
    if os.environ.get("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED)"}), 403
    script = SCRIPTS_DIR / "launch-grafana-bright-prometheus.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    user = (_eget("RACK_PROMETHEUS_BASIC_AUTH_USER") or "").strip()
    password = (_eget("RACK_PROMETHEUS_BASIC_AUTH_PASSWORD") or "").strip()
    tmp_rc: Path | None = None
    extra: dict[str, str] = {}
    if user or password:
        if not user or not password:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Set both Prometheus username and password in Settings → External connections, "
                        "or clear both to use ~/.prometheusrc (or other default paths) on the server."
                    ),
                }
            ), 400
        tmp_rc = _write_prometheus_rc_for_launch(user, password)
        extra["PROMETHEUS_RC"] = str(tmp_rc)
    timeout = max(120, _env_int("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_TIMEOUT", 7200))
    cmd = ["bash", str(script)]
    display_cmd = "bash bin/scripts/launch-grafana-bright-prometheus.sh"
    if extra.get("PROMETHEUS_RC"):
        display_cmd += "  (PROMETHEUS_RC from Settings credentials)"
    proc: subprocess.CompletedProcess[str] | None = None
    err_resp: tuple[object, int] | None = None
    try:
        with _tools_exclusive_lock("launch_grafana_bright_prometheus"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(extra),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except _ToolsExclusiveLockBusyError as e:
        err_resp = (jsonify({"ok": False, "error": str(e)}), 409)
    except subprocess.TimeoutExpired:
        err_resp = (jsonify({"ok": False, "error": f"script timed out after {timeout}s"}), 504)
    except Exception as e:
        err_resp = (jsonify({"ok": False, "error": str(e)}), 500)
    finally:
        if tmp_rc is not None:
            try:
                tmp_rc.unlink(missing_ok=True)
            except OSError:
                pass
    if err_resp is not None:
        return err_resp
    assert proc is not None
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@app.route("/api/tools/launch-grafana-bright-prometheus-stream", methods=["POST"])
def api_tools_launch_grafana_bright_prometheus_stream():
    """Stream ``launch-grafana-bright-prometheus.sh`` (same credentials rules as the JSON POST)."""
    if os.environ.get("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED)"}), 403
    script = SCRIPTS_DIR / "launch-grafana-bright-prometheus.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    user = (_eget("RACK_PROMETHEUS_BASIC_AUTH_USER") or "").strip()
    password = (_eget("RACK_PROMETHEUS_BASIC_AUTH_PASSWORD") or "").strip()
    tmp_rc: Path | None = None
    extra: dict[str, str] = {}
    if user or password:
        if not user or not password:

            def _cred_err() -> Iterator[bytes]:
                yield (
                    "ERROR: Set both Prometheus username and password in Settings → External connections, "
                    "or clear both to use ~/.prometheusrc on the server.\n"
                ).encode("utf-8")

            return Response(
                stream_with_context(_cred_err()),
                status=400,
                mimetype="text/plain; charset=utf-8",
                headers=dict(_STREAM_NO_CACHE_HEADERS),
            )
        tmp_rc = _write_prometheus_rc_for_launch(user, password)
        extra["PROMETHEUS_RC"] = str(tmp_rc)
    timeout = max(120, _env_int("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_TIMEOUT", 7200))
    cmd = ["bash", str(script)]
    display_cmd = "bash bin/scripts/launch-grafana-bright-prometheus.sh"
    if extra.get("PROMETHEUS_RC"):
        display_cmd += "  (PROMETHEUS_RC from Settings credentials)"

    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("launch_grafana_bright_prometheus")
    if lock_busy is not None:
        if tmp_rc is not None:
            try:
                tmp_rc.unlink(missing_ok=True)
            except OSError:
                pass

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode("utf-8")

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _grafana_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra if extra else None,
            )
        finally:
            if tmp_rc is not None:
                try:
                    tmp_rc.unlink(missing_ok=True)
                except OSError:
                    pass
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_grafana_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


def _json_body_bool(body: dict, key: str, *, default: bool) -> bool:
    """Parse a boolean from a JSON object (dashboard Tools POST bodies)."""
    if key not in body:
        return default
    v = body[key]
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("1", "true", "yes", "on"):
            return True
        if t in ("0", "false", "no", "off", ""):
            return False
    return default


@app.route("/api/tools/install-hpcx-workspace", methods=["POST"])
def api_tools_install_hpcx_workspace():
    """Run ``install-hpcx-workspace.sh``: Mellanox HPC-X tarball only (CUDA/NCCL: ``install-cuda-nccl-workspace.sh``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    ws_raw = (body.get("workspace") or "").strip() or "/home/cmsupport/workspace"
    try:
        ws = _resolve_tools_destination(ws_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = SCRIPTS_DIR / "install-hpcx-workspace.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_TIMEOUT", 28800))
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        "bash bin/scripts/install-hpcx-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    try:
        with _tools_exclusive_lock("install_hpcx_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-hpcx-workspace timed out after {timeout}s"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@app.route("/api/tools/install-hpcx-workspace-stream", methods=["POST"])
def api_tools_install_hpcx_workspace_stream():
    """Stream ``install-hpcx-workspace.sh`` (POST JSON: ``workspace``, ``cuda_major``, ``arch``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    ws_raw = (body.get("workspace") or "").strip() or "/home/cmsupport/workspace"
    try:
        ws = _resolve_tools_destination(ws_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = SCRIPTS_DIR / "install-hpcx-workspace.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_TIMEOUT", 28800))
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        "bash bin/scripts/install-hpcx-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_hpcx_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode("utf-8")

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _hpcx_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_hpcx_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


_CUDA_NCCL_BUILD_HOST_RE = re.compile(r"^[a-zA-Z0-9](?:[-a-zA-Z0-9.]{0,251})$")


def _validate_cuda_nccl_build_host(raw: object) -> tuple[str | None, str | None]:
    """Return ``(hostname, None)`` or ``(None, error_message)`` for ``build_host`` JSON field."""
    t = str(raw or "").strip()
    if not t:
        return None, "build_host is required (one Slurm compute node; workspace path must exist there on shared home)."
    if len(t) > 253:
        return None, "build_host is too long."
    if not _CUDA_NCCL_BUILD_HOST_RE.match(t):
        return None, "build_host must be a single hostname (letters, digits, dots, hyphens)."
    return t, None


@app.route("/api/tools/install-cuda-nccl-workspace", methods=["POST"])
def api_tools_install_cuda_nccl_workspace():
    """Run ``install-cuda-nccl-workspace.sh``: download on localhost, install on ``build_host`` via pdsh."""
    if os.environ.get("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    install_cuda = _json_body_bool(body, "install_cuda_toolkit", default=False)
    build_nccl = _json_body_bool(body, "build_nccl", default=False)
    if not install_cuda and not build_nccl:
        return jsonify(
            {"ok": False, "error": "Enable install_cuda_toolkit and/or build_nccl (at least one step)."},
        ), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    ws_raw = (body.get("workspace") or "").strip() or "/home/cmsupport/workspace"
    try:
        ws = _resolve_tools_destination(ws_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = SCRIPTS_DIR / "install-cuda-nccl-workspace.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_TIMEOUT", 28800))
    extra_env = {
        "NVIS_INSTALL_CUDA_TOOLKIT": "1" if install_cuda else "0",
        "NVIS_BUILD_NCCL": "1" if build_nccl else "0",
        "NVIS_CUDA_NCCL_BUILD_HOST": build_host,
    }
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        f"NVIS_INSTALL_CUDA_TOOLKIT={extra_env['NVIS_INSTALL_CUDA_TOOLKIT']} "
        f"NVIS_BUILD_NCCL={extra_env['NVIS_BUILD_NCCL']} "
        f"NVIS_CUDA_NCCL_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-cuda-nccl-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    try:
        with _tools_exclusive_lock("install_cuda_nccl_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-cuda-nccl-workspace timed out after {timeout}s"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "install_cuda_toolkit": install_cuda,
            "build_nccl": build_nccl,
            "build_host": build_host,
        }
    ), (200 if ok else 502)


@app.route("/api/tools/install-cuda-nccl-workspace-stream", methods=["POST"])
def api_tools_install_cuda_nccl_workspace_stream():
    """Stream ``install-cuda-nccl-workspace.sh`` (same POST JSON as ``/api/tools/install-cuda-nccl-workspace``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    install_cuda = _json_body_bool(body, "install_cuda_toolkit", default=False)
    build_nccl = _json_body_bool(body, "build_nccl", default=False)
    if not install_cuda and not build_nccl:
        return jsonify(
            {"ok": False, "error": "Enable install_cuda_toolkit and/or build_nccl (at least one step)."},
        ), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    ws_raw = (body.get("workspace") or "").strip() or "/home/cmsupport/workspace"
    try:
        ws = _resolve_tools_destination(ws_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = SCRIPTS_DIR / "install-cuda-nccl-workspace.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_TIMEOUT", 28800))
    extra_env = {
        "NVIS_INSTALL_CUDA_TOOLKIT": "1" if install_cuda else "0",
        "NVIS_BUILD_NCCL": "1" if build_nccl else "0",
        "NVIS_CUDA_NCCL_BUILD_HOST": build_host,
    }
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        f"NVIS_INSTALL_CUDA_TOOLKIT={extra_env['NVIS_INSTALL_CUDA_TOOLKIT']} "
        f"NVIS_BUILD_NCCL={extra_env['NVIS_BUILD_NCCL']} "
        f"NVIS_CUDA_NCCL_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-cuda-nccl-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_cuda_nccl_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode("utf-8")

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _cuda_nccl_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra_env,
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_cuda_nccl_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


@app.route("/api/tools/install-nvloom-workspace", methods=["POST"])
def api_tools_install_nvloom_workspace():
    """Run ``install-nvloom-workspace.sh``: git clone on localhost, build on ``build_host`` via pdsh."""
    if os.environ.get("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    ws_raw = (body.get("workspace") or "").strip() or "/home/cmsupport/workspace"
    try:
        ws = _resolve_tools_destination(ws_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = SCRIPTS_DIR / "install-nvloom-workspace.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_TIMEOUT", 28800))
    extra_env = {"NVIS_NVLOOM_BUILD_HOST": build_host}
    cmd = ["bash", str(script), str(ws), cuda_major]
    display_cmd = (
        f"NVIS_NVLOOM_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-nvloom-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)}"
    )
    try:
        with _tools_exclusive_lock("install_nvloom_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-nvloom-workspace timed out after {timeout}s"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "build_host": build_host,
        }
    ), (200 if ok else 502)


@app.route("/api/tools/install-nvloom-workspace-stream", methods=["POST"])
def api_tools_install_nvloom_workspace_stream():
    """Stream ``install-nvloom-workspace.sh`` (POST JSON: ``workspace``, ``cuda_major``, ``build_host``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    ws_raw = (body.get("workspace") or "").strip() or "/home/cmsupport/workspace"
    try:
        ws = _resolve_tools_destination(ws_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = SCRIPTS_DIR / "install-nvloom-workspace.sh"
    if not script.is_file():
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_TIMEOUT", 28800))
    extra_env = {"NVIS_NVLOOM_BUILD_HOST": build_host}
    cmd = ["bash", str(script), str(ws), cuda_major]
    display_cmd = (
        f"NVIS_NVLOOM_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-nvloom-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)}"
    )
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_nvloom_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode("utf-8")

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _nvloom_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra_env,
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_nvloom_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


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


def _parse_tail_log_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Validate tail-log POST body; prep has log_path, log_dir, display_cmd, rack."""
    if os.environ.get("RACK_TAIL_LOG_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "tail-log disabled (RACK_TAIL_LOG_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    lf_raw = body.get("log_basename") or body.get("log_file")
    lf = str(lf_raw).strip() if lf_raw else ""
    if lf:
        log_path = _resolve_nemo_log_basename_in_dir(log_dir, lf)
        if log_path is None:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"log file not found or basename must contain NEMO/NCCL: {Path(lf).name!r}"
                        ),
                    }
                ),
                404,
            )
        rk = multirack_display_rack_from_basename(log_path.name)
    else:
        rack = (body.get("rack") or "").strip()
        try:
            rk = _normalize_rack_id(rack)
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        job_raw = (body.get("job_id") or "").strip()
        job_id = job_raw if job_raw and job_raw != "-" else None
        log_path = pick_log_for_rack(log_dir, rack, job_id)
        if log_path is None:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"no matching *.txt for rack {rk!r} under {log_dir} or {log_dir / 'failed'} "
                        f"(NEMO/NCCL basename, or ClusterKit root-CK-/clusterkit with one rack id)"
                        ),
                    }
                ),
                404,
            )
    root = log_dir.resolve()
    lp = log_path.resolve()
    try:
        lp.relative_to(root)
    except ValueError:
        return None, (jsonify({"ok": False, "error": "invalid log path"}), 400)
    rel = str(lp.relative_to(root))
    n0 = max(0, _env_int("RACK_TAIL_INITIAL_LINES", 400))
    display_cmd = f"tail -n {n0} -f {shlex.quote(rel)}"
    return (
        {
            "log_dir": log_dir,
            "log_path": lp,
            "rack": rk,
            "rel_path": rel,
            "display_cmd": display_cmd,
        },
        None,
    )


def _tail_log_stream_generator(prep: dict) -> object:
    """Stream bytes from `tail -n N -f logfile` until client disconnects or error."""
    lp = prep["log_path"]
    n_init = max(0, _env_int("RACK_TAIL_INITIAL_LINES", 400))
    cmd = ["tail", "-n", str(n_init), "-f", str(lp)]
    if (_eget("RACK_TAIL_LINEBUFFER", "1") or "").strip().lower() in ("1", "true", "yes", "on"):
        stdbuf = shutil.which("stdbuf")
        if stdbuf:
            cmd = [stdbuf, "-oL", "-eL", *cmd]
    env = _subprocess_env()
    proc = subprocess.Popen(
        cmd,
        cwd=str(prep["log_dir"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=0,
    )
    outq: queue.Queue[bytes | None] = queue.Queue()

    def feeder() -> None:
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                block = stdout.read(8192)
                if not block:
                    break
                outq.put(block)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            outq.put(None)

    threading.Thread(target=feeder, daemon=True).start()
    header = (
        f"# {prep['display_cmd']}\n# cwd: {prep['log_dir']}\n# file: {lp}\n\n"
    )
    yield header.encode("utf-8")
    try:
        while True:
            try:
                chunk = outq.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if chunk is None:
                break
            yield chunk
        rc = proc.poll()
        if rc is not None and rc != 0:
            yield f"\n--- tail exited {rc} ---\n".encode("utf-8")
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass


def _parse_tail_job_stdout_stream_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """POST JSON ``{log_dir, path}`` — resolve Slurm ``StdOut`` path and tail it (same generator as NEMO tail)."""
    if os.environ.get("RACK_TAIL_LOG_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "tail-log disabled (RACK_TAIL_LOG_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    path_raw = body.get("path")
    if not isinstance(path_raw, str) or not path_raw.strip():
        return None, (jsonify({"ok": False, "error": "path is required"}), 400)
    try:
        lp = _resolve_slurm_job_stdout_path(path_raw.strip(), str(log_dir))
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    n0 = max(0, _env_int("RACK_TAIL_INITIAL_LINES", 400))
    display_cmd = f"tail -n {n0} -f {shlex.quote(str(lp))}"
    return (
        {
            "log_dir": log_dir,
            "log_path": lp,
            "rack": "",
            "rel_path": lp.name,
            "display_cmd": display_cmd,
        },
        None,
    )


@app.route("/api/tail-job-stdout-stream", methods=["POST"])
def api_tail_job_stdout_stream():
    """Stream ``tail -f`` for a Slurm job stdout file (``path`` from ``scontrol show job`` ``StdOut=``)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_tail_job_stdout_stream_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    return Response(
        stream_with_context(_tail_log_stream_generator(prep)),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/tail-log-stream", methods=["POST"])
def api_tail_log_stream():
    """Stream live `tail -f` of the NEMO rack log (same file pick as parser)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_tail_log_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None

    return Response(
        stream_with_context(_tail_log_stream_generator(prep)),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/login", methods=["GET"])
def login():
    """Redirect to SPA — the React LoginPage handles authentication."""
    if not _auth_enabled() or _session_ok():
        return redirect("/")
    return _serve_spa_asset("index.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


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
        if re.search(r"slurm\s*[\d.]|^slurm\b", ln, re.I):
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
    if not host_exec.is_remote() and not shutil.which(cmsh):
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
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    cap = max(80, _env_int("RACK_CLUSTER_BCM_CMSH_DISPLAY_MAX", 600))
    snippet = " | ".join(lines[:4]) if lines else out
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
        return _resolve_path(Path(raw))
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


def build_cluster_overview(log_dir: Path) -> dict[str, object]:
    """Aggregate cluster facts for the React dashboard (env + head-only Slurm; no node SSH/pdsh)."""
    base = _cached_rack_status_copy(log_dir)
    cached: dict | None = dict(base) if isinstance(base, dict) else None
    if cached is not None:
        _merge_fresh_comments_into_status_payload(cached)
        if not _truthy_env("RACK_STATUS_USE_SHELL_TABLE"):
            _merge_live_native_workers_into_status(cached, log_dir)
    rows = list(cached.get("rows") or []) if isinstance(cached, dict) else []
    rack_ids = sorted({str(r.get("rack") or "").strip() for r in rows if r.get("rack")})
    rack_count = len(rack_ids)
    summary = dict(cached.get("summary") or {}) if isinstance(cached, dict) else {}
    for k in ("done", "fail", "ongoing", "pending"):
        if k not in summary:
            summary[k] = 0
    phys = dict(cached.get("physical_rf0") or {}) if isinstance(cached, dict) else {}
    tray_hosts = len(phys)
    gpus_per_tray = max(1, _eget_int("RACK_CLUSTER_GPU_PER_TRAY", 8))
    estimated_gpus = tray_hosts * gpus_per_tray
    validation_total = int(summary.get("done", 0) or 0) + int(summary.get("fail", 0) or 0)
    validation_total += int(summary.get("ongoing", 0) or 0) + int(summary.get("pending", 0) or 0)

    cluster_name = _cluster_env_pick("RACK_CLUSTER_NAME", "CLUSTER_NAME")
    if not cluster_name:
        try:
            cluster_name = socket.gethostname()
        except Exception:
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

    probes: dict[str, object] = {}
    skip_probes = _truthy_env("RACK_CLUSTER_OVERVIEW_DISABLE_PROBES")

    if not skip_probes and not slurm_version:
        v, err = _cluster_probe_slurm_version()
        slurm_version = v or "—"
        probes["slurm"] = {"ok": bool(v) and not err, "detail": err or v}

    if not skip_probes and not (bcm_version or "").strip():
        v_b, err_b = _cluster_probe_bcm_cmsh_version()
        bcm_version = v_b or bcm_version
        probes["bcm_cmsh"] = {"ok": bool(v_b) and not err_b, "detail": (err_b or v_b)[:4000]}

    squeue_block = _cluster_squeue_overview_block()

    if hw_vendor == "":
        hw_vendor = "—"
    if bcm_version == "":
        bcm_version = "—"
    if slurm_version == "":
        slurm_version = "—"
    if nemo_container == "":
        nemo_container = "—"

    cuda_nccl_build_host_candidates = sorted(phys.keys(), key=str.casefold)

    return {
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
        "rack_count": rack_count,
        "tray_hosts": tray_hosts,
        "estimated_gpu_count": estimated_gpus,
        "validation_summary": summary,
        "validation_cells_total": validation_total,
        "status_updated_at": (cached or {}).get("updated_at") if isinstance(cached, dict) else None,
        "probes": probes,
        "app_version": _app_version(),
        "squeue": squeue_block,
        "cluster_dashboard_metadata": dash_meta,
    }


@app.route("/api/nvftp-sync/status", methods=["GET"])
def api_nvftp_sync_status():
    """NVFTP var/cache sync status (no secrets)."""
    nvftp_cache_sync.ensure_background_thread()
    return jsonify({"ok": True, **nvftp_cache_sync.sync_status()})


@app.route("/api/nvftp-sync/run", methods=["POST"])
def api_nvftp_sync_run():
    """Trigger an immediate var/cache upload. JSON body ``{\"force\": true}`` re-uploads every file."""
    body = request.get_json(silent=True) or {}
    force = body.get("force") in (True, 1, "1", "true", "yes")
    out = nvftp_cache_sync.run_sync(force=force)
    code = 200 if out.get("ok") else 502
    return jsonify(out), code


@app.route("/api/nvftp-sync/test", methods=["POST"])
def api_nvftp_sync_test():
    """POST ``{project_token, sftp_user?, token?}`` — SFTP probe file under ``{remote_base}/{project_token}/``."""
    body = request.get_json(silent=True) or {}
    project = (body.get("project_token") or body.get("RACK_PROJECT_TOKEN") or "").strip()
    if not project:
        return jsonify({"ok": False, "error": "project_token (RACK_PROJECT_TOKEN) is required"}), 400
    sftp_user = (body.get("sftp_user") or body.get("RACK_NVFTP_SFTP_USER") or "").strip() or None
    token = (body.get("token") or "").strip() or None
    out = nvftp_cache_sync.test_project_directory(
        project_token=project,
        sftp_user=sftp_user,
        token=token,
    )
    code = 200 if out.get("ok") else 502
    return jsonify(out), code


@app.route("/api/vault/nvftp-token", methods=["GET", "PUT", "DELETE"])
def api_vault_nvftp_token():
    """GET status (masked); PUT ``{token}`` to store encrypted; DELETE to clear."""
    if request.method == "GET":
        try:
            st = token_vault.nvftp_token_status()
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 503
        return jsonify({"ok": True, "tokens_url": NVFTP_TOKENS_URL, **st})
    if request.method == "DELETE":
        try:
            token_vault.clear_nvftp_token()
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "configured": False})
    body = request.get_json(silent=True) or {}
    raw = body.get("token")
    if not isinstance(raw, str):
        return jsonify({"ok": False, "error": 'expected JSON {"token": "…"}'}), 400
    try:
        st = token_vault.store_nvftp_token(raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    nvftp_cache_sync.ensure_background_thread()
    return jsonify({"ok": True, "tokens_url": NVFTP_TOKENS_URL, **st})


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
    """Cluster-wide facts for the dashboard tab (env + head Slurm; uses cached /api/status rows)."""
    nvftp_cache_sync.ensure_background_thread()
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    client_id = (request.args.get("client_id") or "").strip()
    connected_users = _touch_app_client(client_id)
    try:
        payload = build_cluster_overview(log_dir)
        payload["connected_users"] = connected_users
        return jsonify(payload)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/app-stats", methods=["GET"])
def api_app_stats():
    """Lightweight app metrics: active users, version, and running async commands."""
    client_id = (request.args.get("client_id") or "").strip()
    connected_users = _touch_app_client(client_id)
    return jsonify(
        {
            "ok": True,
            "connected_users": connected_users,
            "app_version": _app_version(),
            "async_running": _count_running_async_commands(),
            "active_client_ttl_sec": _app_client_ttl_sec(),
        }
    )


def _merge_project_name_into_status_payload(payload: dict) -> None:
    """SPA sidebar / titles: ``RACK_WEB_PROJECT_NAME`` (and shell ``PROJECT_NAME``) via ``_project_name()``."""
    payload["project_name"] = _project_name()


def _merge_naming_into_status_payload(payload: dict) -> None:
    """Expose ``NVIS_*`` naming hints for the SPA (validation targets, node id parsing)."""
    payload["naming"] = _nvis_naming().schema_dict()


@app.route("/api/status")
def api_status():
    _start_cmsh_rack_list_warm_thread()
    _ensure_rack_status_background_thread()
    nvftp_cache_sync.ensure_background_thread()
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    want_raw = request.args.get("raw") == "1"
    interval = _rack_status_background_interval_sec()
    if _rack_status_background_enabled() and not want_raw:
        cache_key = _register_rack_status_log_dir(log_dir)
        with _rack_status_cache_lock:
            cached = _rack_status_cache.get(cache_key)
        if cached is not None:
            out = dict(cached)
            out["served_from_cache"] = True
            out["rack_status_background_interval_sec"] = interval
            _merge_fresh_comments_into_status_payload(out)
            _merge_live_native_workers_into_status(out, log_dir)
            _merge_project_name_into_status_payload(out)
            _merge_naming_into_status_payload(out)
            return jsonify(out)
        try:
            with _rack_status_exec_lock:
                with _rack_status_cache_lock:
                    cached = _rack_status_cache.get(cache_key)
                if cached is not None:
                    out = dict(cached)
                    out["served_from_cache"] = True
                    out["rack_status_background_interval_sec"] = interval
                    _merge_fresh_comments_into_status_payload(out)
                    _merge_live_native_workers_into_status(out, log_dir)
                    _merge_project_name_into_status_payload(out)
                    _merge_naming_into_status_payload(out)
                    return jsonify(out)
                data = run_rack_status(log_dir, include_raw=False)
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "error": "rack_status.sh timed out"}), 504
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        with _rack_status_cache_lock:
            _rack_status_cache[cache_key] = data
        out = dict(data)
        out["served_from_cache"] = False
        out["rack_status_background_interval_sec"] = interval
        _merge_fresh_comments_into_status_payload(out)
        _merge_live_native_workers_into_status(out, log_dir)
        _merge_project_name_into_status_payload(out)
        _merge_naming_into_status_payload(out)
        return jsonify(out)
    try:
        if _rack_status_background_enabled():
            with _rack_status_exec_lock:
                data = run_rack_status(log_dir, include_raw=want_raw)
        else:
            data = run_rack_status(log_dir, include_raw=want_raw)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "rack_status.sh timed out"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    _merge_fresh_comments_into_status_payload(data)
    _merge_live_native_workers_into_status(data, log_dir)
    _merge_project_name_into_status_payload(data)
    _merge_naming_into_status_payload(data)
    return jsonify(data)


_SPA_DIR = REPO_ROOT / "static" / "spa"




def _serve_spa_asset(path: str = "index.html"):
    """Serve a built SPA asset, fallback to index.html for client routes."""
    spa_file = _SPA_DIR / path
    if spa_file.is_file():
        return send_file(spa_file)
    return send_file(_SPA_DIR / "index.html")


@app.route("/api/auth-check")
def api_auth_check():
    """Check if the current session is authenticated. Returns 401 if not."""
    if not _auth_enabled():
        return jsonify({"authenticated": True, "user": _auth_user(), "auth_disabled": True})
    if _session_ok():
        return jsonify({"authenticated": True, "user": _auth_user()})
    return jsonify({"authenticated": False}), 401


@app.route("/api/login", methods=["POST"])
def api_login():
    """JSON login endpoint for the React SPA. Accepts {"password": "..."}."""
    if not _auth_enabled():
        return jsonify({"ok": True, "user": _auth_user(), "auth_disabled": True})
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password") or "")
    au = _auth_user()
    psvc = _pam_service()
    try:
        ok = _pam_verify(au, pw, psvc)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": f"PAM error: {e}"}), 500
    if ok:
        session.clear()
        session.permanent = True
        session["rack_web_auth"] = "1"
        return jsonify({"ok": True, "user": au})
    return jsonify({"ok": False, "error": "Invalid password"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """JSON logout endpoint for the React SPA."""
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Rack locks (advisory multi-operator coordination). The SPA polls this snapshot
# every 5 s via ``useRackLocks`` and shows a confirmation dialog before POSTing
# operations against a rack held by another operator. Server is never blocking.
# ---------------------------------------------------------------------------


@app.route("/api/rack-locks", methods=["GET"])
def api_rack_locks_get():
    """Snapshot of in-flight operations + explicit claims, grouped by rack."""
    snapshot = rack_locks.build_lock_snapshot(
        _operator_from_request(),
        _iter_active_jobs_for_locks(),
        _active_clients_snapshot(),
    )
    return jsonify(snapshot)


@app.route("/api/rack-locks/<rack>", methods=["POST"])
def api_rack_locks_claim(rack: str):
    """Create or refresh a manual claim. Requires ``X-Rack-Operator`` header (advisory)."""
    operator = _operator_from_request()
    if not operator:
        return jsonify(
            {"ok": False, "error": "X-Rack-Operator header is required to claim a rack"}
        ), 400
    body = request.get_json(silent=True) or {}
    note = str(body.get("note") or "") if isinstance(body.get("note"), str) else ""
    ttl_raw = body.get("ttl_sec")
    ttl_sec = None
    if isinstance(ttl_raw, (int, float)) and not isinstance(ttl_raw, bool):
        try:
            ttl_sec = int(ttl_raw)
        except (TypeError, ValueError):
            ttl_sec = None
    client_id = _client_id_from_request()
    force = bool(body.get("force")) if isinstance(body, dict) else False
    try:
        claim = rack_locks.upsert_claim(
            rack,
            operator,
            client_id=client_id or None,
            note=note,
            ttl_sec=ttl_sec,
            force=force,
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True, "claim": claim})


@app.route("/api/rack-locks/<rack>", methods=["DELETE"])
def api_rack_locks_release(rack: str):
    """Release a manual claim. Only the recorded owner may release a live claim; expired
    entries can be cleared by anyone."""
    operator = _operator_from_request()
    try:
        removed = rack_locks.release_claim(rack, operator)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    return jsonify({"ok": True, "removed": bool(removed)})



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

_WORKFLOW_TOOL_KEYS = {
    "RACK_STATUS_SCRIPT": ("rack_status.sh", "Native Status"),
    "RACK_PARSER_SCRIPT": ("parser.sh", "Native Status"),
    "RACK_VALIDATION_SCRIPT": ("Singlerack validation", "Validation"),
    "RACK_NEMO_XRACK_SCRIPT": ("Multinode NEMO", "Validation"),
    "RACK_LLAMA_SCRIPT": ("Llama run", "Validation"),
    "RACK_MNUBERGEMM_STRESS_SCRIPT": ("MNU Bergemm stress", "Validation"),
    "RACK_NVLOOM_SINGLERACK_SCRIPT": ("NVLoom singlerack", "Validation"),
    "RACK_CLUSTERKIT_SCRIPT": ("ClusterKit singlerack", "Validation"),
    "RACK_SYSINFO_SCRIPT": ("sysinfo / Precheck", "Cluster Tools"),
    "RACK_HW_CHECK_SCRIPT": ("Hardware check (pdsh)", "Cluster Tools"),
    "RACK_GPU_RESET_SCRIPT": ("GPU reset", "Cluster Tools"),
    "RACK_NVSWITCH_AUDIT_SCRIPT": ("NVSwitch audit", "Cluster Tools"),
    "RACK_NVSWITCH_SDN_RESET_SCRIPT": ("NVSwitch SDN reset", "Cluster Tools"),
    "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT": ("NVSwitch system reset", "Cluster Tools"),
    "RACK_IMEX_RESET_SCRIPT": ("IMEX reset", "Cluster Tools"),
    "RACK_IMEX_CHECK_SCRIPT": ("IMEX check", "Cluster Tools"),
}


@app.route("/api/tools/available", methods=["GET"])
def api_tools_available():
    """Return predefined scripts/tools available for workflow automation steps."""
    tools = []
    for env_key, (label, category) in _WORKFLOW_TOOL_KEYS.items():
        desc = ""
        for defn in JOB_ENV_UI_DEFINITIONS:
            if defn.get("key") == env_key:
                desc = defn.get("description", "")
                break
        tools.append({
            "id": env_key,
            "label": label,
            "description": desc,
            "category": category,
            "path": os.environ.get(env_key, ""),
        })
    return jsonify({"ok": True, "tools": tools})


# ---------------------------------------------------------------------------
# AI Chat routes
# ---------------------------------------------------------------------------


@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    """POST SSE streaming chat. Body: {messages: [{role, content}], context: {page, rack?}}"""
    if not ai_chat.is_ai_enabled() and not _truthy_env("RACK_STATUS_DEMO"):
        return jsonify({"ok": False, "error": "AI is not configured. Set an API key in Settings."}), 503

    body = request.get_json(silent=True) or {}
    messages = body.get("messages")
    if not messages or not isinstance(messages, list) or len(messages) == 0:
        return jsonify({"ok": False, "error": "messages array is required"}), 400

    context = body.get("context") or {}
    settings = ai_chat.get_ai_settings()
    system_prompt = ai_chat.build_system_prompt(context, settings.get("system_rules", ""))

    full_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        if isinstance(m, dict) and m.get("role") and m.get("content"):
            full_messages.append({"role": m["role"], "content": m["content"]})

    def generate():
        try:
            use_demo = _truthy_env("RACK_STATUS_DEMO") and not ai_chat.is_ai_enabled()
            if use_demo:
                stream = ai_chat.demo_stream_response(full_messages, context)
            else:
                stream = ai_chat.chat_completion_stream(full_messages)
            for chunk in stream:
                yield f"data: {json.dumps({'content': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            _logger.exception("AI chat stream error")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/ai/settings", methods=["GET", "PUT"])
def api_ai_settings():
    """GET returns masked settings; PUT updates endpoint/model/key."""
    if request.method == "GET":
        return jsonify({"ok": True, **ai_chat.get_ai_settings()})

    body = request.get_json(silent=True) or {}
    updated = ai_chat.update_ai_settings(body)
    return jsonify({"ok": True, **updated})


@app.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    """Test the AI endpoint connectivity."""
    result = ai_chat.test_connection()
    status = 200 if result.get("ok") else 502
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Help / knowledge content routes
# ---------------------------------------------------------------------------


@app.route("/api/help/content", methods=["GET"])
def api_help_content():
    """Return the full app knowledge registry (mirror of the frontend content)."""
    entries = help_content.get_help_content()
    return jsonify({"ok": True, "count": len(entries), "entries": entries})


@app.route("/api/help/content/<content_id>", methods=["GET"])
def api_help_content_entry(content_id: str):
    """Return a single help entry by id."""
    entry = help_content.get_entry(content_id)
    if not entry:
        return jsonify({"ok": False, "error": "Help content not found"}), 404
    return jsonify({"ok": True, "entry": entry})


# ---------------------------------------------------------------------------
# Workflow routes
# ---------------------------------------------------------------------------


@app.route("/api/workflows", methods=["GET"])
def api_workflows_list():
    """List available workflow templates."""
    return jsonify({"ok": True, "workflows": workflow_engine.list_workflows()})


@app.route("/api/workflows/<workflow_id>", methods=["GET"])
def api_workflow_get(workflow_id: str):
    """Get a specific workflow template."""
    wf = workflow_engine.get_workflow(workflow_id)
    if not wf:
        return jsonify({"ok": False, "error": "Workflow not found"}), 404
    return jsonify({"ok": True, "workflow": wf})


@app.route("/api/workflows", methods=["POST"])
def api_workflow_save():
    """Create or update a workflow template."""
    body = request.get_json(silent=True) or {}
    if not body.get("name"):
        return jsonify({"ok": False, "error": "name is required"}), 400
    try:
        wf = workflow_engine.save_workflow(body)
        return jsonify({"ok": True, "workflow": wf})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/workflows/<workflow_id>", methods=["DELETE"])
def api_workflow_delete(workflow_id: str):
    """Delete a workflow template."""
    deleted = workflow_engine.delete_workflow(workflow_id)
    if not deleted:
        return jsonify({"ok": False, "error": "Workflow not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/workflows/<workflow_id>/run", methods=["POST"])
def api_workflow_run(workflow_id: str):
    """Start a workflow execution instance."""
    body = request.get_json(silent=True) or {}
    params = body.get("params")
    try:
        result = workflow_engine.start_workflow_run(workflow_id, params)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/workflow-runs", methods=["GET"])
def api_workflow_runs_list():
    """List active and recent workflow runs."""
    return jsonify({"ok": True, "runs": workflow_engine.list_runs()})


@app.route("/api/workflow-runs/<run_id>", methods=["GET"])
def api_workflow_run_state(run_id: str):
    """Get current state of a workflow run."""
    run = workflow_engine.get_run_state(run_id)
    if not run:
        return jsonify({"ok": False, "error": "Run not found"}), 404
    return jsonify({"ok": True, "run": run})


@app.route("/api/workflow-runs/<run_id>/advance", methods=["POST"])
def api_workflow_run_advance(run_id: str):
    """Advance a workflow (for manual/decision steps)."""
    body = request.get_json(silent=True) or {}
    try:
        result = workflow_engine.advance_run(run_id, body)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/workflow-runs/<run_id>/cancel", methods=["POST"])
def api_workflow_run_cancel(run_id: str):
    """Cancel a running workflow."""
    try:
        result = workflow_engine.cancel_run(run_id)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/workflow-runs/<run_id>/output/<step_id>", methods=["GET"])
def api_workflow_run_output(run_id: str, step_id: str):
    """Get the full output log for a specific step in a workflow run."""
    output = workflow_engine.get_step_output(run_id, step_id)
    if output is None:
        return jsonify({"ok": False, "error": "Output not found"}), 404
    return jsonify({"ok": True, "output": output, "step_id": step_id, "run_id": run_id})


@app.route("/api/workflows/generate-mermaid", methods=["POST"])
def api_workflow_generate_mermaid():
    """Generate mermaid diagram from workflow steps."""
    body = request.get_json(silent=True) or {}
    steps = body.get("steps", [])
    if not steps:
        return jsonify({"ok": False, "error": "steps array is required"}), 400
    mermaid = workflow_engine.generate_mermaid_from_steps(steps)
    return jsonify({"ok": True, "mermaid": mermaid})


# ---------------------------------------------------------------------------
# WebSocket terminal — interactive SSH to compute nodes
# ---------------------------------------------------------------------------

_TERMINAL_ENABLED = os.environ.get("RACK_ENABLE_WEB_TERMINAL", "1").strip().lower() in (
    "1", "true", "yes", "on",
)

class _TerminalSession:
    """Holds state for a single PTY session that can outlive a WebSocket."""

    def __init__(self, session_id: str, master_fd: int, proc: subprocess.Popen,
                 target: str, mode: str):
        self.session_id = session_id
        self.master_fd = master_fd
        self.proc = proc
        self.target = target
        self.mode = mode
        self.alive = True
        self.ws_count = 0
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue[bytes | None]] = []
        self._start_reader()

    def _start_reader(self) -> None:
        def _pty_reader():
            try:
                while self.alive:
                    rlist, _, _ = select.select([self.master_fd], [], [], 0.5)
                    if rlist:
                        try:
                            data = os.read(self.master_fd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        with self.lock:
                            for q in self.subscribers:
                                q.put(data)
                    if self.proc.poll() is not None:
                        time.sleep(0.2)
                        try:
                            remaining = os.read(self.master_fd, 4096)
                            if remaining:
                                with self.lock:
                                    for q in self.subscribers:
                                        q.put(remaining)
                        except OSError:
                            pass
                        msg = f"\r\n\x1b[31m[Process exited with code {self.proc.returncode}]\x1b[0m\r\n".encode()
                        with self.lock:
                            for q in self.subscribers:
                                q.put(msg)
                        break
            except Exception as exc:
                msg = f"\r\n\x1b[31m[PTY error: {exc}]\x1b[0m\r\n".encode()
                with self.lock:
                    for q in self.subscribers:
                        q.put(msg)
            finally:
                self.alive = False
                with self.lock:
                    for q in self.subscribers:
                        q.put(None)

        threading.Thread(target=_pty_reader, daemon=True).start()

    def subscribe(self) -> "queue.Queue[bytes | None]":
        q: queue.Queue[bytes | None] = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
            self.ws_count += 1
        return q

    def unsubscribe(self, q: "queue.Queue[bytes | None]") -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
            self.ws_count -= 1

    def write(self, data: bytes) -> None:
        if self.alive:
            os.write(self.master_fd, data)

    def resize(self, cols: int, rows: int) -> None:
        if self.alive:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            os.kill(self.proc.pid, signal.SIGWINCH)

    def force_redraw(self) -> None:
        """Send SIGWINCH to the process group to trigger a full screen redraw."""
        if self.alive:
            try:
                os.kill(self.proc.pid, signal.SIGWINCH)
            except (ProcessLookupError, OSError):
                pass

    def destroy(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        _logger.info("WebSocket terminal: session %s (%s) destroyed", self.session_id, self.target)


_TERMINAL_GRACE_PERIOD = 30  # seconds to keep PTY alive with no WebSocket


class _WebSocketTerminalMiddleware:
    """WSGI middleware that handles /ws/terminal at the WSGI layer.

    This bypasses Flask's response handling entirely, which prevents
    Werkzeug from writing HTTP headers into the WebSocket stream.

    Sessions are tracked by UUID so a WebSocket can disconnect and a new
    one (e.g. from a pop-out tab) can reattach to the same PTY.
    """

    def __init__(self, wsgi_app):  # type: ignore[no-untyped-def]
        self.wsgi_app = wsgi_app
        self._sessions: dict[str, _TerminalSession] = {}
        self._sessions_lock = threading.Lock()

    def __call__(self, environ, start_response):  # type: ignore[no-untyped-def]
        path = environ.get("PATH_INFO", "")
        if path == "/ws/terminal" and "werkzeug.socket" in environ and _TERMINAL_ENABLED:
            self._handle_terminal(environ)
            return []
        return self.wsgi_app(environ, start_response)

    def _cleanup_session(self, session_id: str) -> None:
        """Destroy a session after the grace period if no clients reconnected."""
        import time
        time.sleep(_TERMINAL_GRACE_PERIOD)
        with self._sessions_lock:
            sess = self._sessions.get(session_id)
            if sess and sess.ws_count <= 0:
                sess.destroy()
                del self._sessions[session_id]
                _logger.info("WebSocket terminal: session %s expired after grace period", session_id)

    def _get_or_create_session(self, qs: dict) -> "_TerminalSession | None":
        """Return an existing session for reconnect, or create a new one."""
        session_id = (qs.get("session") or [""])[0].strip()
        if session_id:
            with self._sessions_lock:
                sess = self._sessions.get(session_id)
            if sess and sess.alive:
                return sess
            return None

        # New session
        target = (qs.get("target") or qs.get("node") or [""])[0].strip()
        mode = (qs.get("mode") or ["node"])[0].strip()
        if not target and mode != "headnode":
            return None

        if _truthy_env("RACK_STATUS_DEMO"):
            _logger.info("WebSocket terminal: blocked in demo mode (target=%s)", target or "headnode")
            return None

        if mode == "headnode":
            argv = host_exec.ssh_to_headnode_argv()
            target = target or "headnode"
        else:
            argv = host_exec.ssh_to_node_argv(target)

        _logger.info("WebSocket terminal: new session for %s via %s", target, " ".join(argv))

        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=env,
        )
        os.close(slave_fd)

        new_id = str(uuid.uuid4())
        sess = _TerminalSession(new_id, master_fd, proc, target, mode)
        with self._sessions_lock:
            self._sessions[new_id] = sess
        return sess

    def _handle_terminal(self, environ):  # type: ignore[no-untyped-def]
        from simple_websocket import Server as _WSServer, ConnectionClosed as _WSClosed
        from urllib.parse import parse_qs

        ws = _WSServer(environ)
        outq = None
        sess = None
        try:
            qs = parse_qs(environ.get("QUERY_STRING", ""))
            sess = self._get_or_create_session(qs)

            if sess is None:
                reason = "Demo mode — terminal connections are disabled" if _truthy_env("RACK_STATUS_DEMO") else "Missing target node"
                ws.send(json.dumps({"type": "error", "error": reason}))
                return

            # Send session ID so the frontend can use it for pop-out
            is_reconnect = bool((qs.get("session") or [""])[0].strip())
            ws.send(json.dumps({
                "type": "session",
                "id": sess.session_id,
                "target": sess.target,
                "mode": sess.mode,
                "reconnect": is_reconnect,
            }))

            outq = sess.subscribe()
            pending_redraw = is_reconnect

            while True:
                # Drain PTY output -> WS
                while True:
                    try:
                        chunk = outq.get_nowait()
                    except queue.Empty:
                        break
                    if chunk is None:
                        _logger.info("WebSocket terminal: PTY EOF for %s", sess.target)
                        return
                    ws.send(chunk)

                # Read WS input -> PTY (with short timeout)
                try:
                    msg = ws.receive(timeout=0.05)
                except _WSClosed:
                    _logger.info("WebSocket terminal: client disconnected from %s (session %s)",
                                 sess.target, sess.session_id)
                    return
                except OSError:
                    return
                except Exception as exc:
                    _logger.warning("WebSocket terminal: receive error: %s", exc)
                    continue

                if msg is None:
                    continue

                if isinstance(msg, str):
                    try:
                        ctrl = json.loads(msg)
                        if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                            sess.resize(int(ctrl.get("cols", 80)), int(ctrl.get("rows", 24)))
                            if pending_redraw:
                                pending_redraw = False
                                time.sleep(0.1)
                                sess.force_redraw()
                            continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                    sess.write(msg.encode("utf-8"))
                elif isinstance(msg, bytes):
                    sess.write(msg)

        except _WSClosed:
            pass
        except Exception:
            _logger.exception("ws_terminal: unhandled error")
        finally:
            if sess and outq:
                sess.unsubscribe(outq)
                if sess.ws_count <= 0 and sess.alive:
                    threading.Thread(
                        target=self._cleanup_session,
                        args=(sess.session_id,),
                        daemon=True,
                    ).start()
            try:
                ws.close()
            except Exception:
                pass


if _TERMINAL_ENABLED:
    app.wsgi_app = _WebSocketTerminalMiddleware(app.wsgi_app)  # type: ignore[assignment]

# After all routes and middleware are registered (avoid circular import via nvftp_cache_sync._eget → app).
_bootstrap_catalog_at_startup()
_bootstrap_nvftp_sync_at_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
