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
"""NVFTP ``var/cache`` sync status, manual runs and SFTP probes.

All behaviour lives in the top-level ``nvftp_cache_sync`` module; these routes only parse and
report. Credentials never appear in a response -- the status payload is deliberately secret-free.
"""
from __future__ import annotations

import nvftp_cache_sync
from audit.actions import action
from flask import Blueprint, jsonify, request

# Empty prefix: these routes carry the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("nvftp_sync", __name__, url_prefix="")


@bp.route("/api/nvftp-sync/status", methods=["GET"])
@action(get="nvftp:read")
def api_nvftp_sync_status():
    """NVFTP var/cache sync status (no secrets)."""
    nvftp_cache_sync.ensure_background_thread()
    return jsonify({"ok": True, **nvftp_cache_sync.sync_status()})


@bp.route("/api/nvftp-sync/run", methods=["POST"])
@action("nvftp:sync")
def api_nvftp_sync_run():
    """Trigger an immediate var/cache upload. JSON body ``{\"force\": true}`` re-uploads every file."""
    body = request.get_json(silent=True) or {}
    force = body.get("force") in (True, 1, "1", "true", "yes")
    out = nvftp_cache_sync.run_sync(force=force)
    code = 200 if out.get("ok") else 502
    return jsonify(out), code


@bp.route("/api/nvftp-sync/test", methods=["POST"])
@action("nvftp:test")
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
