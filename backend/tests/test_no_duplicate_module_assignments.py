# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""No module-level name is assigned twice with nothing reading it in between.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``_JOB_ENV_BOOTSTRAP_DONE = False`` appeared twice in ``app.py``, ~100 lines apart, from the
``config_env`` extraction. Harmless by luck -- both wrote the same value -- but the same shape with
two different values is a bug that reads as a declaration, and the second write silently wins.

No linter catches it: ``ruff --select ALL`` and ``pylint --enable=all`` both report only the naming
style, because ``X = 1`` then ``X = 2`` is ordinary accumulation. What makes it a defect here is the
absence of any read between the two writes, which is cheap to check and is what this test does.

Scoped deliberately narrow to stay free of false alarms: only assignments directly in the module
body count, so the ``X = None`` / ``if cond: X = 1`` idiom is untouched, as is any rebind guarded by
a ``try``/``except`` import fallback.
"""

from __future__ import annotations

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parent.parent


def _assigned_names(stmt: ast.stmt) -> list[str]:
    """Plain ``name = ...`` targets, ignoring tuple unpacking and attribute/subscript writes."""
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
        return [stmt.target.id]
    return []


def _reads(stmts: list[ast.stmt], name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
        for stmt in stmts
        for node in ast.walk(stmt)
    )


def _redundant_rebinds(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # a file that cannot parse is a different test's problem
        return []

    seen: dict[str, tuple[int, int]] = {}  # name -> (lineno, index in module body)
    out: list[str] = []
    for index, stmt in enumerate(tree.body):
        for name in _assigned_names(stmt):
            previous = seen.get(name)
            if previous is not None and not _reads(tree.body[previous[1] + 1:index], name):
                out.append(
                    f"{path.relative_to(BACKEND.parent)}: {name} assigned at line "
                    f"{previous[0]} and again at line {stmt.lineno} with no read in between"
                )
            seen[name] = (stmt.lineno, index)
    return out


def test_no_module_level_name_is_written_twice_unread() -> None:
    offenders = [
        finding
        for path in sorted(BACKEND.rglob("*.py"))
        if "__pycache__" not in path.parts
        for finding in _redundant_rebinds(path)
    ]
    assert not offenders, "redundant module-level rebinds:\n  " + "\n  ".join(offenders)
