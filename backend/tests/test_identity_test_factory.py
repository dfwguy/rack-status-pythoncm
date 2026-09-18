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
"""The test credential factory: a token obtained by authenticating, not by minting.

**Why this file exists.** `enrol` and `token_for` call `tokens.mint` directly, so a suite built on
them proves nothing about the login path — it would stay green through a change that made every
real login fail. `log_in` drives the actual SRP exchange over HTTP, and these tests assert the
distinction rather than assuming it: that the token came from the route, that no fake signing key
was involved, and that the exchange is genuinely a proof of the password rather than a formality.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import testing as identity_testing


@pytest.fixture
def anon():
    from app import app

    with identity_testing.unauthenticated_client(app) as client:
        yield client


class TestItAuthenticatesRatherThanMinting:
    def test_the_token_comes_back_and_works(self, anon):
        principal = identity_testing.enrol("factory-alice")
        token = identity_testing.log_in(anon, principal.login)

        assert token, "log_in returned no token"
        # The client is authenticated by its cookie jar, so no header is needed.
        assert anon.get("/api/status").status_code == 200

    def test_the_token_was_issued_by_the_route_not_by_the_test(self, anon):
        """The claims are the route's, which is the whole point of authenticating.

        Asserted by *delivery* rather than by a claim, and deliberately: `auth_time` would be the
        obvious tell, but it is emitted only in OIDC mode, so a legacy-mode run would fail on a
        difference that means nothing. What no direct mint can do is arrive as a `Set-Cookie` from
        the server — the token is in the jar because the login route put it there.
        """
        from identity import tokens

        identity_testing.enrol("factory-bob")
        token = identity_testing.log_in(anon, "factory-bob")
        claims = tokens.verify(token, tokens.TOKEN_ACCESS)

        assert claims["sub"] == "user:factory-bob"
        assert anon.get_cookie("nvis_access").value == token, (
            "the token did not come from the route's Set-Cookie, so it was not issued by a login"
        )

    def test_the_refresh_cookie_is_delivered_too(self, anon):
        """A session is two tokens. A factory that produced only an access token would leave every
        test unable to exercise renewal, which is most of what `apiFetch` does."""
        identity_testing.enrol("factory-carol")
        identity_testing.log_in(anon, "factory-carol")

        assert anon.get_cookie("nvis_access") is not None
        # Path-scoped to `/api/auth`, so it is only sent to the renewal routes and never rides
        # along on ordinary API traffic. `get_cookie` defaults to `/` and returns None without it.
        assert anon.get_cookie("nvis_refresh", path="/api/auth") is not None, (
            "no refresh cookie; renewal is untestable"
        )

    def test_the_tokens_are_httponly(self, anon):
        """The reason cookies were chosen over a JS-held token: script cannot read them, so an XSS
        that runs in the page still cannot exfiltrate the session."""
        identity_testing.enrol("factory-dora")
        identity_testing.log_in(anon, "factory-dora")

        for name, path in (("nvis_access", "/"), ("nvis_refresh", "/api/auth")):
            assert anon.get_cookie(name, path=path).http_only, f"{name} is readable by script"


class TestItIsARealProofOfThePassword:
    def test_a_wrong_password_does_not_authenticate(self, anon):
        """Otherwise the factory would be a bypass wearing an SRP costume."""
        identity_testing.enrol("factory-eve", password="the-right-one")

        with pytest.raises(AssertionError):
            identity_testing.log_in(anon, "factory-eve", password="the-wrong-one")

        assert anon.get("/api/status").status_code == 401

    def test_an_unknown_login_does_not_authenticate(self, anon):
        with pytest.raises(AssertionError):
            identity_testing.log_in(anon, "nobody-here", password="anything")


class TestItNeedsNoFakeSigningKey:
    def test_it_works_against_the_deployments_own_secret(self, anon):
        """No `RACK_WEB_SECRET_KEY` is set by this file, and login still works.

        That is the property that lets 43 test files stop faking one. `app.py` bootstraps a real
        secret from `var/secrets/` at startup, so a test setting its own was never supplying
        something missing — it was *repointing* the key and invalidating tokens issued before it
        (INV-17), which is precisely why the client had to start minting per request.
        """
        assert os.environ.get("RACK_WEB_SECRET_KEY"), (
            "the app should have bootstrapped a secret; the factory relies on it"
        )
        identity_testing.enrol("factory-frank")
        assert identity_testing.log_in(anon, "factory-frank")

    def test_repointing_the_secret_no_longer_invalidates_a_token(self, anon, monkeypatch):
        """**This test asserted the opposite this morning, and the inversion is the record.**

        It demonstrated INV-17: repointing `RACK_WEB_SECRET_KEY` killed every token issued earlier,
        which is why 13 files faking a secret were a hazard and why the test client mints per
        request. Making the OIDC provider unconditional moved signing to the ES256 key set, so the
        secret no longer signs JWTs and this direction is now a non-event.

        The reason for removing the fakes is unchanged and still good — a test should authenticate
        rather than invent key material — but it now rests on honesty rather than on breakage. The
        per-request minting stays as belt-and-braces; it costs microseconds and it would matter again
        the moment anything reintroduces a symmetric path.
        """
        from identity import tokens

        identity_testing.enrol("factory-grace")
        token = identity_testing.log_in(anon, "factory-grace")
        assert tokens.verify(token, tokens.TOKEN_ACCESS)["sub"] == "user:factory-grace"

        monkeypatch.setenv("RACK_WEB_SECRET_KEY", "a-different-secret-entirely")
        assert tokens.verify(token, tokens.TOKEN_ACCESS)["sub"] == "user:factory-grace"
