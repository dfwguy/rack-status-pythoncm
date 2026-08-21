# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A standard-library module is imported at the top of the file, never inside a function.

Author: Rafael M. Koike & Cursor Agent ("Ada")

`os`, `json`, `time` and friends cannot participate in an import cycle and cost nothing to import,
so deferring one buys nothing and hides the module's real dependencies from anyone reading its
header. Worse, an import inside an `if` -- `demo_data` had `import json` under `if include_raw:` --
makes the binding conditional, so the name exists only on some paths.

FIRST-PARTY deferred imports are deliberately NOT covered. Several are load-bearing and removing
them breaks the app at import time: `transport/base.py` imports `transport.hop` inside a function
and `cluster_report` imports `cluster_report_pdf` the same way, both to break a real cycle.
`platforms/__init__.py` defers the BCM and MAAS providers so a BCM site never imports MAAS's
dependencies. That is the "specific use case" exception, and a linter cannot tell it apart from
laziness -- so this test restricts itself to the case where there is never a good reason.

Imports inside `try:` are exempt regardless of module: that shape is a capability probe (does this
deployment have the module?), not a deferred import, and it must stay where the handler is.

Ruff cannot express this: PLC0415 flags EVERY function-level import, which would fail on all 31
first-party ones. pylint's import-outside-toplevel is the same rule and is disabled in .pylintrc for
that reason.
"""

from __future__ import annotations

import ast
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)


def _root_module(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.Import):
        return node.names[0].name.split(".")[0]
    return (node.module or "").split(".")[0]


def _try_guarded(tree: ast.Module) -> set[int]:
    """Line numbers of imports inside a ``try:`` body -- capability probes, not deferrals."""
    return {
        inner.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for stmt in node.body
        for inner in ast.walk(stmt)
        if isinstance(inner, (ast.Import, ast.ImportFrom))
    }


def _offenders(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    guarded = _try_guarded(tree)
    out: list[str] = []
    for func in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        for node in ast.walk(func):
            if node is func or not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            root = _root_module(node)
            if root in STDLIB and node.lineno not in guarded:
                out.append(
                    f"{path.relative_to(BACKEND.parent)}:{node.lineno} imports stdlib "
                    f"'{root}' inside {func.name}() -- move it to the top of the file"
                )
    return out


def test_stdlib_is_imported_at_module_level() -> None:
    offenders = [
        finding
        for path in sorted(BACKEND.rglob("*.py"))
        if "__pycache__" not in path.parts and "tests" not in path.parts
        for finding in _offenders(path)
    ]
    assert not offenders, "function-level stdlib imports:\n  " + "\n  ".join(offenders)
