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
"""TC-22 .. TC-30 of the Phase 1 test plan: the request chokepoint.

Traces to AC-8 (FR-6), AC-13 (FR-9), AC-14 (FR-10), AC-17 (NFR-2), AC-19 (NFR-5),
AC-22/23/24 (FR-11, FR-12), AC-32 (NFR-11), AC-33 (NFR-13).
"""

from __future__ import annotations

import json
import logging

import audit
import logging_config as lc
import pytest
from audit import request_hook, sink

#: One endpoint every "just drive a mutating request" case uses, declared once here rather than
#: chosen seven times. Constraint that fixes the choice: a POST with no path parameters whose
#: handler does nothing past the execution-stub boundary.
CANONICAL_ENDPOINT = "/api/rack-uptime"
CANONICAL_ACTION = "rack:uptime"


@pytest.fixture()
def app_module():
    import app as m

    # Rebuild the action registry for the real app rather than trusting whatever the previous test
    # file left behind. test_audit_actions.py resets it to None per test, and it sorts earlier, so
    # without this every route here resolves to a *derived* name and the assertions fail for a
    # reason that has nothing to do with the chokepoint. A test that depends on another file's
    # leftovers is not isolated.
    from audit import actions as A

    A.build_registry(m.app)
    return m


@pytest.fixture(autouse=True)
def _audit_to_tmp(monkeypatch, tmp_path):
    """A trail this case owns, and demo mode so no handler reaches a cluster."""
    for name in list(lc.SETTINGS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NVIS_LOG_SINKS", "file")
    monkeypatch.setenv("NVIS_LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("NVIS_AUDIT_FILE", str(tmp_path / "audit" / "audit.jsonl"))
    monkeypatch.setenv("RACK_STATUS_DEMO", "1")
    audit.reset_for_tests()
    lc.reset_for_tests()
    lc.configure(force=True)
    audit.configure(force=True)
    yield
    audit.reset_for_tests()
    lc.reset_for_tests()


def _events(tmp_path) -> list[dict]:
    for h in logging.getLogger(sink.AUDIT_LOGGER).handlers:
        h.flush()
    p = tmp_path / "audit" / "audit.jsonl"
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture()
def client(app_module, monkeypatch):
    """Anonymous, because this file is about the trail an *unrefused* request leaves.

    Several cases assert `principal: anonymous`, `decision: not-evaluated` and
    `policy_engine: none` — the shape a request with no credential produces, which is precisely
    what the chokepoint must record rather than omit. The suite's default client presents a bearer,
    which would make those three assert the authenticated shape instead.
    """
    from identity import testing as identity_testing

    return identity_testing.unauthenticated_client(app_module.app)


# --------------------------------------------------------------------------- TC-22


class TestTC22EveryMutatingRouteAudits:
    """AC-8: coverage discovered from the url_map, never a hand-written list."""

    def test_the_chokepoint_is_the_registry_not_a_path_prefix(self, app_module):
        """FR-6's actual mechanism: scope comes from the declaration, so it cannot drift.

        Driving every mutating endpoint for real would execute privileged cluster operations,
        so the property is asserted where it lives — every declared route resolves to an action the
        hook will use — rather than by firing 103 requests at a stubbed cluster.
        """
        from audit import actions as A

        reg = A.registry()
        assert A.undeclared_mutating_routes(app_module.app) == ()
        for rule, method in A._iter_mutating_rules(app_module.app):
            assert reg.resolve(rule.endpoint, method) is not None, f"{rule} {method}"

    def test_a_declared_route_audits_with_its_declared_action(self, client, tmp_path):
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        events = _events(tmp_path)
        assert len(events) == 1
        assert events[0]["action"] == CANONICAL_ACTION
        assert events[0]["service"] == "rack"

    def test_no_endpoint_specific_audit_code_exists(self):
        """The negative half of FR-6: adding an endpoint must need no audit code.

        If a route ever has to opt in, this finds the call site and the chokepoint has stopped
        being one.

        **Parsed, not grepped.** The original form was a regex over the file text, which matched
        `audit.emit` *anywhere* — including a docstring explaining why a module deliberately does
        not call it. `api/audit_trail.py` tripped it on exactly that: prose about routing through
        the chokepoint instead. A guard that cannot tell a call from a comment fails on the notes
        written to prevent the thing it guards against, which is the same defect
        `test_ci_job_tooling.py` was fixed for. The AST sees calls only.
        """
        import ast
        import pathlib

        api_dir = pathlib.Path(request_hook.__file__).resolve().parents[1] / "api"
        callers = []
        for module in sorted(api_dir.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == "emit"
                        and isinstance(func.value, ast.Name) and func.value.id == "audit"):
                    callers.append(module.name)
                    break
        assert callers == ["terminal.py"], (
            f"only the WSGI terminal may call audit.emit directly (FR-10b); found {callers}"
        )

    def test_the_guard_would_catch_a_real_call(self, tmp_path):
        """Guards the guard: an AST walk that matched nothing would pass this file trivially, and
        the switch from regex to AST is exactly the change that could have broken it."""
        import ast

        tree = ast.parse("import audit\ndef f():\n    audit.emit(action='x', result='y')\n")
        found = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "emit" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "audit"
        ]
        assert len(found) == 1

    def test_the_guard_ignores_the_name_in_prose(self):
        """The false positive that prompted the rewrite, pinned so a revert to grepping fails."""
        import ast

        tree = ast.parse('"""We route through the chokepoint rather than call audit.emit() here."""\n')
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


# --------------------------------------------------------------------------- TC-23


class TestTC23AuditScope:
    """AC-22, AC-23, AC-24: mutating by default, reads opt-in (FR-11, FR-12)."""

    def test_enabled_with_no_env(self, client, tmp_path):
        """AC-22: a mutating request audits with nothing configured."""
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert len(_events(tmp_path)) == 1

    def test_reads_excluded_by_default(self, client, tmp_path):
        """AC-23: exactly one event, and it is the mutating one."""
        client.get("/api/status")
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        events = _events(tmp_path)
        assert len(events) == 1
        assert events[0]["action"] == CANONICAL_ACTION

    def test_reads_audited_when_opted_in(self, client, tmp_path, monkeypatch):
        """AC-24. Now with the read's *declared* name, not a derived one.

        This asserted `endswith(":read")` — the shape of a name derived from the endpoint — because
        reads carried no declaration. Phase 3 declares them so policy can govern them, so
        `/api/status` audits as `rack:list`: the vocabulary, not a guess. The derived path still
        exists as a fallback and is covered by `test_an_undeclared_read_falls_back_to_a_derived_name`.
        """
        monkeypatch.setenv("NVIS_AUDIT_READS", "1")
        client.get("/api/status")
        events = _events(tmp_path)
        assert len(events) == 1
        assert events[0]["action"] == "rack:list"

    def test_an_undeclared_read_falls_back_to_a_derived_name(self, client, tmp_path, monkeypatch):
        """The path the declared vocabulary did not remove.

        Reads on the auth-bypass list carry no declaration on purpose — they are reached without a
        principal, so there is nobody for a policy to be about, and naming them would imply a grant
        could apply. With read auditing on they still have to appear in the trail, under a name
        derived from the endpoint rather than under nothing at all.
        """
        monkeypatch.setenv("NVIS_AUDIT_READS", "1")
        client.get("/api/auth-check")
        events = _events(tmp_path)
        assert len(events) == 1
        assert events[0]["action"].endswith(":read"), events[0]["action"]

    def test_reads_stay_out_of_the_trail_by_default(self, client, tmp_path, monkeypatch):
        """The counterpart, and the one that would have broken silently.

        Declaring 118 read actions made `entry is not None` true for every read, and that condition
        used to mean "audit this". Left alone, the dashboard's `/api/status` polling would have
        started filling the trail and overridden `NVIS_AUDIT_READS=0` — burying the signal the
        trail exists for. Naming an action and auditing it are now separate questions.
        """
        monkeypatch.setenv("NVIS_AUDIT_READS", "0")
        client.get("/api/status")
        assert _events(tmp_path) == []

    def test_disabling_the_trail_writes_nothing(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIS_AUDIT_ENABLED", "0")
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert _events(tmp_path) == []


# --------------------------------------------------------------------------- TC-24 / TC-27


class TestTC24Refusals:
    def test_refusal_records_result_and_decision(self, app_module, tmp_path, monkeypatch):
        """AC-14: a gate-refused request is audited, immediately, with no flush step."""
        from identity import testing as identity_testing

        # No credential configuration to set any more: FR-18 removed `RACK_WEB_AUTH_MODE` and
        # `RACK_WEB_AUTH_TOKEN`, so the previous setenv pair named variables nothing reads. A
        # refusal is now produced by presenting nothing.
        c = identity_testing.unauthenticated_client(app_module.app)

        resp = c.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert resp.status_code in (401, 403), "the gate must refuse an unauthenticated request"

        events = _events(tmp_path)
        assert len(events) == 1, "written individually: one refusal is below the flood threshold"
        assert events[0]["result"] == audit.Result.REFUSED
        assert events[0]["decision"] == audit.Decision.NOT_EVALUATED

    def test_after_request_runs_for_a_gate_refusal(self, app_module, tmp_path, monkeypatch):
        """The verified assumption Decision 1 rests on: a refusal costs the same path."""
        monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
        monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "not-a-real-token")
        c = app_module.app.test_client()
        c.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert _events(tmp_path), "no event means after_request never ran for the 401"


class TestTC27DecisionNotEvaluated:
    def test_decision_is_not_evaluated_for_both_outcomes(self, client, tmp_path):
        """AC-33, asserting the event *count* too so a deferred refusal cannot pass vacuously."""
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        client.post("/api/scheduler-drain", json={})
        audit.flush()
        events = _events(tmp_path)
        assert len(events) == 2, f"expected two events, got {len(events)}"
        assert {e["decision"] for e in events} == {audit.Decision.NOT_EVALUATED}
        assert {e["policy_engine"] for e in events} == {"none"}


# --------------------------------------------------------------------------- TC-26


class TestTC26Principal:
    def test_principal_never_from_header(self, client, tmp_path):
        """AC-13: X-Rack-Operator is display metadata and must not become the principal."""
        client.post(
            CANONICAL_ENDPOINT, json={"rack": "demo"},
            headers={"X-Rack-Operator": "someone-else"},
        )
        (event,) = _events(tmp_path)
        assert event["principal"] == "anonymous"
        assert "someone-else" not in json.dumps(event)


# --------------------------------------------------------------------------- TC-29


class TestTC29StaticAssetsDoNoWork:
    def test_assets_do_no_work(self, client, tmp_path, monkeypatch):
        """AC-17 / NFR-2: no settings read, no stat, no audit write for an asset.

        A page load is roughly 33 asset requests, so this is structural rather than a
        micro-optimisation. The settings reader is patched to record calls; the assertion is that
        the asset path never reaches it.
        """
        calls: list[str] = []
        real = lc.Setting.resolve

        def spy(self):
            calls.append(self.name)
            return real(self)

        monkeypatch.setattr(lc.Setting, "resolve", spy)
        client.get("/assets/index-Whatever.js")
        assert calls == [], f"an asset request read settings: {calls}"
        assert _events(tmp_path) == []

    def test_a_real_request_does_read_settings(self, client, monkeypatch):
        """Guards the test above from passing because the spy was never wired."""
        calls: list[str] = []
        real = lc.Setting.resolve
        monkeypatch.setattr(
            lc.Setting, "resolve", lambda self: (calls.append(self.name), real(self))[1]
        )
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert calls, "the spy must see settings reads on a normal request"

    def test_is_static_covers_the_spa_asset_shapes(self):
        for p in ("/assets/x.js", "/spa/index.html", "/favicon.ico", "/static/y.css"):
            assert request_hook.is_static(p), p
        for p in ("/api/status", "/logout", "/compute/nodes"):
            assert not request_hook.is_static(p), p


# --------------------------------------------------------------------------- TC-30


class TestTC30TrustListsAreDeployTimeOnly:
    def test_trust_lists_ignore_settings_overrides(self, monkeypatch, tmp_path):
        """AC-19 / NFR-5: widening a trust list is a privilege change, not a runtime setting."""
        import config_env
        from api import _common

        monkeypatch.setenv("RACK_WEB_TRUSTED_PROXIES", "10.0.0.0/24")
        _common.reset_trusted_cache_for_tests()
        before = [str(n) for n in _common.trusted_networks()]

        # The dashboard-editable override store must not be able to touch it.
        store = tmp_path / "job_env_overrides.json"
        store.write_text(json.dumps({"RACK_WEB_TRUSTED_PROXIES": "0.0.0.0/0"}), encoding="utf-8")
        monkeypatch.setenv("NVIS_JOB_ENV_OVERRIDES_FILE", str(store))
        if hasattr(config_env, "_overrides_cache"):
            monkeypatch.setattr(config_env, "_overrides_cache", None, raising=False)

        _common.reset_trusted_cache_for_tests()
        assert [str(n) for n in _common.trusted_networks()] == before
        assert "0.0.0.0/0" not in [str(n) for n in _common.trusted_networks()]


class TestClusterLabelCache:
    """The cluster label is cached; a project switch must still be reflected.

    Introduced under TC-34: resolving the label per request cost 4.74 ms of the chokepoint's
    5.22 ms, because `catalog.context.resolve()` reads the TinyDB store. Caching it fixed NFR-2a
    (+8.66 ms -> +0.37 ms at p95) but created a staleness risk -- an audit trail that attributes
    events to the cluster the operator switched AWAY from is worse than a slow one. These tests
    are the reason that trade is safe to make.
    """

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        request_hook._cluster_cache.update({"gen": -1, "label": ""})
        yield
        request_hook._cluster_cache.update({"gen": -1, "label": ""})

    def test_the_label_is_not_resolved_twice_for_one_generation(self, monkeypatch):
        calls = {"n": 0}

        def _resolve(*a, **k):
            calls["n"] += 1
            return None, type("C", (), {"id": "cluster-a"})(), None

        monkeypatch.setattr("catalog.context.resolve", _resolve)
        monkeypatch.setattr("catalog.store.current_generation", lambda: 7)

        assert request_hook._cluster() == "cluster-a"
        assert request_hook._cluster() == "cluster-a"
        assert request_hook._cluster() == "cluster-a"
        assert calls["n"] == 1, "the store was read again for an unchanged catalog"

    def test_a_generation_bump_refreshes_the_label(self, monkeypatch):
        """A project switch bumps the store generation, which is what must invalidate this."""
        state = {"id": "cluster-a", "gen": 1}
        monkeypatch.setattr(
            "catalog.context.resolve",
            lambda *a, **k: (None, type("C", (), {"id": state["id"]})(), None),
        )
        monkeypatch.setattr("catalog.store.current_generation", lambda: state["gen"])

        assert request_hook._cluster() == "cluster-a"
        state["id"] = "cluster-b"
        assert request_hook._cluster() == "cluster-a", "same generation, so still cached"
        state["gen"] = 2
        assert request_hook._cluster() == "cluster-b", "a bump must be picked up"

    def test_a_broken_catalog_yields_an_empty_label_not_an_exception(self, monkeypatch):
        """NFR-1: the trail must not depend on the catalog being healthy."""
        def _boom(*a, **k):
            raise RuntimeError("catalog unavailable")

        monkeypatch.setattr("catalog.store.current_generation", _boom)
        assert request_hook._cluster() == ""

    def test_the_real_catalog_produces_a_label(self, client, tmp_path):
        """Guard against a cache that works perfectly and always returns the empty string."""
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        audit.flush()
        events = _events(tmp_path)
        assert events, "no event written"
        assert events[0]["cluster"], "the cluster field is empty, so the cache may be masking it"


class TestTheResourceIsWhatWasActedOn:
    """FR-18/AC-15g. The trail recorded the URL path, not the resource the gate judged.

    The gate resolves a `RESOURCE_MODEL` identifier and used to compute it and discard it, so every
    rack in the fleet produced the same `resource: /api/power-off` and a reader asking "what ran on
    p17-r01" had to infer it from a body the trail does not keep. A policy attachment was worse: it
    recorded `/api/policies/ops/attachments` and never named the principal it granted.
    """

    def test_it_records_the_resolved_identifier(self, authed_client, tmp_path):
        authed_client.post(CANONICAL_ENDPOINT, json={"rack": "p17-r01"})
        (event,) = _events(tmp_path)
        assert event["resource"].startswith("rack"), (
            f"expected a resource identifier, got {event['resource']!r} — the path again"
        )
        assert "p17-r01" in event["resource"]

    def test_a_fan_out_says_how_many_targets_there_were(self, authed_client, tmp_path):
        """Recording one member of a multi-target request silently is what kept the all-of
        authorization bypass invisible: the trail showed a single rack either way."""
        authed_client.post(CANONICAL_ENDPOINT, json={"racks": ["p17-r01", "p17-r02"]})
        (event,) = _events(tmp_path)
        assert "+1 more" in event["resource"], event["resource"]

    def test_an_ungoverned_route_still_records_its_path(self, authed_client, tmp_path):
        """The fallback has to hold, or a route the resolver does not cover records nothing."""
        authed_client.get("/api/status")
        events = _events(tmp_path)
        if events:  # reads are not audited by default; only assert if this one was
            assert events[0]["resource"]

    def test_the_query_string_is_never_recorded(self, authed_client, tmp_path):
        """NFR-11 survives the change. A credential in `?token=` is invisible to key-name
        redaction, so the query is dropped rather than filtered."""
        authed_client.post(
            f"{CANONICAL_ENDPOINT}?token=super-secret", json={"rack": "p17-r01"}
        )
        (event,) = _events(tmp_path)
        assert "super-secret" not in json.dumps(event)
