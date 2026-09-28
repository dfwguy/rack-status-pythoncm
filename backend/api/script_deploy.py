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
"""Send the shipped script layers to the cluster head.

Separate from `project_scripts.py` because the subject is different and the order matters:
this writes the **shared** layers the head does not have yet, and that module composes a
**project's** copy *from* them. Run the wrong one first on a fresh head and it fails looking
for defaults that were never sent -- which is what the setup wizard used to do.

Neither replaces `bin/deploy-scripts.sh`; see `backend/script_deploy.py` for why a browser
flow cannot use it.
"""
from __future__ import annotations

import logging

import script_deploy
import script_tree
from audit.actions import action
from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

bp = Blueprint("script_deploy", __name__, url_prefix="/api/scripts")


@bp.route("/deploy-preview", methods=["GET"])
@action(get="catalog.scripts:preview_deploy")
def api_script_deploy_preview():
    """What a deploy would send, and where — without touching the cluster.

    A GET with no connection attempt on purpose: the wizard shows this on the step *before*
    the operator has proven SSH works, so a preview that needed the head would be blank
    exactly when it is most wanted.
    """
    try:
        summary = script_deploy.deploy(dry_run=True)
    except script_deploy.UnsafeScript as exc:
        # Surfaced in the preview so the wizard shows it before an operator commits to a
        # deploy, which is the whole reason the preview builds the bundle for real.
        return jsonify({"ok": False, "error": str(exc)}), 500
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({**summary, "layout": script_tree.describe()})


@bp.route("/deploy", methods=["POST"])
@action("catalog.scripts:deploy")
def api_script_deploy():
    """Copy the shipped script layers onto the head node's tree.

    Body::

        {"dry_run": false}

    Idempotent, and additive: `custom/` and `project-*/` are not in the archive, so nothing
    an operator or a project owns can be overwritten by running this again.
    """
    body = request.get_json(silent=True) or {}
    try:
        summary = script_deploy.deploy(dry_run=bool(body.get("dry_run")))
    except script_deploy.UnsafeScript as exc:
        # The app's own tree contains something that must not be shipped to a shared head
        # node. Nothing was sent, and the fix is in this install, not on the cluster.
        _logger.error("script deploy refused: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    except FileNotFoundError as exc:
        # The app's own scripts are missing from the image. Not the cluster's fault, and a
        # 500 rather than a 502 says so.
        return jsonify({"ok": False, "error": str(exc)}), 500

    if not summary.get("ok"):
        _logger.warning("script deploy failed: %s", summary.get("error"))
        # 502: we reached for the head and the head is what did not cooperate.
        return jsonify({**summary, "layout": script_tree.describe()}), 502

    _logger.info(
        "scripts deployed: dest=%s files=%s bytes=%s",
        summary.get("dest"), summary.get("files"), summary.get("bytes"),
    )
    return jsonify({**summary, "layout": script_tree.describe()})
