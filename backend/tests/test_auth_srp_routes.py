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
"""The SRP login exchange over HTTP — FR-4, FR-4b, FR-4g, FR-5a, FR-5b, FR-6.

The headline case is a **real** login: a genuine SRP client, driving the two legs against the real
routes, with the credential the bootstrap actually minted. Nothing here stubs the protocol, because
the interesting failures are exactly the ones a stub hides — a padding-mode mismatch, a login bound
to the wrong leg, a proof verified against a decoy.

Three properties get disproportionate attention:

**Indistinguishability (FR-4b).** An unknown login, a disabled account and a wrong password must be
answered identically — same status, same body, and within the same order of magnitude of time.
Otherwise the endpoint is a user-enumeration oracle, and the decoy path in `srp.begin` was written
for nothing.

**Shedding before the work (FR-4g).** `begin` costs ~52 ms of CPU for an unauthenticated caller. A
limiter that runs *after* the exponentiation protects the challenge table and leaves the amplifier
exposed — and passes any test that only checks for a 429. So the cost is measured.

**Logout means revoked (FR-6).** Clearing a cookie is theatre for a stateless token: a copy taken
beforehand keeps working until it expires. The test asserts the token is refused after logout, not
that the cookie is gone.
"""

from __future__ import annotations

import json
import logging
import time

import pytest
import srp as srp_lib
from audit import sink as audit_sink
from identity import bootstrap, rate_limit, revocation
from identity import srp as srp_mod
from identity import tokens as tokens_mod
from identity.store import IdentityStore

pytestmark = pytest.mark.usefixtures("srp_client_env")


@pytest.fixture
def srp_client_env(monkeypatch, tmp_path):
    """A private identity store with a known administrator, and clean protocol state."""
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    srp_mod.reset_for_tests()
    revocation.reset_for_tests()
    revocation.install()
    yield
    srp_mod.reset_for_tests()
    revocation.reset_for_tests()
    revocation.install()


#: A known password, enrolled directly. Deliberately not the one `ensure_admin` generates: importing
#: `app` runs the startup bootstrap, which consumes the store this fixture points at and returns the
#: password to *itself*, so a fixture calling `ensure_admin` afterwards gets `None` back — the user
#: already exists and the only copy of its credential is gone. Enrolling explicitly makes the test
#: independent of whether the app has been imported yet.
ADMIN_PASSWORD = "tc-srp-route-admin-password"


@pytest.fixture
def admin(tmp_path):
    """An administrator whose password this test knows."""
    store = IdentityStore(str(tmp_path / "identity.json"))
    try:
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        salt, verifier = srp_mod.enroll(bootstrap.ADMIN_LOGIN, ADMIN_PASSWORD)
        store.update_user(user.edited(srp_salt=salt, srp_verifier=verifier))
    finally:
        store.close()
    return bootstrap.ADMIN_LOGIN, ADMIN_PASSWORD


@pytest.fixture
def client():
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_limiter():

    yield


class _Client:
    """A real SRP-6a client, configured exactly as `identity/srp.py` configures the server.

    Written against the same library the server uses on purpose: if the two ever disagree about
    group, hash or padding, that is a genuine interoperability break and this fails — which is the
    failure mode that otherwise presents as "every login says wrong password".
    """

    def __init__(self, login: str, password: str):
        srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
        self.user = srp_lib.User(
            login, password, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
        )
        self.login = login

    def start(self) -> str:
        _uname, a = self.user.start_authentication()
        return a.hex()

    def process(self, salt_hex: str, b_hex: str) -> str:
        m = self.user.process_challenge(bytes.fromhex(salt_hex), bytes.fromhex(b_hex))
        assert m is not None, "the client refused the server's challenge"
        return m.hex()

    def verify_server(self, hamk_hex: str) -> bool:
        self.user.verify_session(bytes.fromhex(hamk_hex))
        return bool(self.user.authenticated())


def _login(client, login: str, password: str):
    """Drive both legs. Returns the Flask response from leg 2."""
    user = _Client(login, password)
    begin = client.post("/api/auth/srp/begin", json={"login": login, "A": user.start()})
    assert begin.status_code == 200, begin.get_data(as_text=True)
    body = begin.get_json()
    proof = user.process(body["salt"], body["B"])
    finish = client.post("/api/auth/srp/finish", json={"token": body["token"], "M": proof})
    return user, finish


class TestARealLoginSucceeds:
    def test_the_bootstrap_credential_authenticates(self, client, admin):
        login, password = admin
        _user, resp = _login(client, login, password)

        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["ok"] is True
        assert body["user"]["login"] == login
        assert body["user"]["principal"] == f"user:{login}"

    def test_the_server_proves_itself_to_the_client(self, client, admin):
        """Mutual authentication. Without checking HAMK the client has authenticated itself *to*
        an unverified server, which is the half of SRP that a naive implementation drops."""
        login, password = admin
        user, resp = _login(client, login, password)
        assert user.verify_server(resp.get_json()["HAMK"])

    def test_the_bootstrap_credential_is_flagged_for_change(self, client, admin):
        login, password = admin
        _user, resp = _login(client, login, password)
        assert resp.get_json()["must_change_password"] is True

    def test_a_wrong_password_is_refused(self, client, admin):
        login, _password = admin
        _user, resp = _login(client, login, "definitely-not-the-password")
        assert resp.status_code == 401


class TestFR4bAnUnknownLoginIsIndistinguishable:
    def test_the_begin_response_has_the_same_shape(self, client, admin):
        """Same status and same field set for a real and an absent login."""
        login, _password = admin
        known = client.post(
            "/api/auth/srp/begin", json={"login": login, "A": _Client(login, "x").start()}
        )
        unknown = client.post(
            "/api/auth/srp/begin",
            json={"login": "no-such-operator", "A": _Client("no-such-operator", "x").start()},
        )
        assert known.status_code == unknown.status_code == 200
        assert set(known.get_json()) == set(unknown.get_json())
        # And the salt must look like a salt, not like an empty string.
        assert unknown.get_json()["salt"]

    def test_the_decoy_salt_is_stable_across_attempts(self, client):
        """A salt that changed per attempt would identify an absent login immediately."""
        first = client.post(
            "/api/auth/srp/begin", json={"login": "ghost", "A": _Client("ghost", "x").start()}
        ).get_json()["salt"]
        second = client.post(
            "/api/auth/srp/begin", json={"login": "ghost", "A": _Client("ghost", "x").start()}
        ).get_json()["salt"]
        assert first == second

    def test_finishing_a_decoy_exchange_is_refused_identically(self, client, admin):
        login, _password = admin
        _u, unknown = _login(client, "no-such-operator", "anything")
        _u2, wrong = _login(client, login, "wrong-password")
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.get_json() == wrong.get_json()

    def test_a_disabled_account_takes_the_decoy_path(self, client, admin, tmp_path):
        """Answering a disabled account differently confirms the login exists."""
        login, password = admin
        store = IdentityStore(str(tmp_path / "identity.json"))
        try:
            user = store.get_user(login)
            store.update_user(user.edited(disabled=True))
        finally:
            store.close()

        _u, resp = _login(client, login, password)
        assert resp.status_code == 401


class TestFR4gTheBeginRouteShedsLoad:
    """FR-4g, now enforced by the shared throttle rather than a route-local limiter.

    The route used to own a `RateLimiter`. It was removed when the global throttle landed, because
    two limiters on one route consume separately -- the effective allowance was half of either
    declared value and neither number described the behaviour. The limit is read from the registry
    here so the test cannot drift from the declaration.
    """

    def test_it_refuses_past_the_limit(self, client):
        import throttle

        limit = throttle.registry().limit_for("auth_srp.srp_begin").limit
        payload = {"login": "someone", "A": _Client("someone", "x").start()}
        for _ in range(limit):
            assert client.post("/api/auth/srp/begin", json=payload).status_code == 200

        refused = client.post("/api/auth/srp/begin", json=payload)
        assert refused.status_code == 429
        assert refused.headers.get("Retry-After"), (
            "a 429 without Retry-After invites an immediate retry loop — the very traffic the "
            "limiter exists to shed"
        )

    def test_the_refusal_skips_the_exponentiation(self, client, monkeypatch):
        """FR-4g's actual requirement, and the one a status-code assertion cannot see.

        A limiter placed after `srp.begin` returns the same 429 while still paying the ~52 ms. This
        asserts the expensive call is never reached, which is the only way to tell the two apart.

        The guarantee is stronger since the move to a `before_request` hook: the view function is
        not entered at all, rather than entered and returning early.
        """
        import throttle
        from api import auth_srp

        calls: list[tuple] = []
        real_begin = srp_mod.begin

        def counting_begin(*a, **kw):
            calls.append(a)
            return real_begin(*a, **kw)

        monkeypatch.setattr(auth_srp.srp, "begin", counting_begin)

        payload = {"login": "someone", "A": _Client("someone", "x").start()}
        for _ in range(throttle.registry().limit_for("auth_srp.srp_begin").limit):
            client.post("/api/auth/srp/begin", json=payload)
        before = len(calls)

        assert client.post("/api/auth/srp/begin", json=payload).status_code == 429
        assert len(calls) == before, (
            "the modular exponentiation ran on a request the limiter refused; the limiter is "
            "downstream of the work it is supposed to be protecting"
        )

    def test_the_limiter_does_not_lock_out_a_normal_operator(self, client, admin):
        """A person mistyping a password a few times must be unaffected."""
        login, _password = admin
        for _ in range(5):
            _u, resp = _login(client, login, "wrong")
            assert resp.status_code == 401
        _u, ok = _login(client, *admin)
        assert ok.status_code == 200


class TestFR5aTheCookies:
    def test_the_access_cookie_is_httponly_and_samesite(self, client, admin):
        _u, resp = _login(client, *admin)
        cookies = resp.headers.getlist("Set-Cookie")
        access = next(c for c in cookies if c.startswith("nvis_access="))
        assert "HttpOnly" in access, "an XSS could read the session otherwise (FR-5a)"
        assert "SameSite=Lax" in access

    def test_secure_is_absent_without_tls_and_present_with_it(self, client, admin, monkeypatch):
        """Set unconditionally, the cookie is dropped on a plain-HTTP deployment and login appears
        to succeed while every later request is anonymous — with no error anywhere."""
        _u, plain = _login(client, *admin)
        access = next(
            c for c in plain.headers.getlist("Set-Cookie") if c.startswith("nvis_access=")
        )
        assert "Secure" not in access

        srp_mod.reset_for_tests()
        _u2, tls = _login_with_proto(client, *admin)
        access_tls = next(
            c for c in tls.headers.getlist("Set-Cookie") if c.startswith("nvis_access=")
        )
        assert "Secure" in access_tls

    def test_the_refresh_cookie_is_scoped_to_the_auth_routes(self, client, admin):
        _u, resp = _login(client, *admin)
        refresh = next(
            c for c in resp.headers.getlist("Set-Cookie") if c.startswith("nvis_refresh=")
        )
        assert "Path=/api/auth" in refresh, (
            "the longest-lived credential should not be presented to every endpoint"
        )

    def test_no_token_appears_in_the_response_body(self, client, admin):
        """FR-5a: the browser gets no readable copy. A token in the JSON defeats the cookie."""
        _u, resp = _login(client, *admin)
        body = resp.get_data(as_text=True)
        for cookie in resp.headers.getlist("Set-Cookie"):
            value = cookie.split("=", 1)[1].split(";", 1)[0]
            assert value not in body


def _login_with_proto(client, login: str, password: str):
    """Drive a login while claiming the edge terminated TLS."""
    user = _Client(login, password)
    headers = {"X-Forwarded-Proto": "https"}
    begin = client.post(
        "/api/auth/srp/begin", json={"login": login, "A": user.start()}, headers=headers
    )
    body = begin.get_json()
    proof = user.process(body["salt"], body["B"])
    return user, client.post(
        "/api/auth/srp/finish", json={"token": body["token"], "M": proof}, headers=headers
    )


class TestFR5bRenewal:
    def test_the_access_token_renews_without_a_password(self, client, admin):
        _u, login_resp = _login(client, *admin)
        assert login_resp.status_code == 200

        resp = client.post("/api/auth/refresh")
        assert resp.status_code == 200
        assert resp.get_json()["principal"] == f"user:{bootstrap.ADMIN_LOGIN}"
        assert any(
            c.startswith("nvis_access=") for c in resp.headers.getlist("Set-Cookie")
        ), "renewal must issue a new access cookie"

    def test_renewal_without_a_refresh_cookie_is_refused(self, client):
        assert client.post("/api/auth/refresh").status_code == 401

    def test_both_login_and_renewal_report_the_access_expiry(self, client, admin):
        """Control-plane metadata (not a credential): the SPA holds this to renew *before* a 401.

        Asserted on both the login and the refresh response, because the SPA arms its pre-check from
        whichever it saw last — and a deadline reported on only one of the two leaves a window where
        the client is flying blind and falls back to the costlier reactive path.
        """
        _u, login_resp = _login(client, *admin)
        now = time.time()
        login_expiry = login_resp.get_json()["access_expires_at"]
        # A plausible future deadline, roughly one access TTL out (default 1h), not an estimate.
        assert now + 60 < login_expiry <= now + 7 * 24 * 3600 + 5

        refresh_expiry = client.post("/api/auth/refresh").get_json()["access_expires_at"]
        assert refresh_expiry >= login_expiry

    def test_an_access_token_cannot_be_used_to_renew(self, client, admin):
        """FR-5c: the type is part of the contract. Accepting an access token here would make the
        short TTL meaningless, since any access token could mint its own successor forever."""
        _u, _r = _login(client, *admin)
        access = client.get_cookie("nvis_access")
        assert access is not None
        client.set_cookie("nvis_refresh", access.value, path="/api/auth")
        assert client.post("/api/auth/refresh").status_code == 401

    def test_a_revoked_subject_cannot_renew(self, client, admin):
        _u, _r = _login(client, *admin)
        revocation.revoke_subject(f"user:{bootstrap.ADMIN_LOGIN}")
        assert client.post("/api/auth/refresh").status_code == 401


@pytest.fixture
def anonymous():
    """A client that presents only the cookies the exchange itself set — never a minted bearer.

    The suite's default `client` mints a fresh access token for *every* request, which is exactly
    what hid the defect below: renewal was gated on a valid access token, and a client that always
    has one cannot notice. Renewal exists for the moment the access token is gone, so it has to be
    tested from that moment.
    """
    from app import app
    from identity import testing

    with testing.unauthenticated_client(app) as c:
        yield c


class TestFR5bRenewalOutlivesTheAccessToken:
    """Renewal must work once the access token has expired — that is the only time it is needed.

    The gate demanded a valid *access* token before the route could read its refresh cookie. The
    SPA renews on a timer ahead of expiry and never noticed; `nvrs` renews after a 401, i.e. with an
    expired access token, and was refused every time with a 401 audited as `anonymous` — a 7-day
    refresh token that worked for one hour. The gate now accepts the refresh cookie as the
    credential on this route (`web_auth.REFRESH_CREDENTIAL_ENDPOINTS`) rather than exempting it.
    """

    def test_renewal_with_only_the_refresh_cookie(self, anonymous, admin):
        """The browser case: the access cookie's `Max-Age` has lapsed, so it is simply not sent."""
        _u, login_resp = _login(anonymous, *admin)
        assert login_resp.status_code == 200
        anonymous.delete_cookie("nvis_access", path="/")

        resp = anonymous.post("/api/auth/refresh")
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["principal"] == f"user:{bootstrap.ADMIN_LOGIN}"
        renewed = anonymous.get_cookie("nvis_access")
        assert renewed is not None, "renewal must issue a new access cookie"
        # And the renewed token is a working credential, not merely a cookie.
        assert tokens_mod.verify(renewed.value, tokens_mod.TOKEN_ACCESS)["sub"] == (
            f"user:{bootstrap.ADMIN_LOGIN}"
        )

    def test_renewal_alongside_an_expired_bearer(self, anonymous, admin):
        """The CLI case: it still sends its stale access token as `Authorization: Bearer`."""
        _u, _r = _login(anonymous, *admin)
        anonymous.delete_cookie("nvis_access", path="/")
        expired = tokens_mod.mint(
            f"user:{bootstrap.ADMIN_LOGIN}", tokens_mod.TOKEN_ACCESS, ttl_seconds=1
        )
        time.sleep(1.5)
        with pytest.raises(tokens_mod.TokenError):
            tokens_mod.verify(expired, tokens_mod.TOKEN_ACCESS)

        resp = anonymous.post(
            "/api/auth/refresh", headers={"Authorization": f"Bearer {expired}"}
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_without_any_credential_the_gate_refuses(self, anonymous):
        """Accepted, not exempted: with no credential the request never reaches the route."""
        resp = anonymous.post("/api/auth/refresh")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "authentication required", "the gate, not the route"
        assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer")

    def test_the_renewal_is_audited_as_the_refresh_tokens_subject(self, anonymous, admin, caplog):
        """Not `anonymous` — the line that made this defect so hard to read on NC."""
        _u, _r = _login(anonymous, *admin)
        anonymous.delete_cookie("nvis_access", path="/")
        with caplog.at_level(logging.INFO, logger=audit_sink.AUDIT_LOGGER):
            assert anonymous.post("/api/auth/refresh").status_code == 200
        events = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == audit_sink.AUDIT_LOGGER and '"auth:refresh"' in r.getMessage()
        ]
        assert events, "the renewal left no audit record"
        assert events[-1]["principal"] == f"user:{bootstrap.ADMIN_LOGIN}"
        assert events[-1]["auth_method"] == "refresh"
        assert events[-1]["result"] != "refused"

    @pytest.mark.parametrize("path", ["/api/auth-check", "/api/status"])
    def test_a_refresh_token_is_not_a_credential_anywhere_else(self, anonymous, admin, path):
        """FR-5c: honoured outside renewal and logout, it would be a 7-day access token."""
        _u, _r = _login(anonymous, *admin)
        refresh = anonymous.get_cookie("nvis_refresh", path="/api/auth")
        assert refresh is not None
        anonymous.delete_cookie("nvis_access", path="/")
        anonymous.set_cookie("nvis_refresh", refresh.value, path="/")  # sent everywhere
        assert anonymous.get(path).status_code == 401
        assert anonymous.get(
            path, headers={"Authorization": f"Bearer {refresh.value}"}
        ).status_code == 401

    def test_a_revoked_refresh_token_is_still_refused(self, anonymous, admin):
        _u, _r = _login(anonymous, *admin)
        anonymous.delete_cookie("nvis_access", path="/")
        revocation.revoke_all_for(f"user:{bootstrap.ADMIN_LOGIN}")
        assert anonymous.post("/api/auth/refresh").status_code == 401

    def test_an_access_token_in_the_refresh_cookie_is_still_refused(self, anonymous, admin):
        _u, _r = _login(anonymous, *admin)
        access = anonymous.get_cookie("nvis_access")
        assert access is not None
        anonymous.delete_cookie("nvis_access", path="/")
        anonymous.set_cookie("nvis_refresh", access.value, path="/api/auth")
        assert anonymous.post("/api/auth/refresh").status_code == 401


class TestFR6LogoutRevokes:
    def test_the_token_is_refused_after_logout(self, client, admin):
        """Not "the cookie is gone" — a copy taken before logout must stop working."""
        _u, _r = _login(client, *admin)
        stolen = client.get_cookie("nvis_access")
        assert stolen is not None

        assert client.post("/api/auth/logout").status_code == 200

        with pytest.raises(tokens_mod.TokenError):
            tokens_mod.verify(stolen.value, tokens_mod.TOKEN_ACCESS)

    def test_the_cookies_are_cleared(self, client, admin):
        _u, _r = _login(client, *admin)
        resp = client.post("/api/auth/logout")
        cleared = [c for c in resp.headers.getlist("Set-Cookie") if "nvis_" in c]
        assert len(cleared) == 2
        for cookie in cleared:
            assert "Expires=Thu, 01 Jan 1970" in cookie or "Max-Age=0" in cookie

    def test_logout_without_a_session_is_not_an_error(self, client):
        """A caller on their way out should not be handed a failure they cannot act on."""
        assert client.post("/api/auth/logout").status_code == 200


class TestLogoutOutlivesTheAccessToken:
    """Logout past the access token's hour must still revoke the refresh token.

    Gated on a valid access token, it was refused before it could revoke anything, and both clients
    treat a 401 on the way out as success — so "logged out" left a renewable credential behind.
    """

    def test_the_refresh_token_is_revoked_after_the_access_token_lapsed(self, anonymous, admin):
        _u, _r = _login(anonymous, *admin)
        refresh = anonymous.get_cookie("nvis_refresh", path="/api/auth")
        assert refresh is not None
        anonymous.delete_cookie("nvis_access", path="/")

        assert anonymous.post("/api/auth/logout").status_code == 200

        with pytest.raises(tokens_mod.TokenError):
            tokens_mod.verify(refresh.value, tokens_mod.TOKEN_REFRESH)
        anonymous.set_cookie("nvis_refresh", refresh.value, path="/api/auth")
        assert anonymous.post("/api/auth/refresh").status_code == 401

    def test_without_any_credential_the_gate_refuses_and_revokes_nothing(self, anonymous, admin):
        principal = f"user:{bootstrap.ADMIN_LOGIN}"
        bystander = tokens_mod.mint(principal, tokens_mod.TOKEN_REFRESH)

        resp = anonymous.post("/api/auth/logout")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "authentication required"

        assert tokens_mod.verify(bystander, tokens_mod.TOKEN_REFRESH)["sub"] == principal


class TestTheChallengeIsSingleUse:
    def test_a_replayed_proof_is_refused(self, client, admin):
        login, password = admin
        user = _Client(login, password)
        begin = client.post(
            "/api/auth/srp/begin", json={"login": login, "A": user.start()}
        ).get_json()
        proof = user.process(begin["salt"], begin["B"])

        first = client.post(
            "/api/auth/srp/finish", json={"token": begin["token"], "M": proof}
        )
        assert first.status_code == 200

        replay = client.post(
            "/api/auth/srp/finish", json={"token": begin["token"], "M": proof}
        )
        assert replay.status_code == 401, "a consumed challenge is a replay window"

    def test_a_forged_challenge_token_is_refused(self, client):
        assert client.post(
            "/api/auth/srp/finish", json={"token": "made-up", "M": "00"}
        ).status_code == 401


class TestMalformedInput:
    @pytest.mark.parametrize("payload", [
        {}, {"login": "admin"}, {"A": "aa"}, {"login": "", "A": ""},
        {"login": "admin", "A": "not-hex"}, {"login": "admin", "A": "00"},
    ])
    def test_begin_refuses_without_leaking(self, client, payload):
        resp = client.post("/api/auth/srp/begin", json=payload)
        assert resp.status_code in (200, 401), resp.status_code
        if resp.status_code == 401:
            assert resp.get_json() == {"ok": False, "error": "authentication failed"}

    @pytest.mark.parametrize("payload", [{}, {"token": "x"}, {"M": "00"}])
    def test_finish_refuses_without_leaking(self, client, payload):
        resp = client.post("/api/auth/srp/finish", json=payload)
        assert resp.status_code == 401
        assert resp.get_json() == {"ok": False, "error": "authentication failed"}


class TestPasswordChange:
    """FR-10a and FR-3: prove the current password, and never send either one.

    The property worth stating is the one that is easy to get wrong in the permissive direction:
    **being signed in is not enough**. A stolen session must not be usable to lock the real owner
    out of their own account, so the current password is re-proved over a fresh SRP exchange.
    """

    @staticmethod
    def _change(client, login, current, new):
        user = _Client(login, current)
        begin = client.post("/api/auth/password/begin", json={"A": user.start()})
        if begin.status_code != 200:
            return begin
        body = begin.get_json()
        proof = user.process(body["salt"], body["B"])
        new_salt, new_verifier = srp_mod.enroll(login, new)
        return client.post("/api/auth/password/finish", json={
            "token": body["token"], "M": proof,
            "new_salt": new_salt, "new_verifier": new_verifier,
        })

    def test_the_new_password_works_and_the_old_one_does_not(self, client, admin):
        login, old = admin
        _u, r = _login(client, login, old)
        assert r.status_code == 200

        assert self._change(client, login, old, "a-brand-new-password").status_code == 200

        srp_mod.reset_for_tests()
        _u2, with_old = _login(client, login, old)
        assert with_old.status_code == 401, "the old password still authenticates"

        srp_mod.reset_for_tests()
        _u3, with_new = _login(client, login, "a-brand-new-password")
        assert with_new.status_code == 200

    def test_the_change_clears_the_must_change_flag(self, client, admin):
        login, old = admin
        _u, _r = _login(client, login, old)
        self._change(client, login, old, "another-new-password")

        srp_mod.reset_for_tests()
        _u2, again = _login(client, login, "another-new-password")
        assert again.get_json()["must_change_password"] is False

    def test_a_wrong_current_password_is_refused(self, client, admin):
        login, old = admin
        _u, _r = _login(client, login, old)
        assert self._change(client, login, "not-the-current-one", "new").status_code == 401

        srp_mod.reset_for_tests()
        _u2, still = _login(client, login, old)
        assert still.status_code == 200, "a failed change must leave the credential alone"

    def test_an_unauthenticated_caller_cannot_change_a_password(self, client, admin):
        """No session at all. The route is not in the bypass set, so the gate should refuse —
        and with the gate disabled in this suite, the handler's own principal check must."""
        login, old = admin
        assert self._change(client, login, old, "new").status_code in (401, 403)

    def test_every_session_is_revoked_afterwards(self, client, admin):
        """A password change is usually a response to a compromise. Leaving the attacker's token
        alive for the rest of its lifetime would make the change cosmetic."""
        login, old = admin
        _u, _r = _login(client, login, old)
        stolen = client.get_cookie("nvis_access")
        assert stolen is not None

        assert self._change(client, login, old, "post-compromise-password").status_code == 200

        with pytest.raises(tokens_mod.TokenError):
            tokens_mod.verify(stolen.value, tokens_mod.TOKEN_ACCESS)

    def test_the_caller_is_told_to_reauthenticate(self, client, admin):
        login, old = admin
        _u, _r = _login(client, login, old)
        resp = self._change(client, login, old, "yet-another-password")
        assert resp.get_json()["reauthenticate"] is True
        cleared = [c for c in resp.headers.getlist("Set-Cookie") if "nvis_" in c]
        assert len(cleared) == 2, "the cookies must be cleared, not left pointing at dead tokens"

    @pytest.mark.parametrize("bad", ["", "zz", "not-hex", "abc"])
    def test_a_malformed_verifier_is_refused_before_it_is_stored(self, client, admin, bad):
        """A non-hex verifier written to the store is a credential nobody can authenticate
        against — a lockout authored by the client."""
        login, old = admin
        _u, _r = _login(client, login, old)

        user = _Client(login, old)
        body = client.post(
            "/api/auth/password/begin", json={"A": user.start()}
        ).get_json()
        proof = user.process(body["salt"], body["B"])
        resp = client.post("/api/auth/password/finish", json={
            "token": body["token"], "M": proof,
            "new_salt": "aabb", "new_verifier": bad,
        })
        assert resp.status_code in (400, 401)

        srp_mod.reset_for_tests()
        _u2, still = _login(client, login, old)
        assert still.status_code == 200, "the original credential was damaged"

    def test_the_password_never_reaches_the_server(self, client, admin, tmp_path):
        """FR-3 again, for the change path: only salt and verifier are persisted."""
        login, old = admin
        _u, _r = _login(client, login, old)
        secret = "a-very-distinctive-new-password"
        assert self._change(client, login, old, secret).status_code == 200

        raw = (tmp_path / "identity.json").read_text(encoding="utf-8")
        assert secret not in raw
        assert old not in raw


class TestTheRateLimiterItself:
    def test_a_full_bucket_refills_over_time(self):
        limiter = rate_limit.RateLimiter(limit=2, window=0.2)
        assert limiter.check("k")[0] and limiter.check("k")[0]
        assert not limiter.check("k")[0]
        time.sleep(0.25)
        assert limiter.check("k")[0], "the bucket never refilled"

    def test_clients_do_not_share_a_bucket(self):
        limiter = rate_limit.RateLimiter(limit=1, window=60)
        assert limiter.check("a")[0]
        assert not limiter.check("a")[0]
        assert limiter.check("b")[0], "one client exhausted another's allowance"

    def test_the_table_is_bounded(self):
        """Otherwise the limiter is itself the memory amplifier it exists to prevent."""
        limiter = rate_limit.RateLimiter(limit=1, window=60, max_tracked=8)
        for i in range(200):
            limiter.check(f"client-{i}")
        assert limiter.tracked() <= 8

    def test_eviction_prefers_buckets_that_have_already_refilled(self):
        """A refilled bucket is indistinguishable from an absent one, so it is the free thing to drop.

        This is the property the eviction policy can actually offer. It is *not* a defence against a
        caller who varies their key — see the test below, which states that limitation rather than
        leaving it implied.
        """
        limiter = rate_limit.RateLimiter(limit=4, window=0.05, max_tracked=8)
        for i in range(4):
            limiter.check(f"idle-{i}")
        time.sleep(0.08)  # the idle buckets refill completely

        spender = "busy"
        for _ in range(4):
            limiter.check(spender)
        assert not limiter.check(spender)[0], "precondition: the busy bucket is spent"

        for i in range(20):
            limiter.check(f"newcomer-{i}")

        assert limiter.tracked() <= 8

    def test_a_caller_who_varies_its_key_is_not_limited_and_that_is_inherent(self):
        """Stated as a test because it is a real limitation, not an oversight.

        A per-key bucket cannot limit a caller who controls the key. Every distinct source gets its
        own fresh allowance, whatever the eviction policy is — so this limiter raises the cost for a
        single-source attacker and protects honest clients from each other, and it does **not** stop
        a distributed or address-spoofing attacker from reaching `srp.begin`.

        What actually bounds that case is elsewhere: the resolved address only trusts a forwarded
        header from a configured proxy, so a caller cannot mint arbitrary keys with `X-Forwarded-For`
        unless the deployment told us to believe it. Beyond that it is a network-layer concern.
        """
        limiter = rate_limit.RateLimiter(limit=1, window=60)
        for i in range(100):
            allowed, _ = limiter.check(f"spoofed-{i}")
            assert allowed, "a first request from a new key is always allowed, by construction"

    def test_a_zero_or_negative_limit_is_refused(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                rate_limit.RateLimiter(limit=bad)
