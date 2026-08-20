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
"""TC-15 .. TC-17 of the Phase 1 test plan: the declared action vocabulary.

Traces to AC-28, AC-28a (FR-14), AC-29 (FR-14a), AC-29a, AC-29b (FR-14c).

TC-18 — the coverage gate, the endpoint→action snapshot and the two real multi-method endpoints —
needs the 103 declarations and lands with work item 3.
"""

from __future__ import annotations

import pytest
from audit import actions as A
from flask import Blueprint, Flask


@pytest.fixture(autouse=True)
def _no_leaked_registry():
    A.reset_for_tests()
    yield
    A.reset_for_tests()


def _app(*routes) -> Flask:
    """Minimal app. Each route is ``(rule, methods, view)``; the view carries its declarations."""
    app = Flask(__name__)
    bp = Blueprint("t", __name__)
    for rule, methods, view in routes:
        bp.add_url_rule(rule, view_func=view, methods=list(methods))
    app.register_blueprint(bp)
    return app


# --------------------------------------------------------------------------- TC-15


class TestTC15GrammarAndStability:
    """The action survives a rename, matches the grammar, and yields its own service (AC-28/28a)."""

    def test_action_survives_function_rename(self):
        @A.action("rack.gpu:reset", variant=A.VARIANT_ASYNC)
        def api_gpu_reset_async():
            return ""

        first = A.build_registry(_app(("/a", ["POST"], api_gpu_reset_async)))

        # The same declaration on a function with a different name — i.e. exactly what a refactor
        # produces. request.endpoint would change here; a declared action must not.
        @A.action("rack.gpu:reset", variant=A.VARIANT_ASYNC)
        def api_rack_gpu_reset():  # renamed
            return ""

        second = A.build_registry(_app(("/a", ["POST"], api_rack_gpu_reset)))

        assert first.entries[0].endpoint != second.entries[0].endpoint, "the endpoint did change"
        assert first.entries[0].action == second.entries[0].action == "rack.gpu:reset"
        assert first.actions() == second.actions()

    @pytest.mark.parametrize(
        "name",
        ["auth:login", "rack.power:on", "fabric.nvswitch:reset_sdn", "validation:run_llmb_nccl"],
    )
    def test_grammar_accepts_the_real_vocabulary(self, name):
        assert A.ACTION_RE.match(name), f"{name} is from the drafted vocabulary and must parse"

    def test_service_is_derived_from_the_name_not_the_blueprint(self):
        @A.action("rack.power:on")
        def view():
            return ""

        reg = A.build_registry(_app(("/p", ["POST"], view)))
        entry = reg.entries[0]
        assert entry.endpoint.startswith("t."), "the blueprint really is named something else"
        assert entry.service == "rack.power"
        assert entry.verb == "on"

    def test_dotted_subservice_groups_under_its_parent_prefix(self):
        """What lets Phase 3 write one rule for everything under ``fabric.nvswitch``."""

        @A.action("fabric.nvswitch:check")
        def a():
            return ""

        @A.action("fabric.nvswitch:reset_sdn")
        def b():
            return ""

        reg = A.build_registry(_app(("/a", ["POST"], a), ("/b", ["POST"], b)))
        assert reg.services() == ("fabric.nvswitch",)
        assert len(reg.actions()) == 2

    def test_declaring_both_forms_at_once_is_refused(self):
        with pytest.raises(A.ActionRegistryError, match="not both"):
            A.action("comments:update", put="comments:update")

    def test_declaring_nothing_is_refused(self):
        with pytest.raises(A.ActionRegistryError, match="needs an action name"):
            A.action()


# --------------------------------------------------------------------------- TC-16


class TestTC16StartupValidation:
    """Malformed names and repeated (action, variant) pairs fail at startup, not in a log (AC-29)."""

    @pytest.mark.parametrize(
        "bad",
        [
            "RackPower:On",       # not lowercase
            "rack.power",         # no verb
            "rack.power:",        # empty verb
            ":on",                # empty service
            "rack..power:on",     # empty sub-service
            "rack.power.extra:on",  # two sub-services
            "rack power:on",      # space
            "rack.power:on-now",  # hyphen
        ],
    )
    def test_invalid_name_fails_startup(self, bad):
        @A.action(bad)
        def view():
            return ""

        with pytest.raises(A.ActionRegistryError, match="does not match"):
            A.build_registry(_app(("/x", ["POST"], view)))

    def test_unknown_variant_fails_startup(self):
        @A.action("rack.power:on", variant="turbo")
        def view():
            return ""

        with pytest.raises(A.ActionRegistryError, match="unknown variant"):
            A.build_registry(_app(("/x", ["POST"], view)))

    def test_duplicate_action_and_variant_fails_startup(self):
        @A.action("rack.power:on")
        def one():
            return ""

        @A.action("rack.power:on")
        def two():
            return ""

        with pytest.raises(A.ActionRegistryError, match="both claim action"):
            A.build_registry(_app(("/one", ["POST"], one), ("/two", ["POST"], two)))

    def test_redeclaring_a_different_action_on_one_view_is_refused(self):
        def view():
            return ""

        A.action("rack.power:on")(view)
        with pytest.raises(A.ActionRegistryError, match="refusing to replace"):
            A.action("rack.power:off")(view)

    def test_validation_raises_rather_than_warning(self, caplog):
        """The distinction FR-14a draws: a diagnostic in a log is not a gate."""

        @A.action("Nope:Nope")
        def view():
            return ""

        with pytest.raises(A.ActionRegistryError):
            A.build_registry(_app(("/x", ["POST"], view)))
        assert not caplog.records, "validation must not be downgraded to a log line"


# --------------------------------------------------------------------------- TC-17


class TestTC17TransportTwins:
    """One operation over two transports is one action with two variants (AC-29a, AC-29b)."""

    def test_transport_twins_share_one_action(self):
        @A.action("tools:install_hpcx")
        def sync_route():
            return ""

        @A.action("tools:install_hpcx", variant=A.VARIANT_STREAM)
        def stream_route():
            return ""

        reg = A.build_registry(
            _app(("/i", ["POST"], sync_route), ("/i-stream", ["POST"], stream_route))
        )
        assert reg.actions() == ("tools:install_hpcx",), "one action, not two"
        assert {e.variant for e in reg.entries} == {A.VARIANT_SYNC, A.VARIANT_STREAM}

    def test_registry_inverts_to_action_to_routes(self):
        """Structural, not a pinned count: a count would fail on any unrelated route addition."""

        @A.action("tools:install_hpcx")
        def sync_route():
            return ""

        @A.action("tools:install_hpcx", variant=A.VARIANT_STREAM)
        def stream_route():
            return ""

        @A.action("rack.power:on")
        def power():
            return ""

        app = _app(
            ("/i", ["POST"], sync_route),
            ("/i-stream", ["POST"], stream_route),
            ("/power", ["POST"], power),
        )
        reg = A.build_registry(app)

        inverted = reg.by_action()
        declared_pairs = {(e.endpoint, e.method) for e in reg.entries}
        inverted_pairs = {(e.endpoint, e.method) for group in inverted.values() for e in group}
        assert inverted_pairs == declared_pairs, "the inverse must lose nothing"

        # Every mutating pair on the app resolves; none is reachable by only a subset of its routes.
        for rule, method in A._iter_mutating_rules(app):
            assert reg.resolve(rule.endpoint, method) is not None
        assert len(inverted["tools:install_hpcx"]) == 2

    def test_a_twin_missing_its_declaration_is_visible(self):
        """The failure the twin design exists to prevent, made detectable rather than silent."""

        @A.action("tools:install_hpcx")
        def sync_route():
            return ""

        def stream_route():  # the sibling nobody decorated
            return ""

        app = _app(("/i", ["POST"], sync_route), ("/i-stream", ["POST"], stream_route))
        A.build_registry(app)
        missing = A.undeclared_mutating_routes(app)
        assert [m[0] for m in missing] == ["t.stream_route"]

    def test_per_method_actions(self):
        """The escape hatch for the two real endpoints that serve more than one mutating method."""

        @A.action(put="comments:update", delete="comments:delete")
        def api_comment_put():
            return ""

        reg = A.build_registry(_app(("/c/<rack>", ["PUT", "DELETE"], api_comment_put)))
        assert reg.resolve("t.api_comment_put", "PUT").action == "comments:update"
        assert reg.resolve("t.api_comment_put", "DELETE").action == "comments:delete"
        assert reg.actions() == ("comments:delete", "comments:update")

    def test_read_methods_are_not_registered(self):
        """A GET on a mixed-method endpoint carries no action: FR-12 excludes reads by default."""

        @A.action("vault:set_token")
        def api_vault_token():
            return ""

        reg = A.build_registry(_app(("/v", ["GET", "PUT"], api_vault_token)))
        assert reg.resolve("t.api_vault_token", "PUT") is not None
        assert reg.resolve("t.api_vault_token", "GET") is None

    def test_resolve_before_build_is_not_an_error(self):
        """The chokepoint may run before the registry exists; it must degrade, not raise."""
        assert A.resolve("anything", "POST") is None
        with pytest.raises(A.ActionRegistryError, match="has not run yet"):
            A.registry()
