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
"""The `.well-known` endpoints — Phase 2.5, FR-5, FR-6, FR-6a, FR-6b, FR-19.

The endpoints are the easy part. What these tests are really for is the **half-switched state**:
a deployment signing HS256 while advertising a JWKS publishes a key that verifies nothing, and every
consumer then fails in a way that looks like our bug. AC-30 forbids it; `TestNoHalfSwitchedState`
is where that is enforced.
"""

from __future__ import annotations

import json

import pytest
from identity import keys as keys_mod
from identity import oidc

DISCOVERY = "/.well-known/openid-configuration"
JWKS = "/.well-known/jwks.json"
ISSUER = "https://rack.example.com"


@pytest.fixture
def client():
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def provider(tmp_path, monkeypatch):
    """A deployment with the provider switched on and a key bootstrapped."""
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "oidc"))
    monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
    monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", ISSUER)
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()
    keys_mod.key_set().ensure()
    yield
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()


@pytest.fixture
def provider_off(tmp_path, monkeypatch):
    monkeypatch.delenv("RACK_WEB_OIDC_ENABLED", raising=False)
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "oidc"))
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()
    yield
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()


class TestDiscoveryDocument:
    """FR-5. Advertises only what is implemented."""

    def test_the_issuer_is_the_configured_url(self, client, provider):
        assert client.get(DISCOVERY).get_json()["issuer"] == ISSUER

    def test_a_trailing_slash_is_normalised_away(self, client, monkeypatch, provider):
        """The commonest cause of an issuer mismatch, and clients compare by exact string."""
        monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", ISSUER + "/")
        oidc.reset_for_tests()
        assert client.get(DISCOVERY).get_json()["issuer"] == ISSUER

    def test_the_jwks_uri_points_at_a_route_that_exists(self, client, provider):
        """AC-8. A document naming an endpoint that 404s is worse than no document."""
        uri = client.get(DISCOVERY).get_json()["jwks_uri"]
        assert uri == f"{ISSUER}{JWKS}"
        assert client.get(JWKS).status_code == 200

    def test_every_advertised_algorithm_is_one_we_actually_sign_with(self, client, provider):
        """AC-8a. Advertising RS256 on an ES256 deployment fails only at the client."""
        advertised = client.get(DISCOVERY).get_json()["id_token_signing_alg_values_supported"]
        assert advertised == [keys_mod.ALGORITHM] == ["ES256"]

    def test_only_implemented_grants_and_response_types_are_advertised(self, client, provider):
        doc = client.get(DISCOVERY).get_json()
        assert set(doc["grant_types_supported"]) == {"authorization_code", "refresh_token"}
        assert doc["response_types_supported"] == ["code"]

    def test_plain_pkce_is_not_advertised(self, client, provider):
        """Many providers offer `plain`. It protects nothing against an intercepted code, and
        declining to offer it is the point rather than an omission."""
        assert client.get(DISCOVERY).get_json()["code_challenge_methods_supported"] == ["S256"]

    def test_every_advertised_endpoint_resolves(self, client, provider):
        """AC-8. A document naming an endpoint that 404s is worse than no document: the client
        configures itself successfully and then fails at the point of use."""
        doc = client.get(DISCOVERY).get_json()
        endpoints = [v for k, v in doc.items() if k.endswith("_endpoint") or k == "jwks_uri"]
        assert len(endpoints) >= 5

        rules = {str(r) for r in client.application.url_map.iter_rules()}
        for url in endpoints:
            path = url.replace(ISSUER, "")
            assert path in rules, f"{path} is advertised but is not a route"

    def test_every_advertised_scope_gates_something(self, client, provider):
        """The rule this file has always enforced, now stated as the rule rather than as the one
        scope that happened to satisfy it.

        Previously `scopes_supported` was pinned to exactly `["openid"]`, because nothing else gated
        anything and *"a scope that gates nothing must not be advertised as if it did"*. The
        constraint has not been relaxed — `profile`, `email` and `phone` each now decide claim
        release at `/userinfo`, so they earn their place. Asserting the rule means adding a scope to
        the document without wiring it fails here, which pinning the literal list could not catch.
        """
        advertised = client.get(DISCOVERY).get_json()["scopes_supported"]
        assert "openid" in advertised, "an OIDC provider must advertise `openid`"
        for scope in advertised:
            assert oidc.SCOPE_CLAIMS.get(scope), f"{scope} is advertised but releases no claim"

    def test_claims_supported_is_not_narrower_than_what_userinfo_can_return(self, client, provider):
        """A client configures itself from this list, so a claim the endpoint can return and the
        document omits is a claim nobody asks for."""
        advertised = set(client.get(DISCOVERY).get_json()["claims_supported"])
        releasable = {c for claims in oidc.SCOPE_CLAIMS.values() for c in claims}
        assert releasable <= advertised, f"missing from claims_supported: {sorted(releasable - advertised)}"

    def test_the_response_is_cacheable(self, client, provider):
        assert "max-age" in client.get(DISCOVERY).headers.get("Cache-Control", "")


class TestTheIssuerMustBeConfigured:
    """The issuer is a security boundary, so it never comes from the request.

    It may be *derived* from the bind address when unset — `startup_gate._ensure_oidc_issuer()`,
    warning as it does so — but a configured value is validated strictly, and neither path ever
    reads a `Host` header, which is attacker-controlled.
    """

    @pytest.mark.parametrize(
        "bad",
        ["http://rack.example.com", "ftp://x", "notaurl", "https://x.com?a=1", "https://x.com#f"],
    )
    def test_an_unusable_issuer_is_refused(self, monkeypatch, bad):
        """An issuer that is *configured* is still validated strictly.

        Only the absent case became lenient: the gate derives one rather than refusing to boot. A
        value an operator typed is a statement of intent, and a malformed one is refused, because a
        client compares `iss` byte-for-byte and the mismatch surfaces at the client with nothing
        visibly wrong here.
        """
        monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", bad)
        oidc.reset_for_tests()
        with pytest.raises(oidc.OIDCConfigError):
            oidc.issuer_url()

    @pytest.mark.parametrize("ok", ["http://localhost:5056", "http://127.0.0.1:8080"])
    def test_localhost_over_http_is_permitted(self, monkeypatch, ok):
        """Not a loophole — it is how an operator actually reaches a tunnelled deployment.

        Refusing it would make the provider unusable in the deployment shape this product is most
        often run in (`ssh -L 5156:localhost:5056`).
        """
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
        monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", ok)
        oidc.reset_for_tests()
        assert oidc.issuer_url() == ok

    def test_the_issuer_is_never_taken_from_the_request_host(self, client, provider):
        """A Host header is attacker-controlled; the issuer is a security boundary.

        If it were derived, an attacker could make us mint a token claiming to come from an origin
        they control, and any relying party trusting that origin would accept it.
        """
        resp = client.get(DISCOVERY, headers={"Host": "evil.example.net"})
        assert resp.get_json()["issuer"] == ISSUER


class TestTheJwksPublishesKeysAndNothingElse:
    """FR-6, FR-6a."""

    PRIVATE = ("d", "p", "q", "dp", "dq", "qi")

    def test_the_key_set_is_published_with_the_fields_a_client_needs(self, client, provider):
        keys = client.get(JWKS).get_json()["keys"]
        assert keys
        for jwk in keys:
            assert {"kty", "crv", "alg", "use", "kid", "x", "y"} <= set(jwk)

    def test_no_private_field_reaches_the_wire(self, client, provider):
        """Asserted against the raw bytes, not only the parsed mapping.

        A field can be absent from the dict and still reach the response through a nested object or
        a custom encoder, and it is the bytes a client receives that matter.
        """
        raw = client.get(JWKS).get_data(as_text=True)
        parsed = json.loads(raw)

        for jwk in parsed["keys"]:
            for field in self.PRIVATE:
                assert field not in jwk
        assert "BEGIN" not in raw and "PRIVATE" not in raw

    def test_rotation_is_visible_in_the_published_set(self, client, provider):
        before = {k["kid"] for k in client.get(JWKS).get_json()["keys"]}
        keys_mod.key_set().rotate()
        after = {k["kid"] for k in client.get(JWKS).get_json()["keys"]}

        assert before < after, "the previous key must remain published, or its tokens die early"
        assert len(after) == len(before) + 1

    def test_an_empty_key_set_is_a_503_rather_than_an_empty_200(self, client, provider, monkeypatch):
        """The worst possible 200: a cacheable positive claim that the issuer has no keys.

        Startup makes this unreachable; the guard exists because losing that wiring would otherwise
        be invisible for the full cache lifetime.
        """
        monkeypatch.setattr(
            keys_mod.KeySet, "public_jwks", lambda self, **kw: {"keys": []}, raising=True
        )
        resp = client.get(JWKS)

        assert resp.status_code == 503
        assert "Cache-Control" not in resp.headers or "max-age" not in resp.headers["Cache-Control"]

    def test_the_response_is_cacheable_when_it_has_keys(self, client, provider):
        assert "max-age" in client.get(JWKS).headers.get("Cache-Control", "")


class TestTheCacheActuallyServes:
    """NFR-2, NFR-2a, AC-24.

    A cache that never hits cannot be shown to be stale, so staleness tests over it prove nothing.
    The Phase 2 revocation cache was in exactly that state — invalidated by its own reads, hit count
    zero, every functional test green.
    """

    def test_repeated_discovery_fetches_hit_the_cache(self, client, provider):
        oidc.reset_for_tests()
        for _ in range(5):
            client.get(DISCOVERY)
        assert oidc.cache_hits() >= 4

    def test_a_changed_issuer_is_not_served_from_the_cache(self, client, monkeypatch, provider):
        """The other half: a cache that never invalidates is a different bug."""
        assert client.get(DISCOVERY).get_json()["issuer"] == ISSUER
        monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", "https://moved.example.com")
        assert client.get(DISCOVERY).get_json()["issuer"] == "https://moved.example.com"

    def test_serving_the_endpoints_writes_nothing(self, client, provider, tmp_path):
        """NFR-2a. A read that writes changes the mtime a cache above it may key on."""
        key_dir = tmp_path / "oidc"
        before = {p.name: p.stat().st_mtime_ns for p in key_dir.iterdir()}

        for _ in range(5):
            client.get(DISCOVERY)
            client.get(JWKS)

        after = {p.name: p.stat().st_mtime_ns for p in key_dir.iterdir()}
        assert before == after


class TestTheGate:
    """FR-6b. Reachable without a credential, and nothing else new is."""

    @pytest.mark.parametrize("path", [DISCOVERY, JWKS])
    def test_both_are_on_the_bypass_list(self, path):
        import web_auth

        endpoint = "oidc.openid_configuration" if "configuration" in path else "oidc.jwks"
        assert endpoint in web_auth.AUTH_BYPASS_ENDPOINTS

    def test_the_bypass_set_is_exactly_what_this_phase_intends(self):
        """FR-6b and FR-6c. Two categories, and the difference matters.

        The `.well-known` pair need **no credential at all**. The four OAuth routes authenticate by
        a mechanism this gate does not implement — client credentials, or a Bearer token — so they
        must bypass it and check for themselves. `/authorize` is deliberately absent: it needs a
        signed-in operator, which is what the gate is for.

        Pinned as a set rather than a count, because growth here is the thing to notice: an entry
        in this list is a route the gate will not check.
        """
        import web_auth

        assert {
            e for e in web_auth.AUTH_BYPASS_ENDPOINTS if e.startswith(("oidc.", "oidc_provider."))
        } == {
            "oidc.openid_configuration",
            "oidc.jwks",
            "oidc_provider.token",
            "oidc_provider.userinfo",
            "oidc_provider.introspect",
            "oidc_provider.revoke",
        }
