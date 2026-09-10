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
"""Policy enforcement at the gate, over real HTTP (FR-9, FR-19; TC-18..TC-21).

The unit tests either side of this one prove the engine decides correctly and the resolver names
resources correctly. **Neither proves the two are wired together**, and that wiring is where the
original defect lived: the engine was right, the resolver did not exist, and the gate handed over a
URL. Everything below therefore goes through `app.test_client()` with the gate **on** and a real
policy file on disk — the only configuration in which the product's authorization actually runs.

Same structural reason as `test_gate_token_auth.py`: `conftest.py` sets `RACK_WEB_AUTH_DISABLED=1`
for every other module, so ~3,000 cases never reach `_require_dashboard_auth` at all (SUTP G-10).
"""

from __future__ import annotations

import json

import authz
import pytest
from identity import bootstrap, revocation
from identity import srp as srp_mod
from identity import tokens as tokens_mod
from identity.models import Group, User
from identity.store import IdentityStore

# A MUTATING route, deliberately. The action registry covers POST/PUT/PATCH/DELETE only -- there
# are zero GET actions -- so the gate consults the authorizer on mutations and never on reads. Using
# `/api/status` here silently tested nothing: it returned 200 with no policy attached, because no
# action resolved and the gate skipped authorization entirely. See TestReadsAreNotAuthorizedYet.
POWER_ON = "/api/power-on"
GROUPS = "/api/identity/groups"


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    """The gate on, a private identity store, a real policy file, and a signed-in operator."""
    identity_file = tmp_path / "identity.json"
    policy_file = tmp_path / "policies.json"
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(identity_file))
    monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("RACK_STATUS_DEMO", "1")  # no cluster to talk to

    srp_mod.reset_for_tests()
    revocation.reset_for_tests()
    revocation.install()

    store = IdentityStore(str(identity_file))
    try:
        bootstrap.ensure_admin(store, emit=False)
        store.create_user(User(login="operator", display_name="Operator"))
        store.create_group(Group(name="ops", description="rack operators"))
        store.add_member("ops", "operator")
    finally:
        store.close()

    from app import app

    app.config["TESTING"] = True

    class Harness:
        def __init__(self, client):
            self.client = client
            self.identity_file = identity_file

        def write_policies(self, documents) -> None:
            """Install a policy set and make the engine notice it.

            The engine keys its cache on (path, mtime), and a test writes twice inside one mtime
            tick on a coarse filesystem — so this stamps a distinct time rather than racing the
            clock. Without it a test passes or fails on filesystem timestamp granularity, which is
            the least debuggable flake there is.
            """
            import os

            policy_file.write_text(json.dumps({"version": 1, "policies": documents}))
            os.utime(policy_file, (self._tick, self._tick))
            self._tick += 1
            authz.install_default()

        _tick = 1_000_000

        def attach(self, subject, policy_id) -> None:
            s = IdentityStore(str(identity_file))
            try:
                s.attach_policy(subject, policy_id)
            finally:
                s.close()

        def sign_in_as(self, login) -> None:
            """A real minted access token, presented as a Bearer credential.

            Minting directly rather than driving the SRP handshake: the handshake is covered by
            `test_auth_srp_routes.py`, and repeating it here would make an authorization test fail
            for authentication reasons. Bearer rather than the `nvis_access` cookie because a
            cookie carries a domain that has to match the test client's — one more thing between
            this test and the behaviour it is about.
            """
            self._token = tokens_mod.mint(f"user:{login}", token_type=tokens_mod.TOKEN_ACCESS)

        _token = ""

        def get(self, path, **kw):
            return self.client.get(path, headers=self._auth(kw), **kw)

        def post(self, path, **kw):
            return self.client.post(path, headers=self._auth(kw), **kw)

        def _auth(self, kw):
            headers = dict(kw.pop("headers", {}) or {})
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            return headers

    with app.test_client() as c:
        yield Harness(c)

    srp_mod.reset_for_tests()


def _power_on(deployment):
    return deployment.post(POWER_ON, json={"rack": "a01"}, content_type="application/json")


def _permitted(deployment) -> bool:
    """Authorization let it through.

    Asserted as "not 403" rather than "200" deliberately. Past the gate the view applies its own
    input contract and answers 400 for a payload this test has no reason to satisfy -- asserting 200
    would couple every authorization test to the power-on request schema, so a change there would
    fail these tests for a reason that has nothing to do with policy.
    """
    return _power_on(deployment).status_code != 403


def _refused(deployment) -> bool:
    return _power_on(deployment).status_code == 403


ALLOW_POWER = {
    "id": "power-a01",
    "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                    "resources": ["rack.power:a01"]}],
}


class TestAPolicyDecidesARealRequest:
    def test_an_attached_grant_permits(self, deployment):
        deployment.write_policies([ALLOW_POWER])
        deployment.attach("user:operator", "power-a01")
        deployment.sign_in_as("operator")

        assert _permitted(deployment)

    def test_no_attachment_refuses_with_403(self, deployment):
        """403, not 401. The caller IS authenticated and simply may not act; a 401 would send a
        client into a credential-prompt loop it can never satisfy."""
        deployment.write_policies([ALLOW_POWER])
        deployment.sign_in_as("operator")  # policy exists, nothing attached

        response = deployment.post(POWER_ON, json={"rack": "a01"}, content_type="application/json")
        assert response.status_code == 403
        assert response.get_json()["ok"] is False

    def test_a_group_attachment_reaches_the_member(self, deployment):
        """The end-to-end path through the real store adapters, over HTTP."""
        deployment.write_policies([ALLOW_POWER])
        deployment.attach("group:ops", "power-a01")
        deployment.sign_in_as("operator")

        assert _permitted(deployment)

    def test_detaching_takes_effect_without_a_new_login(self, deployment):
        """AC-15a, and the property that makes revocation meaningful: the same cookie must stop
        working, or removing a grant is a request rather than an instruction."""
        deployment.write_policies([ALLOW_POWER])
        deployment.attach("group:ops", "power-a01")
        deployment.sign_in_as("operator")
        assert _permitted(deployment)

        store = IdentityStore(str(deployment.identity_file))
        try:
            store.detach_policy("group:ops", "power-a01")
        finally:
            store.close()

        assert _refused(deployment)

    def test_a_deny_beats_the_group_allow_over_http(self, deployment):
        deployment.write_policies([ALLOW_POWER, {
            "id": "no-power",
            "statements": [{"effect": "deny", "actions": ["rack.power:*"], "resources": ["*"]}],
        }])
        deployment.attach("group:ops", "power-a01")
        deployment.attach("user:operator", "no-power")
        deployment.sign_in_as("operator")

        assert _refused(deployment)


class TestTheResolvedResourceIsWhatThePolicySees:
    """TC-20. The wiring the original bug broke, asserted at the seam rather than inferred."""

    def test_the_engine_is_asked_about_a_resource_model_identifier(self, deployment):
        """A spy on `authorize`, because the resource the engine receives is not observable from
        the response — a 200 would look identical whether the gate passed `rack:a01` or
        `/api/status`, which is precisely how the defect stayed hidden."""
        seen: list[tuple[str, str, str]] = []
        real = authz.authorize

        def spy(principal, action, resource):
            seen.append((principal, action, resource))
            return real(principal, action, resource)

        deployment.write_policies([ALLOW_POWER])
        deployment.attach("user:operator", "power-a01")
        deployment.sign_in_as("operator")

        import app as app_mod

        original = app_mod.authz.authorize
        app_mod.authz.authorize = spy
        try:
            deployment.post(POWER_ON, json={"rack": "a01"}, content_type="application/json")
        finally:
            app_mod.authz.authorize = original

        assert seen, "the gate did not consult the authorizer at all"
        _, action, resource = seen[0]
        assert not resource.startswith("/"), (
            f"the gate passed a URL path as the resource ({resource!r}) — no policy can name that, "
            "so every resource-scoped statement silently matches nothing"
        )
        assert resource.startswith(action.split(":", 1)[0] + ":"), (
            f"resource {resource!r} is not an identifier of the action's own type ({action!r})"
        )


class TestAnUnidentifiableTargetIsRefused:
    """FR-6a. A resolution failure must not become a grant."""

    def test_a_missing_rack_is_refused_even_with_a_wildcard_grant(self, deployment):
        """The hazard in one test: `rack.power:*` is what an operator writes for "may power-cycle
        racks", and it matches any filler id. So the gate has to refuse *before* evaluating."""
        deployment.write_policies([{
            "id": "power-everything",
            "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                            "resources": ["rack.power:*"]}],
        }])
        deployment.attach("user:operator", "power-everything")
        deployment.sign_in_as("operator")

        response = deployment.post(
            "/api/power-on", json={}, content_type="application/json"
        )
        # 400, not 403: the request never named its target, so it is malformed rather than
        # forbidden. Telling the caller "not authorized" would send them chasing permissions they
        # already hold. Still fail-closed -- the action does not execute.
        assert response.status_code == 400
        body = response.get_json() or {}
        assert "could not be identified" in str(body.get("error", "")), body

    def test_the_refusal_does_not_echo_the_resolver_internals(self, deployment):
        """The log gets the detail; the caller gets a stable sentence. A response naming the field
        the resolver wanted is a map of the request schema for anyone probing (NFR-3)."""
        deployment.write_policies([])
        deployment.sign_in_as("operator")

        body = deployment.post(
            "/api/power-on", json={}, content_type="application/json"
        ).get_json() or {}
        assert "resources.py" not in json.dumps(body)
        assert "rack_id" not in json.dumps(body)


class TestTheGateDoesNotEatTheRequestBody:
    """The subtle one, and the reason `_request_facts` is documented the way it is.

    The resolver reads the JSON body to find a rack id. Flask caches the parsed body, so this is
    safe — but a future change to read `request.data` or the raw stream would leave every view with
    an empty body, and the breakage would land only on POST routes with a payload. Cheap to pin,
    genuinely expensive to debug.
    """

    def test_a_view_still_sees_the_body_the_resolver_read(self, deployment):
        deployment.write_policies([{
            "id": "power",
            "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                            "resources": ["rack.power:a01"]}],
        }])
        deployment.attach("user:operator", "power")
        deployment.sign_in_as("operator")

        response = deployment.post(
            "/api/power-on",
            json={"rack": "a01", "nodes": "a01-h15-15"},
            content_type="application/json",
        )

        # Authorized on `rack.power:a01`, so the resolver read `rack` out of the body. If the view
        # then saw an empty body it would answer 400 "rack required" — which is the failure this
        # asserts against, regardless of what the demo backend does with the request.
        assert response.status_code != 403
        body = response.get_json() or {}
        assert "rack" not in str(body.get("error", "")).lower(), (
            f"the view could not see the body the resolver read: {body}"
        )


class TestReadsAreGoverned:
    """Reads entered the vocabulary in Phase 3, so a policy can now restrict them.

    This class replaces `TestReadsAreNotAuthorizedYet`, whose whole purpose was to fail when this
    changed — it did, which is the only reason the swap is a reviewed diff rather than a surprise.

    The gap it recorded: the action vocabulary was built in Phase 1 for **auditing**, where covering
    mutations only is right, and Phase 3 reused it for **authorization** and inherited that scope.
    118 read routes now carry declarations, so "may view rack status" is expressible and a read-only
    role is achievable.
    """

    def test_a_read_is_refused_with_nothing_attached(self, deployment):
        """The headline change. This returned 200 before, because no action resolved for a GET and
        the gate skipped authorization entirely."""
        deployment.write_policies([])
        deployment.sign_in_as("operator")

        assert deployment.get("/api/status").status_code == 403

    def test_a_read_grant_permits_the_read(self, deployment):
        deployment.write_policies([{
            "id": "viewer",
            "statements": [{"effect": "allow", "actions": ["rack:list"], "resources": ["*"]}],
        }])
        deployment.attach("user:operator", "viewer")
        deployment.sign_in_as("operator")

        assert deployment.get("/api/status").status_code == 200

    def test_a_deny_now_stops_a_read(self, deployment):
        """Previously an explicit `deny *` could not stop a read, because the gate never asked."""
        deployment.write_policies([{
            "id": "deny-all",
            "statements": [{"effect": "deny", "actions": ["*"], "resources": ["*"]}],
        }])
        deployment.attach("user:operator", "deny-all")
        deployment.sign_in_as("operator")

        assert deployment.get("/api/status").status_code == 403

    def test_a_read_only_role_cannot_mutate(self, deployment):
        """The role the old scope made impossible, asserted from both sides — a grant that let a
        viewer power-cycle a rack would be worse than no read control at all."""
        deployment.write_policies([{
            "id": "read-only",
            "statements": [{"effect": "allow", "actions": ["rack:list"], "resources": ["*"]}],
        }])
        deployment.attach("user:operator", "read-only")
        deployment.sign_in_as("operator")

        assert deployment.get("/api/status").status_code == 200
        assert _refused(deployment), "a read-only grant permitted a mutation"

    def test_a_write_grant_does_not_confer_the_read(self, deployment):
        """The inverse leak. It is available by accident: a route-wide `@action("x")` used to name
        every method on its route, so a GET+PUT endpoint registered its GET under the write verb.
        `test_a_route_wide_declaration_does_not_name_a_read` guards the mechanism; this guards the
        outcome.
        """
        deployment.write_policies([ALLOW_POWER])
        deployment.attach("user:operator", "power-a01")
        deployment.sign_in_as("operator")

        assert _permitted(deployment)
        assert deployment.get("/api/status").status_code == 403


class TestCollectionsAndInstancesAreDifferentResources:
    """`:list` resolves to the collection, `:read` to an instance (FR-6b).

    Forced by the shape of the routes: 103 of 118 reads carry no id, so a `<type>:<id>` resolver
    would refuse every one. It is also the IAM model — `ListBucket` acts on the bucket, `GetObject`
    on the object.
    """

    def test_a_list_verb_needs_no_id(self, deployment):
        from authz import resources as R

        assert R.resolve("rack:list", R.RequestFacts({}, {}, {})) == "rack:_all"

    def test_a_wildcard_over_instances_also_grants_listing(self, deployment):
        """Deliberate: "may read all racks" should include being able to enumerate them."""
        deployment.write_policies([{
            "id": "all-racks",
            "statements": [{"effect": "allow", "actions": ["rack:*"], "resources": ["rack:*"]}],
        }])
        deployment.attach("user:operator", "all-racks")
        deployment.sign_in_as("operator")

        assert deployment.get("/api/status").status_code == 200

    def test_a_single_instance_grant_does_not_confer_listing(self, deployment):
        """The other side of the same choice, and the one to know about: a UI showing the rack
        table needs `rack:_all` or a wildcard, not `rack:a01`."""
        deployment.write_policies([{
            "id": "just-a01",
            "statements": [{"effect": "allow", "actions": ["rack:*"], "resources": ["rack:a01"]}],
        }])
        deployment.attach("user:operator", "just-a01")
        deployment.sign_in_as("operator")

        assert deployment.get("/api/status").status_code == 403

    def test_the_collection_id_cannot_collide_with_a_real_one(self):
        """`_all` is reserved by shape: cluster ids are site slugs and catalog ids are
        operator-chosen names, and neither may begin with an underscore."""
        from authz import resources as R

        assert R.COLLECTION.startswith("_")


class TestTheOperatorLabelIsEvidence:
    """FR-16 / AC-26 — the durable record names the authenticated principal, not a header.

    `operator_from_request()` read `X-Rack-Operator` unconditionally and stamped it onto rack lock
    claims and validation activities. It was correctly never trusted for *authorization*, and its
    docstring said so — but that was never the whole job. Those records answer "who locked this
    rack" and "who ran this on that tray" after the process that did it is gone, which is the
    question every handover asks. A label anyone could set with a curl flag made the answer
    unprovable, so the ledger held a *claim* rather than evidence.

    Asserted through the **stored artifact** rather than by spying on the function: a rack lock is
    exactly the durable record the requirement is about, and reading back what was persisted proves
    the whole path — gate, principal, label, store — rather than one call. All 47 call sites across
    12 api modules route through that one function.
    """

    def _signed_in_with_everything(self, deployment):
        """Grants the lock verbs by pattern, NOT with a bare `*`.

        A bare `*` in an allow is refused by the schema on purpose, so a policy using one fails to
        load and the engine default-denies the whole request — which is what the first draft of
        this test hit, and is the guard behaving correctly rather than a test problem.
        """
        deployment.write_policies([{
            "id": "lock-operator",
            "statements": [{
                "effect": "allow",
                "actions": ["rack.lock:*", "rack:*"],
                "resources": ["rack.lock:*", "rack:*"],
            }],
        }])
        deployment.attach("user:operator", "lock-operator")
        deployment.sign_in_as("operator")

    def _stored_operators(self, deployment):
        body = deployment.get("/api/rack-locks").get_json() or {}
        rows = body.get("locks") or body.get("claims") or body.get("data") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        return [str(r.get("operator", "")) for r in rows if isinstance(r, dict)]

    def test_a_spoofed_header_cannot_override_the_principal(self, deployment):
        """The headline property. Previously the header won outright, so this stored
        `somebody-else` — a name nobody authenticated as."""
        self._signed_in_with_everything(deployment)

        claim = deployment.post(
            "/api/rack-locks/p17-r01",
            json={},
            content_type="application/json",
            headers={"X-Rack-Operator": "somebody-else"},
        )
        assert claim.status_code < 400, claim.get_json()

        stored = self._stored_operators(deployment)
        assert stored, "the claim was not persisted, so this test proves nothing"
        assert "somebody-else" not in stored, (
            f"a client header was stored as the operator on a durable lock claim: {stored} — the "
            "activity ledger records a forgeable name"
        )
        assert "operator" in stored, f"expected the authenticated login, stored {stored}"

    def test_the_label_is_the_bare_login_not_the_principal_rendering(self, deployment):
        """`operator`, not `user:operator`. The label is shown to operators and stored beside
        free-text history; nothing compares it, so readability wins."""
        self._signed_in_with_everything(deployment)
        deployment.post("/api/rack-locks/p17-r02", json={}, content_type="application/json")

        assert all(not s.startswith("user:") for s in self._stored_operators(deployment))

    def test_the_header_is_ignored_even_with_no_principal(self):
        """**Inverted on 2026-09-10.** This asserted the opposite: that `X-Rack-Operator` was
        honoured when `g` held no principal, which was the fallback's one purpose under
        `RACK_WEB_AUTH_DISABLED`.

        That switch was removed on 2026-09-08, which made the fallback unreachable on any gated
        route — and left a client-supplied string able to name the operator in a durable record if
        it ever became reachable again. So the fallback is gone, and this now pins its absence: the
        header names nobody, ever. An empty label says "we do not know", which is what makes the
        record evidence rather than a field the caller filled in.
        """
        from api import _common
        from app import app

        with app.test_request_context("/api/status", headers={"X-Rack-Operator": "dev-laptop"}):
            assert _common.operator_from_request() == ""

    def test_it_returns_empty_outside_a_request(self):
        """Worker threads call this too, and must not raise."""
        from api import _common

        assert _common.operator_from_request() == ""
