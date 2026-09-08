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
"""The identity startup wiring exists and is in the right order — FR-6a, FR-10, FR-10b.

This file exists for the same reason `test_config_materialize_hook.py` does, and the precedent is
worth stating because it is the whole justification: in Phase 1, the config materialise hook was a
one-line registration that nothing asserted. Unregistered, nothing raised — the overlay simply
stopped being applied, producing wrong values on some clusters and an error on none. A test was
added purely to assert the wire existed.

The identity wiring has the same shape and a worse failure. `tokens.verify` refuses **every** token
until `revocation.install()` registers a checker (FR-6a, fail-closed by design). Drop that one call
and the product does not degrade — it stops authenticating anyone, with an error message about a
revocation checker that reads like an internal fault rather than a missing wire. And drop
`ensure_admin` and a fresh deployment comes up with an empty identity store, which after FR-18 means
nobody can log in at all.

Neither failure is visible in any other test, because every other test either bypasses the gate or
constructs its own store.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app.py"
SOURCE = APP.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} is not defined in app.py")


def _calls_in(fn: ast.FunctionDef) -> list[str]:
    """Dotted call names in source order, so ordering can be asserted."""
    out: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
            out.append(".".join(reversed(parts)))
    return out


class TestTheBootstrapIsWired:
    def test_the_function_exists(self):
        assert _function("_bootstrap_identity_at_startup")

    def test_it_is_actually_called_at_module_level(self):
        """Defining it is not wiring it. This is the half that silently goes missing."""
        called = [
            n.value.func.id
            for n in TREE.body
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
        ]
        assert "_bootstrap_identity_at_startup" in called, (
            "the identity bootstrap is defined but never called, so no deployment ever gets an "
            "administrator and no revocation checker is registered"
        )

    def test_it_runs_after_the_catalog(self):
        """The catalog materialises the active cluster into the environment; identity paths and
        the audit `cluster` label read from it, so identity must not run first."""
        order = [
            n.value.func.id
            for n in TREE.body
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id.startswith("_bootstrap_")
        ]
        assert order.index("_bootstrap_catalog_at_startup") < order.index(
            "_bootstrap_identity_at_startup"
        )


class TestFR6aTheRevocationCheckerIsRegistered:
    def test_install_is_called(self):
        calls = _calls_in(_function("_bootstrap_identity_at_startup"))
        assert any(c.endswith("revocation.install") for c in calls), (
            "revocation.install() is not called. tokens.verify refuses EVERY token until a checker "
            "is registered (FR-6a), so without this the product authenticates nobody — and the "
            "error names a revocation checker, not a missing wire, so it reads as an internal fault"
        )

    def test_it_is_registered_before_the_admin_is_minted(self):
        """Order is the requirement, not an aesthetic.

        Minting deliberately stays available before the checker is installed so the bootstrap can
        run; the checker still goes first, so there is no window in which a token could be verified
        against an unregistered checker.
        """
        calls = _calls_in(_function("_bootstrap_identity_at_startup"))
        install = next(i for i, c in enumerate(calls) if c.endswith("revocation.install"))
        mint = next(i for i, c in enumerate(calls) if c.endswith("ensure_admin"))
        assert install < mint

    def test_the_checker_actually_refuses_when_absent(self, monkeypatch):
        """Prove the fail-closed contract this wiring exists to satisfy, rather than assume it.

        Without this, the two assertions above are pinning a call whose absence might be harmless.

        The signing secret is set explicitly because this module deliberately does not import `app`
        — it reads app.py as source — so app.py's own secret bootstrap has not run.
        """
        from identity import revocation, tokens

        revocation.reset_for_tests()
        tokens.reset_for_tests()
        try:
            token = tokens.mint("user:admin", tokens.TOKEN_ACCESS)
            with pytest.raises(tokens.TokenError):
                tokens.verify(token, tokens.TOKEN_ACCESS)
        finally:
            revocation.install()


class TestTheAdminIsMinted:
    def test_ensure_admin_is_called(self):
        calls = _calls_in(_function("_bootstrap_identity_at_startup"))
        assert any(c.endswith("ensure_admin") for c in calls), (
            "no administrator is bootstrapped, so a fresh deployment — and every deployment "
            "upgrading from shared-token auth (FR-10b) — comes up with nobody able to log in"
        )

    def test_the_store_is_closed(self):
        """TinyDB holds the file open; leaking a handle per start is a slow leak in a long process."""
        calls = _calls_in(_function("_bootstrap_identity_at_startup"))
        assert any(c.endswith("store.close") or c == "store.close" for c in calls)


class TestTheImportIsNotSwallowed:
    def test_identity_is_imported_plainly(self):
        """`core-designs.mdc` §0: a module that cannot import must take startup down and name itself.

        The catalog above is wrapped in try/except and degrades to legacy env behaviour, which is
        defensible for a data store and indefensible for authentication: a swallowed ImportError
        there would produce a deployment that looks healthy and cannot authenticate anyone.
        """
        for name in ("identity_bootstrap", "identity_revocation", "identity_store"):
            pattern = rf"^from identity import \w+ as {name}$"
            assert re.search(pattern, SOURCE, re.MULTILINE), f"{name} is not imported at top level"

        # And that those imports are not inside a Try node.
        for node in ast.walk(TREE):
            if not isinstance(node, ast.Try):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and (child.module or "").startswith("identity"):
                    pytest.fail(
                        "an identity import is wrapped in try/except; a broken install must fail "
                        "loudly at startup rather than leave authentication silently absent"
                    )


class TestTheTestSuiteIsIsolatedFromTheRealStore:
    def test_conftest_redirects_the_identity_file(self):
        """The bootstrap mints a real credential. Unisolated, every test run writes one into the
        worktree at `var/identity/identity.json` and prints it into the test output."""
        conftest = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
        assert "NVIS_IDENTITY_FILE" in conftest

    def test_the_active_path_is_a_temp_directory(self):
        import os

        from identity import store as store_mod

        path = store_mod.identity_file()
        assert "nvis-identity-test-" in path, (
            f"tests are pointed at {path!r}; a real credential would be written there"
        )
        assert os.environ.get("NVIS_IDENTITY_FILE") == path
