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
"""What did ``cmsh -c ds`` actually return on the last worker pass?

A deliberate window into ``native_status``'s private per-cluster state. It reads underscored
internals (``_states``) because the point is to show what the collector really holds when the
dashboard disagrees with the cluster -- a sanitised view would hide the bug being chased.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify

_logger = logging.getLogger(__name__)

# Empty prefix: this route carries the path it had in app.py, so the move cannot change a URL.
bp = Blueprint("ds_diag", __name__, url_prefix="")


@bp.route("/api/ds-diag", methods=["GET"])
def api_ds_diag():
    """Diagnostic: what did `cmsh -c ds` actually return on the last worker pass?"""
    import native_status as _ns
    states = {}
    try:
        reg = getattr(_ns, "_states", None) or {}
        for k, st in list(reg.items()):
            with st.lock:
                states[str(k)] = {
                    "racks": len(st.racks),
                    "rack_sample": list(st.racks)[:3],
                    "ct_by_rack_len": len(st.ct_by_rack),
                    "ct_keys_sample": list(st.ct_by_rack)[:3],
                    "ds_done": st.ds_done,
                    "errors": list(st.last_errors)[-3:],
                }
    except Exception as e:
        _logger.debug("api_ds_diag: ignoring error", exc_info=True)
        states["_error"] = str(e)
    return jsonify({"ok": True, "diag": dict(getattr(_ns, "DS_LAST_DIAG", {}) or {}),
                    "states": states})
