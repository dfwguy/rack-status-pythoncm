# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""No backend module may reach into ``app`` at call time.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``import app`` inside a function body, then ``app.<name>``, is the pattern ``core-designs.mdc`` calls
debt: it dodges a circular import by deferring the lookup to runtime, which means the name is never
checked until the code path actually runs.

That bill came due three times while the routes were being extracted. Each time, a name became unused in
``app.py`` (correctly -- its routes had moved), ruff removed the import, and a module that was reading it
off ``app`` at call time broke with no error at import, no lint finding, and a green test suite:

* ``workflow_engine`` lost ``validation_target_for_rack`` (five sites) and
  ``_run_scontrol_node_update`` -- every fan-out step that resolves a rack to nodes;
* ``agent/registrations`` lost the five job registries and ``_validation_ct_inner`` -- the AI
  assistant's job-status and rack tools;
* ``bcm_query`` lost ``SCRIPTS_DIR``.

All three now import from the module that owns the name. This test is the guard: the pattern is banned
outright, so the next person cannot reintroduce it and discover the breakage on a customer cluster.

``agent/registrations.py`` is the one allowed exception and it earns it: it looks up Flask VIEW functions
by name to expose them as agent tools, which genuinely requires the app object, and it goes through the
``_AppViews`` facade whose every reference ``test_agent_tools`` separately asserts resolves.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# Reaching app for VIEW functions is legitimate; reaching it for helpers is the debt.
ALLOWED = {"agent/registrations.py"}


def _modules_importing_app_in_a_function() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for py in sorted(BACKEND.rglob("*.py")):
        rel = py.relative_to(BACKEND).as_posix()
        if rel.startswith("tests/") or rel == "app.py" or rel in ALLOWED:
            continue
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Import) and any(a.name == "app" for a in inner.names) or isinstance(inner, ast.ImportFrom) and inner.module == "app":
                    found.setdefault(rel, []).append(inner.lineno)
    return found


def test_no_module_defers_an_import_of_app_into_a_function_body():
    offenders = _modules_importing_app_in_a_function()
    assert not offenders, (
        "these modules resolve names off `app` at call time, so a name removed from app.py breaks them "
        f"only when the code path runs: {offenders}. Import from the module that OWNS the name instead."
    )


def test_no_api_module_imports_app_at_all():
    """``api/`` modules must not depend on ``app`` in either direction (core-designs citizen 0)."""
    offenders: dict[str, list[int]] = {}
    for py in sorted((BACKEND / "api").glob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "app" for a in node.names) or isinstance(node, ast.ImportFrom) and node.module == "app":
                offenders.setdefault(py.name, []).append(node.lineno)
    assert not offenders, f"api/ modules importing app recreates the cycle the package removed: {offenders}"


def test_the_allowed_exception_still_exists_and_is_still_needed():
    """If registrations stops reaching app, tighten this test rather than leaving a stale allowance."""
    reg = BACKEND / "agent" / "registrations.py"
    assert reg.exists(), "the allow-list entry names a file that no longer exists"
    assert "_AppViews" in reg.read_text(), (
        "registrations.py no longer uses the _AppViews facade — remove it from ALLOWED so the ban is total"
    )
