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
import io
import json
import math
import zipfile
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import time
import warnings
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)

APP_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = APP_DIR / "bin" / "scripts"
_app_dir_s = str(APP_DIR)
if _app_dir_s not in sys.path:
    sys.path.insert(0, _app_dir_s)

import native_status  # noqa: E402 — after sys.path so ``import native_status`` works when cwd != APP_DIR

DEFAULT_SCRIPT = SCRIPTS_DIR / "rack_status.sh"
DEFAULT_LOG_DIR = APP_DIR / "var" / "logs"

app = Flask(__name__)

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

_PHYSICAL_RF0_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_PHYSICAL_RF0_TRAY_HOST_RE = re.compile(r"^p[0-9]+-r[0-9]+-ct[0-9]+$", re.IGNORECASE)
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

    def host_ok(h: str) -> bool:
        if _PHYSICAL_RF0_TRAY_HOST_RE.match(h):
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
        cmsh_bin = shutil.which("cmsh")
        if not cmsh_bin:
            _physical_rf0_loaded = True
            return {}
        cmd = [
            cmsh_bin,
            "-c",
            "device; foreach -t physicalnode (get hostname; interfaces; use rf0; get ip)",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
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
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=15)
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
        rc = proc.returncode if proc.returncode is not None else -1
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


def _stress_worker(job_id: str, cmd: list[str], cwd: str, timeout: int, display_cmd: str) -> None:
    """Run mnubergemm stress bash in a background thread; update _stress_jobs[job_id]."""

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
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error="mnubergemm stress script timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _stress_job_purge_later(job_id)
            return
        rc = proc.returncode if proc.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else "mnubergemm stress script exited non-zero",
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
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=15)
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
        rc = proc.returncode if proc.returncode is not None else -1
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
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=15)
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
        rc = proc.returncode if proc.returncode is not None else -1
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


# Remote command for pdsh (single argv): mount + service restarts; DCGM unit fallback.
_SERVICES_RESTART_PDSH_REMOTE = (
    "mount -a; "
    "systemctl restart munge; "
    "systemctl restart slurmd; "
    "systemctl restart nvidia-imex; "
    "systemctl restart nvidia-dcgm 2>/dev/null || systemctl restart cuda-dcgm"
)


def run_services_restart(pdsh_w: str) -> dict:
    """Run pdsh across hosts: mount -a, restart munge/slurmd/nvidia-imex, then nvidia-dcgm or cuda-dcgm."""
    w = (pdsh_w or "").strip()
    if not w:
        raise ValueError("empty pdsh -w pattern")
    timeout = max(30, _env_int("RACK_SERVICES_RESTART_TIMEOUT", 1800))
    dry = os.environ.get("RACK_SERVICES_RESTART_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["pdsh", "-w", w, _SERVICES_RESTART_PDSH_REMOTE]
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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
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
    """Run ``pdsh -w <rack>-ct[RANGE] uptime`` (RANGE from RACK_VALIDATION_CT_RANGE)."""
    rk = _normalize_rack_id(rack)
    w = f"{rk}-ct[{_validation_ct_inner()}]"
    timeout = max(15, _env_int("RACK_UPTIME_PDSH_TIMEOUT", 120))
    dry = os.environ.get("RACK_UPTIME_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["pdsh", "-w", w, "uptime"]
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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
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


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _project_name() -> str:
    """Optional label appended to page titles (``PROJECT_NAME`` or ``RACK_WEB_PROJECT_NAME``)."""
    for key in ("PROJECT_NAME", "RACK_WEB_PROJECT_NAME"):
        v = (os.environ.get(key) or "").strip()
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


def _auth_enabled() -> bool:
    """Dashboard login via PAM unless RACK_WEB_AUTH_DISABLED is set."""
    return not _truthy_env("RACK_WEB_AUTH_DISABLED")


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


def _configure_session(app: Flask) -> None:
    key = (os.environ.get("RACK_WEB_SECRET_KEY") or "").strip()
    if key:
        app.secret_key = key
    elif _auth_enabled():
        app.secret_key = secrets.token_hex(32)
        warnings.warn(
            "RACK_WEB_SECRET_KEY is not set; using an ephemeral secret. "
            "Sessions reset on restart. Set RACK_WEB_SECRET_KEY for stable logins.",
            stacklevel=2,
        )
    else:
        app.secret_key = secrets.token_hex(16)
    app.permanent_session_lifetime = _session_lifetime()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _truthy_env("RACK_WEB_COOKIE_SECURE")


_configure_session(app)


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
            env=os.environ.copy(),
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


def _pam_verify(username: str, password: str, service: str) -> bool:
    if password is None:
        return False
    try:
        from rack_web.pam_ctypes import authenticate as pam_authenticate
    except (ImportError, OSError):
        return _pam_verify_subprocess(username, password, service)
    return bool(pam_authenticate(username, password, service=service))


@app.before_request
def _require_dashboard_auth():
    if not _auth_enabled():
        return None
    if request.endpoint in ("login", "static"):
        return None
    if _session_ok():
        return None
    if request.path.startswith("/api/"):
        return (
            jsonify({"ok": False, "error": "authentication required", "login": "/login"}),
            401,
        )
    nxt = _safe_next(request.full_path)
    if nxt:
        return redirect(url_for("login", next=nxt))
    return redirect(url_for("login"))


def _script_path() -> Path:
    p = os.environ.get("RACK_STATUS_SCRIPT", str(DEFAULT_SCRIPT))
    return Path(p).resolve()


def _parser_path() -> Path:
    p = os.environ.get(
        "RACK_PARSER_SCRIPT",
        str(SCRIPTS_DIR / "parser.sh"),
    )
    return Path(p).resolve()


def _parser_extra_args() -> list[str]:
    extra = os.environ.get("RACK_PARSER_EXTRA_ARGS", "")
    if not extra.strip():
        return []
    return shlex.split(extra)


def _parser_export_glob() -> str:
    """Basename-only glob under log_dir root for exports (default *.txt)."""
    raw = (os.environ.get("RACK_PARSER_EXPORT_GLOB") or "*.txt").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        return "*.txt"
    return raw


def _nemo_nccl_in_log_basename(name: str) -> bool:
    """Dashboard and rack_status consider only logs whose basename contains NEMO or NCCL (case-insensitive)."""
    low = name.lower()
    return "nemo" in low or "nccl" in low


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
    if (low.startswith("root-nemo-") or low.startswith("root-nccl-")) and re.search(
        r",\s*p\d+-r", low
    ):
        return True
    racks = {m.group(1).lower() for m in re.finditer(r"\b(p\d+-r\d+)\b", name, re.IGNORECASE)}
    return len(racks) >= 2


def _multirack_site_prefix() -> str:
    """Pod id for ``r[01-04]``-style names without ``pN-`` (e.g. ``p5``). See RACK_MULTIRACK_SITE_PREFIX (defaults to p5)."""
    raw = os.environ.get("RACK_MULTIRACK_SITE_PREFIX")
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
    racks = sorted({m.group(1).lower() for m in re.finditer(r"\b(p\d+-r\d+)\b", name, re.IGNORECASE)})
    if len(racks) >= 2:
        return "+".join(racks) + suffix
    m = re.search(r"(p\d+)-r\[([\d,\-]+)\]", name, re.IGNORECASE)
    if m:
        pod = m.group(1).lower()
        nums = _expand_r_inner_to_rack_nums(m.group(2))
        if nums:
            return "+".join(f"{pod}-r{n:02d}" for n in nums) + suffix
    m2 = re.search(r"(?:^|[\-_])r\[([\d,\-]+)\]", name, re.IGNORECASE)
    if m2:
        pod = _multirack_site_prefix()
        nums = _expand_r_inner_to_rack_nums(m2.group(1))
        if nums and pod:
            pl = pod.lower().strip()
            if not re.match(r"^p\d+$", pl):
                pl = "p" + pl.lstrip("p")
            return "+".join(f"{pl}-r{n:02d}" for n in nums) + suffix
    stem = Path(name).stem
    return (stem[:120] if stem else name) + suffix


def _multirack_physical_rack_ids(name: str) -> list[str]:
    """Rack ids (``pN-rMM``) for Slurm sinfo aggregation; mirrors ``multirack_display_rack_from_basename`` cases."""
    racks = sorted({m.group(1).lower() for m in re.finditer(r"\b(p\d+-r\d+)\b", name, re.IGNORECASE)})
    if len(racks) >= 2:
        return list(racks)
    m = re.search(r"(p\d+)-r\[([\d,\-]+)\]", name, re.IGNORECASE)
    if m:
        pod = m.group(1).lower()
        nums = _expand_r_inner_to_rack_nums(m.group(2))
        if nums:
            return [f"{pod}-r{n:02d}" for n in nums]
    m2 = re.search(r"(?:^|[\-_])r\[([\d,\-]+)\]", name, re.IGNORECASE)
    if m2:
        pod = _multirack_site_prefix()
        nums = _expand_r_inner_to_rack_nums(m2.group(1))
        if nums and pod:
            pl = pod.lower().strip()
            if not re.match(r"^p\d+$", pl):
                pl = "p" + pl.lstrip("p")
            return [f"{pl}-r{n:02d}" for n in nums]
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


_NEMO_1RACK_FN_RE = re.compile(r"1Rack-(p[0-9]+-r[0-9]+)-", re.IGNORECASE)


def _rack_id_from_nemo_1rack_filename(name: str) -> str | None:
    """Return normalized rack id pNN-rMM if basename looks like a NEMO 1Rack log, else None."""
    m = _NEMO_1RACK_FN_RE.search(name)
    if not m:
        return None
    r = m.group(1).strip().lower()
    if not _RACK_ID_RE.match(r):
        return None
    return r


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


def list_log_txt_paths_for_export(log_dir: Path) -> list[Path]:
    """Matching *.txt under log_dir root only (excludes failed/).

    NEMO one-rack logs (*1Rack-pNN-rMM-*.txt): at most one file per rack — the newest by mtime
    among logs that look successful (same heuristics as parser.sh: no Slurm hard-fail grep, LLM
    markers present). Only racks whose **nemo** cell from ``rack_status.sh`` is ``done`` (not
    ongoing / fail / pending) are included unless ``RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER`` is set.
    Racks with no successful log contribute no 1Rack file. Other matching files in the root are
    included unchanged.
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

    out = others + selected_rack
    out.sort(key=lambda p: p.name.lower())
    return out


def find_log_paths_for_rack(log_dir: Path, rack: str) -> list[Path]:
    """Paths to root-NEMO-1Rack-<rack>-*.txt in log_dir and log_dir/failed (newest first)."""
    rk = _normalize_rack_id(rack)
    pat = f"*1Rack-{rk}-*.txt"
    out: list[Path] = []
    for base in (log_dir, log_dir / "failed"):
        if base.is_dir():
            out.extend(
                p
                for p in base.glob(pat)
                if p.is_file() and _nemo_nccl_in_log_basename(p.name)
            )
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


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
    """Run ``cmd`` under ``bash -lc`` after optional ``module load`` (same semantics as Slurm drain helpers)."""
    skip_mod = os.environ.get("RACK_SLURM_SKIP_MODULE_LOAD", "").strip().lower() in ("1", "true", "yes")
    mod = (os.environ.get("RACK_SLURM_MODULE") or "slurm").strip() or "slurm"
    if skip_mod:
        inner = cmd
    else:
        inner = f"module load {shlex.quote(mod)} && {cmd}"
    return subprocess.run(
        ["bash", "-lc", inner],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _scontrol_hostnames(nodespec: str) -> list[str]:
    nodespec = nodespec.strip()
    if not nodespec or nodespec.lower() in ("none", "null"):
        return []
    proc = _run_bash_slurm_prefix(
        "scontrol show hostnames " + shlex.quote(nodespec),
        timeout=max(30, _env_int("RACK_SCONTROL_HOSTNAMES_TIMEOUT", 120)),
    )
    if proc.returncode != 0:
        return []
    return [ln.strip().lower() for ln in (proc.stdout or "").splitlines() if ln.strip()]


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
    hosts_set: dict[str, None] = {}
    for line in (proc.stdout or "").splitlines():
        seg = line.strip()
        if not seg:
            continue
        for part in seg.split("+"):
            part = part.strip()
            if not part:
                continue
            for h in _scontrol_hostnames(part):
                hosts_set.setdefault(h, None)
    hosts = sorted(hosts_set)
    return hosts, ("sacct" if hosts else "sacct_empty")


def _hostnames_for_rack_validation_expand(rack: str) -> tuple[list[str], str]:
    try:
        tgt = validation_target_for_rack(rack, None)
    except ValueError:
        return [], "target_error"
    hs = _scontrol_hostnames(tgt)
    return hs, ("rack_ct_expand" if hs else "rack_ct_expand_failed")


def _job_hosts_for_grafana(rack: str, job_raw: str | None) -> tuple[list[str], str]:
    hosts, src = _hostnames_for_slurm_job(job_raw)
    if hosts:
        return hosts, src
    hosts2, src2 = _hostnames_for_rack_validation_expand(rack)
    if hosts2:
        return hosts2, src2
    return [], "none"


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
    raw = (os.environ.get("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ") or "UTC").strip() or "UTC"
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
    return (os.environ.get("RACK_WEB_GRAFANA_DASH_UID") or "bright-gpu-per-host").strip() or "bright-gpu-per-host"


def _grafana_dash_slug() -> str:
    s = (os.environ.get("RACK_WEB_GRAFANA_DASH_SLUG") or "bright-gpu-thermal-and-power").strip()
    return s or "bright-gpu-thermal-and-power"


def _rack_web_grafana_uri() -> str:
    u = (os.environ.get("RACK_WEB_GRAFANA_URI") or "").strip()
    return u.rstrip("/") if u else ""


def _rack_web_grafana_port() -> str:
    p = (os.environ.get("RACK_WEB_GRAFANA_PORT") or "3000").strip() or "3000"
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
    g = (os.environ.get("RACK_WEB_GRAFANA_TIMEZONE") or "").strip()
    if g:
        gl = g.lower()
        if gl in ("browser", "utc"):
            return gl
        return g
    logtz = (os.environ.get("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ") or "UTC").strip() or "UTC"
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
    if hosts:
        host_regex = "|".join(re.escape(h) for h in hosts)
    else:
        host_regex = ".*"
    return {
        "ok": True,
        "iframe_ready": True,
        "from_ms": int(lo * 1000),
        "to_ms": int(hi * 1000),
        "from_iso_utc": _iso_utc_z(lo),
        "to_iso_utc": _iso_utc_z(hi),
        "host_regex": host_regex,
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
    if hosts:
        host_regex = "|".join(re.escape(h) for h in hosts)
    else:
        host_regex = ".*"
    return {
        "ok": True,
        "iframe_ready": True,
        "from_ms": int(lo * 1000),
        "to_ms": int(hi * 1000),
        "from_iso_utc": _iso_utc_z(lo),
        "to_iso_utc": _iso_utc_z(hi),
        "host_regex": host_regex,
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
    s = (os.environ.get("RACK_VALIDATION_CT_RANGE", "01-18") or "").strip()
    if not s or not _CT_RANGE_INNER_RE.match(s):
        return "01-18"
    return s


def _validation_script_path() -> Path:
    p = os.environ.get(
        "RACK_VALIDATION_SCRIPT",
        str(SCRIPTS_DIR / "run_singlerack.sh"),
    )
    return Path(p).resolve()


def _mnubergemm_stress_script_path() -> Path:
    p = os.environ.get(
        "RACK_MNUBERGEMM_STRESS_SCRIPT",
        str(SCRIPTS_DIR / "run_mnubergemm_stress.sh"),
    )
    return Path(p).resolve()


def _sysinfo_script_path() -> Path:
    p = os.environ.get(
        "RACK_SYSINFO_SCRIPT",
        str(SCRIPTS_DIR / "sysinfo.sh"),
    )
    return Path(p).resolve()


def _nvswitches_rack_audit_script_path() -> Path:
    p = os.environ.get(
        "RACK_NVSWITCH_AUDIT_SCRIPT",
        str(SCRIPTS_DIR / "nvswitches_rack_audit.sh"),
    )
    return Path(p).resolve()


def _nvswitch_sdn_reset_script_path() -> Path:
    p = os.environ.get(
        "RACK_NVSWITCH_SDN_RESET_SCRIPT",
        str(SCRIPTS_DIR / "nvswitch_sdn_reset.sh"),
    )
    return Path(p).resolve()


def _nvswitch_system_reset_script_path() -> Path:
    p = os.environ.get(
        "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT",
        str(SCRIPTS_DIR / "nvswitch_system_reset.sh"),
    )
    return Path(p).resolve()


def _imex_reset_script_path() -> Path:
    p = os.environ.get(
        "RACK_IMEX_RESET_SCRIPT",
        str(SCRIPTS_DIR / "imex_reset.sh"),
    )
    return Path(p).resolve()


def _imex_check_script_path() -> Path:
    p = os.environ.get(
        "RACK_IMEX_CHECK_SCRIPT",
        str(SCRIPTS_DIR / "imex_check.sh"),
    )
    return Path(p).resolve()


def _nemo_xrack_script_path() -> Path:
    p = os.environ.get("RACK_NEMO_XRACK_SCRIPT", str(SCRIPTS_DIR / "run-nemo-xrack.sh"))
    return Path(p).resolve()


def _run_llama_script_path() -> Path:
    p = os.environ.get("RACK_LLAMA_SCRIPT", str(SCRIPTS_DIR / "run-llama.sh"))
    return Path(p).resolve()


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


_VALIDATION_TARGET_RE = re.compile(r"^p[0-9]+-r[0-9]+-ct\[[0-9,\-]+\]$", re.IGNORECASE)


def validation_target_for_rack(rack: str, override: str | None) -> str:
    """Build e.g. p26-r05-ct[01-18] for run_singlerack.sh (override must match same pattern)."""
    rk = _normalize_rack_id(rack)
    o = (override or "").strip()
    if o:
        if not _VALIDATION_TARGET_RE.fullmatch(o):
            raise ValueError("invalid target (expected pNN-rMM-ct[digits/ranges])")
        return o.lower()
    return f"{rk}-ct[{_validation_ct_inner()}]"


def _default_log_dir() -> Path:
    p = os.environ.get("RACK_STATUS_LOG_DIR", str(DEFAULT_LOG_DIR))
    return Path(p).resolve()


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("RACK_STATUS_ALLOWED_PREFIXES", "")
    if not raw.strip():
        return []
    return [Path(x.strip()).resolve() for x in raw.split(",") if x.strip()]


def _resolve_log_dir(raw: str | None) -> Path:
    base = Path(raw or _default_log_dir()).expanduser()
    log_dir = base.resolve()
    roots = _allowed_roots()
    if roots:
        ok = any(str(log_dir).startswith(str(r) + os.sep) or log_dir == r for r in roots)
        if not ok:
            raise ValueError(f"log_dir must be under one of: {[str(r) for r in roots]}")
    if not log_dir.is_dir():
        raise ValueError(f"not a directory: {log_dir}")
    return log_dir


def _extra_script_args() -> list[str]:
    extra = os.environ.get("RACK_STATUS_EXTRA_ARGS", "")
    if not extra.strip():
        return []
    return shlex.split(extra)


# Tray tokens from rack_status "ct_down_unk": DN:…; INST:…; UP:… (legacy DN/UNK or unprefixed).
_CT_POWER_TOKEN_RE = re.compile(
    r"(p[0-9]+-r[0-9]+-ct\[[0-9,\-]+\])",
    re.IGNORECASE,
)


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
        for m in _CT_POWER_TOKEN_RE.finditer(fragment):
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


_CT_POWER_TRAY_FULL_RE = re.compile(r"^p[0-9]+-r[0-9]+-ct\[[0-9,\-]+\]$", re.IGNORECASE)
_CT_POWER_SINGLE_CT_RE = re.compile(r"^(p[0-9]+-r[0-9]+)-ct\[(\d+)\]$", re.IGNORECASE)


def _condense_ct_numbers(nums: list[int]) -> str:
    """Turn sorted unique tray indices into inner bracket text like 01,03-05,18."""
    u = sorted(set(nums))
    if not u:
        return ""
    mx = u[-1]
    w = 2 if mx < 100 else len(str(mx))

    def fmt(n: int) -> str:
        return f"{n:0{w}d}"

    parts: list[str] = []
    s = e = u[0]
    for n in u[1:]:
        if n == e + 1:
            e = n
        else:
            parts.append(fmt(s) if s == e else f"{fmt(s)}-{fmt(e)}")
            s = e = n
    parts.append(fmt(s) if s == e else f"{fmt(s)}-{fmt(e)}")
    return ",".join(parts)


def merge_ct_individuals_for_power(nodes: list[str]) -> list[str]:
    """
    Merge per-node tokens (pNN-rMM-ct[dd] with one decimal id) into tray specs
    pNN-rMM-ct[ranges] for cmsh device; power -n …
    """
    by_rack: dict[str, list[int]] = {}
    for raw in nodes:
        if not isinstance(raw, str):
            raise ValueError("each entry in specs must be a string")
        s = raw.strip().lower()
        m = _CT_POWER_SINGLE_CT_RE.match(s)
        if not m:
            raise ValueError(
                f"invalid node token {raw!r} — expected pNN-rMM-ct[number] with a single tray id"
            )
        rr = m.group(1).lower()
        n = int(m.group(2), 10)
        by_rack.setdefault(rr, []).append(n)
    out: list[str] = []
    for rr in sorted(by_rack.keys(), key=str.lower):
        inner = _condense_ct_numbers(by_rack[rr])
        if inner:
            tok = f"{rr}-ct[{inner}]"
            if not _CT_POWER_TRAY_FULL_RE.match(tok):
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
    cmd_display = f"{shlex.quote(cmsh)} -c {shlex.quote(inner)}"
    try:
        proc = subprocess.run(
            [cmsh, "-c", inner],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError(f"cmsh executable not found: {cmsh}") from None
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
    cmsh = os.environ.get("RACK_CMSH", "cmsh")
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
    return Path(os.environ.get("RACK_COMMENTS_FILE", str(APP_DIR / "rack_comments.json"))).resolve()


_RACK_ID_RE = re.compile(r"^p[0-9]+-r[0-9]+$", re.IGNORECASE)
_NVSWITCH_HOSTNAME_RE = re.compile(r"^p[0-9]+-r[0-9]+-nvsw-\d+$", re.IGNORECASE)


def _normalize_rack_id(rack: str) -> str:
    r = (rack or "").strip().lower()
    if not _RACK_ID_RE.match(r):
        raise ValueError("invalid rack id (expected e.g. p17-r01)")
    return r


def _normalize_nvswitch_host_for_rack(rack: str, raw: object) -> str:
    """Require a full hostname ``pNN-rMM-nvsw-<index>`` belonging to *rack*."""
    rk = _normalize_rack_id(rack)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("nvswitch_host is required (e.g. p01-r01-nvsw-01)")
    h = raw.strip().lower()
    if not _NVSWITCH_HOSTNAME_RE.fullmatch(h):
        raise ValueError("invalid nvswitch_host (expected pNN-rMM-nvsw-<index>)")
    if not h.startswith(rk + "-nvsw-"):
        raise ValueError("nvswitch_host does not match rack")
    return h


def _nvswitch_host_sort_key(hostname: str) -> tuple[int, str]:
    m = re.search(r"-nvsw-(\d+)$", hostname, flags=re.IGNORECASE)
    if m:
        return (int(m.group(1)), hostname.lower())
    return (0, hostname.lower())


def nvswitch_hosts_for_rack_list(rack: str) -> list[str]:
    """Expand NV switch hostnames for *rack* via ``nodeset`` when available; else ``rack-nvsw-01`` … ``-09``."""
    rk = _normalize_rack_id(rack)
    out: list[str] = []
    timeout = max(5, _env_int("RACK_NVSWITCH_NODESET_TIMEOUT", 30))
    for spec in (f"@{rk}-nvsw", f"{rk}-nvsw-[01-09]"):
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
        out = [f"{rk}-nvsw-{i:02d}" for i in range(1, 10)]
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
    """Refresh ``native_workers`` from in-memory state so cached /api/status shows live thread activity."""
    if _truthy_env("RACK_STATUS_USE_SHELL_TABLE"):
        return
    snap = native_status.worker_snapshot_for_log_dir(log_dir.resolve())
    if snap is not None:
        out["native_workers"] = snap


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
            # New: rack, nemo, last_job, sinfo, ct_down_unk (5 cols). Legacy: rack, cm, nemo, last_job, sinfo, ct_down_unk.
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
                rows.append(
                    {
                        "rack": parts[0],
                        "nemo": parts[1],
                        "last_job": parts[2],
                        "sinfo": parts[3],
                        "ct_down_unk": parts[4],
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
_CMSH_RACK_LIST_PATH = APP_DIR / "var" / "cache" / "cmsh_rack_list.txt"
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
        if not shutil.which("cmsh"):
            _CMSH_RACK_LIST_STATE = "failed"
            return
        try:
            proc = subprocess.run(
                ["cmsh", "-c", "rack; list"],
                capture_output=True,
                text=True,
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
    if _truthy_env("RACK_STATUS_USE_SHELL_TABLE"):
        script = _script_path()
        if not script.is_file():
            raise FileNotFoundError(f"rack_status script not found: {script}")

        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        _ensure_cmsh_rack_list_env_for_subprocess(env)

        tmp_pre: Path | None = None
        proc: subprocess.CompletedProcess | None = None
        try:
            tmp_pre = _maybe_build_tmp_status_from_log_cache(log_dir)
            if tmp_pre is not None:
                cmd = [str(script), *_extra_script_args(), "--tmp-status", str(tmp_pre)]
            else:
                cmd = [str(script), *_extra_script_args()]
            proc = subprocess.run(
                cmd,
                cwd=str(log_dir),
                env=env,
                capture_output=True,
                text=True,
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
    return max(5, _env_int("RACK_STATUS_BACKGROUND_INTERVAL_SEC", 30))


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
    interval = _rack_status_background_interval_sec()
    first = True
    while True:
        if not first:
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
    try:
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
    raw = (os.environ.get("RACK_GPU_RESET_SCRIPT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (SCRIPTS_DIR / "gpu_reset.sh").resolve()


def run_gpu_reset(pdsh_w: str) -> dict:
    """Run gpu_reset.sh once; argument is the pdsh -w host pattern (may contain commas)."""
    script = _gpu_reset_script_path()
    if not script.is_file():
        raise OSError(f"gpu_reset script not found: {script}")
    w = (pdsh_w or "").strip()
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
    try:
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
    w = (pdsh_w or "").strip()
    if not w:
        raise ValueError("empty pdsh -w pattern")
    remote = _bug_report_remote_dir()
    rq = shlex.quote(remote)
    inner = f"mkdir -p {rq}/$HOSTNAME && cd {rq}/$HOSTNAME && nvidia-bug-report.sh"
    timeout = max(120, _env_int("RACK_BUG_REPORT_TIMEOUT", 10800))
    dry = os.environ.get("RACK_BUG_REPORT_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    fan = max(1, _env_int("RACK_BUG_REPORT_PDSH_FANOUT", 32))
    cmd = ["pdsh", "-f", str(fan), "-w", w, inner]
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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
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


def _parse_bug_report_paths_from_pdsh(stdout: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in (stdout or "").splitlines():
        m = _BUG_REPORT_PDSH_PATH_RE.match(line.strip())
        if not m:
            continue
        host = m.group(1).strip().lower()
        path = m.group(2).strip()
        key = (host, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"host": host, "path": path})
    return out


def run_bug_report_list_pdsh(pdsh_w: str) -> dict:
    """List newest nvidia-bug-report *.log.gz per host under ``<remote>/$HOSTNAME/``."""
    w = (pdsh_w or "").strip()
    if not w:
        raise ValueError("empty pdsh -w pattern")
    remote = _bug_report_remote_dir()
    rq = shlex.quote(remote)
    inner = f"ls -t {rq}/$HOSTNAME/nvidia-bug-report*.log.gz 2>/dev/null | head -1"
    timeout = max(30, _env_int("RACK_BUG_REPORT_LIST_TIMEOUT", 600))
    fan = max(1, _env_int("RACK_BUG_REPORT_PDSH_FANOUT", 32))
    cmd = ["pdsh", "-f", str(fan), "-w", w, inner]
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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
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
    return Path(_bug_report_remote_dir()).expanduser().resolve()


def _local_bug_report_inventory() -> tuple[list[dict[str, str]], str | None]:
    """Scan ``<RACK_BUG_REPORT_REMOTE_DIR>/<subdir>/`` for ``nvidia-bug-report*.log.gz`` and ``nvlmapper.*.zip``."""
    base = _bug_report_local_base_path()
    items: list[dict[str, str]] = []
    if not base.exists():
        return [], f"directory does not exist: {base}"
    if not base.is_dir():
        return [], f"not a directory: {base}"
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
            except OSError:
                continue
    except OSError as e:
        return [], str(e)
    items.sort(key=lambda x: (x["host"].lower(), x["name"].lower()))
    return items, None


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
                host = str(ent.get("host") or "").strip().lower()
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
    """List ``nvidia-bug-report*.log.gz`` and ``nvlmapper.*.zip`` under ``RACK_BUG_REPORT_REMOTE_DIR/<subdir>/`` (no pdsh)."""
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
    """Download one local file from ``<RACK_BUG_REPORT_REMOTE_DIR>/<host>/<name>`` (bug-report ``*.log.gz`` or NVLMAPPER ``nvlmapper.*.zip``)."""
    if os.environ.get("RACK_BUG_REPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "Bug report disabled (RACK_BUG_REPORT_DISABLED)"}), 403
    host = (request.args.get("host") or "").strip()
    name = (request.args.get("name") or "").strip()
    if not host or not name:
        return jsonify({"ok": False, "error": "host and name are required"}), 400
    if not _BUG_REPORT_SUBDIR_RE.fullmatch(host):
        return jsonify({"ok": False, "error": "invalid host (subdir name)"}), 400
    if not (
        _BUG_REPORT_BASENAME_RE.fullmatch(name) or _NVLMAPPER_ZIP_BASENAME_RE.fullmatch(name)
    ):
        return jsonify({"ok": False, "error": "invalid file name"}), 400
    base = _bug_report_local_base_path()
    path = (base / host / name).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "path outside reports directory"}), 400
    if not path.is_file():
        return jsonify({"ok": False, "error": "file not found"}), 404
    is_zip = bool(_NVLMAPPER_ZIP_BASENAME_RE.fullmatch(name))
    return send_file(
        path,
        mimetype="application/zip" if is_zip else "application/gzip",
        as_attachment=True,
        download_name=f"{host}_{name}",
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
    try:
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
    raw = (os.environ.get("RACK_HW_CHECK_SCRIPT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (SCRIPTS_DIR / "hw_check_pdsh.sh").resolve()


def _hw_check_argv_for_preset(preset: str) -> list[str]:
    key = (preset or "").strip().lower() or "default"
    if key not in _HW_CHECK_PRESETS:
        allowed = ", ".join(sorted(_HW_CHECK_PRESETS))
        raise ValueError(f"unknown preset {preset!r}; use one of: {allowed}")
    return list(_HW_CHECK_PRESETS[key])


def run_hw_check(pdsh_w: str, checker_argv: list[str]) -> dict:
    """Run hw_check_pdsh.sh once; checker_argv is passed after -- to checker.sh on each node."""
    script = _hw_check_pdsh_script_path()
    if not script.is_file():
        raise OSError(f"hw_check_pdsh.sh not found: {script}")
    w = (pdsh_w or "").strip()
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
    return (SCRIPTS_DIR / "checker.sh").resolve()


@app.route("/api/hw-check-help", methods=["GET"])
def api_hw_check_help():
    """checker.sh --help on the dashboard host (RACK_WEB_PRINT_HELP=1 skips root check)."""
    if os.environ.get("RACK_HW_CHECK_HELP_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_HW_CHECK_HELP_DISABLED)", "help": ""}), 403
    script = _checker_script_path()
    if not script.is_file():
        return jsonify({"ok": False, "error": f"checker.sh not found: {script}", "help": ""}), 404
    env = os.environ.copy()
    env["RACK_WEB_PRINT_HELP"] = "1"
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
    if not script.is_file():
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
            env=os.environ.copy(),
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
    if not script.is_file():
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
            env=os.environ.copy(),
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
    if not script.is_file():
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
            env=os.environ.copy(),
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
    if not script.is_file():
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
            env=os.environ.copy(),
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
    raw = (os.environ.get("RACK_NVLMAPPER_REPORTS_BASE") or "/home/cmsupport/reports").strip()
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
    if not nvlmapper_bin.is_file():
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
    if not runner.is_file():
        return None, (jsonify({"ok": False, "error": f"run_nvlmapper.sh not found ({runner})"}), 500)
    return {"rack": rack}, None


def _nvlmapper_zip_path_allowed(path: Path, rack: str) -> bool:
    base = (_nvlmapper_reports_base() / rack).resolve()
    try:
        path.resolve().relative_to(base)
    except ValueError:
        return False
    name = path.name
    if not name.startswith(f"nvlmapper.{rack}.") or not name.lower().endswith(".zip"):
        return False
    return path.is_file()


def _nvlmapper_async_worker(job_id: str, rack: str) -> None:
    """Stage ``nvlmapper`` + ``run_nvlmapper.sh`` (embedded YAML template inside the script), run ``./run_nvlmapper.sh``, zip, prune."""

    def _patch(**kwargs: object) -> None:
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)

    timeout_sec = max(120, _env_int("RACK_NVLMAPPER_TIMEOUT", 14400))
    work = (_nvlmapper_reports_base() / rack).resolve()
    display_cmd = f"(cd {shlex.quote(str(work))} && ./run_nvlmapper.sh {shlex.quote(rack)})"
    env = os.environ.copy()

    def _zip_and_prune() -> str:
        """Write ``nvlmapper.<rack>.<ts>.zip`` under *work*, delete everything else there; return path or ""."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        zip_name = f"nvlmapper.{rack}.{ts}.zip"
        zip_path = work / zip_name
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(work.iterdir(), key=lambda x: x.name):
                if not p.is_file():
                    if p.is_dir():
                        for sub in sorted(p.rglob("*")):
                            if sub.is_file():
                                zf.write(sub, arcname=sub.relative_to(work).as_posix())
                    continue
                if p.name in ("nvlmapper", "run_nvlmapper.sh", zip_name):
                    continue
                if p.suffix.lower() == ".zip":
                    continue
                zf.write(p, arcname=p.name)
        for p in list(work.iterdir()):
            if p.name == zip_name:
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        return str(zip_path) if zip_path.is_file() else ""

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
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
        zip_path_str = _zip_and_prune()
        ok = proc.returncode == 0
        zp = zip_path_str if zip_path_str and _nvlmapper_zip_path_allowed(Path(zip_path_str), rack) else ""
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=proc.returncode,
            stdout=out,
            stderr=err,
            command_display=display_cmd,
            nvlmapper_zip=zp,
            error=None
            if ok
            else "NVLMAPPER run exited non-zero (see stdout/stderr); zip was still written if staging produced files",
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
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@app.route("/api/nvlmapper-async", methods=["POST"])
def api_nvlmapper_async():
    """POST JSON ``{"rack": "pNN-rMM"}`` — stage, ``./run_nvlmapper.sh`` (embedded template + RC merge), zip; poll ``/api/reset-job-status``."""
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
            "rack": rack,
            "phase": "starting",
            "created_at": created,
            "nvlmapper_zip": "",
        }
    threading.Thread(
        target=_nvlmapper_async_worker,
        args=(job_id, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvlmapper", "rack": rack})


@app.route("/api/nvlmapper-zip", methods=["GET"])
def api_nvlmapper_zip():
    """GET ``?job_id=…`` — download ``nvlmapper.<rack>.<ts>.zip`` from a finished NVLMAPPER job (same host)."""
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
        return jsonify({"ok": False, "error": "no zip path recorded for this job"}), 400
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

    timeout_sec = max(30, _env_int("RACK_SYSINFO_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=os.environ.copy(),
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
            env=os.environ.copy(),
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
    if not script.is_file():
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

    timeout_sec = max(30, _env_int("RACK_SYSINFO_TIMEOUT", 1800))
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=os.environ.copy(),
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
    scontrol = (os.environ.get("RACK_SLURM_SCONTROL") or "scontrol").strip() or "scontrol"
    skip_mod = os.environ.get("RACK_SLURM_SKIP_MODULE_LOAD", "").strip().lower() in ("1", "true", "yes")
    mod = (os.environ.get("RACK_SLURM_MODULE") or "slurm").strip() or "slurm"
    nodename_arg = f"nodename={target}"
    scontrol_argv_tail = ["update", nodename_arg, *state_kv]

    if skip_mod:
        cmd: list[str] = [scontrol, *scontrol_argv_tail]
        cmd_display = " ".join(shlex.quote(c) for c in cmd)
    else:
        inner = (
            f"module load {shlex.quote(mod)} && {shlex.quote(scontrol)} "
            + " ".join(shlex.quote(a) for a in scontrol_argv_tail)
        )
        cmd = ["bash", "-lc", inner]
        cmd_display = "bash -lc " + shlex.quote(inner)

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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        if skip_mod:
            raise OSError(
                f"scontrol executable not found ({scontrol!r}); set RACK_SLURM_SCONTROL to the full path"
            ) from None
        raise OSError(
            "bash not found on PATH (needed to run `module load` before scontrol); "
            "install bash or set RACK_SLURM_SKIP_MODULE_LOAD=1 with RACK_SLURM_SCONTROL set to the real binary"
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
        r = (os.environ.get("RACK_SLURM_DRAIN_REASON") or "admin").strip() or "admin"
    if not _SLURM_DRAIN_REASON_RE.fullmatch(r):
        return jsonify(
            {
                "ok": False,
                "error": "invalid reason (1–256 chars: letters, digits, space, _.,:/()- only)",
            }
        ), 400
    try:
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
    try:
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
    scancel = (os.environ.get("RACK_SLURM_SCANCEL") or "scancel").strip() or "scancel"
    skip_mod = os.environ.get("RACK_SLURM_SKIP_MODULE_LOAD", "").strip().lower() in ("1", "true", "yes")
    mod = (os.environ.get("RACK_SLURM_MODULE") or "slurm").strip() or "slurm"
    if skip_mod:
        cmd: list[str] = [scancel, jid]
        cmd_display = " ".join(shlex.quote(c) for c in cmd)
    else:
        inner = f"module load {shlex.quote(mod)} && {shlex.quote(scancel)} {shlex.quote(jid)}"
        cmd = ["bash", "-lc", inner]
        cmd_display = "bash -lc " + shlex.quote(inner)
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        if skip_mod:
            raise OSError(
                f"scancel executable not found ({scancel!r}); set RACK_SLURM_SCANCEL to the full path"
            ) from None
        raise OSError(
            "bash not found on PATH (needed to run `module load` before scancel); "
            "install bash or set RACK_SLURM_SKIP_MODULE_LOAD=1 with RACK_SLURM_SCANCEL set to the real binary"
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
    squeue = (os.environ.get("RACK_SLURM_SQUEUE") or "squeue").strip() or "squeue"
    skip_mod = os.environ.get("RACK_SLURM_SKIP_MODULE_LOAD", "").strip().lower() in ("1", "true", "yes")
    mod = (os.environ.get("RACK_SLURM_MODULE") or "slurm").strip() or "slurm"
    if skip_mod:
        cmd: list[str] = [squeue]
        cmd_display = " ".join(shlex.quote(c) for c in cmd)
    else:
        inner = f"module load {shlex.quote(mod)} && {shlex.quote(squeue)}"
        cmd = ["bash", "-lc", inner]
        cmd_display = "bash -lc " + shlex.quote(inner)
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        if skip_mod:
            raise OSError(
                f"squeue executable not found ({squeue!r}); set RACK_SLURM_SQUEUE to the full path"
            ) from None
        raise OSError(
            "bash not found on PATH (needed to run `module load` before squeue); "
            "install bash or set RACK_SLURM_SKIP_MODULE_LOAD=1 with RACK_SLURM_SQUEUE set to the real binary"
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
    sinfo = (os.environ.get("RACK_SLURM_SINFO") or "sinfo").strip() or "sinfo"
    skip_mod = os.environ.get("RACK_SLURM_SKIP_MODULE_LOAD", "").strip().lower() in ("1", "true", "yes")
    mod = (os.environ.get("RACK_SLURM_MODULE") or "slurm").strip() or "slurm"
    if skip_mod:
        cmd: list[str] = [sinfo, "-R", "--long"]
        cmd_display = " ".join(shlex.quote(c) for c in cmd)
    else:
        inner = f"module load {shlex.quote(mod)} && {shlex.quote(sinfo)} -R --long"
        cmd = ["bash", "-lc", inner]
        cmd_display = "bash -lc " + shlex.quote(inner)
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        if skip_mod:
            raise OSError(
                f"sinfo executable not found ({sinfo!r}); set RACK_SLURM_SINFO to the full path"
            ) from None
        raise OSError(
            "bash not found on PATH (needed to run `module load` before sinfo); "
            "install bash or set RACK_SLURM_SKIP_MODULE_LOAD=1 with RACK_SLURM_SINFO set to the real binary"
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
    scontrol = (os.environ.get("RACK_SLURM_SCONTROL") or "scontrol").strip() or "scontrol"
    skip_mod = os.environ.get("RACK_SLURM_SKIP_MODULE_LOAD", "").strip().lower() in ("1", "true", "yes")
    mod = (os.environ.get("RACK_SLURM_MODULE") or "slurm").strip() or "slurm"
    tail = ["show", "job", jid]
    if skip_mod:
        cmd: list[str] = [scontrol, *tail]
        cmd_display = " ".join(shlex.quote(c) for c in cmd)
    else:
        inner = (
            f"module load {shlex.quote(mod)} && {shlex.quote(scontrol)} "
            + " ".join(shlex.quote(a) for a in tail)
        )
        cmd = ["bash", "-lc", inner]
        cmd_display = "bash -lc " + shlex.quote(inner)
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        if skip_mod:
            raise OSError(
                f"scontrol executable not found ({scontrol!r}); set RACK_SLURM_SCONTROL to the full path"
            ) from None
        raise OSError(
            "bash not found on PATH (needed to run `module load` before scontrol); "
            "install bash or set RACK_SLURM_SKIP_MODULE_LOAD=1 with RACK_SLURM_SCONTROL set to the real binary"
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
    p = Path(raw).expanduser().resolve()
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
                        f"no log *1Rack-{rk}-*.txt under {log_dir} or {log_dir / 'failed'}"
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


@app.route("/api/parser-results")
def api_parser_results():
    """Run parser.sh --json on the log for rack (same cwd convention as rack_status log_dir).

    Optional ``log_file``: basename under log_dir (or failed/) for multirack / explicit logs (must
    contain NEMO or NCCL in the basename). When set, ``rack`` is optional (used for Grafana context).

    Optional ``display_timezone`` query (IANA, e.g. ``Europe/Berlin``): zone for naive wall-clock
    timestamps in the NEMO log when deriving the LLM/Grafana time window (same as Charts TZ in the UI).
    """
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
                        f"no log *1Rack-{rk}-*.txt under {log_dir} or {log_dir / 'failed'}"
                    ),
                }
            ), 404
    parser = _parser_path()
    if not parser.is_file():
        return jsonify({"ok": False, "error": f"parser.sh not found: {parser}"}), 500
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
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
            timeout=max(30, _env_int("RACK_PARSER_TIMEOUT", 300)),
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
    display_cmd = "./parser.sh --json " + shlex.quote(log_path.name)
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
            "data": parsed,
            "stderr": err_tail,
            "grafana_uri": _rack_web_grafana_uri(),
            "grafana_port": _rack_web_grafana_port(),
            "grafana_embed": g_embed,
            "parser_display_timezone": pdisp,
        }
    )


def _probe_log_json_via_rack_status(abs_path: Path) -> dict[str, object] | None:
    """Run ``rack_status.sh --probe-json`` for one log file (same nemo/nccl/squeue heuristics as the table)."""
    script = _script_path()
    if not script.is_file():
        return None
    ap = abs_path.resolve()
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    _ensure_cmsh_rack_list_env_for_subprocess(env)
    # Multirack aggregate logs often lack per-collective / LLM signals that 1-rack runs have; use a
    # looser default unless VALIDATION_TEST is set. Override default via RACK_MULTIRACK_VALIDATION_TEST.
    if _is_multirack_log_basename(ap.name):
        if not str(env.get("VALIDATION_TEST", "")).strip():
            mvt = (env.get("RACK_MULTIRACK_VALIDATION_TEST") or "nccl_all_reduce").strip()
            env["VALIDATION_TEST"] = mvt or "nccl_all_reduce"
    try:
        proc = subprocess.run(
            [str(script), "--probe-json", str(ap)],
            cwd=str(ap.parent),
            env=env,
            capture_output=True,
            text=True,
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
_LOG_ANALYSIS_CACHE_PATH = APP_DIR / "var" / "cache" / "log_analysis_cache.json"


def _glob_one_rack_nemo_logs_for_rack_status(log_dir: Path) -> list[Path]:
    """Same basename rules as rack_status.sh in log_dir (root only, not failed/)."""
    root = log_dir.resolve()
    out: list[Path] = []
    for p in sorted(root.glob("*.txt"), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        if not _nemo_nccl_in_log_basename(p.name):
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
    try:
        proc = subprocess.run(
            ["squeue", "-u", user, "-h", "-o", "%i"],
            capture_output=True,
            text=True,
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
        rack = _rack_id_from_nemo_1rack_filename(p.name) or "?"
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
    n = _env_int("RACK_EXPORT_NODES_PER_RACK", 18)
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


@app.route("/api/parser-export-excel")
def api_parser_export_excel():
    """Run parser.sh --json on export-selected logs (root only; one successful 1Rack log per rack with nemo=done), then .xlsx."""
    if os.environ.get("RACK_PARSER_EXPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "export disabled (RACK_PARSER_EXPORT_DISABLED)"}), 403
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
                    "(root only; NEMO 1Rack: one successful log per rack with nemo=done in rack_status, "
                    "unless RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER is set)"
                ),
            }
        ), 404
    parser = _parser_path()
    if not parser.is_file():
        return jsonify({"ok": False, "error": f"parser.sh not found: {parser}"}), 500
    rel_args: list[str] = []
    for p in paths:
        rel_args.append(str(p.resolve().relative_to(log_dir.resolve())))
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
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
    return Response(
        body,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Export-Log-Count": str(len(paths)),
        },
    )


@app.route("/api/parser-export-csv")
def api_parser_export_csv_legacy():
    """Backward-compatible redirect; exports are now multi-sheet Excel."""
    return redirect(url_for("api_parser_export_excel", **request.args), code=307)


@app.route("/api/export-logs-zip")
def api_export_logs_zip():
    """Zip exportable matching .txt under log_dir root (same rules as parser Excel export)."""
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
                    "(root only; NEMO 1Rack: one successful log per rack with nemo=done in rack_status, "
                    "unless RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER is set)"
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
    if not script.is_file():
        return None, (jsonify({"ok": False, "error": f"validation script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(30, _env_int("RACK_VALIDATION_TIMEOUT", 600))
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
        proc = subprocess.run(
            cmd,
            cwd=str(log_dir),
            env=os.environ.copy(),
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
    if not script.is_file():
        return None, (jsonify({"ok": False, "error": f"run-nemo-xrack script not found: {script}"}), 500)
    arg = ",".join(specs)
    cmd = ["bash", str(script), arg]
    display_cmd = "./" + script.name + " " + shlex.quote(arg)
    timeout = max(300, _env_int("RACK_NEMO_XRACK_TIMEOUT", 7200))
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
    if not script.is_file():
        return None, (jsonify({"ok": False, "error": f"run-llama.sh not found: {script}"}), 500)
    inner = f"module load slurm && exec bash {shlex.quote(str(script))} {shlex.quote(nodes_spec)}"
    cmd = ["bash", "-lc", inner]
    display_cmd = f"module load slurm && ./{script.name} {shlex.quote(nodes_spec)}"
    timeout = max(120, _env_int("RACK_LLAMA_TIMEOUT", 7200))
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
    if not script.is_file():
        return None, (jsonify({"ok": False, "error": f"stress script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(120, _env_int("RACK_MNUBERGEMM_STRESS_TIMEOUT", 86400))
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
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
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
    if not script.is_file():
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
) -> Iterator[bytes]:
    """Yield merged stdout/stderr from a subprocess until exit (or timeout)."""
    cmd_run = list(cmd)
    if os.environ.get(linebuffer_env_var, linebuffer_default).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        stdbuf = shutil.which("stdbuf")
        if stdbuf:
            cmd_run = [stdbuf, "-oL", "-eL", *cmd_run]
    env = os.environ.copy()
    proc = subprocess.Popen(
        cmd_run,
        cwd=cwd,
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
    timeout_sec = max(30, _env_int("RACK_SYSINFO_TIMEOUT", 1800))
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        str(prep["log_dir"]),
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
    if not script.is_file():
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
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        str(prep["log_dir"]),
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
        proc = subprocess.run(
            prep["cmd"],
            cwd=str(prep["log_dir"]),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=max(30, _env_int("RACK_SYSINFO_TIMEOUT", 1800)),
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


@app.route("/api/run-sysinfo-stream", methods=["POST"])
def api_run_sysinfo_stream():
    """Stream merged stdout/stderr from sysinfo.sh as text/plain (UTF-8)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None

    return Response(
        stream_with_context(_sysinfo_stream_generator(prep)),
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
    return Response(
        stream_with_context(_nvswitch_audit_stream_generator(prep)),
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
                            f"no log *1Rack-{rk}-*.txt under {log_dir} or {log_dir / 'failed'}"
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
    if os.environ.get("RACK_TAIL_LINEBUFFER", "1").strip().lower() in ("1", "true", "yes", "on"):
        stdbuf = shutil.which("stdbuf")
        if stdbuf:
            cmd = [stdbuf, "-oL", "-eL", *cmd]
    env = os.environ.copy()
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


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _auth_enabled():
        return redirect(url_for("index"))
    au = _auth_user()
    psvc = _pam_service()
    default_next = url_for("index")
    if _session_ok():
        dest = _safe_next(request.args.get("next")) or default_next
        return redirect(dest or default_next)
    if request.method == "POST":
        pw_raw = request.form.get("password")
        if pw_raw is None:
            pw = ""
        elif isinstance(pw_raw, str):
            pw = pw_raw
        else:
            pw = str(pw_raw)
        next_url = _safe_next(request.form.get("next")) or default_next
        if not next_url:
            next_url = default_next
        try:
            ok = _pam_verify(au, pw, psvc)
        except RuntimeError as e:
            return (
                render_template(
                    "login.html",
                    error=False,
                    next="",
                    auth_user=au,
                    pam_service=psvc,
                    fatal=str(e),
                    project_name=_project_name(),
                ),
                500,
            )
        if ok:
            session.clear()
            session.permanent = True
            session["rack_web_auth"] = "1"
            return redirect(next_url)
        return render_template(
            "login.html",
            error=True,
            next=_safe_next(request.form.get("next")) or "",
            auth_user=au,
            pam_service=psvc,
            project_name=_project_name(),
        )
    next_q = _safe_next(request.args.get("next"))
    return render_template(
        "login.html",
        error=False,
        next=next_q or "",
        auth_user=au,
        pam_service=psvc,
        project_name=_project_name(),
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    dr = _env_int("RACK_STATUS_DEFAULT_REFRESH", 30)
    if not (5 <= dr <= 86400):
        dr = 30
    pwr_sleep = max(0, _env_int("RACK_POWER_RESET_SLEEP_SEC", 10))
    html = render_template(
        "index.html",
        default_log_dir=str(_default_log_dir()),
        default_refresh=dr,
        power_reset_sleep_sec=pwr_sleep,
        validation_ct_inner=_validation_ct_inner(),
        validation_script_name=_validation_script_path().name,
        mnubergemm_stress_script_name=_mnubergemm_stress_script_path().name,
        sysinfo_script_name=_sysinfo_script_path().name,
        nvswitch_audit_script_name=_nvswitches_rack_audit_script_path().name,
        nvswitch_sdn_reset_script_name=_nvswitch_sdn_reset_script_path().name,
        nvswitch_system_reset_script_name=_nvswitch_system_reset_script_path().name,
        imex_reset_script_name=_imex_reset_script_path().name,
        imex_check_script_name=_imex_check_script_path().name,
        nemo_xrack_script_name=_nemo_xrack_script_path().name,
        run_llama_script_name=_run_llama_script_path().name,
        gpu_reset_script_name=_gpu_reset_script_path().name,
        hw_check_script_name=_hw_check_pdsh_script_path().name,
        auth_required=_auth_enabled(),
        grafana_uri=_rack_web_grafana_uri(),
        grafana_port=_rack_web_grafana_port(),
        grafana_embed_disabled=_truthy_env("RACK_WEB_GRAFANA_EMBED_DISABLED"),
        project_name=_project_name(),
    )
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


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


@app.route("/api/status")
def api_status():
    _start_cmsh_rack_list_warm_thread()
    _ensure_rack_status_background_thread()
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
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
