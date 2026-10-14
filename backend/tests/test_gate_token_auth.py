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
"""The gate now verifies a token, and asks an authorizer — FR-12, FR-16, FR-18.

**These are the tests the rest of the suite structurally cannot provide.** `conftest.py` sets
`RACK_WEB_AUTH_DISABLED=1` for every other module, so ~2,500 cases reach every endpoint with no
credential and never touch `_require_dashboard_auth` at all (SUTP gap G-10). This file turns the
gate **on** and drives it, which is the only way any of the following is verified.

Two separations matter more than the individual cases:

**401 is not 403.** An unauthenticated caller is told to authenticate; an authenticated caller who
may not act is told they may not. Returning 401 for a policy denial sends a client into a
credential-prompt loop it can never satisfy, and returning 403 for a missing credential hides the
fact that logging in would help. `docs/API_RESPONSE_CODES.md` fixes this and these tests hold it.

**The principal is the token's subject, and nothing else can supply it.** That is what turns the
activity ledger and the rack locks from advisory records into evidence (FR-16).
"""

from __future__ import annotations

import authz
import pytest
import srp as srp_lib
from identity import bootstrap, revocation
from identity import srp as srp_mod
from identity import tokens as tokens_mod
from identity.store import IdentityStore

PASSWORD = "gate-under-test-password"
PROTECTED = "/api/status"


@pytest.fixture
def gated(monkeypatch, tmp_path):
    """The gate ON, a private identity store, and a known administrator credential."""
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    srp_mod.reset_for_tests()
    revocation.reset_for_tests()
    revocation.install()
    authz.install_default()

    # The SRP rate limiter is module-level state keyed by client address, and every test here logs
    # in from 127.0.0.1. Without this reset the bucket carries over from whatever ran earlier in the
    # session — so these tests pass alone and 429 in a full run, which is the least useful failure
    # mode there is.

    store = IdentityStore(str(tmp_path / "identity.json"))
    try:
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        salt, verifier = srp_mod.enroll(bootstrap.ADMIN_LOGIN, PASSWORD)
        store.update_user(user.edited(srp_salt=salt, srp_verifier=verifier))
        # This fixture owns its identity store but shares the session's policy file, so the
        # documents exist while *this* administrator has no attachment — default-deny, and every
        # authenticated request here would be a 403 about authorization in a file about
        # authentication. Attached directly rather than through `ensure_admin_policy`, which
        # deliberately does nothing once a policy set exists.
        store.attach_policy(f"user:{bootstrap.ADMIN_LOGIN}", bootstrap.ADMIN_POLICY_ID)
    finally:
        store.close()

    from app import app

    # Anonymous by default. This file's subject is the gate, and every test either asserts a
    # refusal or obtains its own credential through the real SRP exchange (`_log_in`), whose cookie
    # lands in the client jar. The suite's default client presents a minted bearer, which would
    # make every 401 assertion here pass for the wrong reason — or rather, fail.
    from identity import testing as identity_testing

    with identity_testing.unauthenticated_client(app) as c:
        yield c

    srp_mod.reset_for_tests()
    revocation.reset_for_tests()
    revocation.install()
    authz.install_default()


def _log_in(client) -> str:
    """Drive a real SRP login. Returns the access token."""
    srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
    user = srp_lib.User(
        bootstrap.ADMIN_LOGIN, PASSWORD, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
    )
    _name, a = user.start_authentication()
    begin = client.post(
        "/api/auth/srp/begin", json={"login": bootstrap.ADMIN_LOGIN, "A": a.hex()}
    ).get_json()
    proof = user.process_challenge(bytes.fromhex(begin["salt"]), bytes.fromhex(begin["B"]))
    resp = client.post(
        "/api/auth/srp/finish", json={"token": begin["token"], "M": proof.hex()}
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return client.get_cookie("nvis_access").value


class TestAuthentication:
    def test_no_credential_is_refused(self, gated):
        assert gated.get(PROTECTED).status_code == 401

    def test_the_refusal_carries_a_challenge(self, gated):
        """RFC 9110 §11.6.1 makes `WWW-Authenticate` mandatory on a 401."""
        assert "WWW-Authenticate" in gated.get(PROTECTED).headers

    def test_a_real_login_gets_through(self, gated):
        _log_in(gated)
        assert gated.get(PROTECTED).status_code == 200

    def test_a_bearer_header_works_for_a_programmatic_client(self, gated):
        """CI and `curl` cannot hold a cookie jar, so the header has to be a first-class path."""
        token = _log_in(gated)
        from app import app

        with app.test_client() as fresh:
            resp = fresh.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200

    @pytest.mark.parametrize("bad", [
        "not.a.token", "", "Bearer", "aaaa.bbbb.cccc",
    ])
    def test_a_forged_or_malformed_token_is_refused(self, gated, bad):
        assert gated.get(
            PROTECTED, headers={"Authorization": f"Bearer {bad}"}
        ).status_code == 401

    def test_a_session_cookie_no_longer_authenticates(self, gated):
        """FR-18: the legacy channel is removed, not merely deprioritised.

        A deployment still holding a signed Flask session must not be quietly honoured — that would
        leave two credential systems live at once, which is the state this phase exists to end.
        """
        from app import app

        serializer = app.session_interface.get_signing_serializer(app)
        forged = serializer.dumps({"rack_web_auth": "1"})
        gated.set_cookie(app.config.get("SESSION_COOKIE_NAME") or "session", forged)
        assert gated.get(PROTECTED).status_code == 401

    def test_logout_ends_access_immediately(self, gated):
        _log_in(gated)
        assert gated.get(PROTECTED).status_code == 200
        gated.post("/api/auth/logout")
        assert gated.get(PROTECTED).status_code == 401

    def test_a_revoked_subject_is_refused_without_waiting_for_expiry(self, gated):
        """FR-6: revocation is a fast path, not a lifetime."""
        _log_in(gated)
        assert gated.get(PROTECTED).status_code == 200
        revocation.revoke_subject(f"user:{bootstrap.ADMIN_LOGIN}")
        assert gated.get(PROTECTED).status_code == 401

    def test_auth_check_reports_the_access_ttl_for_proactive_renewal(self, gated, monkeypatch):
        """The SPA times its pre-expiry renewal off this, so it must be the *configured* TTL.

        A frontend constant would drift the moment a deployment changes
        ``RACK_WEB_ACCESS_TTL_SECONDS``; reporting the live value keeps the renewal cadence and the
        token lifetime moving together. Asserted against a non-default override so a hard-coded 3600
        here could not pass by coincidence.
        """
        monkeypatch.setenv("RACK_WEB_ACCESS_TTL_SECONDS", "1800")
        _log_in(gated)
        body = gated.get("/api/auth-check").get_json()
        assert body["authenticated"] is True
        assert body["access_ttl_seconds"] == 1800


class TestAuthorizationIsSeparateFromAuthentication:
    def test_a_denied_action_is_403_not_401(self, gated):
        """The distinction that keeps a client out of a credential-prompt loop it cannot satisfy."""
        _log_in(gated)
        authz.set_engine(authz.DenyAllEngine())
        resp = gated.post("/api/rack-uptime", json={"rack": "p5-r01"})
        assert resp.status_code == 403
        assert resp.get_json().get("reason") == "policy"

    def test_a_denied_action_tells_the_caller_who_can_grant_it(self, gated):
        """The words a refused user reads in the SPA's error banner. They point at the person who can
        review the policy, and name neither the action nor the resource that was refused."""
        _log_in(gated)
        authz.set_engine(authz.DenyAllEngine())
        body = gated.post("/api/rack-uptime", json={"rack": "p5-r01"}).get_json()
        assert body == {
            "ok": False,
            "error": "Access Denied. Contact administrator to request access to this resource",
            "reason": "policy",
        }
        assert "rack" not in body["error"].lower()

    def test_the_permissive_engine_allows(self, gated):
        """Installs it by name. `install_default()` would hand back the *enforcing* engine whenever
        a policy file exists, which is the state the suite runs in — so this would then assert
        permissiveness against an engine correctly refusing an unattached principal."""
        _log_in(gated)
        authz.set_engine(authz.AllowAllEngine())
        assert gated.post("/api/rack-uptime", json={"rack": "p5-r01"}).status_code != 403

    def test_an_unregistered_engine_refuses_rather_than_opens(self, gated):
        """The failure this seam exists to prevent: a missing startup line silently permitting all."""
        _log_in(gated)
        authz.reset_for_tests()
        try:
            assert gated.post("/api/rack-uptime", json={"rack": "p5-r01"}).status_code == 403
        finally:
            authz.install_default()

    def test_authorization_runs_only_after_authentication(self, gated):
        """An anonymous caller must get 401, never 403 — even with a deny-all engine.

        Otherwise the response tells an unauthenticated prober that the route exists and that policy
        was consulted, when the honest answer is simply "who are you?".
        """
        authz.set_engine(authz.DenyAllEngine())
        assert gated.post("/api/rack-uptime", json={"rack": "p5-r01"}).status_code == 401


class TestFR16ThePrincipalComesFromTheToken:
    def test_the_audit_record_names_the_authenticated_subject(self, gated, tmp_path, monkeypatch):
        import json
        import logging

        import audit
        import logging_config as lc
        from audit import sink

        audit_file = tmp_path / "audit" / "audit.jsonl"
        monkeypatch.setenv("NVIS_AUDIT_FILE", str(audit_file))
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        monkeypatch.setenv("NVIS_LOG_FILE", str(tmp_path / "app.log"))
        audit.reset_for_tests()
        lc.reset_for_tests()
        lc.configure(force=True)
        audit.configure(force=True)
        try:
            _log_in(gated)
            gated.post("/api/rack-uptime", json={"rack": "p5-r01"})
            for handler in logging.getLogger(sink.AUDIT_LOGGER).handlers:
                handler.flush()

            events = [
                json.loads(line)
                for line in audit_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            audited = [e for e in events if e.get("action") == "rack:uptime"]
            assert audited, "the request was not audited"
            assert audited[-1]["principal"] == f"user:{bootstrap.ADMIN_LOGIN}", (
                "the trail must name the token's subject; `anonymous` here means the gate and the "
                "audit hook disagree about who the caller was"
            )
            assert audited[-1]["decision"] == "allow"
            # Against the engine that is actually installed, not a literal. The intent is that the
            # record names *which* engine decided — `allow` is only as strong as the engine beside
            # it — and hardcoding `allow-all` asserted the suite's configuration instead, so the
            # test failed the moment the suite began enforcing policy.
            assert audited[-1]["policy_engine"] == authz.engine_name(), (
                "`allow` is only as strong as the engine beside it; the record must say which"
            )
        finally:
            audit.reset_for_tests()
            lc.reset_for_tests()

    def test_a_self_reported_header_cannot_set_the_principal(self, gated):
        """`X-Rack-Operator` is display metadata. If it could name the principal, the ledger would
        be a suggestion box and every rack lock advisory."""
        token = _log_in(gated)
        claims = tokens_mod.verify(token, tokens_mod.TOKEN_ACCESS)
        assert claims["sub"] == f"user:{bootstrap.ADMIN_LOGIN}"

        resp = gated.get(PROTECTED, headers={"X-Rack-Operator": "someone-else"})
        assert resp.status_code == 200  # the header is ignored, not rejected

    def test_an_unauthenticated_request_is_not_attributed(self, gated):
        """No credential must not produce a principal at all — not a default, not a guess."""
        assert gated.get(PROTECTED).status_code == 401


class TestFR18TheLegacyCredentialsAreGone:
    """Removed, not deprecated — and this is the test that stops them coming back.

    The failure that motivated deletion is worth restating, because a future "just add a compatible
    fallback" would recreate it exactly: once the gate verified tokens, `POST /api/login` still
    answered `200 {"ok": true}` and set a Flask session the gate ignored. An operator logged in, got
    a dashboard shell, and every panel returned 401 with nothing on screen explaining why. A
    credential path that succeeds and grants nothing is worse than an absent one.
    """

    def test_the_login_route_grants_nothing(self, gated):
        """Whatever the refusal code, the point is that no credential comes out of it.

        The path now falls through to the GET-only SPA catch-all, so a POST is 405. What matters is
        that the response is not a success and sets no session cookie — a 200 here would mean the
        route came back.
        """
        resp = gated.post("/api/login", json={"password": "anything"})
        assert resp.status_code >= 400
        assert not any("session" in c for c in resp.headers.getlist("Set-Cookie"))

    def test_no_route_in_the_map_is_the_legacy_login(self):
        from app import app

        assert "/api/login" not in {str(r) for r in app.url_map.iter_rules()}

    @pytest.mark.parametrize("symbol", [
        "_auth_mode", "_auth_token", "_pam_service", "_pam_verify", "_token_verify", "_session_ok",
    ])
    def test_the_mechanism_is_gone_from_web_auth(self, symbol):
        """Leaving these importable invites a caller to reach for one."""
        import web_auth

        assert not hasattr(web_auth, symbol), f"web_auth.{symbol} still exists"

    def test_the_pam_bindings_are_deleted(self):
        from pathlib import Path

        rack_web = Path(__file__).resolve().parents[1] / "rack_web"
        leftovers = sorted(p.name for p in rack_web.glob("pam_*.py"))
        assert not leftovers, f"vendored PAM bindings remain: {leftovers}"

    @pytest.mark.parametrize("env", [
        "RACK_WEB_AUTH_TOKEN", "RACK_WEB_AUTH_MODE", "RACK_WEB_PAM_SERVICE",
    ])
    def test_the_removed_variables_grant_nothing(self, gated, monkeypatch, env):
        """FR-12: no environment variable grants access. Setting these must change nothing.

        Asserted against a *running gate* rather than by grepping, because the question is whether
        the variable has any effect, not whether the string appears anywhere.
        """
        monkeypatch.setenv(env, "some-value")
        assert gated.get(PROTECTED).status_code == 401

    def test_the_bypass_set_names_no_removed_route(self):
        """An entry for a route that no longer exists reads as an intentional exemption."""
        import web_auth
        from app import app

        known = {r.endpoint for r in app.url_map.iter_rules()}
        stale = [e for e in web_auth.AUTH_BYPASS_ENDPOINTS if e not in known]
        assert not stale, f"bypass entries with no matching route: {stale}"


# Removed 2026-09-08: `TestTheDisableSwitchStillWorks::test_auth_disabled_bypasses_the_gate`.
#
# It asserted `c.get(PROTECTED).status_code == 200` under the heading "the disable switch still
# works" — and by the end it passed for the opposite reason: `app.test_client()` is the suite's
# *authenticated* client, so the 200 proved authentication working, not a bypass. A test whose name
# and docstring claim an authentication bypass exists, passing green for an unrelated reason, is
# worse than no test: the next reader either believes the bypass is real or "fixes" the wrong thing.
# There is no switch (FR-18c), and `test_gate_is_actually_on` asserts the refusal directly.


class TestTheLoginRoutesAreReachableWithoutACredential:
    def test_the_login_routes_stay_reachable_without_a_credential(self, gated):
        """They are how a caller obtains one; gating them would make login impossible.

        Driven with a *valid* public value. A malformed `A` makes the route itself answer 401 via
        its uniform-failure path, which is indistinguishable from the gate refusing — so the first
        version of this test passed the wrong thing and then failed for the wrong reason.
        """
        srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
        user = srp_lib.User(
            "nobody", "irrelevant", hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
        )
        _name, a = user.start_authentication()

        resp = gated.post("/api/auth/srp/begin", json={"login": "nobody", "A": a.hex()})
        assert resp.status_code == 200, (
            "the SRP begin route must be reachable without a credential, and must answer an "
            "unknown login exactly as it answers a known one (FR-4b)"
        )
        assert "WWW-Authenticate" not in resp.headers, "the gate refused a bypassed route"

    def test_refresh_is_not_in_the_bypass_set(self, gated):
        """It authenticates with the refresh cookie and must be refused without one.

        Still true, and now by the gate itself: renewal and logout accept the refresh cookie as
        their credential (`REFRESH_CREDENTIAL_ENDPOINTS`) instead of being exempted from the gate,
        so neither may ever appear in the bypass set.
        """
        import web_auth

        assert gated.post("/api/auth/refresh").status_code == 401
        for endpoint in web_auth.REFRESH_CREDENTIAL_ENDPOINTS:
            assert endpoint not in web_auth.AUTH_BYPASS_ENDPOINTS, endpoint

    def test_the_refresh_credential_endpoints_are_real_routes(self, gated):
        """A misspelt entry would silently restore the defect it exists to fix."""
        import web_auth
        from app import app

        known = {rule.endpoint for rule in app.url_map.iter_rules()}
        assert web_auth.REFRESH_CREDENTIAL_ENDPOINTS <= known

    def test_a_refresh_token_is_ignored_off_the_refresh_routes(self, gated):
        import web_auth

        principal = "user:admin"
        refresh = tokens_mod.mint(principal, tokens_mod.TOKEN_REFRESH)
        jar = {web_auth.REFRESH_COOKIE: refresh}
        assert web_auth.verified_refresh_claims("auth_srp.refresh_session", jar)["sub"] == principal
        assert web_auth.verified_refresh_claims("status.api_status", jar) == {}
        assert web_auth.verified_refresh_claims(None, jar) == {}
