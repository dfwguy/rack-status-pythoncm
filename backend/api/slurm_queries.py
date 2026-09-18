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
"""Read-only Slurm queries: squeue, sinfo -R, and recent finished jobs from sacct.

Three views of the same scheduler, kept together because they share the module-load semantics and
the timeout knob, and because the interesting one only makes sense next to the others:

``sacct`` exists here because ``squeue`` drops a job the instant it ends -- the "blink and you miss
it" gap. The Scheduler panel needs finished jobs (success *and* failure) to still be listed with
their log, so it reads the accounting DB instead.

Every command goes through the platform provider's own binary and ``host_exec.run_slurm_on_host``,
which applies the ``module load slurm`` dance unless RACK_SLURM_SKIP_MODULE_LOAD says otherwise. A
timeout returns ok=false with the elapsed budget rather than raising: a slow controller is a
reportable condition, not a 500.
"""
from __future__ import annotations

import logging
import os

from audit.actions import action
from flask import Blueprint, jsonify, request
from slurm_query import _run_sacct_recent, _run_sinfo_r, _run_squeue

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("slurm_queries", __name__, url_prefix="")


@bp.route("/api/scheduler-jobs", methods=["GET"])
@action(get="scheduler.queue:list")
def api_slurm_squeue():
    """Run ``module load slurm && squeue`` (unless ``RACK_SLURM_SKIP_MODULE_LOAD``) for the dashboard host."""
    if os.environ.get("RACK_SLURM_SQUEUE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "squeue disabled (RACK_SLURM_SQUEUE_DISABLED)"}), 403
    try:
        result = _run_squeue()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)


@bp.route("/api/scheduler-history", methods=["GET"])
@action(get="scheduler.queue:list")
def api_slurm_sacct_recent():
    """Recent finished Slurm jobs via ``sacct`` — query ``limit`` (1-50, default 10) and ``state`` (default COMPLETED)."""
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
@action(get="scheduler.queue:list")
def api_slurm_sinfo_r():
    """Run ``module load slurm && sinfo -R --long`` (unless ``RACK_SLURM_SKIP_MODULE_LOAD``) for the dashboard host."""
    if os.environ.get("RACK_SLURM_SINFO_R_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "sinfo -R --long disabled (RACK_SLURM_SINFO_R_DISABLED)"}), 403
    try:
        result = _run_sinfo_r()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)
