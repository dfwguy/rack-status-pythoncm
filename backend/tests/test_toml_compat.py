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
"""TOML parser selection (FR-4, FR-4a, FR-4b, FR-4c; TC-1, TC-2, TC-40).

**Every test here has to work against a defect that is invisible in the tree it runs in.**
`tomllib` is stdlib from Python 3.11; the runtime image is `ubuntu:22.04` whose `python3` is
3.10.12, where it does not exist. All three CI jobs are `python:3.12-slim`, and local development is
3.12. So the interpreter that would expose the bug is never the interpreter running these tests.

That is why `TestTC1TheFallbackBranchIsReachable` masks `tomllib` in `sys.modules` rather than
checking a version: without masking, the `tomli` branch is dead code everywhere the suite runs, and
a test that cannot execute a branch cannot defend it. It is the same reason
`local-vs-ci-tree.mdc` exists, with the image as a third tree nobody tests.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _reimport_with_tomllib_hidden():
    """Import `toml_compat` as if this interpreter had no `tomllib`.

    Blocks the name at the *import system* level rather than deleting it from `sys.modules`, because
    a plain delete is undone by the next successful import and would let the real stdlib module
    satisfy the call anyway.
    """
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'", name="tomllib")
        return real_import(name, *args, **kwargs)

    saved_compat = sys.modules.pop("toml_compat", None)
    saved_tomllib = sys.modules.pop("tomllib", None)
    builtins.__import__ = guarded
    try:
        return importlib.import_module("toml_compat")
    finally:
        builtins.__import__ = real_import
        sys.modules.pop("toml_compat", None)
        if saved_compat is not None:
            sys.modules["toml_compat"] = saved_compat
        if saved_tomllib is not None:
            sys.modules["tomllib"] = saved_tomllib


class TestTC1TheFallbackBranchIsReachable:
    """FR-4. The branch the container actually takes, exercised on an interpreter that has both."""

    def test_it_binds_tomli_when_tomllib_is_absent(self):
        mod = _reimport_with_tomllib_hidden()
        assert mod.IMPLEMENTATION == "tomli", (
            f"fallback bound {mod.IMPLEMENTATION!r}; on the container this is the only branch"
        )

    def test_both_implementations_parse_identically(self):
        """API-identical is the whole premise of the fallback, so it is asserted, not assumed."""
        import toml_compat as native

        fallback = _reimport_with_tomllib_hidden()
        document = 'name = "x"\nn = 7\nflag = true\nitems = ["a", "b"]\n[sect]\nk = 1\n'

        assert native.loads(document) == fallback.loads(document)
        assert native.IMPLEMENTATION != fallback.IMPLEMENTATION, "the two branches did not differ"

    def test_a_parse_error_is_catchable_through_the_module(self):
        """A call site that reached for `tomllib.TOMLDecodeError` itself would need the conditional
        import back, which is the split this module exists to prevent."""
        import toml_compat

        with pytest.raises(toml_compat.TOMLDecodeError):
            toml_compat.loads("this is = = not toml")

    def test_the_fallbacks_error_type_is_catchable_too(self):
        fallback = _reimport_with_tomllib_hidden()
        with pytest.raises(fallback.TOMLDecodeError):
            fallback.loads("nope = = =")


class TestTC2ABrokenTomllibDoesNotSilentlyFallBack:
    """FR-4. `ModuleNotFoundError`, not `ImportError` — and the distinction is load-bearing.

    `ModuleNotFoundError` subclasses `ImportError`, so catching the broader type would also swallow
    a `tomllib` that exists but is *damaged*: a partial install, a shadowing local file, a
    compiled-extension mismatch. Falling back then hides a broken interpreter and sends the next
    person to debug the wrong component.
    """

    def test_a_damaged_tomllib_raises_rather_than_falling_back(self):
        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "tomllib":
                raise ImportError("cannot import name 'load' from partially initialised tomllib")
            return real_import(name, *args, **kwargs)

        saved_compat = sys.modules.pop("toml_compat", None)
        saved_tomllib = sys.modules.pop("tomllib", None)
        builtins.__import__ = guarded
        try:
            with pytest.raises(ImportError):
                importlib.import_module("toml_compat")
        finally:
            builtins.__import__ = real_import
            sys.modules.pop("toml_compat", None)
            if saved_compat is not None:
                sys.modules["toml_compat"] = saved_compat
            if saved_tomllib is not None:
                sys.modules["tomllib"] = saved_tomllib

    def test_the_handler_catches_the_narrow_error(self):
        """Read the handler through the AST, not by grepping the source.

        The first version of this test string-matched `"except ImportError" not in source` and
        failed against the module's own **docstring**, which discusses that idiom in prose. A
        source grep cannot tell code from the comment explaining the code — and the failure was
        confusing in the least useful direction, reporting a defect in a passage of documentation.
        """
        tree = ast.parse((BACKEND / "toml_compat.py").read_text())
        caught: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name):
                caught.append(node.type.id)

        assert "ModuleNotFoundError" in caught, f"handlers catch {caught}"
        assert "ImportError" not in caught, (
            f"handlers catch {caught}; catching ImportError would swallow a damaged tomllib and "
            "fall back silently to a different parser"
        )


class TestTC40NoOtherModuleImportsTomlDirectly:
    """FR-4b. The check that keeps the decision in one place.

    The `try`/`except` idiom is short enough to inline, which is exactly the hazard: it gets
    inlined at some sites and forgotten at others, and the forgotten one is invisible until a
    container starts. This is what `npi_import.py` did, and it cost months of a silently dead NPI
    catalog.
    """

    def test_only_toml_compat_names_a_toml_parser(self):
        offenders: list[str] = []
        for source in sorted(BACKEND.rglob("*.py")):
            rel = source.relative_to(BACKEND)
            if rel.parts[0] in ("tests", "__pycache__") or rel.name == "toml_compat.py":
                continue
            try:
                tree = ast.parse(source.read_text())
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] in ("tomllib", "tomli"):
                        offenders.append(f"{rel}:{node.lineno} imports {name}")

        assert not offenders, (
            "these modules choose a TOML parser themselves instead of using backend/toml_compat.py, "
            "so one of them will be forgotten and fail only inside the container:\n  "
            + "\n  ".join(offenders)
        )


class TestTC29TheNpiImporterIsFixedAndLoud:
    """FR-4b, FR-4c — both faces of the defect, on an interpreter where neither is visible."""

    def test_the_npi_importer_imports_on_an_interpreter_without_tomllib(self):
        """The boot path. This raised `ModuleNotFoundError` on every container start."""
        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "tomllib":
                raise ModuleNotFoundError("No module named 'tomllib'", name="tomllib")
            return real_import(name, *args, **kwargs)

        for name in ("catalog.npi_import", "toml_compat", "tomllib"):
            sys.modules.pop(name, None)
        builtins.__import__ = guarded
        try:
            module = importlib.import_module("catalog.npi_import")
            assert hasattr(module, "import_npi_catalog")
        finally:
            builtins.__import__ = real_import
            for name in ("catalog.npi_import", "toml_compat"):
                sys.modules.pop(name, None)
            importlib.import_module("catalog.npi_import")

    def test_the_seed_swallow_logs_above_debug(self):
        """FR-4c. `_logger.debug` is what made the real defect invisible: the branch fired on every
        container start and said nothing at the default level."""
        source = (BACKEND / "catalog" / "seed.py").read_text()
        marker = 'summary["npi"] = import_npi_catalog(repo)'
        assert marker in source
        tail = source[source.index(marker) : source.index(marker) + 1200]

        assert "_logger.warning" in tail or "_logger.error" in tail, (
            "the NPI import failure is still swallowed below WARNING, so a recurrence would again "
            "leave no visible trace"
        )
        assert "_logger.debug(" not in tail.split("summary[\"npi\"] = {}")[0], (
            "a debug-level log remains on the failure path"
        )
