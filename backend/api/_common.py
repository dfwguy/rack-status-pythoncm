# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Flask-dependent helpers shared across API modules.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``core.py`` deliberately imports no Flask, so anything that builds a response cannot live there.
This module can, and the loader skips it: underscore-prefixed modules are helpers, not endpoints.

Importable from ``app.py`` too, without a cycle -- ``app`` already imports the ``api`` package, and
nothing under ``api`` imports ``app``.
"""

from __future__ import annotations

import re

import rack_locks
from flask import jsonify, request


def history_unavailable_response():
    """The 503 returned when the run-history store is absent or disabled."""
    return jsonify({
        "ok": False,
        "error": "run history is unavailable (results_history not installed or disabled)",
    }), 503

_APP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")


def operator_from_request() -> str:
    """Operator label from the ``X-Rack-Operator`` header. Advisory metadata only -- never
    trusted for authorization. Empty when the SPA hasn't set a display name."""
    try:
        raw = request.headers.get("X-Rack-Operator", "")
    except RuntimeError:
        # Outside a request context (worker thread): fall back to empty label.
        raw = ""
    return rack_locks.sanitize_operator(raw)


def client_id_from_request() -> str:
    """Stable browser id from the ``X-Rack-Client-Id`` header (also accepted as
    ``?client_id=`` for backward compatibility with the existing useRackStatus poll)."""
    try:
        raw = request.headers.get("X-Rack-Client-Id", "") or request.args.get("client_id", "")
    except RuntimeError:
        return ""
    if not isinstance(raw, str):
        return ""
    cid = raw.strip()[:80]
    return cid if _APP_CLIENT_ID_RE.fullmatch(cid) else ""
