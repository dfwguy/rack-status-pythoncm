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
"""`app.py` defines no API route (citizen 0).

The api-module migration moved 199 routes into `backend/api/` and left `app.py` as a composition
root: the Flask object, the bootstraps, the auth gate, the middleware, and the SPA catch-alls.
Nothing asserted that, so it drifted back — `/api/lake/health` stayed behind and was only noticed
by a human reading a diff months later.

**Why no existing test caught it.** `test_route_parity.py` snapshots URL paths and methods, so a
route keeps passing wherever it is defined; that is deliberate, because the snapshot's job is to
notice a changed URL. `test_action_coverage.py` checks declarations, not location. Neither has any
notion of *which module* a route lives in, which is the property citizen 0 is actually about.

This one is an AST test rather than a URL-map test on purpose: the offending route was nested
inside a `try/except Exception` that registers two optional blueprints, so on any deployment where
those imports fail it is absent from the URL map entirely — a runtime check would have passed for
the wrong reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"

#: The SPA catch-alls. They belong here: they are the fall-through for anything the API did not
#: claim, so they must be registered last, and `api/_common.serve_spa_asset` already owns the
#: behaviour they delegate to.
ALLOWED = {"/", "/spa/", "/spa/<path:path>", "/<path:path>"}


def routes_defined_in_app() -> list[tuple[str, int]]:
    """Every `@app.route(...)` / `@app.get(...)` literal in `app.py`, with its line.

    Walks the whole tree, not just module level, because the route this test exists for was nested
    two blocks deep inside a `try`.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(APP.read_text())):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if not isinstance(fn, ast.Attribute) or fn.attr not in {
                "route", "get", "post", "put", "patch", "delete"
            }:
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "app"):
                continue
            for arg in dec.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((arg.value, dec.lineno))
    return found


class TestAppPyDefinesNoApiRoute:
    def test_no_api_route_is_defined_in_app_py(self):
        offenders = [
            f"{rule} (line {line})"
            for rule, line in routes_defined_in_app()
            if rule.startswith(("/api/", "/ws/", "/oidc/"))
        ]
        assert not offenders, (
            "these API routes are defined in the composition root:\n  "
            + "\n  ".join(offenders)
            + "\n\nMove each to a module in backend/api/ — expose `bp` and the loader finds it. "
            "See core-designs.mdc citizen 0."
        )

    def test_only_the_spa_catch_alls_remain(self):
        """Tighter than the check above, and the one that will actually fire first.

        A new non-API route in `app.py` is usually the first step toward an API one, and it is far
        easier to redirect while it is still one line. If a genuinely new fall-through is needed,
        add it to `ALLOWED` and say why in the commit — that is the reviewed diff this test exists
        to force.
        """
        unexpected = sorted(
            {rule for rule, _ in routes_defined_in_app()} - ALLOWED
        )
        assert not unexpected, (
            f"unexpected routes in app.py: {unexpected}. app.py is a composition root; routes "
            "belong in backend/api/."
        )

    def test_the_check_can_see_a_nested_route(self):
        """Guards the test itself.

        The route that prompted this was nested inside a `try/except`, so a walker that only looked
        at module level would have reported a clean `app.py` — the failure mode where the gate
        passes because it is looking in the wrong place. Asserted by parsing a fixture rather than
        by trusting the walker.
        """
        tree = ast.parse(
            "try:\n"
            "    @app.route('/api/nested')\n"
            "    def nested():\n"
            "        pass\n"
            "except Exception:\n"
            "    pass\n"
        )
        rules = [
            arg.value
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for dec in node.decorator_list
            if isinstance(dec, ast.Call)
            for arg in dec.args
            if isinstance(arg, ast.Constant)
        ]
        assert rules == ["/api/nested"], "the walker cannot see a nested route"
