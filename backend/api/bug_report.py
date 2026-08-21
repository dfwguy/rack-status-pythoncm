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
"""nvidia-bug-report collection: run it across a rack, then list and serve what landed.

Fan out ``nvidia-bug-report.sh`` over pdsh, then let an operator find and download the results. The whole
point is a file an operator attaches to a support case, so the awkward parts are all about getting bytes
back from a host this process cannot read directly.

The safety-relevant pieces, which are not decoration:

* ``_BUG_REPORT_BASENAME_RE``, ``_BUG_REPORT_SUBDIR_RE`` and friends constrain what
  ``api_bug_report_file`` will serve. The route takes a name from the browser and reads it off the
  execution host, so without those patterns it is an arbitrary remote file read.
* ``_parse_bug_report_paths_from_pdsh`` reads paths out of pdsh's per-host output rather than guessing
  them from a template, because each node names its own archive.
* ``_ssh_cat_remote_bytes`` exists because the file lives on a node this process may only reach through
  the head; a bounded ``cat`` over the existing connection is how it arrives.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shlex
import subprocess
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import host_exec
from audit.actions import action
from config_env import _env_int
from core import rpath as _rpath
from flask import Blueprint, jsonify, request, send_file
from job_registry import (
    _reset_job_purge_later,
    _reset_jobs,
    _reset_jobs_lock,
)
from naming import _normalize_rack_id
from node_targets import _expand_pdsh_w_slurm_nodenames
from paths import _bug_report_local_base_path, _bug_report_remote_dir
from platforms import get_platform as _get_platform
from results_ingest import _lake_push_job
from validation_launch import merge_ct_individuals_for_power

from ._common import operator_from_request as _operator_from_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("bug_report", __name__, url_prefix="")


def run_bug_report_pdsh(pdsh_w: str) -> dict:
    """Run nvidia-bug-report.sh on each host via pdsh (same -w pattern as GPU reset)."""
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    rq = shlex.quote(_bug_report_remote_dir())
    inner = f"mkdir -p {rq}/$HOSTNAME && cd {rq}/$HOSTNAME && nvidia-bug-report.sh"
    fan = max(1, _env_int("RACK_BUG_REPORT_PDSH_FANOUT", 32))
    return host_exec.run_pdsh_job(
        _get_platform().pdsh_cmd(w, inner, fanout=fan),
        pdsh_w=w,
        # Hours, not minutes: nvidia-bug-report.sh is slow and runs on every node at once.
        timeout=max(120, _env_int("RACK_BUG_REPORT_TIMEOUT", 10800)),
        dry_run=os.environ.get("RACK_BUG_REPORT_DRY_RUN", "").strip().lower()
        in ("1", "true", "yes"),
        dry_run_label="RACK_BUG_REPORT_DRY_RUN",
    )


_BUG_REPORT_PDSH_PATH_RE = re.compile(
    r"^([a-z0-9][a-z0-9.-]*):\s*(/\S+\.log\.gz)\s*$",
    re.IGNORECASE,
)


_BUG_REPORT_BASENAME_RE = re.compile(r"^nvidia-bug-report.*\.log\.gz$", re.IGNORECASE)


_NVLMAPPER_ZIP_BASENAME_RE = re.compile(r"^nvlmapper\.[A-Za-z0-9][A-Za-z0-9._-]*\.zip$", re.IGNORECASE)


_NVLMAPPER_TABLE_BASENAME_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._\-]*\.(csv|xls)$", re.IGNORECASE
)


_REPORT_EXCEL_BASENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]*\.xlsx$", re.IGNORECASE)


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


@bp.route("/api/bug-report-async", methods=["POST"])
@action("support:bug_report", variant="async")
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


@bp.route("/api/bug-report-bundle", methods=["GET"])
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


@bp.route("/api/bug-report-inventory", methods=["GET"])
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


@bp.route("/api/bug-report-file", methods=["GET"])
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
