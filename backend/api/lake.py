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
"""Parquet lake health.

Moved out of `app.py`, where it was the **only** `/api/*` route left in the composition root — a
citizen-0 violation that survived the api-module migration because it was nested inside a
`try/except` block registering two optional blueprints, so no route census caught it.

The move is not cosmetic. In `app.py` this route lived inside
`except Exception: _logger.warning(...)`, so *any* error while defining it — including a
`NameError` from a missing import, which is exactly what happened — silently deleted the endpoint
and left one warning line in a log nobody reads during a bring-up. Here, an import failure takes
startup down and names itself, which is what `core-designs.mdc` asks of an api module.
"""

from __future__ import annotations

from audit.actions import action
from flask import Blueprint, jsonify

bp = Blueprint("lake", __name__, url_prefix="")


@bp.route("/api/lake/health")
@action(get="observability:read")
def api_lake_health():
    """Is the Parquet lake reachable and accepting run provenance writes?

    `lake_sink` is imported per-request rather than at module scope, and that is deliberate: it is
    one of the soft-imported optional modules (`app.py` guards it with `is None` at every call
    site), so a deployment without it must still serve this route — answering "not loaded" is the
    useful reply to a health probe, where a 500 or an absent endpoint is not.
    """
    try:
        import lake_sink
    except ImportError:
        # 503, not the implicit 200 this returned in `app.py`. Moving the route into `api/` put it
        # in scope of `test_api_response_codes.py`, which caught an `{"ok": false}` body served
        # with a success status — a client checking the status code alone would have read "the lake
        # is fine". An absent dependency is unavailability, the same reading as the unbuilt-SPA 503
        # (`docs/API_RESPONSE_CODES.md`).
        return jsonify({"ok": False, "error": "lake_sink not loaded"}), 503

    return jsonify(lake_sink.health())
