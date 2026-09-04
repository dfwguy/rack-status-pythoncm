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


def enrol(
    login: str = DEFAULT_LOGIN,
    *,
    display_name: str = "",
    groups: tuple[str, ...] = (),
    password: str | None = None,
    must_change_password: bool = False,
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

    #: Set by the conftest fixture once the principal exists.
    nvis_bearer: str = ""

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
        elif "Authorization" not in headers and self.nvis_bearer:
            headers["Authorization"] = self.nvis_bearer

        kwargs["headers"] = headers
        return super().open(*args, **kwargs)  # type: ignore[misc]


def unauthenticated() -> dict[str, str]:
    """Headers that make an authenticated client send no credential.

    ``client.get("/api/status", headers=unauthenticated())`` reads better at the call site than an
    empty string, and it is greppable when the 401 paths need auditing.
    """
    return {"Authorization": ""}
