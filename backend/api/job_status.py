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
"""Status of a launched async job, one endpoint per tool family.

Every route here is the same shape: look up a job id in its registry under the registry's lock and
report the row. They share this module because they share the registries -- ``_stress_jobs`` alone
backs six of them (clusterkit, nvloom, nccl-fullrack, mnubergemm, llmb-nccl, exemplar-llama) -- and
splitting them per URL would mean six modules reaching into the same mutable dict.

The launch halves still live in ``app.py``: they need the ``_prepare_*`` builders and worker threads,
which have not moved yet. The status halves are pure reads and could go first.

These registries are in-process only. A status poll for a job id the app has never seen, or has
already purged, is a 404 rather than an error -- the durable record of a run is
``validation_activities.py``.
"""
from __future__ import annotations

import validation_activities
from flask import Blueprint, jsonify, request
from job_registry import (
    _llama_jobs,
    _llama_jobs_lock,
    _reset_jobs,
    _reset_jobs_lock,
    _stress_jobs,
    _stress_jobs_lock,
    _validation_jobs,
    _validation_jobs_lock,
    _xrack_jobs,
    _xrack_jobs_lock,
)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("job_status", __name__, url_prefix="")


def _job_payload(job_id: str, registry: dict, lock) -> dict | None:
    """Read the live row, then the durable activity after restart or purge."""
    with lock:
        row = registry.get(job_id)
        payload = dict(row) if row is not None else None
    if payload is None:
        payload = validation_activities.get_activity(job_id)
    return payload


@bp.route("/api/reset-job-status", methods=["GET"])
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


@bp.route("/api/run-validation-status", methods=["GET"])
def api_run_validation_status():
    """Return state for a job started via /api/run-validation-async (phase, stdout, …)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _validation_jobs, _validation_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/run-nemo-xrack-status", methods=["GET"])
def api_run_nemo_xrack_status():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _xrack_jobs, _xrack_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/run-llama-status", methods=["GET"])
def api_run_llama_status():
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _llama_jobs, _llama_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/run-mnubergemm-stress-status", methods=["GET"])
def api_run_mnubergemm_stress_status():
    """Return state for a job started via /api/run-mnubergemm-stress-async."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _stress_jobs, _stress_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/run-nccl-fullrack-status", methods=["GET"])
def api_run_nccl_fullrack_status():
    """Return state for a job started via /api/run-nccl-fullrack-async (shared stress job store)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _stress_jobs, _stress_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/run-nvloom-status", methods=["GET"])
def api_run_nvloom_status():
    """Return state for a job started via /api/run-nvloom-async (same store as mnubergemm stress)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _stress_jobs, _stress_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/run-clusterkit-status", methods=["GET"])
def api_run_clusterkit_status():
    """Return state for a job started via /api/run-clusterkit-async."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _stress_jobs, _stress_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)


@bp.route("/api/run-llmb-nccl-status", methods=["GET"])
def api_run_llmb_nccl_status():
    """Return state for a job started via /api/run-llmb-nccl-async."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    payload = _job_payload(job_id, _stress_jobs, _stress_jobs_lock)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)
