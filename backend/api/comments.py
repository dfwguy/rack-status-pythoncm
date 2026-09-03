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
"""Reading and writing operator comments on racks and multirack runs.

Two routes over ``comments_store``. The PUT is per-rack or per-multirack-run, and an empty body is a
delete rather than an empty comment, because that is what the UI's clear button sends.
"""
from __future__ import annotations

import logging

from audit.actions import action
from comments_store import (
    _multirack_comment_storage_key,
    read_comments,
    set_comment_for_multirack_log,
    set_comment_for_rack,
)
from flask import Blueprint, jsonify, request
from naming import _normalize_rack_id

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("comments", __name__, url_prefix="")


@bp.route("/api/comments", methods=["GET"])
@action(get="comments:read")
def api_comments_list():
    return jsonify({"ok": True, "comments": read_comments()})


@bp.route("/api/comments/<rack_id>", methods=["PUT", "DELETE"])
@action(put="comments:update", delete="comments:delete")
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
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "invalid rack id or multirack log basename (use pN-rMM or mr:<log>.txt)",
                    }
                ),
                400,
            )
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
