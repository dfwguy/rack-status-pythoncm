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
"""Real credentials for tests, so the suite authenticates instead of bypassing (FR-18b).

**Why this exists.** ~2,500 tests ran with `RACK_WEB_AUTH_DISABLED=1`, so they never touched
`_require_dashboard_auth` at all — recorded as SUTP gap G-10. A suite that switches off the gate is
not testing the product; it is testing a configuration no deployment uses. Every component this app
requires at runtime should be invoked for real in a test, and the only things worth faking are the
ones a test genuinely cannot reach: a cloud service, an external call, a cluster.

Authentication is not one of those. It is entirely in-process — SRP against a local store, a token
signed with a local key — so a test can do the real thing at roughly the cost of a dict lookup.

**Nothing here weakens anything.** No new bypass, no test-only branch in product code, no relaxed
verification. It enrols a genuine SRP credential through `identity.srp.enroll` and mints a genuine
token through `identity.tokens.mint`, which is the same call `/api/auth/srp/finish` makes. A token
from this module is indistinguishable from one an operator obtains by signing in, which is the
property that makes the suite's result mean something: if the gate would refuse a real operator, it
refuses these too.

The cost of the alternative is worth naming. `RACK_WEB_AUTH_DISABLED` had to survive Phase 2
because 67 test files depended on it, and it was retained *with an expiry* (FR-18a → FR-18b →
FR-18c). This module is that expiry arriving.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from identity import srp, tokens
from identity.models import Group, User
from identity.principals import user_principal
from identity.store import IdentityStore

#: The principal the suite runs as unless a test asks for another. A named account rather than the
#: bootstrap `admin`, so a test that accidentally depends on being the bootstrap administrator
#: fails loudly instead of passing for the wrong reason.
DEFAULT_LOGIN = "test-operator"
DEFAULT_DISPLAY = "Test Operator"


@dataclass(frozen=True)
class TestPrincipal:
    """A real, usable identity: its login, its password, and a token that verifies."""

    login: str
    password: str
    principal: str
    access_token: str
    groups: tuple[str, ...] = ()

    @property
    def bearer(self) -> str:
        return f"Bearer {self.access_token}"


#: Id of the policy a test principal is attached to. One shared name, so a suite that turns
#: enforcement on grants every enrolled principal through a single document.
GRANT_ALL_POLICY_ID = "test-principals"


def grant_all_document() -> dict:
    """A policy granting everything, for test principals.

    Reuses the administrator document's shape — enumerated `<type>:*` rather than a bare `*`, which
    `_no_bare_star_in_allow` refuses — so it cannot fall behind the resource types the product
    declares. Its own id, because a test asserting something about the `administrators` policy
    should not be reading a document the harness happened to write.
    """
    from identity.bootstrap import admin_policy_document

    document = dict(admin_policy_document())
    document["id"] = GRANT_ALL_POLICY_ID
    document["description"] = "Grants test principals every action. Written by identity.testing."
    return document


def enforce_policies() -> None:
    """Write :func:`grant_all_document`, turning the attachments :func:`enrol` makes into grants.

    **Writing any policy document is what switches a deployment from the permissive engine to
    default-deny**, which is why this is a separate, explicit call rather than something `enrol`
    does. A suite that has not asked for enforcement keeps `AllowAllEngine` and the attachments sit
    inert, naming a document that does not exist — which the engine logs and skips.

    Idempotent, so a session fixture and an individual test can both ask.
    """
    from authz import store as policy_store

    if any(d.id == GRANT_ALL_POLICY_ID for d in policy_store.read_all()):
        return
    policy_store.create(grant_all_document())


def enrol(
    login: str = DEFAULT_LOGIN,
    *,
    display_name: str = "",
    groups: tuple[str, ...] = (),
    password: str | None = None,
    must_change_password: bool = False,
    authorize: bool = True,
    store: IdentityStore | None = None,
) -> TestPrincipal:
    """Create (or refresh) a real account and return a working credential.

    `groups` is why this takes a list rather than a single name: AC-28e requires two principals with
    *different* group memberships in one run, which is what makes an authorization test about
    anything. Groups are created on demand — a membership naming a group that does not exist is
    refused by the store, correctly, and a test should not have to know that.

    Idempotent per login, so a session-scoped fixture and a test that wants a second credential for
    the same person do not fight.
    """
    owns_store = store is None
    store = store or IdentityStore()
    try:
        secret = password or f"test-pw-{secrets.token_hex(8)}"
        salt, verifier = srp.enroll(login, secret)

        existing = store.get_user(login)
        if existing is None:
            store.create_user(
                User(
                    login=login,
                    display_name=display_name or login,
                    srp_salt=salt,
                    srp_verifier=verifier,
                    must_change_password=must_change_password,
                )
            )
        else:
            store.update_user(
                existing.edited(
                    srp_salt=salt,
                    srp_verifier=verifier,
                    must_change_password=must_change_password,
                )
            )

        for group in groups:
            if store.get_group(group) is None:
                store.create_group(Group(name=group, description=f"test group {group}"))
            store.add_member(group, login)

        if authorize:
            # The attachment, not the document. Harmless while no policy file exists — the engine
            # is permissive, and an attachment naming an absent policy is logged and skipped — and
            # it becomes a real grant the moment a suite calls `enforce_policies`.
            #
            # Default-on because a credential that authenticates and is then refused everything is
            # not what an operator holds, so a test using one asserts against a state the product
            # does not ship. Pass `authorize=False` where the refusal is the point.
            store.attach_policy(f"user:{login}", GRANT_ALL_POLICY_ID)
    finally:
        if owns_store:
            store.close()

    principal = f"user:{login}"
    return TestPrincipal(
        login=login,
        password=secret,
        principal=principal,
        access_token=tokens.mint(principal, token_type=tokens.TOKEN_ACCESS),
        groups=tuple(groups),
    )


def token_for(login: str = DEFAULT_LOGIN) -> str:
    """A fresh access token for an already-enrolled login.

    Separate from `enrol` because a test that needs a *second* token for the same principal — to
    check that revoking one does not revoke the other — must not re-enrol and change the password
    underneath itself.
    """
    return tokens.mint(f"user:{login}", token_type=tokens.TOKEN_ACCESS)


class AuthenticatedClient:
    """Mixin for Flask's test client that presents a real credential on every request.

    Installed as `app.test_client_class`, which is the reason this works without touching the 50
    places that build a client: `app.test_client()` already routes through that attribute, so every
    existing fixture and every bare `test_client()` call gets a credential with no edit.

    A test that needs to be *unauthenticated* — the gate's own tests, the 401 paths — passes
    `headers={"Authorization": ""}` or uses `unauthenticated()` below. Opting out is explicit,
    which is the right way round: previously opting *in* was explicit and almost nobody did.
    """

    #: Set by the conftest fixture once the principal exists. A *fallback*: `open` prefers a token
    #: minted for the current process state, for the reason below.
    nvis_bearer: str = ""

    #: The principal to mint for. Overridden by a fixture wanting a different subject.
    nvis_principal_login: str = DEFAULT_LOGIN

    #: Set `False` for a client whose whole purpose is to be refused — the gate's own tests, the
    #: 401 contract, the endpoint smoke sweep. Clearer at the fixture than repeating
    #: `headers=unauthenticated()` on every call, and greppable when the 401 paths need auditing.
    nvis_authenticate: bool = True

    def nvis_credential(self) -> str:
        """A `Bearer …` header value valid for **this request**, minted now.

        **Minted per request rather than captured once**, and that is not an optimisation — it is
        INV-17. Dozens of fixtures set their own `RACK_WEB_SECRET_KEY`, and the signing key is
        HKDF-derived from it, so a token minted when the client was built stops verifying the moment
        a fixture repoints the secret. The symptom is a 401 that looks exactly like a gate bug, in a
        test that never mentions authentication.

        Also installs the revocation checker when none is registered: `tokens.verify` fail-closes
        and refuses everything until one is (FR-6a, INV-18), and a fixture that calls
        `revocation.reset_for_tests()` would otherwise leave every later request unauthenticated.
        """
        from identity import revocation  # `tokens` is already imported at module level

        # Unconditional: `install()` is a registration, so re-registering is free, and a fixture
        # that called `reset_for_tests()` would otherwise leave `verify` fail-closed (INV-18).
        revocation.install()
        token = tokens.mint(user_principal(self.nvis_principal_login), tokens.TOKEN_ACCESS)
        return f"Bearer {token}"

    def open(self, *args, **kwargs):  # type: ignore[override]
        headers = kwargs.get("headers") or {}
        if hasattr(headers, "items") and not isinstance(headers, list):
            headers = dict(headers)
        else:
            headers = dict(headers or [])

        # An explicit empty Authorization means "I am testing the unauthenticated path"; drop it
        # so the gate sees no credential at all rather than a malformed one.
        if "Authorization" in headers and not headers["Authorization"]:
            headers.pop("Authorization")
        elif "Authorization" not in headers and self.nvis_authenticate:
            try:
                headers["Authorization"] = self.nvis_credential()
            except Exception:  # noqa: BLE001 - see below
                # A process with no signing secret cannot mint, and that is a legitimate state for
                # the tests that assert exactly that. Falling back to the captured bearer, then to
                # nothing, keeps this mixin from turning an unrelated failure into a mint error.
                if self.nvis_bearer:
                    headers["Authorization"] = self.nvis_bearer

        kwargs["headers"] = headers
        return super().open(*args, **kwargs)  # type: ignore[misc]


def unauthenticated_client(app: object) -> object:
    """A test client that never presents a credential.

    For the suites whose subject *is* the refusal. Building one is explicit because the default is
    now authenticated — the reverse of how it used to be, when opting in was explicit and almost
    nobody did.
    """
    from flask.testing import FlaskClient

    class _Anonymous(AuthenticatedClient, FlaskClient):
        nvis_authenticate = False

    app.config["TESTING"] = True  # type: ignore[attr-defined]
    previous = app.test_client_class  # type: ignore[attr-defined]
    app.test_client_class = _Anonymous  # type: ignore[attr-defined]
    try:
        return app.test_client()  # type: ignore[attr-defined]
    finally:
        app.test_client_class = previous  # type: ignore[attr-defined]


def unauthenticated() -> dict[str, str]:
    """Headers that make an authenticated client send no credential.

    ``client.get("/api/status", headers=unauthenticated())`` reads better at the call site than an
    empty string, and it is greppable when the 401 paths need auditing.
    """
    return {"Authorization": ""}
