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
"""Three Phase 3 acceptance criteria that nothing exercised.

* **AC-15g** — attach and detach must emit audited events naming the policy *and* the principal.
  They were decorated all along, so events fired; but the policy resolver is a fallback chain
  (`policy_id`, `id`, `subject`) and `policy_id` is in the URL, so the resource resolved to
  `policy:<id>` and the principal never reached the durable record. `request_hook._resource`'s own
  docstring named this gap while only half-closing it. Now carried by `name_target`.

* **AC-16a** — a refusal for a resource that does not exist must be byte-identical to one for a
  resource the caller may not touch. A test existed and asserted something weaker: that the
  resource name is absent from the reason. Necessary, not sufficient — absence of the name says
  nothing about the other fields, and *any* difference between the two answers is an oracle.

* **AC-18** — repeated authorization performs no write and opens no outbound connection. Nothing
  asserted either half. It matters because the engine is on the hot path of every mutating request:
  a socket there would put an authorization decision behind a network timeout, and a write would
  make the gate a contention point.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit
from authz.policy import PolicyEngine

ALICE = "user:alice"


def _engine(tmp_path: Path, documents: list[dict], attach: tuple[str, ...] = ()) -> PolicyEngine:
    policy_file = tmp_path / "policies.json"
    policy_file.write_text(json.dumps({"policies": documents}), encoding="utf-8")
    return PolicyEngine(
        policy_file, group_lookup=lambda _p: (), attachment_lookup=lambda _s: attach
    )


# --------------------------------------------------------------------------- AC-15g


class TestAC15gAttachmentIsAudited:
    """The event must answer "who was granted this", which is the whole point of the record."""

    @pytest.fixture
    def deployment(self, tmp_path, monkeypatch):
        """A real policy set and identity store, with the audit sink pointed at tmp_path."""
        import authz
        from identity import bootstrap, testing
        from identity.store import IdentityStore

        # The product's own administrator document, not a hand-written one. A bare `actions: ["*"]`
        # in an allow is refused by `Statement._no_bare_star_in_allow` — my first attempt used one
        # and every request came back 403 with `policy set unusable, denying all requests`, which
        # is the schema working correctly. `admin_policy_document()` enumerates `<type>:*` from
        # `declared_types()`, so this fixture also cannot fall behind a new resource type.
        policy_file = tmp_path / "policies.json"
        policy_file.write_text(
            json.dumps({"policies": [bootstrap.admin_policy_document()]}), encoding="utf-8"
        )
        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))
        monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
        monkeypatch.setenv("NVIS_AUDIT_FILE", str(tmp_path / "audit" / "audit.jsonl"))
        monkeypatch.setenv("NVIS_AUDIT_ENABLED", "1")

        import app as app_module

        store = IdentityStore(str(tmp_path / "identity.json"))
        try:
            bootstrap.ensure_admin(store)
        finally:
            store.close()

        captured = authz.current_engine()
        authz.install_default()
        audit.configure(force=True)
        try:
            # Enrol, then attach through the store: `enrol` has no `policies` argument, because
            # attachment is a separate relation from identity by design (the whole point of the
            # IAM-style model). Same shape as `test_policy_preview.py`'s `viewer`.
            testing.enrol("attacher", authorize=False)
            # `bob` is a real account because `attach_policy` verifies the principal end — the
            # store refuses `no such user 'bob'`. That asymmetry is deliberate (the policy end
            # cannot be verified, since policies live in a file an operator may be mid-edit on),
            # and it means an attachment audit can never name a principal who does not exist.
            testing.enrol("bob", authorize=False)
            attach_store = IdentityStore()
            try:
                attach_store.attach_policy("user:attacher", "administrators")
            finally:
                attach_store.close()

            from flask.testing import FlaskClient

            class _AsAttacher(testing.AuthenticatedClient, FlaskClient):
                nvis_principal_login = "attacher"

            app_module.app.config["TESTING"] = True
            previous = app_module.app.test_client_class
            app_module.app.test_client_class = _AsAttacher
            try:
                yield app_module.app, tmp_path
            finally:
                app_module.app.test_client_class = previous
        finally:
            authz.set_engine(captured)

    def _events(self, tmp_path) -> list[dict]:
        audit.flush()
        rows: list[dict] = []
        for line in (tmp_path / "audit").rglob("*.jsonl*"):
            for raw in line.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    rows.append(json.loads(raw))
        return rows

    def test_an_attach_names_both_the_policy_and_the_principal(self, deployment):
        app_module, tmp_path = deployment
        client = app_module.test_client()

        resp = client.post(
            "/api/policies/administrators/attachments", json={"subject": "user:bob"}
        )
        assert resp.status_code == 201, resp.get_data(as_text=True)

        attaches = [e for e in self._events(tmp_path) if e["action"] == "policy:attach"]
        assert attaches, "no policy:attach event was written"
        event = attaches[-1]
        assert "administrators" in event["resource"], f"policy not named: {event['resource']}"
        assert event.get("target") == "user:bob", (
            f"the principal granted the policy is not in the record: {event.get('target')!r}. "
            f"Without it the trail says a policy was attached and never to whom."
        )

    def test_a_detach_names_both(self, deployment):
        app_module, tmp_path = deployment
        client = app_module.test_client()

        client.post("/api/policies/administrators/attachments", json={"subject": "user:bob"})
        resp = client.delete("/api/policies/administrators/attachments/user:bob")
        assert resp.status_code == 200, resp.get_data(as_text=True)

        detaches = [e for e in self._events(tmp_path) if e["action"] == "policy:detach"]
        assert detaches, "no policy:detach event was written"
        assert detaches[-1].get("target") == "user:bob"

    def test_a_refused_attach_claims_no_grant(self, deployment):
        """The ordering that keeps the record honest: `name_target` runs only after the store
        accepts the attachment, so a failure cannot leave a record implying somebody was granted
        something."""
        app_module, tmp_path = deployment
        client = app_module.test_client()

        resp = client.post(
            "/api/policies/no-such-policy/attachments", json={"subject": "user:bob"}
        )
        assert resp.status_code == 404

        events = [e for e in self._events(tmp_path) if e["action"] == "policy:attach"]
        assert events, "the attempt should still be recorded"
        assert events[-1].get("target") is None, (
            "a failed attach named a target, so the trail claims a grant that did not happen"
        )

    def test_an_ordinary_event_carries_no_target_field(self, deployment):
        """`target` is an optional extra, not a new member of `EVENT_FIELDS` — which is a pinned
        contract. An empty string on every event would both break that test and add a column
        meaning "not applicable" to the overwhelming majority of rows."""
        app_module, tmp_path = deployment
        client = app_module.test_client()
        client.post("/api/policies/administrators/attachments", json={"subject": "user:bob"})

        others = [e for e in self._events(tmp_path) if e["action"] != "policy:attach"]
        for event in others:
            assert "target" not in event, f"{event['action']} carries a target it never named"


# --------------------------------------------------------------------------- AC-16a


class TestAC16aTheRefusalIsNotAnExistenceOracle:
    """NFR-3. The two refusals must be indistinguishable, not merely both unhelpful.

    The pre-existing test asserted the resource name is absent from the reason. That is necessary
    and not sufficient: an oracle is *any* observable difference, so the assertion has to compare
    the two answers with each other rather than inspect one of them.
    """

    GRANT: ClassVar[dict[str, object]] = {
        "id": "rack-a01",
        "statements": [{
            "sid": "one-rack", "effect": "allow",
            "actions": ["rack.power:*"], "resources": ["rack.power:a01"],
        }],
    }

    @pytest.fixture
    def engine(self, tmp_path):
        return _engine(tmp_path, [self.GRANT], attach=("rack-a01",))

    def test_the_control_a_granted_resource_is_permitted(self, engine):
        """Without this the comparison below is satisfied by an engine that refuses everything
        identically, which would pass while proving nothing."""
        assert engine.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

    def test_a_forbidden_and_a_nonexistent_resource_refuse_identically(self, engine):
        """`a02` exists and is not granted; `does-not-exist-at-all` is not a rack anywhere. A
        caller must not be able to tell which is which."""
        forbidden = engine.evaluate(ALICE, "rack.power:cycle", "rack.power:a02")
        absent = engine.evaluate(ALICE, "rack.power:cycle", "rack.power:does-not-exist-at-all")

        assert forbidden.allowed is absent.allowed is False
        assert forbidden.reason == absent.reason, (
            f"the refusals differ, so the reason distinguishes a resource that exists from one "
            f"that does not:\n  forbidden: {forbidden.reason!r}\n  absent:    {absent.reason!r}"
        )
        assert forbidden.engine == absent.engine

    def test_neither_reason_quotes_the_resource(self, engine):
        """Kept from the original test, because equality alone would also be satisfied by two
        refusals that each name their own resource in the same *shape*."""
        for resource in ("rack.power:a02", "rack.power:secret-lab-01"):
            reason = engine.evaluate(ALICE, "rack.power:cycle", resource).reason
            assert resource.split(":", 1)[1] not in reason

    def test_the_http_refusals_are_identical_too(self, engine):
        """The engine agreeing is not the whole contract — the caller sees a response body. A
        difference reintroduced at the API layer would be just as much an oracle."""
        import authz

        captured = authz.current_engine()
        authz.set_engine(engine)
        try:
            import app as app_module

            client = app_module.app.test_client()
            # `/api/power-on`. The first version posted to `/api/power-off`, which does not exist:
            # the 405 tripped a `pytest.skip` and the case silently asserted nothing at all — the
            # weak-test shape this whole exercise is about. Asserted, not skipped, so a gate that
            # stops refusing fails here.
            a = client.post("/api/power-on", json={"rack": "a02"})
            b = client.post("/api/power-on", json={"rack": "does-not-exist-at-all"})
        finally:
            authz.set_engine(captured)

        assert a.status_code == 403, (
            f"the gate did not refuse a rack outside the grant (got {a.status_code}); the "
            f"comparison below would then be vacuous"
        )
        assert a.status_code == b.status_code
        assert a.get_data() == b.get_data(), (
            "the two refusal bodies differ, which tells a prober which racks exist"
        )


# --------------------------------------------------------------------------- AC-18


class TestAC18AuthorizationIsPureAndLocal:
    """NFR-2. The engine sits on the hot path of every mutating request.

    A socket there would put an authorization verdict behind a network timeout; a write would make
    the gate a contention point between concurrent requests. Both are asserted directly rather
    than inferred from the benchmark, because a fast decision can still be an impure one.
    """

    GRANT: ClassVar[dict[str, object]] = {
        "id": "ops",
        "statements": [{
            "sid": "power", "effect": "allow",
            "actions": ["rack.power:*"], "resources": ["rack.power:*"],
        }],
    }

    #: **Both verdicts, and this is load-bearing.** The grant below allows `rack.power:*`, so a
    #: test that only asks about a permitted resource never walks the refusal path. A mutation that
    #: opened a socket in the `no policy grants ...` branch passed all eleven cases — the same
    #: under-coverage that made the rack-expansion test pass for the wrong reason. Every case here
    #: drives an allowed *and* a denied evaluation.
    PROBES = (
        ("rack.power:cycle", "rack.power:a01"),   # allowed by the grant
        ("rack.status:read", "rack.power:a01"),   # denied: action outside the grant
        ("secrets:read", "vault:prod"),           # denied: nothing matches at all
    )

    @pytest.fixture
    def engine(self, tmp_path):
        return _engine(tmp_path, [self.GRANT], attach=("ops",))

    def test_the_probes_cover_both_verdicts(self, engine):
        """Guards the two cases below. If every probe started being allowed — or every one denied —
        one of the engine's two paths would go unexercised and the guarantees would be half-tested
        while still passing."""
        verdicts = {
            engine.evaluate(ALICE, action, resource).allowed for action, resource in self.PROBES
        }
        assert verdicts == {True, False}, f"the probes no longer cover both paths: {verdicts}"

    def test_it_opens_no_socket(self, engine, monkeypatch):
        """Every constructor in `socket` raises, so any outbound attempt fails loudly rather than
        being counted — a count can be zero because the call was cached rather than absent."""
        def _refuse(*_a, **_kw):
            raise AssertionError("authorization opened a socket")

        monkeypatch.setattr(socket, "socket", _refuse)
        monkeypatch.setattr(socket, "create_connection", _refuse)
        monkeypatch.setattr(socket, "getaddrinfo", _refuse)

        for _ in range(50):
            for action, resource in self.PROBES:
                engine.evaluate(ALICE, action, resource)

    def test_it_writes_nothing(self, engine, tmp_path):
        """Including the policy file itself. `revocation_snapshot` once rewrote its file on every
        *read* — TinyDB's `remove()` writes even when nothing matches — which defeated its own
        mtime-keyed cache and was invisible to every functional test (`identity-auth-traps.mdc` §2).
        The same shape here would be just as silent, so mtime and size are both compared.
        """
        before = {
            p: (p.stat().st_mtime_ns, p.stat().st_size)
            for p in tmp_path.rglob("*") if p.is_file()
        }
        assert before, "nothing to observe; the fixture wrote no files"

        for _ in range(50):
            for action, resource in self.PROBES:
                engine.evaluate(ALICE, action, resource)

        after = {
            p: (p.stat().st_mtime_ns, p.stat().st_size)
            for p in tmp_path.rglob("*") if p.is_file()
        }
        assert after == before, (
            f"authorization touched the filesystem: "
            f"{ {p.name for p in set(after) ^ set(before)} or 'contents changed in place'}"
        )

    def test_repetition_is_what_is_measured(self, engine):
        """Guards the two cases above: both loop 50 times so a *cached* first decision cannot be
        the only thing exercised. If evaluation were memoised to a no-op, this still holds — but a
        per-call socket or write would have to survive 50 calls to pass, which it cannot."""
        for action, resource in self.PROBES:
            verdicts = {engine.evaluate(ALICE, action, resource).allowed for _ in range(50)}
            assert len(verdicts) == 1, f"{action} on {resource} gave an unstable verdict"
