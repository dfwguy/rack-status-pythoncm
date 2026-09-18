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
"""The local login issues an id token in the response body (FR-5) — and it authorizes nothing.

**These two facts belong in one file, because the second is what licenses the first.** An id token
is a statement *to the client* about who signed in, so a client that cannot read it has been handed
nothing; putting it in the JSON body is the standard's intent. The access and refresh tokens are the
opposite — they are credentials, and they stay `HttpOnly` where script cannot reach them.

That asymmetry is only defensible if presenting an id token as a bearer is refused. `verify` demands
an exact token type and the gate asks for `TOKEN_ACCESS`, so it is; AC-11e recorded the API-route
half of that as UNVERIFIED, and the tests below close it. If any of them ever fail, moving the id
token out of the body is the correct response.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import testing as identity_testing

PROTECTED = "/api/status"


@pytest.fixture
def oidc_mode(monkeypatch, tmp_path):
    """The provider on, which is when an id token is meaningful.

    In legacy HS256 mode the login deliberately omits it: an id token no `kid` identifies and no
    JWKS describes cannot be validated by a conforming client, so shipping one would be a claim we
    cannot back. `TestLegacyModeOmitsIt` asserts that direction.
    """
    from identity import keys as keys_mod
    from identity import oidc as oidc_mod

    monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
    monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", "https://greenlight.test:8443")
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "oidc"))
    keys_mod.reset_for_tests()
    oidc_mod.reset_for_tests()
    keys_mod.key_set().ensure()
    yield
    keys_mod.reset_for_tests()
    oidc_mod.reset_for_tests()


def _login(client, login: str) -> dict:
    """Drive the real SRP exchange and return the finish response body."""
    import srp as srp_lib
    from identity import srp as srp_mod

    principal = identity_testing.enrol(login)
    srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
    user = srp_lib.User(
        login, principal.password, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
    )
    _name, a = user.start_authentication()
    begin = client.post("/api/auth/srp/begin", json={"login": login, "A": a.hex()}).get_json()
    proof = user.process_challenge(
        bytes.fromhex(begin["salt"]), bytes.fromhex(begin["B"])
    )
    resp = client.post(
        "/api/auth/srp/finish", json={"token": begin["token"], "M": proof.hex()}
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_json()


@pytest.fixture
def anon(oidc_mode):
    from app import app

    with identity_testing.unauthenticated_client(app) as client:
        yield client


class TestTheLoginIssuesOne:
    def test_the_body_carries_an_id_token(self, anon):
        body = _login(anon, "idtok-alice")
        assert body.get("id_token"), "FR-5 asks for an id token and the login issued none"

    def test_it_verifies_as_an_id_token(self, anon):
        from identity import tokens

        body = _login(anon, "idtok-bob")
        claims = tokens.verify(body["id_token"], tokens.TOKEN_ID)
        assert claims["sub"] == "user:idtok-bob"

    def test_it_carries_the_authentication_moment(self, anon):
        """Not the mint moment. The same property the code flow needed fixing for."""
        from identity import tokens

        body = _login(anon, "idtok-carol")
        access = tokens.verify(anon.get_cookie("nvis_access").value, tokens.TOKEN_ACCESS)
        ident = tokens.verify(body["id_token"], tokens.TOKEN_ID)
        assert float(ident["auth_time"]) == pytest.approx(float(access["auth_time"]), abs=0.001)

    def test_at_hash_binds_it_to_this_access_token(self, anon):
        """OIDC Core §3.1.3.6, and the half that gets missed is which half of the digest.

        Computed here independently of the implementation rather than by calling `oidc.at_hash`,
        so a change to that function fails this test instead of agreeing with itself.
        """
        import base64
        import hashlib

        from identity import tokens

        body = _login(anon, "idtok-dora")
        access_raw = anon.get_cookie("nvis_access").value
        digest = hashlib.sha256(access_raw.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest[: len(digest) // 2]).decode().rstrip("=")

        assert tokens.verify(body["id_token"], tokens.TOKEN_ID)["at_hash"] == expected

    def test_the_credentials_are_not_in_the_body(self, anon):
        """The other half of the split: what a client may read, and what it may not.

        An access or refresh token in the body would be readable by script, which is the exposure
        `HttpOnly` exists to remove — and it would make the id token's presence there look like the
        same category of decision, when it is the opposite one.
        """
        body = _login(anon, "idtok-erin")
        serialised = json.dumps(body)
        for cookie, path in (("nvis_access", "/"), ("nvis_refresh", "/api/auth")):
            value = anon.get_cookie(cookie, path=path).value
            assert value not in serialised, f"{cookie} leaked into the response body"


class TestItAuthorizesNothing:
    """AC-11e. The property that licenses putting the id token where script can read it."""

    def test_it_is_refused_as_a_bearer_on_an_api_route(self, anon):
        body = _login(anon, "idtok-frank")

        with identity_testing.unauthenticated_client(__import__("app").app) as fresh:
            resp = fresh.get(PROTECTED, headers={"Authorization": f"Bearer {body['id_token']}"})
        assert resp.status_code == 401, (
            "an id token authenticated an API route. It is readable by script, so it must not be "
            "a credential — either the gate stopped demanding TOKEN_ACCESS, or `verify` stopped "
            "checking the type."
        )

    def test_it_is_refused_in_the_access_cookie(self, anon):
        """The cookie is the transport the gate prefers, so the type check has to hold there too."""
        from app import app

        body = _login(anon, "idtok-gail")
        with identity_testing.unauthenticated_client(app) as fresh:
            fresh.set_cookie("nvis_access", body["id_token"], domain="localhost")
            assert fresh.get(PROTECTED).status_code == 401

    def test_a_refresh_token_is_also_refused_as_a_bearer(self, anon):
        """The neighbouring confusion, and the more dangerous one: a refresh token lives far longer
        than an access token, so accepting it as one would extend a session's blast radius."""
        from app import app

        _login(anon, "idtok-hana")
        refresh = anon.get_cookie("nvis_refresh", path="/api/auth").value
        with identity_testing.unauthenticated_client(app) as fresh:
            resp = fresh.get(PROTECTED, headers={"Authorization": f"Bearer {refresh}"})
        assert resp.status_code == 401

    def test_the_access_token_still_works(self, anon):
        """The control. Without it the three refusals above would pass on a gate that refuses
        everything, which is a different bug wearing the same green tick."""
        from app import app

        _login(anon, "idtok-ivan")
        access = anon.get_cookie("nvis_access").value
        with identity_testing.unauthenticated_client(app) as fresh:
            resp = fresh.get(PROTECTED, headers={"Authorization": f"Bearer {access}"})
        assert resp.status_code == 200
