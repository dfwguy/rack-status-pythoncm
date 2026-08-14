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
"""The URL surface must survive refactoring.

`app.py` is being split into per-domain blueprints under `backend/api/`. Moving 200-odd routes
between modules is exactly the kind of change that silently renames or drops one: the SPA calls
these paths by hand, the OpenAPI document publishes them, and nothing else in the test suite would
notice a URL quietly changing shape.

So the route map is snapshotted. Every route that existed keeps existing, with the same methods.
Adding routes is fine and needs no ceremony -- new features do that. Changing or removing one is a
deliberate act that has to update the snapshot, which makes it visible in review.

Regenerate after an intentional change:

    cd backend && ../.venv/bin/python -c "
    import json, app
    from backend.tests.test_route_parity import _live_routes   # same aggregation
    rows = [{'rule': k, 'methods': v} for k, v in sorted(_live_routes().items())]
    open('tests/route_snapshot.json','w').write(json.dumps(rows, indent=1) + chr(10))"
"""
from __future__ import annotations

import json
from pathlib import Path

from app import app

SNAPSHOT = Path(__file__).parent / "route_snapshot.json"


def _live_routes() -> dict[str, list[str]]:
    """Path -> every verb it accepts.

    Aggregated, because one path is often several Rule objects: the catalog registers PUT and
    DELETE on ``/api/catalog/tools/<doc_id>`` from two separate handlers. Keyed naively, one
    silently overwrites the other and the snapshot records only whichever came last.
    """
    out: dict[str, set[str]] = {}
    for r in app.url_map.iter_rules():
        if r.endpoint == "static":
            continue
        out.setdefault(str(r), set()).update(
            m for m in r.methods if m not in ("HEAD", "OPTIONS")
        )
    return {rule: sorted(methods) for rule, methods in out.items()}


def _snapshot_routes() -> dict[str, list[str]]:
    return {row["rule"]: row["methods"] for row in json.loads(SNAPSHOT.read_text())}


def test_no_route_disappeared():
    """A path that existed must still exist. This is what a bad move looks like."""
    live, snap = _live_routes(), _snapshot_routes()
    missing = sorted(set(snap) - set(live))
    assert not missing, (
        f"{len(missing)} route(s) vanished — a refactor moved or renamed them:\n  "
        + "\n  ".join(missing)
    )


def test_no_route_changed_methods():
    """A path that existed must still accept the same verbs."""
    live, snap = _live_routes(), _snapshot_routes()
    changed = {r: (snap[r], live[r]) for r in snap if r in live and snap[r] != live[r]}
    assert not changed, "route methods changed:\n  " + "\n  ".join(
        f"{r}: was {was}, now {now}" for r, (was, now) in sorted(changed.items())
    )


def test_every_route_has_exactly_one_handler():
    """Two blueprints claiming one path is a silent shadowing bug.

    Flask raises on a duplicate blueprint *name*, but not on two different blueprints registering
    the same rule — the first registered wins and the second is dead code that looks live.
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in rule.methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            seen.setdefault((str(rule), method), []).append(rule.endpoint)
    clashes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not clashes, "several handlers claim the same path+method:\n  " + "\n  ".join(
        f"{path} {method} -> {handlers}" for (path, method), handlers in sorted(clashes.items())
    )


def test_the_spa_catch_all_stays_last():
    """`/<path:path>` serves the SPA for anything unmatched.

    Werkzeug sorts by specificity rather than registration order, so this is not fragile — but if
    the catch-all ever stopped existing, every client route would 404 instead of loading the app.
    """
    assert "/<path:path>" in _live_routes()
