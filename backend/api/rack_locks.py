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
"""Advisory rack locks: who is working on which rack, and explicit operator claims.

Advisory by design. The server never blocks an operation on a lock -- it reports what is in flight so
the SPA can warn before a second operator launches against a rack someone else is driving. Two
operators (or two agents) racing on one NVL72 is the failure this exists to make visible, not to
prevent by force.

A snapshot merges three sources: the in-memory job registries (what is running), explicit claims with
a TTL (what an operator reserved), and the browser heartbeat map (whether a claim's owner is still
online, so an abandoned claim can be cleared).

``import rack_locks`` below is the top-level store module, not this one -- absolute imports mean a
module inside a package does not shadow a same-named top-level module.
"""
from __future__ import annotations

import rack_locks
from app_clients import _active_clients_snapshot
from audit.actions import action
from flask import Blueprint, jsonify, request
from job_registry import (
    _llama_jobs,
    _llama_jobs_lock,
    _reset_jobs,
    _reset_jobs_lock,
    _stress_jobs,
    _stress_jobs_lock,
    _validation_jobs,
    _validation_jobs_lock,
    _xrack_jobs,
    _xrack_jobs_lock,
)

from ._common import client_id_from_request as _client_id_from_request
from ._common import operator_from_request as _operator_from_request

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("rack_locks_api", __name__, url_prefix="")


def _iter_active_jobs_for_locks() -> list[dict]:
    """Flatten the five in-memory job dicts into the shape ``rack_locks`` expects:
    ``{job_id, rack | racks, kind, operator, phase, started_at, sync}``.

    Called by ``GET /api/rack-locks`` only; cheap (≤ tens of entries in practice)."""
    rows: list[dict] = []
    for src_lock, src_dict, default_kind in (
        (_reset_jobs_lock, _reset_jobs, "reset"),
        (_validation_jobs_lock, _validation_jobs, "validation"),
        (_stress_jobs_lock, _stress_jobs, "stress"),
        (_xrack_jobs_lock, _xrack_jobs, "xrack"),
        (_llama_jobs_lock, _llama_jobs, "llama"),
    ):
        with src_lock:
            snapshot = list(src_dict.items())
        for jid, row in snapshot:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "job_id": str(jid),
                    "rack": row.get("rack"),
                    "racks": row.get("racks"),
                    "kind": str(row.get("kind") or default_kind),
                    "operator": str(row.get("operator") or ""),
                    "phase": str(row.get("phase") or ""),
                    "started_at": str(row.get("started_at") or row.get("created_at") or ""),
                    "sync": bool(row.get("sync")),
                }
            )
    return rows


@bp.route("/api/rack-locks", methods=["GET"])
@action(get="rack.lock:list")
def api_rack_locks_get():
    """Snapshot of in-flight operations + explicit claims, grouped by rack."""
    snapshot = rack_locks.build_lock_snapshot(
        _operator_from_request(),
        _iter_active_jobs_for_locks(),
        _active_clients_snapshot(),
    )
    return jsonify(snapshot)


@bp.route("/api/rack-locks/<rack>", methods=["POST"])
@action("rack.lock:claim")
def api_rack_locks_claim(rack: str):
    """Create or refresh a manual claim. Requires ``X-Rack-Operator`` header (advisory)."""
    operator = _operator_from_request()
    if not operator:
        return jsonify(
            {"ok": False, "error": "X-Rack-Operator header is required to claim a rack"}
        ), 400
    body = request.get_json(silent=True) or {}
    note = str(body.get("note") or "") if isinstance(body.get("note"), str) else ""
    ttl_raw = body.get("ttl_sec")
    ttl_sec = None
    if isinstance(ttl_raw, (int, float)) and not isinstance(ttl_raw, bool):
        try:
            ttl_sec = int(ttl_raw)
        except (TypeError, ValueError):
            ttl_sec = None
    client_id = _client_id_from_request()
    force = bool(body.get("force")) if isinstance(body, dict) else False
    try:
        claim = rack_locks.upsert_claim(
            rack,
            operator,
            client_id=client_id or None,
            note=note,
            ttl_sec=ttl_sec,
            force=force,
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True, "claim": claim})


@bp.route("/api/rack-locks/<rack>", methods=["DELETE"])
@action("rack.lock:release")
def api_rack_locks_release(rack: str):
    """Release a manual claim. Only the recorded owner may release a live claim; expired
    entries can be cleared by anyone."""
    operator = _operator_from_request()
    try:
        removed = rack_locks.release_claim(rack, operator)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    return jsonify({"ok": True, "removed": bool(removed)})
