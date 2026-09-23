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
"""Hardware and NVLink/Network deep-audit runs.

A thin read-only view over the workflow engine: a deep audit *is* a workflow run, filtered to the
audit workflow ids and projected down to what the rack UI shows. Nothing here owns state.
"""
from __future__ import annotations

import workflow_engine
from audit.actions import action
from core import DEEP_AUDIT_WORKFLOWS
from flask import Blueprint, jsonify, request

# Empty prefix: these routes carry the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("deep_audits", __name__, url_prefix="")


@bp.route("/api/deep-audits", methods=["GET"])
@action(get="audit.deep:list")
def api_deep_audits_list():
    """Recent Hardware and NVLink/Network deep-audit runs, optionally for one target."""
    rack = (request.args.get("rack") or "").strip()
    target = (request.args.get("target") or "").strip()
    kind = (request.args.get("kind") or "").strip().lower()
    workflow_id = {
        "hardware": "hardware-deep-audit",
        "network": "nvlink-network-deep-audit",
        "nvlink": "nvlink-network-deep-audit",
    }.get(kind, "")
    try:
        limit = max(1, min(100, int(request.args.get("limit") or "30")))
    except ValueError:
        return jsonify({"ok": False, "error": "limit must be an integer"}), 400
    out = []
    for row in workflow_engine.list_runs():
        if row.get("workflow_id") not in DEEP_AUDIT_WORKFLOWS:
            continue
        if workflow_id and row.get("workflow_id") != workflow_id:
            continue
        full = workflow_engine.get_run_state(str(row.get("run_id") or "")) or row
        if full.get("invalidated_at"):
            continue
        params = full.get("params") or {}
        if rack and str(params.get("rack") or "") != rack:
            continue
        if target and str(params.get("target") or "") != target:
            continue
        out.append({
            "run_id": full.get("run_id"),
            "workflow_id": full.get("workflow_id"),
            "workflow_name": full.get("workflow_name"),
            "state": full.get("state"),
            "rack": str(params.get("rack") or ""),
            "target": str(params.get("target") or ""),
            "started_at": full.get("started_at"),
            "updated_at": full.get("updated_at"),
            "audits": full.get("audits") or {},
            "error": full.get("error") or "",
        })
        if len(out) >= limit:
            break
    return jsonify({"ok": True, "runs": out})


@bp.route("/api/deep-audits/<run_id>", methods=["GET"])
@action(get="audit.deep:read")
def api_deep_audit_get(run_id: str):
    """One deep-audit run with per-node results and automatic Slurm actions."""
    run = workflow_engine.get_run_state((run_id or "").strip())
    if (
        not run
        or run.get("workflow_id") not in DEEP_AUDIT_WORKFLOWS
        or run.get("invalidated_at")
    ):
        return jsonify({"ok": False, "error": "deep audit run not found"}), 404
    return jsonify({"ok": True, "run": run})
