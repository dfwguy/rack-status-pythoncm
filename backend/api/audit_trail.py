# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""`GET /api/audit-trail` — read the security audit trail.

The trail has been written since Phase 1 and could not be read: the only way to answer "who did
what, to which rack, when, and what came of it" was to reach the host and `cat` a file. This is that
question's endpoint.

**Reading it is a privileged act**, because the trail records every operator's activity, not the
caller's. Two consequences visible here:

* the route is governed by `audit.trail:list`, a resource type of its own, so a deployment grants it
  deliberately rather than inheriting it from a broad read grant;
* the read is recorded **even when `NVIS_AUDIT_READS` is off**, via `actions.ALWAYS_AUDITED`. That
  goes through the existing chokepoint rather than an `audit.emit()` call here, because
  `request_hook`'s docstring commits to two emit sites and says a third would mean the chokepoint
  had failed.

Self-amplification is prevented by where the event is written, not by a guard: the chokepoint emits
in `after_request`, after this handler has built its body, so a read's own event cannot appear in
its own response.
"""

from __future__ import annotations

from audit import reader
from audit.actions import action
from flask import Blueprint, jsonify, request

bp = Blueprint("audit_trail", __name__, url_prefix="")

#: Query parameters that map straight onto `reader.Filter`.
FILTER_FIELDS = ("principal", "action", "service", "resource", "decision", "result",
                 "since", "until")


def _filter_from_query() -> reader.Filter:
    return reader.Filter(**{
        name: (request.args.get(name) or "").strip() for name in FILTER_FIELDS
    })


@bp.route("/api/audit-trail", methods=["GET"])
@action(get="audit.trail:list")
def api_audit_trail():
    """One page of the trail, newest first.

    `limit` is clamped by the reader rather than validated here: a caller asking for 10,000 gets
    `MAX_PAGE`, which is friendlier than a 400 and keeps the bound in one place.
    """
    try:
        limit = int(request.args.get("limit") or reader.DEFAULT_PAGE)
    except ValueError:
        return jsonify({"ok": False, "error": "limit must be an integer"}), 400

    page = reader.read(
        _filter_from_query(),
        limit=limit,
        cursor=(request.args.get("cursor") or "").strip(),
    )

    return jsonify({
        "ok": True,
        "events": page.events,
        "cursor": page.cursor,
        # Everything below is how a caller tells a complete answer from a partial one. Returning
        # only `events` would make "nobody did that" and "we stopped looking" the same response.
        "truncated": page.truncated,
        "cursor_expired": page.cursor_expired,
        "scanned_events": page.scanned_events,
        "scanned_bytes": page.scanned_bytes,
        "skipped_lines": page.skipped_lines,
        # A fact the reader holds, not a retention period computed from the configured budget —
        # which would be a guess, since how far back the trail reaches depends on event rate.
        "oldest_held": page.oldest_held,
        "files": page.files,
    })


@bp.route("/api/audit-trail/fields", methods=["GET"])
@action(get="audit.trail:read")
def api_audit_trail_fields():
    """What the trail can be filtered by, and the page bounds.

    Served rather than hard-coded in the frontend, for the reason the action vocabulary is: a
    duplicated list is wrong the first time somebody adds a field, and the failure is a filter that
    silently does nothing.
    """
    return jsonify({
        "ok": True,
        "filters": list(FILTER_FIELDS),
        "default_page": reader.DEFAULT_PAGE,
        "max_page": reader.MAX_PAGE,
        "max_scan_events": reader.MAX_SCAN_EVENTS,
        "max_scan_bytes": reader.MAX_SCAN_BYTES,
    })
