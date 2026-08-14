# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Help content: the knowledge registry served to the SPA and the AI.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Moved out of ``app.py``. The blueprint uses ``url_prefix=""`` so every route keeps the exact
path it always had -- a migration must not change a URL, and the route snapshot proves it.
"""

from __future__ import annotations

import logging

import help_content
from flask import Blueprint, jsonify

_logger = logging.getLogger("rack_web.api.help")

bp = Blueprint("help", __name__, url_prefix="")


@bp.route("/api/help/content", methods=["GET"])
def api_help_content():
    """Return the full app knowledge registry (mirror of the frontend content)."""
    entries = help_content.get_help_content()
    return jsonify({"ok": True, "count": len(entries), "entries": entries})


@bp.route("/api/help/content/<content_id>", methods=["GET"])
def api_help_content_entry(content_id: str):
    """Return a single help entry by id."""
    entry = help_content.get_entry(content_id)
    if not entry:
        return jsonify({"ok": False, "error": "Help content not found"}), 404
    return jsonify({"ok": True, "entry": entry})
