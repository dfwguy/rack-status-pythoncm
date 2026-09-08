# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""``url_for`` targets survive the move into a blueprint.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Moving a route into a blueprint RENAMES its Flask endpoint: ``api_parser_export_excel`` becomes
``exports.api_parser_export_excel``. Nothing catches that at import time, ruff cannot see inside the
string, and ``test_route_parity`` only checks URL paths -- so a stale ``url_for`` target is a 500 on a
request nobody makes until a customer clicks the old link.

``/api/parser-export-csv`` is the one route in the tree whose entire job is a ``url_for`` redirect, so it
is the one that can break this way. These tests exist because that failure mode is invisible to every
other check we run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


def test_legacy_csv_export_redirects_to_the_excel_route(client):
    """A stale endpoint name raises BuildError here, which surfaces as a 500 rather than a redirect."""
    res = client.get("/api/parser-export-csv?log_dir=/logs")
    assert res.status_code == 307, res.get_data(as_text=True)
    assert "/api/parser-export-excel" in res.headers["Location"]


def test_the_redirect_preserves_the_query_string(client):
    """The legacy route forwards its args; dropping them would export a different log dir."""
    res = client.get("/api/parser-export-csv?log_dir=/logs&rack=p05-r01")
    assert res.status_code == 307
    loc = res.headers["Location"]
    assert "log_dir=%2Flogs" in loc or "log_dir=/logs" in loc
    assert "rack=p05-r01" in loc


def test_every_url_for_target_in_the_tree_resolves():
    """Guard the whole class, not just the one instance.

    Any ``url_for("literal")`` in backend code must name an endpoint Flask actually knows about. This
    walks the registered endpoints rather than the source, so it also fails if a blueprint is renamed
    and a target is left behind.
    """
    import ast

    backend = Path(__file__).resolve().parents[1]
    known = set(app_module.app.view_functions)
    missing: list[str] = []
    for py in sorted(backend.rglob("*.py")):
        if "tests" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:  # pragma: no cover - not our concern here
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "url_for"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                target = node.args[0].value
                # A leading dot is blueprint-relative and only resolvable in a request context; check
                # the suffix against the endpoints that exist.
                if target.startswith("."):
                    suffix = target[1:]
                    if not any(e.split(".")[-1] == suffix for e in known):
                        missing.append(f"{py.name}:{node.lineno} {target}")
                elif target not in known:
                    missing.append(f"{py.name}:{node.lineno} {target}")
    assert not missing, f"url_for targets that no endpoint answers: {missing}"
