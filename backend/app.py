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
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import uuid
import warnings
from pathlib import Path

from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    request,
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

# Logging is configured before any other first-party import, because every one of them obtains a
# module logger at import time and until this call the root logger has no handler at all — Python
# falls back to ``logging.lastResort`` at WARNING, so INFO and DEBUG go nowhere. Configuring here
# rather than inside an app factory means an import-time warning from a module below is captured
# too. Idempotent and non-fatal by contract (FR-1, NFR-1).
import logging_config

logging_config.configure()

import native_status
import nvis_id_templates

# --- optional backend modules (added 2026-07-28) -------------------------------------
# Each is bind-mounted read-only from the repo (docker-compose.override.yml). A missing
# mount, a syntax error or an unavailable stdlib feature MUST degrade to the previous
# behaviour, never stop the app booting mid cluster bring-up — hence the soft import and
# the ``is None`` guard at every call site (``results_history``, ``_uparse``, ``_bringup``,
# ``_node_report``).
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
import core
import host_exec
import nvftp_cache_sync
import validation_activities

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

# Identity. Imported plainly, NOT behind a try/except like the catalog above: `core-designs.mdc`
# §0 says a module that cannot import must take startup down and name itself, because a swallowed
# ImportError means the feature is simply absent with no error anywhere. For authentication that is
# the worst possible failure shape — a deployment that looks healthy and cannot authenticate anyone,
# or worse, one that skipped the bootstrap and has no administrator. `srp` and `PyJWT` are declared
# runtime dependencies, so an ImportError here is a broken install and should read as one.
# ---- Host paths, naming and per-request identity now live in their own modules ------------
# Imported under the existing private names, so the call sites that moved to ``backend/api/``
# did not have to change with them.
# Request-scoped helpers need Flask, so they live with the API package, not in core.
import authz
from api._common import (
    operator_from_request as _operator_from_request,
)
from api._common import (
    serve_spa_asset as _serve_spa_asset,
)
from identity import bootstrap as identity_bootstrap
from identity import keys as identity_keys
from identity import oidc as identity_oidc
from identity import revocation as identity_revocation
from identity import store as identity_store
from rack_status_engine import (
    _ai_live_inventory_block,
    _rack_status_cache_clear_all,
)

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

_rack_status_exec_lock = threading.Lock()


# ---- Configuration resolution lives in config_env.py -------------------------------------
# Imported under the existing private names so the call sites did not change when the ladder
# moved out — most of them now live in ``backend/api/`` rather than here. The writer
# (_job_env_overrides_write) stays in this file: it invalidates the rack-status cache, which
# config_env has no business knowing about.
import config_env
from config_env import (
    _STATUS_ROW_ENV_KEYS,
    _eget,
    _job_env_overrides_load,
)

# _JOB_ENV_BOOTSTRAP_DONE stays here: only the catalog bootstrap below touches it, and a `global`
# statement cannot rebind another module's name.
_JOB_ENV_BOOTSTRAP_DONE = False


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
# re-exported under a new name so the call sites here stay untouched.
_truthy_env = core.truthy_env
_SLURM_STDOUT_RE = core.SLURM_STDOUT_RE
_slurm_job_base_id = core.slurm_job_base_id
import tls_server
import web_auth
from activity_hydration import (
    _hydrate_activity_task_logs,
    _task_needs_slurm_hydration,
)
from security_headers import grafana_frame_origin, security_headers
from web_auth import _auth_enabled, _safe_next, _session_lifetime

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


def _tls_cert_configured() -> str:
    """The configured TLS certificate path, or "" when this process serves plain HTTP.

    Read straight from the environment rather than through the config ladder: it is consumed
    before the app finishes booting (and by the entrypoint, outside Python entirely), and like
    the origin allowlist it is a deploy-time property that no operator should be able to change
    from the Settings page.
    """
    return (os.environ.get("RACK_WEB_TLS_CERT") or "").strip()


def _configure_session(app: Flask) -> None:
    _bootstrap_rack_web_secret_key()
    key = (os.environ.get("RACK_WEB_SECRET_KEY") or "").strip()
    app.secret_key = key or secrets.token_hex(32)
    app.permanent_session_lifetime = _session_lifetime()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Secure cookies follow TLS automatically: a deployment that configured a certificate
    # should not also have to remember a second flag, and forgetting it is silent. The explicit
    # env still forces it on for a deployment where TLS terminates in a proxy in front of us,
    # which this process cannot see.
    app.config["SESSION_COOKIE_SECURE"] = (
        _truthy_env("RACK_WEB_COOKIE_SECURE") or bool(_tls_cert_configured())
    )


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


def _bootstrap_identity_at_startup() -> None:
    """Register the revocation checker, then mint an administrator if no user exists.

    **Order matters and the first half is not optional.** ``tokens.verify`` refuses *every* token
    until a revocation checker is installed (FR-6a) — deliberately, because the alternative default
    is "nothing is revoked", and a control that fails open is not a control. The consequence is that
    an unwired checker does not degrade the product, it stops it: every authenticated request is
    refused with a message about the checker. Phase 1 supplied this exact precedent with the config
    materialise hook, which needed a test purely to assert the wire existed.

    Minting stays available before the checker is registered, so the bootstrap below can run.

    Failures are logged rather than fatal **only while the gate still authenticates by the legacy
    shared token**. The moment FR-18 lands and the gate reads token verdicts, a deployment that
    reaches this point without an administrator is a deployment nobody can log into, and this should
    take startup down instead of logging — the whole point of FR-10b is that the release which
    removes the old credential mints the new one in the same start.
    """
    try:
        identity_revocation.install()
    except Exception:
        _logger.exception(
            "identity: revocation checker not installed; every token will be refused (FR-6a)"
        )
    # Authorization engine. Permissive for now and explicitly so: `authz.authorize` refuses when
    # nothing is registered, which keeps "the deployment chose a permissive policy" distinguishable
    # from "nobody wired an engine". Phase 3 changes this one line.
    try:
        authz.install_default()
    except Exception:
        _logger.exception("authz: default engine not installed; every request will be refused")
    # Signing key, before the administrator (Phase 2.5, FR-2). Order matters: the bootstrap below
    # prints a one-time password and the operator's first act is to sign in, which mints a token —
    # so the key has to exist before the first credential does.
    #
    # It is also what stops `/.well-known/jwks.json` publishing `{"keys": []}`. Minting lazily on
    # first sign-in would leave a window where the provider is enabled and advertises no keys, and a
    # client would cache that empty document for the full hour we ask it to. The read path cannot
    # fix this itself: minting is a write, and a read that writes defeats the cache above it.
    if identity_oidc.enabled():
        try:
            key = identity_keys.key_set().ensure()
            _logger.info("identity: signing key %s active (OIDC provider enabled)", key.kid)
        except Exception:
            # Fatal in effect rather than by exception: with the provider on and no key, nothing can
            # be signed. Logged loudly rather than raised so the rest of the bootstrap still runs and
            # the operator gets a diagnosable process instead of a crash loop.
            _logger.exception(
                "identity: no signing key could be established while %s is on; "
                "token minting will fail until this is resolved",
                identity_oidc.FLAG_ENV,
            )

    try:
        store = identity_store.IdentityStore()
    except Exception:
        _logger.exception("identity: store unavailable; no administrator could be bootstrapped")
        return
    try:
        # `ensure_admin` is idempotent on a populated store and emits nothing there, so this is a
        # no-op on every start after the first (AC-19).
        identity_bootstrap.ensure_admin(store)
    except Exception:
        _logger.exception("identity: administrator bootstrap failed")
    finally:
        store.close()


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
#: Re-exported so the historical name keeps working; the list itself lives in `web_auth`, which is
#: also where `openapi.py` reads it from — see the note there.
_AUTH_BYPASS_ENDPOINTS = web_auth.AUTH_BYPASS_ENDPOINTS


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

    # Authentication: a signed access token, from the HttpOnly cookie or an Authorization header.
    # The principal is stashed on `g` so the audit chokepoint and the authorizer read the SAME
    # verified subject this gate accepted — one verification per request, and no second opinion.
    #
    # A plain string, empty when there is no credential — deliberately not a `(principal, method)`
    # tuple. A tuple here is a trap: `(None, "none")` is a non-empty tuple and therefore truthy, so
    # `if principal:` would admit every anonymous request, and no existing test would catch it
    # because the suite runs with the gate disabled.
    principal = web_auth.authenticated_principal(request.headers, request.cookies)
    if principal:
        g.auth_principal = principal
        g.auth_method = "token"

        # Authorization. Permissive today (`allow-all`), but the seam is real: the verdict and the
        # engine that produced it land in the audit record, so a reader can tell how much the
        # `allow` is worth. Phase 3 swaps the engine without touching this call.
        act = _audit_actions.resolve(request.endpoint, request.method)
        if act is not None:
            decision = authz.authorize(principal, act.action, request.path)
            g.authz_decision = decision
            if not decision.allowed:
                # 403, not 401: the caller IS authenticated and simply may not do this. Returning
                # 401 would tell a client to re-authenticate, which cannot help and produces a
                # credential-prompt loop. See docs/API_RESPONSE_CODES.md.
                return jsonify({
                    "ok": False,
                    "error": "not authorized",
                    "reason": "policy",
                }), 403
        return None

    if request.path.startswith("/api/") or request.path.startswith("/ws/"):
        # `WWW-Authenticate` is mandatory on a 401 (RFC 9110 §11.6.1) and was missing. The body stays
        # uniform: it must not say *which* credential problem applied. See
        # docs/API_RESPONSE_CODES.md.
        resp = jsonify({"ok": False, "error": "authentication required", "login": "/login"})
        resp.headers["WWW-Authenticate"] = web_auth.www_authenticate(
            web_auth.credential_presented(request.headers, request.cookies)
        )
        return resp, 401
    nxt = _safe_next(request.full_path)
    if nxt:
        return redirect(url_for("auth.login", next=nxt))
    return redirect(url_for("auth.login"))


@app.before_request
def _enforce_request_throttle():
    """Shed before the view function. Registered immediately **after** the auth gate, deliberately.

    The tempting placement is first, so a refused request costs nothing. That is wrong here, and
    the reason is worth keeping: `g.auth_principal` is set *by* the gate, so a hook running ahead
    of it sees no principal and falls back to the address for **every** request — which, through an
    SSH tunnel where the whole team shares `127.0.0.1`, is one bucket for everybody. One operator's
    bulk operation would throttle their colleagues. Measured, not reasoned: the first version of
    this ran first and did exactly that.

    Running second costs one token verification per refused request — 0.055 ms for ES256, measured
    — and still sheds before the view function, the cluster queries and every fan-out. For the
    unauthenticated routes that matter most (`srp/begin`, ~52 ms of CPU) the gate bypasses
    verification entirely, so nothing extra is paid there either.

    See `backend/throttle.py` for why the key differs by category.
    """
    endpoint = request.endpoint or ""
    # Static and SPA serving are exempt, and this is a real constraint rather than an optimisation:
    # TC-29 pins that an asset request does *no* work, and resolving the client address reads
    # `RACK_WEB_TRUSTED_PROXIES`. Throttling file serving is also the wrong layer -- a proxy or the
    # OS does that better, and a page load legitimately fetches dozens of hashed chunks at once.
    # All three SPA endpoints, verified by asking the router rather than guessing: `/assets/...`
    # and `/index.html` resolve to `serve_root_spa`, `/spa/...` to `serve_spa`, and `/` to `index`.
    # Exempting only one of them left asset requests reading a setting, which TC-29 caught.
    if not endpoint or endpoint in ("static", "serve_spa", "serve_root_spa", "index"):
        return None

    principal = getattr(g, "auth_principal", "") or ""
    if principal:
        key = f"principal:{principal}"
    else:
        # The same resolver the audit chokepoint uses, so a forwarded header is believed only
        # from a configured trusted proxy — otherwise a header could mint a fresh allowance per
        # request and the limit would be decorative.
        addr, _trust = _api_common.client_addr()
        key = f"addr:{addr or 'unknown'}"

    allowed, retry_after = throttle.registry().limiter_for(endpoint).check(key)
    if allowed:
        return None

    _logger.warning("throttle: refused %s for %s (retry in %.1fs)", endpoint, key, retry_after)
    response = jsonify({
        "error": "rate limited",
        "detail": "too many requests; retry shortly",
        "retry_after_seconds": int(retry_after) + 1,
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(int(retry_after) + 1)
    return response


def _wsgi_environ_authenticated(environ) -> bool:
    """The gate above, for a request Flask never routes.

    ``/ws/terminal`` is served by WSGI middleware wrapping ``app.wsgi_app``, so
    ``_require_dashboard_auth`` cannot see it. The request context is opened to get Werkzeug's
    header and cookie parsing rather than re-deriving it from raw ``environ`` keys -- the terminal
    is the product's highest-privilege surface and is the last place to want a second, subtly
    different parser.

    Same token check as the Flask gate, deliberately: two implementations of "is this caller
    authenticated" is how one of them ends up more permissive than the other, which is exactly the
    defect that left this endpoint open to anonymous upgrades in the first place.
    """
    with app.request_context(environ):
        return bool(web_auth.authenticated_principal(request.headers, request.cookies))


web_auth.set_wsgi_authenticator(_wsgi_environ_authenticated)


# The audit chokepoint is installed HERE, deliberately, and not at the end of the file. Flask runs
# ``after_request`` handlers in reverse registration order, so registering earlier means running
# later — which is what lets the audit hook see the response AFTER the security-headers and
# no-store hooks below have decorated it. Moving this call under those decorators would silently
# invert that, and the trail would record a response that had not finished being built.
#
# It does NOT run last, and an earlier version of this comment claimed it did. ``Compress(app)``
# is registered ~490 lines above, so flask_compress's own handler runs after this one and rewrites
# the body encoding, ``Content-Encoding``, ``Content-Length`` and ``Vary``. Nothing the audit event
# records is affected by that (status, principal, action, resource), but the latency it measures
# excludes compression, and anything added here that needs to observe the truly final response
# would be wrong. The accurate invariant is: last among the application's own handlers.
import audit as _audit
import throttle
from api import _common as _api_common
from audit import request_hook as _audit_request_hook

_audit.configure()
_audit_request_hook.install(app)


@app.after_request
def _api_json_no_store(response):
    """Avoid stale dashboard JSON behind browsers or reverse proxies."""
    if request.path.startswith("/api/") and response.content_type and "application/json" in response.content_type:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.after_request
def _security_headers(response):
    """CSP, HSTS and the always-on hardening headers. The app served none of these before.

    Never overwrites a header a view set deliberately, so an endpoint that needs its own policy
    (a future report-collector, say) stays in control. Policy lives in ``security_headers.py``;
    this only supplies the request-derived inputs.
    """
    scheme = "https" if request.is_secure else "http"
    hdrs = security_headers(
        is_secure=request.is_secure,
        page_scheme=scheme,
        page_host=request.host or "",
        grafana_origin=grafana_frame_origin(
            explicit_uri=_eget("RACK_WEB_GRAFANA_URI", "") or "",
            page_scheme=scheme,
            page_host=request.host or "",
            port=_eget("RACK_WEB_GRAFANA_PORT", "3000") or "3000",
        ),
        csp_mode=(os.environ.get("RACK_WEB_CSP_MODE") or "enforce"),
    )
    for name, value in hdrs.items():
        response.headers.setdefault(name, value)
    return response


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


_nvis_workspace_dir_effective = config_env._nvis_workspace_dir_effective


# The snapshot cache lives here, so app.py is what can build the block; ai_chat only needs to be
# able to call it. Registered once at import so every prompt builder gets it, not just /api/ai/chat.
ai_chat.set_live_inventory_provider(_ai_live_inventory_block)


# last_job / scancel: plain id or job array id (e.g. 12345_0)
_SLURM_SCANCEL_JOB_ID_RE = core.SLURM_JOB_ID_RE  # one definition, in core


_truthy_arg = core.truthy_arg


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
    """Capture mode. Default ``off`` -- request capture is OPT-IN.

    It used to default to ``actions``, so any deployment that had never heard of the data lake still
    captured ~100 mutating endpoints and posted them to ``host.docker.internal:9410``. That looked
    harmless only because usually nothing listens there; the protection was the environment, not a
    decision. A site that wants the lake now says so.
    """
    return (os.environ.get("RACK_LAKE_CAPTURE") or "off").strip().lower()


def _lake_should_capture(path: str, method: str) -> bool:
    mode = _lake_capture_mode()
    if mode == "off" or not path.startswith("/api/"):
        return False
    # Checked before `mode == "all"`, deliberately: "all" means every ordinary endpoint, never the
    # credential-bearing ones. `POST /api/login` carries the dashboard password in its body and
    # `PUT /api/vault/nvftp-token` carries a token; both were captured verbatim into `request_body`
    # until 2026-08-24. The lake retains artifacts permanently on purpose, so this is the one gate
    # that must not be overridable by a debug setting.
    if lake_sink is not None and lake_sink.never_capture(path):
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
            # Was `request.headers.get("X-Forwarded-For") or request.remote_addr`, which believed
            # an attacker-supplied header unconditionally — so any operator could attribute their
            # actions to any address, in a record that reads as authoritative. Same resolver and
            # same rule as the audit trail now: the header counts only when the peer is a
            # configured trusted proxy, and the derivation is labelled either way (FR-8).
            **dict(zip(("remote_addr", "remote_addr_trust"), _common.client_addr())),
            "rack": rack,
        }
        # Scrub before anything leaves. `scrub_record` redacts by key name and by known secret
        # value, and returns None for a path that must not be captured at all -- the same rule as
        # the predicate above, enforced twice on purpose so a future caller building its own record
        # cannot bypass the first gate.
        record["response_body"] = body
        safe = lake_sink.scrub_record(record)
        if safe is None:
            return response
        body = str(safe.pop("response_body", "") or "")
        # submit, not push: this runs inside after_request, so a synchronous POST would put the
        # lake's availability in the operator's latency path -- 20 s of default timeout per
        # captured request, on a dashboard that would simply feel broken with no way to tell why.
        lake_sink.submit_run(
            run_id=lake_sink.make_run_id(f"req-{action}", rack, uuid.uuid4().hex[:12]),
            workload="rackstatus_request", rack=rack, job_id=None, nodes=[],
            status=safe["status"], parsed=safe,
            log_path=f"synthesized:{action}.response.json", log_text=body,
        )
    except Exception as e:  # noqa: BLE001 - never break a response
        _logger.warning("lake request capture failed: %s", e)
    return response


# Upper bound for the launch dialog's NEMO step selector. The UI offers 50 (perf) and 2010
# (soak); the cap only rejects absurd hand-crafted payloads, it does not pin the choices.


@app.route("/")
def index():
    """Serve the React SPA."""
    return _serve_spa_asset("index.html")


@app.route("/spa/")
@app.route("/spa/<path:path>")
def serve_spa(path: str = "index.html"):
    """Serve the React SPA from static/spa/ (built by Vite)."""
    return _serve_spa_asset(path)


@app.route("/<path:path>")
def serve_root_spa(path: str):
    """Serve React SPA for all non-API routes (client-side routing)."""
    return _serve_spa_asset(path)


# Per-domain API modules under backend/api/, discovered automatically. Drop a module in that
# package and it registers itself; nothing here changes. See backend/api/__init__.py.
import api as _api_package

_api_package.register(app)

# The action vocabulary is resolved and validated here, after every blueprint is registered —
# a declaration cannot know its own endpoint name at decoration time, because the function has
# not been bound to a blueprint yet. A malformed name or two routes claiming the same
# (action, variant) raises and takes startup down with it, on purpose: a collision found by
# reading audit records is found after the evidence is already ambiguous (FR-14a).
#
# Coverage is not checked here. FR-14b makes a *missing* declaration fail the build, which is
# test_audit_actions.py's job — enforcing it at startup would stop a half-migrated tree booting
# at all, and the declarations are spread across the whole api package.
import audit.actions as _audit_actions
from api import _common

_audit_actions.build_registry(app)

# After all routes and middleware are registered (avoid circular import via nvftp_cache_sync._eget → app).
_bootstrap_catalog_at_startup()
throttle.install(app)  # collect @throttle declarations now every blueprint is registered
_bootstrap_identity_at_startup()
_bootstrap_nvftp_sync_at_startup()
_bootstrap_validation_activities_refresh_at_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # TLS when a certificate is configured. The same two variables drive gunicorn's
    # --certfile/--keyfile in the container entrypoint, so a deployment configures TLS in one
    # place regardless of which server runs. Binding stays 0.0.0.0 here (a dev server the
    # operator reaches from their own browser); the *container's* published port is what gets
    # restricted to loopback, in compose.
    _cert, _key = _tls_cert_configured(), (os.environ.get("RACK_WEB_TLS_KEY") or "").strip()
    _ssl = None
    if _cert and not _key:
        _logger.error("RACK_WEB_TLS_CERT is set but RACK_WEB_TLS_KEY is not; serving plain HTTP")
    elif _cert:
        # An explicit hardened context, not the (cert, key) tuple: Werkzeug would build its own
        # with whatever floor the Python build defaults to. tls_server is the same policy
        # gunicorn.conf.py applies, so local and container runs cannot diverge.
        _ssl = tls_server.build_ssl_context(_cert, _key)
        _logger.info(
            "serving HTTPS on :%s (cert %s, %s)", port, _cert, tls_server.describe(_ssl)
        )
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1",
        threaded=True,
        ssl_context=_ssl,
    )
