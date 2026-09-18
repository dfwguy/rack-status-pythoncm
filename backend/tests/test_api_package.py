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
"""The auto-discovery contract for ``backend/api/``.

Adding an endpoint should mean adding a file, not editing one that other people are editing.
These tests pin the rules that make that safe, because the failure modes are all quiet ones: a
module that does not register, a prefix that silently changes, or an import error swallowed so a
feature is simply absent with no error anywhere.
"""
from __future__ import annotations

import sys
import types

import api
import pytest
from flask import Blueprint, Flask


class TestDiscovery:
    def test_every_module_in_the_package_is_registrable(self):
        """A module here must expose `bp` or `install` — otherwise it is silently dead code."""
        import importlib

        for name in api.module_names():
            module = importlib.import_module(f"api.{name}")
            assert hasattr(module, "bp") or hasattr(module, "install"), (
                f"backend/api/{name}.py exposes neither"
            )

    def test_helpers_can_live_here_under_an_underscore(self):
        assert not any(n.startswith("_") for n in api.module_names())

    def test_order_is_stable(self):
        """Registration order must not depend on the filesystem."""
        assert api.module_names() == sorted(api.module_names())

    def test_the_terminal_is_discovered(self):
        assert "terminal" in api.module_names()


class TestPrefixConvention:
    def test_underscores_become_hyphens(self):
        """`rack_locks.py` serves /api/rack-locks — Python names cannot contain hyphens."""
        assert api.default_prefix("rack_locks") == "/api/rack-locks"
        assert api.default_prefix("tools") == "/api/tools"

    def test_a_module_may_declare_its_own_prefix(self):
        """Needed for URLs a module name cannot spell, e.g. /api/post-run-node-report."""
        app = Flask(__name__)
        bp = Blueprint("declared", __name__, url_prefix="/api/post-run-node-report")

        @bp.route("/mark", methods=["POST"])
        def mark():
            return "ok"

        app.register_blueprint(bp)
        assert "/api/post-run-node-report/mark" in {str(r) for r in app.url_map.iter_rules()}


class TestFailsLoudly:
    """A module that cannot register must stop startup, not disappear.

    We lost an afternoon to a feature that did nothing because a lazy import inside a `try`
    swallowed a missing module. Silence is the worst available outcome here.
    """

    @pytest.fixture
    def fake_package(self, monkeypatch, tmp_path):
        """A throwaway module inside the real package, so `register` genuinely discovers it."""
        created: list[str] = []

        def make(name: str, body: str):
            full = f"api.{name}"
            module = types.ModuleType(full)
            exec(compile(body, full, "exec"), module.__dict__)  # noqa: S102 - building a
            # synthetic api module IS the subject here: the loader must import it.
            sys.modules[full] = module
            created.append(full)
            return name

        monkeypatch.setattr(api, "module_names", lambda: [n.split(".")[-1] for n in created])
        yield make
        for full in created:
            sys.modules.pop(full, None)

    def test_a_module_with_no_blueprint_raises_and_names_itself(self, fake_package):
        fake_package("orphan", "x = 1\n")
        with pytest.raises(RuntimeError, match="orphan"):
            api.register(Flask(__name__))

    def test_an_import_error_is_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(api, "module_names", lambda: ["does_not_exist_at_all"])
        with pytest.raises(ModuleNotFoundError):
            api.register(Flask(__name__))


class TestRegistration:
    def test_install_only_modules_are_supported(self):
        """The terminal is WSGI middleware, below Flask routing — it cannot be a blueprint."""
        from api import terminal

        assert hasattr(terminal, "install")
        assert not hasattr(terminal, "bp")

    def test_registering_reports_what_it_did(self):
        """The list is logged at startup; without it an auto-loader is a black box."""
        app = Flask(__name__)
        registered = api.register(app)
        assert registered
        assert any("terminal" in entry for entry in registered)

    def test_a_module_carrying_full_paths_is_not_reported_under_a_derived_prefix(
        self, monkeypatch
    ):
        """`url_prefix=""` is an answer, not a missing value.

        Every module migrated out of app.py uses it, so reporting them under the filename-derived
        prefix would make the startup log claim /api/help for routes that do not serve there.
        """
        module = types.SimpleNamespace(bp=Blueprint("help", __name__, url_prefix=""))
        monkeypatch.setattr(api, "module_names", lambda: ["help"])
        monkeypatch.setattr(api.importlib, "import_module", lambda _name: module)

        (entry,) = api.register(Flask(__name__))

        assert "/api/help" not in entry
        assert "full paths" in entry
