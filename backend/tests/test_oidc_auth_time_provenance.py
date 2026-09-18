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
"""`auth_time` must say when the operator authenticated, not when they authorized (FR-8, AC-12).

**Why this file exists rather than another case in `test_oidc_provider.py`.** That suite already
asserts `auth_time` is preserved across a refresh, and it passed throughout — because it hands
`tokens.mint` an explicit `auth_time` and therefore cannot see where the value comes from. The
defect was upstream of the assertion: `/oidc/authorize` read `g.auth_time`, **nothing in the
codebase ever set it**, and `CodeStore.issue` fell back to `now`. So the flow faithfully preserved
a value that was wrong from the moment it was created.

The distinction only shows up if the test *drives the login* and then authorizes at a measurably
later moment, which is what these do. An operator who signed in this morning and completes an
authorization-code flow this afternoon must not get an id token claiming they authenticated
seconds ago — that is precisely the question a relying party's `max_age` check asks, and the wrong
answer is well-formed, plausible, and silent.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import testing as identity_testing

CLIENT_ID = "cli"
REDIRECT = "http://localhost:8123/callback"
CLIENTS = {"clients": [{
    "client_id": CLIENT_ID, "client_type": "public", "redirect_uris": [REDIRECT],
}]}


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce(seed: bytes = b"authtime-seed") -> tuple[str, str]:
    """A verifier and its S256 challenge, the same shape `test_oidc_provider.py` uses."""
    import hashlib

    verifier = _b64(hashlib.sha256(seed).digest())
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


@pytest.fixture
def provider(monkeypatch, tmp_path):
    """The provider enabled with its own key set, clients file and code store.

    A public client, because this flow has no client secret to present and PKCE is what stands in
    for one — which is also the shape the desktop launcher will use.
    """
    import json

    from identity import keys as keys_mod
    from identity import oidc as oidc_mod
    from identity import oidc_clients as clients_mod
    from identity import oidc_codes as codes_mod
    from identity import revocation

    clients_file = tmp_path / "clients.json"
    clients_file.write_text(json.dumps(CLIENTS), encoding="utf-8")
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "oidc"))
    monkeypatch.setenv("RACK_WEB_OIDC_CLIENTS_FILE", str(clients_file))
    monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
    monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", "https://greenlight.test:8443")

    keys_mod.reset_for_tests()
    oidc_mod.reset_for_tests()
    clients_mod.reset_for_tests()
    codes_mod.store().reset()
    revocation.install()
    yield
    keys_mod.reset_for_tests()
    oidc_mod.reset_for_tests()
    clients_mod.reset_for_tests()
    codes_mod.store().reset()


@pytest.fixture
def signed_in(provider):
    """A client that has really logged in, so its token carries a real `auth_time`."""
    from app import app

    with identity_testing.unauthenticated_client(app) as client:
        principal = identity_testing.enrol("authtime-operator")
        identity_testing.log_in(client, principal.login)
        yield client, principal


def _authorize(client, challenge: str) -> str:
    from urllib.parse import urlencode

    query = urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "scope": "openid profile",
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    resp = client.get(f"/oidc/authorize?{query}")
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]
    code = parse_qs(urlparse(resp.headers["Location"]).query).get("code", [""])[0]
    assert code, f"no code issued: {resp.headers['Location']}"
    return code


def _id_token(client, code: str, verifier: str) -> dict:
    from identity import tokens

    resp = client.post("/oidc/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return tokens.verify(resp.get_json()["id_token"], tokens.TOKEN_ID)


class TestAuthTimeComesFromTheLogin:
    def test_it_is_the_login_moment_not_the_authorize_moment(self, signed_in):
        """The regression this file was written for.

        A generous margin is used on purpose. The point is not millisecond fidelity — it is that the
        value is anchored to the login at all. Before the fix `auth_time` tracked the `/authorize`
        call, so the gap below was ~0 no matter how long the operator had been signed in.
        """
        client, _principal = signed_in

        verifier, challenge = pkce()
        login_moment = time.time()
        time.sleep(1.1)
        claims = _id_token(client, _authorize(client, challenge), verifier)

        auth_time = claims.get("auth_time")
        assert auth_time is not None, "no auth_time claim at all"

        # Anchored to the login: at least a second before now, because we slept.
        assert time.time() - float(auth_time) >= 1.0, (
            f"auth_time {auth_time} is ~now, so it was stamped at /authorize rather than carried "
            f"from the login"
        )
        # And not from before the login either — it is that login, not an arbitrary older value.
        assert float(auth_time) <= login_moment + 0.5

    def test_it_matches_the_session_tokens_own_auth_time(self, signed_in):
        """Stronger than a time window: the two must be the *same* authentication event.

        The session cookie's access token already carries the value the SRP login sampled, so this
        compares the id token against it directly rather than against a clock.
        """
        from identity import tokens

        client, _principal = signed_in
        session = tokens.verify(client.get_cookie("nvis_access").value, tokens.TOKEN_ACCESS)
        assert session.get("auth_time"), "the session token has no auth_time to carry forward"

        verifier, challenge = pkce()
        claims = _id_token(client, _authorize(client, challenge), verifier)
        assert float(claims["auth_time"]) == pytest.approx(float(session["auth_time"]), abs=0.001)

    def test_it_survives_a_second_authorization_without_creeping(self, signed_in):
        """Two authorizations from one login must report the same authentication moment.

        Creep is the shape this class of bug takes once the value is *nearly* right: each pass
        through the flow re-stamps it slightly later, so a long-lived session drifts toward "just
        authenticated" and `max_age` never trips.
        """
        client, _principal = signed_in

        v1, c1 = pkce(b"first")
        first = _id_token(client, _authorize(client, c1), v1)
        time.sleep(0.4)
        v2, c2 = pkce(b"second")
        second = _id_token(client, _authorize(client, c2), v2)

        assert float(first["auth_time"]) == pytest.approx(float(second["auth_time"]), abs=0.001)


class TestTheGateIsWhatCarriesIt:
    def test_the_gate_publishes_auth_time_for_the_request(self, signed_in):
        """Pins the mechanism, not only the outcome.

        `/oidc/authorize` reads `g.auth_time`. Asserting only the id token would keep passing if a
        later change made the endpoint recover the value some other way, and the next reader would
        have no idea which path was load-bearing.
        """
        from app import app
        from flask import g

        client, _principal = signed_in
        seen: dict[str, object] = {}

        def _capture():
            seen["auth_time"] = getattr(g, "auth_time", None)
            seen["principal"] = getattr(g, "auth_principal", None)

        # Appended to the list rather than registered with `@app.before_request`, which Flask
        # refuses once the app has served a request. Appending also puts this hook *after* the
        # gate, which is required: the gate is what sets the attribute being observed.
        app.before_request_funcs.setdefault(None, []).append(_capture)
        try:
            client.get("/api/status")
        finally:
            app.before_request_funcs[None].remove(_capture)

        assert seen["principal"] == "user:authtime-operator"
        assert seen["auth_time"] is not None, "the gate did not publish auth_time"

    def test_an_unauthenticated_request_publishes_nothing(self, provider):
        """`g.auth_time` must be absent rather than defaulted, or an anonymous request would carry
        an authentication moment it never had."""
        from app import app
        from flask import g

        seen: dict[str, object] = {}

        def _capture(response):
            seen["auth_time"] = getattr(g, "auth_time", "ABSENT")
            return response

        # `after_request`, not `before_request`, and that is forced rather than stylistic: the gate
        # refuses an anonymous caller by *returning a response*, and Flask stops calling remaining
        # `before_request` handlers as soon as one does. A hook registered after the gate therefore
        # never runs on exactly the request this test is about. `after_request` always runs, and `g`
        # is still the same request context.
        app.after_request_funcs.setdefault(None, []).append(_capture)
        try:
            with identity_testing.unauthenticated_client(app) as anon:
                assert anon.get("/api/status").status_code == 401
        finally:
            app.after_request_funcs[None].remove(_capture)

        assert seen["auth_time"] == "ABSENT"
