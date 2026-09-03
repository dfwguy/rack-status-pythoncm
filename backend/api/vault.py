# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""NVFTP token vault: read-only access to the stored credential.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Moved out of ``app.py``. The blueprint uses ``url_prefix=""`` so every route keeps the exact
path it always had -- a migration must not change a URL, and the route snapshot proves it.
"""

from __future__ import annotations

import logging

import nvftp_cache_sync
import token_vault
from audit.actions import action
from flask import Blueprint, jsonify, request

_logger = logging.getLogger("rack_web.api.vault")

bp = Blueprint("vault", __name__, url_prefix="")

NVFTP_TOKENS_URL = (
    "https://nvftp.nvidia.com/workspace/1099ff0b-621c-490f-a662-c69583ccabad/tokens"
)


@bp.route("/api/vault/nvftp-token", methods=["GET", "PUT", "DELETE"])
@action(put="vault:set_token", delete="vault:delete_token")
@action(get="vault:read")
def api_vault_nvftp_token():
    """GET status (masked); PUT ``{token}`` to store encrypted; DELETE to clear."""
    if request.method == "GET":
        try:
            st = token_vault.nvftp_token_status()
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 503
        return jsonify({"ok": True, "tokens_url": NVFTP_TOKENS_URL, **st})
    if request.method == "DELETE":
        try:
            token_vault.clear_nvftp_token()
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "configured": False})
    body = request.get_json(silent=True) or {}
    raw = body.get("token")
    if not isinstance(raw, str):
        return jsonify({"ok": False, "error": 'expected JSON {"token": "…"}'}), 400
    try:
        st = token_vault.store_nvftp_token(raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    nvftp_cache_sync.ensure_background_thread()
    return jsonify({"ok": True, "tokens_url": NVFTP_TOKENS_URL, **st})
