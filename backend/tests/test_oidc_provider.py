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
"""The OIDC provider endpoints — Phase 2.5, FR-10 .. FR-15a.

Three properties carry most of the weight here, and none of them is "the happy path works".

**A replayed code revokes what it bought** (FR-11a). Declining a duplicate politely leaves an
attacker holding the tokens the first exchange produced.

**`redirect_uri` is compared exactly** (FR-15a). Every well-known instance of this bug is a relaxed
matcher, and the consequence is not a broken redirect — it is the authorization code delivered to an
attacker-nominated origin, which is a full account takeover.

**The gate-bypassing routes authenticate for themselves** (FR-6c). They are on the bypass list
because the gate cannot understand client credentials; that is only safe while each one checks.
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
import pytest
from identity import keys as keys_mod
from identity import oidc, revocation
from identity import oidc_clients as clients_mod
from identity import oidc_codes as codes_mod

ISSUER = "https://rack.example.com"
GRAFANA_URI = "https://grafana.example.com/login/generic_oauth"
CLI_URI = "http://localhost:8123/callback"
SECRET = "s3cr3t"

CLIENTS = {
    "clients": [
        {
            "client_id": "grafana",
            "client_type": "confidential",
            "client_secret": SECRET,
            "redirect_uris": [GRAFANA_URI],
        },
        {"client_id": "cli", "client_type": "public", "redirect_uris": [CLI_URI]},
    ]
}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce(seed: bytes = b"seed") -> tuple[str, str]:
    verifier = _b64(hashlib.sha256(seed).digest())
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


@pytest.fixture
def provider(tmp_path, monkeypatch):
    clients_file = tmp_path / "clients.json"
    clients_file.write_text(json.dumps(CLIENTS))
    monkeypatch.setenv("RACK_WEB_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "oidc"))
    monkeypatch.setenv("RACK_WEB_OIDC_CLIENTS_FILE", str(clients_file))
    monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
    monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", ISSUER)
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()
    clients_mod.reset_for_tests()
    codes_mod.store().reset()
    revocation.install()
    yield
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()
    clients_mod.reset_for_tests()
    codes_mod.store().reset()


@pytest.fixture
def client(provider):
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def signed_in(client, monkeypatch):
    """An operator `/authorize` will treat as signed in.

    Patched at the handler's own seam, because the suite runs with `RACK_WEB_AUTH_DISABLED=1` and a
    disabled gate never resolves a principal at all — so patching the gate's resolver has no effect
    and `g.auth_principal` is simply never set.

    **What this therefore does not prove** is the wiring between the gate and this handler. That is
    covered separately by `TestAuthorizeRequiresASignedInOperator`, which asserts no code is issued
    without a principal, and by the gate's own suite. Said plainly because a fixture that quietly
    bypasses the thing under test is how a hole stays open.
    """
    from api import oidc_provider

    monkeypatch.setattr(oidc_provider, "_current_principal", lambda: "user:tester", raising=True)
    client.post(
        "/api/identity/users", json={"login": "tester", "email": "tester@example.com"}
    )
    return client


def _authorize(client, **overrides):
    verifier, challenge = pkce()
    params = {
        "response_type": "code",
        "client_id": "grafana",
        "redirect_uri": GRAFANA_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    } | overrides
    # Properly percent-encoded, which matters more than it looks. Hand-joining the pairs meant a
    # `#` in a candidate redirect_uri was read as a URL fragment and never reached the server at
    # all, so the fragment case passed for the wrong reason — it was testing that the browser
    # strips fragments, not that we refuse them. Same hazard for `?` and `&`.
    query = urlencode({k: v for k, v in params.items() if v is not None})
    return client.get(f"/oidc/authorize?{query}"), verifier


def _exchange(client, code, verifier, **overrides):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": GRAFANA_URI,
        "client_id": "grafana",
        "client_secret": SECRET,
        "code_verifier": verifier,
    } | overrides
    return client.post("/oidc/token", data=data)


def _code_from(resp) -> str:
    return parse_qs(urlparse(resp.headers["Location"]).query)["code"][0]


class TestTheHappyPath:
    def test_a_full_authorization_code_exchange(self, signed_in):
        resp, verifier = _authorize(signed_in)
        assert resp.status_code == 302

        body = _exchange(signed_in, _code_from(resp), verifier).get_json()
        assert body["token_type"] == "Bearer"
        assert {"access_token", "id_token", "refresh_token"} <= set(body)

    @pytest.mark.parametrize("state", ["xyz", "a b~c/d+e", "%%&=?#", "ünïcode"])
    def test_state_is_echoed_verbatim(self, signed_in, state):
        """The client's CSRF defence, and it only works if what comes back is byte-identical.

        Parametrised over values containing the characters that survive a careless re-encode least
        well — `+` silently becoming a space is the classic.
        """
        resp, _ = _authorize(signed_in, state=state)
        assert parse_qs(urlparse(resp.headers["Location"]).query)["state"] == [state]

    def test_the_id_token_carries_azp_and_the_nonce(self, signed_in):
        resp, verifier = _authorize(signed_in, nonce="n-0S6_WzA2Mj")
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()
        claims = jwt.decode(body["id_token"], options={"verify_signature": False})

        assert claims["azp"] == "grafana"
        assert claims["nonce"] == "n-0S6_WzA2Mj"
        assert claims["token_use"] == "id"

    def test_at_hash_binds_the_id_token_to_the_access_token(self, signed_in):
        """OIDC Core §3.1.3.6, computed the way a conforming client will check it.

        The left-most **half** of the digest, not the whole thing. A full digest looks entirely
        plausible and is rejected by every conforming client, with nothing visibly wrong on our
        side — so the test recomputes it independently rather than comparing against our helper.
        """
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()
        claims = jwt.decode(body["id_token"], options={"verify_signature": False})

        digest = hashlib.sha256(body["access_token"].encode("ascii")).digest()
        expected = _b64(digest[: len(digest) // 2])

        assert claims["at_hash"] == expected
        assert len(base64.urlsafe_b64decode(claims["at_hash"] + "==")) == 16, "half of SHA-256"

    def test_no_nonce_claim_when_none_was_requested(self, signed_in):
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()
        assert "nonce" not in jwt.decode(body["id_token"], options={"verify_signature": False})

    def test_the_refresh_grant_returns_a_new_access_token(self, signed_in):
        resp, verifier = _authorize(signed_in)
        first = _exchange(signed_in, _code_from(resp), verifier).get_json()

        again = signed_in.post("/oidc/token", data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": "grafana",
            "client_secret": SECRET,
        })
        assert again.status_code == 200
        assert again.get_json()["access_token"] != first["access_token"]

    def test_auth_time_survives_the_exchange(self, signed_in):
        """The authentication happened at the authorize step, not at the token exchange."""
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()
        access = jwt.decode(body["access_token"], options={"verify_signature": False})
        idt = jwt.decode(body["id_token"], options={"verify_signature": False})
        assert access["auth_time"] == idt["auth_time"]


class TestCodeReplayRevokesWhatItBought:
    """FR-11a. The property most implementations get wrong by being polite."""

    def test_a_replayed_code_is_refused(self, signed_in):
        resp, verifier = _authorize(signed_in)
        code = _code_from(resp)
        _exchange(signed_in, code, verifier)

        second = _exchange(signed_in, code, verifier)
        assert second.status_code == 400
        assert second.get_json()["error"] == "invalid_grant"

    def test_the_tokens_from_the_first_exchange_stop_working(self, signed_in):
        """Declining the duplicate is not enough — a repeated code means it leaked, so whatever the
        first exchange minted has to be withdrawn."""
        resp, verifier = _authorize(signed_in)
        code = _code_from(resp)
        first = _exchange(signed_in, code, verifier).get_json()

        auth = {"Authorization": f"Bearer {first['access_token']}"}
        assert signed_in.get("/oidc/userinfo", headers=auth).status_code == 200

        _exchange(signed_in, code, verifier)  # the replay

        assert signed_in.get("/oidc/userinfo", headers=auth).status_code == 401

    def test_an_unknown_code_is_refused(self, signed_in):
        assert _exchange(signed_in, "not-a-code", "v").status_code == 400


class TestPkce:
    """FR-11."""

    def test_plain_is_rejected(self, signed_in):
        """No client here cannot hash, so accepting `plain` would weaken the flow for nobody."""
        resp, _ = _authorize(signed_in, code_challenge_method="plain")
        assert "error=invalid_request" in resp.headers["Location"]

    def test_a_missing_challenge_is_rejected(self, signed_in):
        resp, _ = _authorize(signed_in, code_challenge="", code_challenge_method="S256")
        assert "error=invalid_request" in resp.headers["Location"]

    def test_a_wrong_verifier_is_rejected(self, signed_in):
        resp, _ = _authorize(signed_in)
        other, _ = pkce(b"a different seed")
        assert _exchange(signed_in, _code_from(resp), other).status_code == 400

    def test_the_code_is_bound_to_the_client_it_was_issued_to(self, signed_in):
        """Otherwise a code intercepted from one client is exchangeable by another."""
        resp, verifier = _authorize(signed_in)
        assert _exchange(
            signed_in, _code_from(resp), verifier, client_id="cli", client_secret=None
        ).status_code in (400, 401)


class TestRedirectUriIsExact:
    """FR-15a. The permissive matcher must fail, so the variants are the test."""

    @pytest.mark.parametrize("bad", [
        GRAFANA_URI + "/",
        GRAFANA_URI + "/extra",
        GRAFANA_URI + "?next=x",
        "https://grafana.example.com/login/generic_oauth#f",
        "https://evil.grafana.example.com/login/generic_oauth",
        "https://grafana.example.com.evil.net/login/generic_oauth",
        "http://grafana.example.com/login/generic_oauth",
        "https://grafana.example.com/LOGIN/generic_oauth",
    ])
    def test_a_near_miss_is_refused(self, signed_in, bad):
        resp, _ = _authorize(signed_in, redirect_uri=bad)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_request"

    def test_the_refusal_is_not_itself_redirected(self, signed_in):
        """The subtle one: an error about an unvalidated redirect_uri must not be *sent* to it.

        Redirecting here would be the open redirect the exact-match rule exists to prevent.
        """
        resp, _ = _authorize(signed_in, redirect_uri="https://evil.example.net/steal")
        assert resp.status_code == 400
        assert "Location" not in resp.headers

    def test_an_unknown_client_is_refused_without_redirecting(self, signed_in):
        resp, _ = _authorize(signed_in, client_id="nope")
        assert resp.status_code == 400
        assert "Location" not in resp.headers


class TestAuthorizeRequiresASignedInOperator:
    def test_without_a_session_no_code_is_issued(self, client):
        """`/authorize` is deliberately *not* on the bypass list."""
        resp, _ = _authorize(client)
        assert resp.status_code in (401, 302)
        if resp.status_code == 302:
            assert "code=" not in resp.headers.get("Location", "")


class TestTheBypassingRoutesAuthenticateThemselves:
    """FR-6c. These four bypass the gate, so this is what keeps that safe."""

    @pytest.mark.parametrize("path", ["/oidc/token", "/oidc/introspect", "/oidc/revoke"])
    def test_client_credentials_are_required(self, client, path):
        resp = client.post(path, data={"grant_type": "refresh_token", "token": "x"})
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "invalid_client"

    def test_a_wrong_client_secret_is_refused(self, client):
        resp = client.post("/oidc/token", data={
            "grant_type": "refresh_token", "client_id": "grafana", "client_secret": "wrong",
        })
        assert resp.status_code == 401

    def test_userinfo_requires_a_bearer_token(self, client):
        resp = client.get("/oidc/userinfo")
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers


class TestAuthorizeShedsLoad:
    """NFR-6, AC-24b, via the shared throttle rather than a route-local limiter.

    `/authorize` originally carried its own `RateLimiter`. It was removed when the global throttle
    landed: two limiters on one route consume separately, so the effective allowance was half of
    either declared number and neither described the behaviour. The declaration now sits on the
    view; enforcement is `app._enforce_request_throttle`.

    Counted rather than timed, so a loaded runner cannot make this flaky.
    """

    def test_a_flood_is_refused_before_any_code_is_minted(self, signed_in):
        """The point of shedding early: the expensive work must never start.

        A flood here would evict legitimate codes from the bounded table before they could be
        redeemed -- a denial of service against login rather than against the server.
        """
        from identity import oidc_codes

        oidc_codes.store().reset()

        statuses = [_authorize(signed_in)[0].status_code for _ in range(40)]

        assert 429 in statuses, "the throttle never shed"
        assert oidc_codes.store().outstanding() <= 30, (
            "a refused request must not have minted a code"
        )

    def test_the_refusal_says_when_to_retry(self, signed_in):

        resp = None
        for _ in range(40):
            resp, _ = _authorize(signed_in)
            if resp.status_code == 429:
                break

        assert resp is not None and resp.status_code == 429
        assert resp.get_json()["error"] == "rate limited"
        assert int(resp.headers["Retry-After"]) >= 1

    def test_normal_use_is_never_shed(self, signed_in):
        """A limit that fires during ordinary work is a bug, not a control. An interactive
        authorization is a human clicking a button."""

        for _ in range(5):
            assert _authorize(signed_in)[0].status_code == 302


class TestTheDeclaredLimitIsWhatIsEnforced:
    def test_authorize_declares_thirty_and_the_installer_collected_it(self):
        """A declaration the installer never picked up would fall back to the authenticated
        default, which is thirty times looser -- and nothing would say so."""
        import throttle

        declared = throttle.registry().declared()
        assert "oidc_provider.authorize" in declared
        assert declared["oidc_provider.authorize"].limit == 30


class TestTokenTypeConfusion:
    """FR-12a. The property must survive the rename from `typ` to `token_use`."""

    def test_an_id_token_is_refused_at_userinfo(self, signed_in):
        """An id token is a claims carrier handed to the browser and routinely logged by clients.
        It was never meant to be a credential."""
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()

        r = signed_in.get(
            "/oidc/userinfo", headers={"Authorization": f"Bearer {body['id_token']}"}
        )
        assert r.status_code == 401

    def test_a_refresh_token_is_refused_at_userinfo(self, signed_in):
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()

        r = signed_in.get(
            "/oidc/userinfo", headers={"Authorization": f"Bearer {body['refresh_token']}"}
        )
        assert r.status_code == 401


class TestUserinfo:
    def test_it_returns_the_bearers_own_claims(self, signed_in):
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()

        info = signed_in.get(
            "/oidc/userinfo", headers={"Authorization": f"Bearer {body['access_token']}"}
        ).get_json()
        assert info["sub"] == "user:tester"
        assert info["preferred_username"] == "tester"

    def test_the_subject_comes_from_the_token_not_a_parameter(self, signed_in):
        """Otherwise this is a directory any authenticated user could read."""
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()

        info = signed_in.get(
            "/oidc/userinfo?sub=user:admin",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        ).get_json()
        assert info["sub"] == "user:tester"


class TestIntrospectionAndRevocation:
    """FR-13, FR-13a."""

    def _creds(self):
        return {"client_id": "grafana", "client_secret": SECRET}

    def test_an_active_token_reports_active(self, signed_in):
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()

        out = signed_in.post(
            "/oidc/introspect", data={"token": body["access_token"]} | self._creds()
        ).get_json()
        assert out["active"] is True
        assert out["sub"] == "user:tester"
        assert out["token_use"] == "access"

    def test_a_garbage_token_reports_inactive_rather_than_erroring(self, signed_in):
        """RFC 7662 §2.2. An error would let a caller distinguish malformed from expired."""
        out = signed_in.post(
            "/oidc/introspect", data={"token": "not.a.token"} | self._creds()
        )
        assert out.status_code == 200
        assert out.get_json() == {"active": False}

    def test_revocation_makes_a_token_stop_verifying(self, signed_in):
        resp, verifier = _authorize(signed_in)
        body = _exchange(signed_in, _code_from(resp), verifier).get_json()
        auth = {"Authorization": f"Bearer {body['access_token']}"}
        assert signed_in.get("/oidc/userinfo", headers=auth).status_code == 200

        assert signed_in.post(
            "/oidc/revoke", data={"token": body["access_token"]} | self._creds()
        ).status_code == 200

        assert signed_in.get("/oidc/userinfo", headers=auth).status_code == 401

    def test_revoking_an_unknown_token_is_still_200(self, signed_in):
        """RFC 7009 §2.2: an unknown token is already in the state the caller asked for, and
        distinguishing the two would make this the oracle introspection authenticates against."""
        assert signed_in.post(
            "/oidc/revoke", data={"token": "nope"} | self._creds()
        ).status_code == 200


class TestTheFlagGovernsEverything:
    @pytest.mark.parametrize(
        ("method", "path"),
        [("get", "/oidc/authorize"), ("post", "/oidc/token"), ("get", "/oidc/userinfo"),
         ("post", "/oidc/introspect"), ("post", "/oidc/revoke")],
    )
    def test_every_provider_route_404s_when_disabled(self, tmp_path, monkeypatch, method, path):
        monkeypatch.delenv("RACK_WEB_OIDC_ENABLED", raising=False)
        oidc.reset_for_tests()
        from app import app

        app.config["TESTING"] = True
        with app.test_client() as c:
            assert getattr(c, method)(path).status_code == 404
