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
"""Slurm operations an operator drives from the dashboard: drain, undrain, cancel, read a job log.

The endpoints only. Read commands come from ``slurm_query``, state-changing ones from
``slurm_admin`` -- so which of these routes can alter the cluster is answerable from the import block:
drain/undrain/scancel reach for slurm_admin, everything else does not.

Drain and undrain accept a rack and expand it to a node target through ``node_targets``, never through
a ``<rack>-[01-NN]`` guess: on sites where the hostname is not that shape the guess produces targets
that do not resolve, and scontrol then reports a Slurm error rather than a naming mistake.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import shlex
from pathlib import Path

import host_exec
from audit.actions import action
from config_env import _eget, _env_int
from core import SLURM_JOB_ID_RE as _SLURM_SCANCEL_JOB_ID_RE
from core import slurm_job_base_id as _slurm_job_base_id
from flask import Blueprint, Response, jsonify, request, send_file
from job_registry import _track_sync_op
from naming import _normalize_rack_id
from node_targets import validation_target_for_rack
from paths import _resolve_log_dir
from platforms import get_platform as _get_platform
from slurm_admin import _run_scancel, _run_scontrol_node_update, _run_scontrol_reboot
from slurm_query import (
    _resolve_slurm_job_stdout_path,
    _run_scontrol_show_job,
    _slurm_job_log_path,
)

from ._common import operator_from_request as _operator_from_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("slurm_ops", __name__, url_prefix="")


_SLURM_DRAIN_REASON_RE = re.compile(r"^[A-Za-z0-9 _.,:/()\-]{1,256}$")


def _slurm_nodename_target_from_body(body: dict) -> tuple[str | None, str | None]:
    """Parse rack (+ optional target) into Slurm nodename=… value; (None, err) on error."""
    rack = (body.get("rack") or "").strip()
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, "target must be a string"
    if not rack and not str(tgt_ov or "").strip():
        return None, "rack or an explicit node target is required"
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, str(e)
    return target, None


@bp.route("/api/scheduler-drain", methods=["POST"])
@action("scheduler.node:drain")
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
    rack = (body.get("rack") or "").strip()
    payload: dict = {"ok": ok, "rack": _normalize_rack_id(rack) if rack else "", "target": target, **result}
    if ok:
        payload["message"] = f"Undrain (state=resume) applied to {len(target.split(','))} node(s)."
    else:
        err = f"{result.get('stderr') or ''} {result.get('stdout') or ''}".strip()
        # Say WHAT went wrong, not just "see stdout/stderr" — the operator cannot act on that.
        if "Invalid node state" in err:
            payload["error"] = (
                "scontrol rejected the state change — usually because none of these nodes are "
                "drained or down, so there is nothing to undrain. Check Slurm state first "
                "(sinfo -R). No change was made.")
        elif "Invalid node name" in err:
            payload["error"] = "scontrol did not recognise one or more node names. No change was made."
        elif "Access/permission denied" in err or "Invalid user" in err:
            payload["error"] = "scontrol denied the update — the service account lacks Slurm operator rights."
        else:
            payload["error"] = f"scontrol update failed: {err[:300] or 'no output'}"
    return jsonify(payload), (200 if ok else 502)


@bp.route("/api/scheduler-reboot", methods=["POST"])
@action("scheduler.node:reboot")
def api_slurm_reboot():
    """POST JSON {\"target\": \"host1,host2\"} (or {\"rack\": …}) — Slurm-managed node reboot.

    ``scontrol reboot ASAP nextstate=RESUME``: the nodes drain now, reboot as soon as their jobs
    end, and return to service once they register again. Nothing running is interrupted.
    """
    if os.environ.get("RACK_SLURM_NODES_STATE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "Slurm node state changes disabled (RACK_SLURM_NODES_STATE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    target, err = _slurm_nodename_target_from_body(body)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    raw_reason = body.get("reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        return jsonify({"ok": False, "error": "reason must be a string"}), 400
    r = (raw_reason if isinstance(raw_reason, str) else "").strip() or "reboot requested from Rack Status"
    if not _SLURM_DRAIN_REASON_RE.fullmatch(r):
        return jsonify(
            {"ok": False, "error": "invalid reason (1–256 chars: letters, digits, space, _.,:/()- only)"}
        ), 400
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "slurm_reboot", _operator_from_request()):
            result = _run_scontrol_reboot(target, r)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = bool(result.get("ok"))
    payload: dict = {"ok": ok, "target": target, **result}
    if ok:
        payload["message"] = (
            f"Reboot queued for {len(target.split(','))} node(s): they drain now, reboot when idle "
            "and return to service afterwards."
        )
    else:
        err = f"{result.get('stderr') or ''} {result.get('stdout') or ''}".strip()
        if "Access/permission denied" in err or "Invalid user" in err:
            payload["error"] = "scontrol denied the reboot — the service account lacks Slurm operator rights."
        else:
            payload["error"] = f"scontrol reboot failed: {err[:300] or 'no output'}"
    return jsonify(payload), (200 if ok else 502)


@bp.route("/api/scheduler-resume", methods=["POST"])
@action("scheduler.node:undrain")
def api_slurm_undrain():
    """POST JSON {\"rack\": \"p34-r07\"} — ``module load`` then scontrol update nodename=… state=idle."""
    if os.environ.get("RACK_SLURM_NODES_STATE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "Slurm drain/undrain disabled (RACK_SLURM_NODES_STATE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    target, err = _slurm_nodename_target_from_body(body)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    rk_for_lock = (body.get("rack") or "").strip()
    rk_norm = _normalize_rack_id(rk_for_lock) if rk_for_lock else ""

    # Ask Slurm WHICH of these nodes is actually drained/down before touching anything.
    # Slurm rejects BOTH `state=idle` and `state=resume` with "Invalid node state specified" when
    # the target set contains no drained/down node — an operator clicking Undrain on a healthy rack
    # got a red "scontrol update failed" that looked like a broken cluster. Nothing was ever wrong.
    drained: list[str] = []
    state_err = ""
    try:
        # Resolve `sinfo` through the platform layer, exactly as _run_sinfo_r() does. A
        # bare `sinfo` is not enough: a site may set RACK_SLURM_SKIP_MODULE_LOAD=1 and pin
        # RACK_SLURM_SINFO to an absolute path, leaving nothing to put it on PATH. The
        # probe then died with "sinfo: command not found" and its empty stdout read as
        # "no drained nodes", so Undrain returned a green 200 having done nothing.
        plat = _get_platform()
        probe = host_exec.run_slurm_on_host(
            plat.sinfo_cmd(f"-h -N -o '%N %t' -n {shlex.quote(target)}"),
            timeout=max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120)),
        )
        # A probe that FAILED is not evidence that every node is healthy. Treat a
        # non-zero return as unknown and fall through to the update below, rather than
        # reporting "nothing to undrain" -- a check that cannot read its input must
        # never report a healthy state.
        if probe.returncode != 0:
            state_err = (
                f"sinfo probe failed (rc={probe.returncode}): "
                f"{(probe.stderr or '').strip()[:200]}"
            )
        for line in (probe.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and any(
                k in parts[1].lower() for k in ("drain", "down", "fail", "inval")
            ):
                drained.append(parts[0])
    except Exception as e:  # noqa: BLE001 - fall through to the plain update
        state_err = str(e)

    if not drained and not state_err:
        # Nothing to do, and saying so is the honest answer — not an error.
        return jsonify({
            "ok": True, "rack": rk_norm, "target": target, "changed": 0,
            "message": (
                f"Nothing to undrain — all {len(target.split(','))} node(s) are already "
                "schedulable (no drain/down state). No change was made."
            ),
        }), 200

    apply_to = ",".join(drained) if drained else target
    try:
        with _track_sync_op(rk_for_lock, "slurm_undrain", _operator_from_request()):
            # `state=resume` is the canonical verb for returning a DOWN/DRAINED node to service.
            result = _run_scontrol_node_update(apply_to, ["state=resume"])
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    ok = bool(result.get("ok"))
    payload: dict = {"ok": ok, "rack": rk_norm, "target": apply_to,
                     "changed": len(drained), **result}
    if ok:
        payload["message"] = (
            f"Undrained {len(drained) or 'all targeted'} node(s): {apply_to[:200]}")
    else:
        err = f"{result.get('stderr') or ''} {result.get('stdout') or ''}".strip()
        if "Invalid node state" in err:
            payload["error"] = ("Slurm rejected the state change — none of these nodes are "
                                "drained or down. No change was made.")
        elif "Invalid node name" in err:
            payload["error"] = "Slurm did not recognise one or more node names. No change was made."
        elif "denied" in err.lower():
            payload["error"] = "Slurm denied the update — the service account lacks operator rights."
        else:
            payload["error"] = f"scontrol update failed: {err[:300] or 'no output'}"
    return jsonify(payload), (200 if ok else 502)


@bp.route("/api/scheduler-job-log-path", methods=["GET"])
@action(get="scheduler.job:read")
def api_slurm_job_log_path():
    """Resolve a finished job's stdout log path (for the Recent-jobs 'open log' action)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not _slurm_job_base_id(job_id):
        return jsonify({"ok": False, "error": "job_id must be a Slurm job id"}), 400
    try:
        log_dir = _resolve_log_dir((request.args.get("log_dir") or "").strip() or None)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        path, source = _slurm_job_log_path(job_id, log_dir)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not path:
        return jsonify(
            {"ok": False, "error": f"no stdout log found for job {job_id} (aged out of scontrol; no matching file under log_dir)"}
        ), 404
    return jsonify({"ok": True, "job_id": _slurm_job_base_id(job_id), "path": path, "source": source})


@bp.route("/api/scheduler-cancel", methods=["POST"])
@action("scheduler.job:cancel")
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


@bp.route("/api/scheduler-job-detail", methods=["GET"])
@action(get="scheduler.job:read")
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


@bp.route("/api/scheduler-job-output", methods=["GET"])
@action(get="scheduler.job:read")
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
    # The stdout file lives on the execution host, so size and content must be asked
    # of the connection. Calling Path.stat()/read_bytes() here read the container's
    # own filesystem and raised AttributeError on the remote PurePosixPath.
    st = host_exec.stat(lp)
    if st is None:
        return jsonify({"ok": False, "error": "path does not exist"}), 404
    sz = int(st.size)
    if sz > max_inline:
        return jsonify(
            {
                "ok": True,
                "too_large": True,
                "size_bytes": sz,
                "filename": lp.name,
                "max_inline_bytes": max_inline,
                "download_hint": "Use GET /api/scheduler-job-output-download with the same query parameters.",
            }
        )
    text, _meta = host_exec.read_text(lp, whole_bytes=max_inline)
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


@bp.route("/api/scheduler-job-output-download", methods=["GET"])
@action(get="scheduler.job:read")
def api_slurm_job_stdout_download():
    """Download job stdout file (same path rules as ``/api/scheduler-job-output``)."""
    path_raw = (request.args.get("path") or "").strip()
    if not path_raw:
        return jsonify({"ok": False, "error": "path is required"}), 400
    try:
        lp = _resolve_slurm_job_stdout_path(path_raw, request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not host_exec.is_file(lp):
        return jsonify({"ok": False, "error": "not a file"}), 404
    if not isinstance(lp, Path):
        # Remote (ssh mode): the file is on the execution host, so send_file() cannot
        # reach it. Same transfer as api_rack_log_download().
        cp = host_exec.run_on_host(f"base64 -w0 {shlex.quote(str(lp))}")
        if cp.returncode != 0 or not (cp.stdout or "").strip():
            return jsonify(
                {"ok": False, "error": f"cannot read remote log: {lp} {(cp.stderr or '').strip()}"}
            ), 404
        try:
            blob = base64.b64decode((cp.stdout or "").strip())
        except ValueError as e:  # binascii.Error subclasses ValueError
            return jsonify({"ok": False, "error": f"decode failed: {e}"}), 500
        resp = Response(blob, mimetype="text/plain; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{lp.name}"'
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return send_file(
        str(lp),
        as_attachment=True,
        download_name=lp.name,
        mimetype="text/plain; charset=utf-8",
        max_age=0,
    )
