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
"""Single-node Llama log listing.

Paths come back from the execution host, so they are ``PurePosixPath`` and are inspected through
``host_exec`` rather than the local filesystem -- the log directory usually belongs to the head node,
not to this container.
"""
from __future__ import annotations

from pathlib import PurePosixPath

import host_exec
from audit.actions import action
from core import rpath as _rpath
from flask import Blueprint, jsonify, request
from paths import _resolve_log_dir

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("sn_llama_logs", __name__, url_prefix="")


@bp.route("/api/sn-llama-logs", methods=["GET"])
@action(get="logs:read")
def api_sn_llama_logs():
    """List ``*.txt`` under ``log_dir/sn-llama/`` (newest first), optional ``rack`` substring filter."""
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack_f = (request.args.get("rack") or "").strip().lower()
    d = _rpath(log_dir) / "sn-llama"
    # Single listing through the connection: name + mtime is all this endpoint
    # reports, and an absent directory comes back empty.
    entries = host_exec.stat_dir(d, patterns=("*.txt",))
    out: list[dict[str, object]] = []
    for path_s in sorted(entries, key=lambda s: entries[s].mtime, reverse=True)[:200]:
        name = PurePosixPath(path_s).name
        if rack_f and rack_f not in name.lower():
            continue
        out.append({"name": name, "mtime": int(entries[path_s].mtime)})
    return jsonify({"ok": True, "files": out})
