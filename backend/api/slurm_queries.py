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
"""Read-only scheduler views with historical Slurm URL aliases.

Three views of the same scheduler, kept together because they share the module-load semantics and
the timeout knob, and because the interesting one only makes sense next to the others:

``sacct`` exists here because ``squeue`` drops a job the instant it ends -- the "blink and you miss
it" gap. The Scheduler panel needs finished jobs (success *and* failure) to still be listed with
their log, so it reads the accounting DB instead.

The provider-neutral URLs return PythonCM registry and PhysicalNode state on Nscale. Historical
Slurm-named aliases are rejected by the Nscale request guard before these handlers run. Other
deployments retain the original provider behavior. A dependency timeout is reportable state,
not a 500.
"""
from __future__ import annotations

import logging
import os
import re

from flask import Blueprint, jsonify, request
from slurm_query import _run_sacct_recent, _run_sinfo_nodes, _run_sinfo_r, _run_squeue

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("slurm_queries", __name__, url_prefix="")


@bp.route("/api/scheduler-jobs", methods=["GET"])
@bp.route("/api/slurm-squeue", methods=["GET"])
def api_scheduler_jobs():
    """Return active commands from the selected scheduler provider."""
    if os.environ.get("RACK_SLURM_SQUEUE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "squeue disabled (RACK_SLURM_SQUEUE_DISABLED)"}), 403
    try:
        result = _run_squeue()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


@bp.route("/api/scheduler-history", methods=["GET"])
@bp.route("/api/slurm-sacct-recent", methods=["GET"])
def api_scheduler_history():
    """Return recent terminal commands from the selected scheduler provider."""
    if os.environ.get("RACK_SLURM_SACCT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "sacct disabled (RACK_SLURM_SACCT_DISABLED)", "jobs": []}), 403
    try:
        limit = int((request.args.get("limit") or "10").strip())
    except (TypeError, ValueError):
        limit = 10
    limit = min(50, max(1, limit))
    state = (request.args.get("state") or "COMPLETED").strip() or "COMPLETED"
    try:
        result = _run_sacct_recent(limit=limit, state=state)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e), "jobs": []}), 500
    return jsonify(result)


@bp.route("/api/scheduler-node-reasons", methods=["GET"])
@bp.route("/api/slurm-sinfo-r", methods=["GET"])
def api_scheduler_node_reasons():
    """Return unavailable nodes and reasons from the selected scheduler provider."""
    if os.environ.get("RACK_SLURM_SINFO_R_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "sinfo -R --long disabled (RACK_SLURM_SINFO_R_DISABLED)"}), 403
    try:
        result = _run_sinfo_r()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


@bp.route("/api/scheduler-node-status", methods=["GET"])
def api_scheduler_node_status():
    """Return live Slurm state and reason for the requested rack node names."""
    raw_nodes = (request.args.get("nodes") or "").strip()
    node_names = list(dict.fromkeys(name.strip() for name in raw_nodes.split(",") if name.strip()))
    if not node_names:
        return jsonify({"ok": False, "error": "nodes is required", "nodes": []}), 400
    if len(node_names) > 512:
        return jsonify({"ok": False, "error": "at most 512 nodes may be queried", "nodes": []}), 400
    invalid = [name for name in node_names if not re.fullmatch(r"[A-Za-z0-9._-]+", name)]
    if invalid:
        return jsonify({"ok": False, "error": f"invalid node name: {invalid[0]}", "nodes": []}), 400
    try:
        result = _run_sinfo_nodes(node_names)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e), "nodes": []}), 500
    return jsonify(result)
