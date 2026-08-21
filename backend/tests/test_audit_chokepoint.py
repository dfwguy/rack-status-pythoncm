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
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    return app_module.app.test_client()


# --------------------------------------------------------------------------- TC-22


class TestTC22EveryMutatingRouteAudits:
    """AC-8: coverage discovered from the url_map, never a hand-written list."""

    def test_the_chokepoint_is_the_registry_not_a_path_prefix(self, app_module):
        """FR-6's actual mechanism: scope comes from the declaration, so it cannot drift.

        Driving all 103 mutating endpoints for real would execute privileged cluster operations,
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

        If a route ever has to opt in, this grep starts finding call sites and the chokepoint has
        stopped being one.
        """
        import pathlib
        import re

        api_dir = pathlib.Path(request_hook.__file__).resolve().parents[1] / "api"
        callers = [
            p.name for p in api_dir.glob("*.py")
            if re.search(r"\baudit\.emit\(", p.read_text(encoding="utf-8"))
        ]
        assert callers == ["terminal.py"], (
            f"only the WSGI terminal may call audit.emit directly (FR-10b); found {callers}"
        )


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
        """AC-24, and the derived name is distinguishable from the declared vocabulary."""
        monkeypatch.setenv("NVIS_AUDIT_READS", "1")
        client.get("/api/status")
        events = _events(tmp_path)
        assert len(events) == 1
        assert events[0]["action"].endswith(":read")

    def test_disabling_the_trail_writes_nothing(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIS_AUDIT_ENABLED", "0")
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert _events(tmp_path) == []


# --------------------------------------------------------------------------- TC-24 / TC-27


class TestTC24Refusals:
    def test_refusal_records_result_and_decision(self, app_module, tmp_path, monkeypatch):
        """AC-14: a gate-refused request is audited, immediately, with no flush step."""
        monkeypatch.delenv("RACK_WEB_AUTH_DISABLED", raising=False)
        monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
        monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "not-a-real-token")
        c = app_module.app.test_client()

        resp = c.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert resp.status_code in (401, 403), "the gate must refuse an unauthenticated request"

        events = _events(tmp_path)
        assert len(events) == 1, "written individually: one refusal is below the flood threshold"
        assert events[0]["result"] == audit.Result.REFUSED
        assert events[0]["decision"] == audit.Decision.NOT_EVALUATED

    def test_after_request_runs_for_a_gate_refusal(self, app_module, tmp_path, monkeypatch):
        """The verified assumption Decision 1 rests on: a refusal costs the same path."""
        monkeypatch.delenv("RACK_WEB_AUTH_DISABLED", raising=False)
        monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
        monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "not-a-real-token")
        c = app_module.app.test_client()
        c.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        assert _events(tmp_path), "no event means after_request never ran for the 401"


class TestTC27DecisionNotEvaluated:
    def test_decision_is_not_evaluated_for_both_outcomes(self, client, tmp_path):
        """AC-33, asserting the event *count* too so a deferred refusal cannot pass vacuously."""
        client.post(CANONICAL_ENDPOINT, json={"rack": "demo"})
        client.post("/api/slurm-drain", json={})
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
