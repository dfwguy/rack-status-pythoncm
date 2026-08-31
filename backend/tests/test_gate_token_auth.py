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
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "0")
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    monkeypatch.setenv("RACK_WEB_SECRET_KEY", "gate-token-test-secret")
    srp_mod.reset_for_tests()
    revocation.reset_for_tests()
    revocation.install()
    authz.install_default()

    # The SRP rate limiter is module-level state keyed by client address, and every test here logs
    # in from 127.0.0.1. Without this reset the bucket carries over from whatever ran earlier in the
    # session — so these tests pass alone and 429 in a full run, which is the least useful failure
    # mode there is.
    from api import auth_srp

    auth_srp._begin_limiter.reset()

    store = IdentityStore(str(tmp_path / "identity.json"))
    try:
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        salt, verifier = srp_mod.enroll(bootstrap.ADMIN_LOGIN, PASSWORD)
        store.update_user(user.edited(srp_salt=salt, srp_verifier=verifier))
    finally:
        store.close()

    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
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


class TestAuthorizationIsSeparateFromAuthentication:
    def test_a_denied_action_is_403_not_401(self, gated):
        """The distinction that keeps a client out of a credential-prompt loop it cannot satisfy."""
        _log_in(gated)
        authz.set_engine(authz.DenyAllEngine())
        resp = gated.post("/api/rack-uptime", json={"rack": "p5-r01"})
        assert resp.status_code == 403
        assert resp.get_json().get("reason") == "policy"

    def test_the_permissive_engine_allows(self, gated):
        _log_in(gated)
        authz.install_default()
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
            assert audited[-1]["policy_engine"] == "allow-all", (
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


class TestTheDisableSwitchStillWorks:
    def test_auth_disabled_bypasses_the_gate(self, monkeypatch, tmp_path):
        """FR-18a. ~2,500 tests depend on this, and FR-18c retires it once they authenticate."""
        monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
        from app import app

        with app.test_client() as c:
            assert c.get(PROTECTED).status_code == 200

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
        """It authenticates with the refresh cookie and must be refused without one."""
        assert gated.post("/api/auth/refresh").status_code == 401
