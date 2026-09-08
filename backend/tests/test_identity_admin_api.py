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
"""User, group and membership administration — FR-11, FR-13, FR-8, FR-2, FR-2b, NFR-8.

The headline case is the one that proves the feature rather than the plumbing: **a user created
through the API can actually log in with the password it returned.** Everything else — the record
exists, the fields are right — is satisfied equally well by a user nobody can authenticate as.

The rest guards invariants that are cheap to violate and expensive to discover:

- a login is immutable and, once used, never reused (FR-2, FR-2b) — audit records name principals
  by login, so reuse silently reattributes a departed operator's history to a new person;
- groups are flat (FR-8), so there is no transitive resolution to get wrong;
- disabling revokes **now**, not when the token happens to expire, which is the entire point of
  doing it during an offboarding;
- every mutating route carries its own action name (NFR-8), because Phase 3 must be able to say
  "may add members, may not create users" and a shared name makes that unsayable.
"""

from __future__ import annotations

import pytest
import srp as srp_lib
from identity import bootstrap
from identity import srp as srp_mod
from identity.store import IdentityStore


@pytest.fixture
def admin_api(monkeypatch, tmp_path):
    """A private identity store containing the bootstrap administrator.

    The administrator is created explicitly rather than left to app startup: that ran once at import
    time against whatever path was configured then, so without this each test would get an *empty*
    store — and the first user created would also be the last, which the lockout guard correctly
    refuses to remove. The tests below would then fail for a reason unrelated to what they assert.
    """
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    srp_mod.reset_for_tests()

    store = IdentityStore(str(tmp_path / "identity.json"))
    try:
        bootstrap.ensure_admin(store, emit=False)
    finally:
        store.close()

    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    srp_mod.reset_for_tests()


def _create(client, login: str, **extra):
    """Create a user, defaulting the now-required email so these tests keep asserting their own
    subject. A caller that wants to exercise the email rules passes `email=` explicitly; the rules
    themselves live in `test_identity_contact_attributes.py`."""
    body = {"login": login, "email": f"{login}@example.com"}
    body.update(extra)
    return client.post("/api/identity/users", json=body)


class TestACreatedUserCanActuallyLogIn:
    """The test that proves the feature. A record nobody can authenticate as is not a user."""

    def test_the_returned_password_authenticates(self, admin_api):
        created = _create(admin_api, "operator1")
        assert created.status_code == 201
        password = created.get_json()["password"]

        srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
        user = srp_lib.User(
            "operator1", password, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
        )
        _name, a = user.start_authentication()
        begin = admin_api.post(
            "/api/auth/srp/begin", json={"login": "operator1", "A": a.hex()}
        ).get_json()
        proof = user.process_challenge(bytes.fromhex(begin["salt"]), bytes.fromhex(begin["B"]))
        finish = admin_api.post(
            "/api/auth/srp/finish", json={"token": begin["token"], "M": proof.hex()}
        )

        assert finish.status_code == 200, finish.get_data(as_text=True)
        assert finish.get_json()["user"]["login"] == "operator1"

    def test_the_new_account_must_change_its_password(self, admin_api):
        """Same contract as the bootstrap: the operator conveyed it out of band, so it is shared
        until the user replaces it."""
        _create(admin_api, "operator2")
        user = admin_api.get("/api/identity/users/operator2").get_json()["user"]
        assert user["must_change_password"] is True

    def test_the_password_is_returned_once_and_never_again(self, admin_api, tmp_path):
        password = _create(admin_api, "operator3").get_json()["password"]

        again = admin_api.get("/api/identity/users/operator3").get_json()
        assert password not in str(again)
        assert password not in (tmp_path / "identity.json").read_text(encoding="utf-8")

    def test_the_response_never_carries_a_verifier(self, admin_api):
        """The verifier is a credential. Listing users must not hand it out."""
        _create(admin_api, "operator4")
        body = admin_api.get("/api/identity/users").get_data(as_text=True)
        assert "srp_verifier" not in body
        assert "srp_salt" not in body


class TestTheLoginIsImmutableAndNeverReused:
    def test_a_login_cannot_be_renamed(self, admin_api):
        _create(admin_api, "operator5")
        resp = admin_api.patch("/api/identity/users/operator5", json={"login": "someone-else"})
        assert resp.status_code == 400

    def test_a_display_name_can_change_without_touching_the_login(self, admin_api):
        _create(admin_api, "operator6", display_name="Old Name")
        resp = admin_api.patch("/api/identity/users/operator6", json={"display_name": "New Name"})
        assert resp.status_code == 200
        assert resp.get_json()["user"]["display_name"] == "New Name"
        assert resp.get_json()["user"]["login"] == "operator6"

    def test_a_deleted_login_cannot_be_recreated(self, admin_api):
        """FR-2b. Reuse would reattribute a departed operator's audit history to a new person."""
        _create(admin_api, "operator7")
        assert admin_api.delete("/api/identity/users/operator7").status_code == 200
        assert _create(admin_api, "operator7").status_code == 409

    def test_a_duplicate_login_is_refused(self, admin_api):
        _create(admin_api, "operator8")
        assert _create(admin_api, "operator8").status_code == 409

    def test_logins_are_canonicalised_so_case_is_not_a_second_account(self, admin_api):
        assert _create(admin_api, "Operator9").status_code == 201
        assert _create(admin_api, "operator9").status_code == 409


class TestGroupsAreFlat:
    def test_a_group_cannot_contain_a_group(self, admin_api):
        """AC-16 / FR-8. Allowing it would need transitive resolution and a cycle guard."""
        admin_api.post("/api/identity/groups", json={"name": "operators"})
        admin_api.post("/api/identity/groups", json={"name": "admins"})

        resp = admin_api.post(
            "/api/identity/groups/operators/members", json={"login": "admins"}
        )
        assert resp.status_code == 400
        assert "group" in resp.get_json()["error"].lower()

    def test_membership_round_trips(self, admin_api):
        _create(admin_api, "operator10")
        admin_api.post("/api/identity/groups", json={"name": "operators"})

        assert admin_api.post(
            "/api/identity/groups/operators/members", json={"login": "operator10"}
        ).status_code == 201
        assert admin_api.get("/api/identity/users/operator10").get_json()["user"]["groups"] == [
            "operators"
        ]

        assert admin_api.delete(
            "/api/identity/groups/operators/members/operator10"
        ).status_code == 200
        assert admin_api.get("/api/identity/users/operator10").get_json()["user"]["groups"] == []

    def test_adding_an_unknown_user_is_refused(self, admin_api):
        admin_api.post("/api/identity/groups", json={"name": "operators"})
        assert admin_api.post(
            "/api/identity/groups/operators/members", json={"login": "nobody"}
        ).status_code == 404

    def test_a_group_cannot_be_renamed(self, admin_api):
        admin_api.post("/api/identity/groups", json={"name": "operators"})
        assert admin_api.patch(
            "/api/identity/groups/operators", json={"name": "others"}
        ).status_code == 400


class TestDisablingTakesEffectImmediately:
    def test_a_disabled_account_cannot_log_in(self, admin_api):
        password = _create(admin_api, "operator11").get_json()["password"]
        assert admin_api.put(
            "/api/identity/users/operator11/disabled", json={"disabled": True}
        ).status_code == 200

        srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
        user = srp_lib.User(
            "operator11", password, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
        )
        _name, a = user.start_authentication()
        begin = admin_api.post(
            "/api/auth/srp/begin", json={"login": "operator11", "A": a.hex()}
        ).get_json()
        proof = user.process_challenge(bytes.fromhex(begin["salt"]), bytes.fromhex(begin["B"]))
        finish = admin_api.post(
            "/api/auth/srp/finish", json={"token": begin["token"], "M": proof.hex()}
        )
        assert finish.status_code == 401

    def test_re_enabling_restores_access(self, admin_api):
        _create(admin_api, "operator12")
        admin_api.put("/api/identity/users/operator12/disabled", json={"disabled": True})
        admin_api.put("/api/identity/users/operator12/disabled", json={"disabled": False})
        assert admin_api.get(
            "/api/identity/users/operator12"
        ).get_json()["user"]["disabled"] is False

    def test_disabling_revokes_rather_than_waiting_for_expiry(self, admin_api, tmp_path):
        """The whole point of disabling during an offboarding is that it is not advisory."""
        from identity import revocation, tokens

        revocation.reset_for_tests()
        revocation.install()
        _create(admin_api, "operator13")
        token = tokens.mint("user:operator13", tokens.TOKEN_ACCESS)
        assert tokens.verify(token, tokens.TOKEN_ACCESS)

        admin_api.put("/api/identity/users/operator13/disabled", json={"disabled": True})

        with pytest.raises(tokens.TokenError):
            tokens.verify(token, tokens.TOKEN_ACCESS)


class TestAC23aCredentialReset:
    """FR-13a. Four properties, because the tempting implementation satisfies the first two.

    A reset that issues a working password and leaves the old one working, or leaves the flag clear,
    or lets the previous holder's sessions run out their lifetime, is not a reset — and each of
    those failures is invisible from the response, which reports success either way.
    """

    @staticmethod
    def _srp_login(client, login, password):
        srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
        user = srp_lib.User(login, password, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE)
        _name, a = user.start_authentication()
        begin = client.post(
            "/api/auth/srp/begin", json={"login": login, "A": a.hex()}
        ).get_json()
        proof = user.process_challenge(bytes.fromhex(begin["salt"]), bytes.fromhex(begin["B"]))
        return client.post(
            "/api/auth/srp/finish", json={"token": begin["token"], "M": proof.hex()}
        )

    def test_the_new_password_authenticates(self, admin_api):
        original = _create(admin_api, "reset-me").get_json()["password"]
        resp = admin_api.post("/api/identity/users/reset-me/credential")
        assert resp.status_code == 200
        issued = resp.get_json()["password"]
        assert issued != original

        srp_mod.reset_for_tests()
        assert self._srp_login(admin_api, "reset-me", issued).status_code == 200

    def test_the_previous_password_stops_working(self, admin_api):
        original = _create(admin_api, "reset-me2").get_json()["password"]
        admin_api.post("/api/identity/users/reset-me2/credential")

        srp_mod.reset_for_tests()
        assert self._srp_login(admin_api, "reset-me2", original).status_code == 401

    def test_the_account_is_flagged_for_change_again(self, admin_api, tmp_path):
        _create(admin_api, "reset-me3")
        # Clear the flag first, so this cannot pass on the *creation* flag still being set — which
        # it would, and the test would then prove nothing about reset.
        store = IdentityStore(str(tmp_path / "identity.json"))
        try:
            user = store.get_user("reset-me3")
            store.update_user(user.edited(must_change_password=False))
        finally:
            store.close()
        assert admin_api.get(
            "/api/identity/users/reset-me3"
        ).get_json()["user"]["must_change_password"] is False

        admin_api.post("/api/identity/users/reset-me3/credential")
        assert admin_api.get(
            "/api/identity/users/reset-me3"
        ).get_json()["user"]["must_change_password"] is True

    def test_existing_sessions_are_revoked(self, admin_api):
        """A reset whose previous holder keeps working is the opposite of the operation."""
        from identity import revocation, tokens

        revocation.reset_for_tests()
        revocation.install()
        _create(admin_api, "reset-me4")
        token = tokens.mint("user:reset-me4", tokens.TOKEN_ACCESS)
        assert tokens.verify(token, tokens.TOKEN_ACCESS)

        admin_api.post("/api/identity/users/reset-me4/credential")

        with pytest.raises(tokens.TokenError):
            tokens.verify(token, tokens.TOKEN_ACCESS)

    def test_resetting_an_unknown_user_is_404(self, admin_api):
        assert admin_api.post("/api/identity/users/ghost/credential").status_code == 404

    def test_the_password_is_not_persisted(self, admin_api, tmp_path):
        _create(admin_api, "reset-me5")
        issued = admin_api.post(
            "/api/identity/users/reset-me5/credential"
        ).get_json()["password"]
        assert issued not in (tmp_path / "identity.json").read_text(encoding="utf-8")


class TestAC23bNoUnauthenticatedCredentialPath:
    """The half of FR-12 that survives the amendment, and the reason it was safe to narrow.

    Walks the whole URL map rather than checking the routes this file happens to know about — a
    future self-service reset added elsewhere is exactly the regression this is for.
    """

    def test_no_bypassed_route_issues_or_clears_a_credential(self):
        import web_auth
        from app import app

        credential_paths = []
        for rule in app.url_map.iter_rules():
            path = str(rule)
            if not any(
                token in path for token in ("/credential", "/password", "/users")
            ):
                continue
            mutating = bool({"POST", "PUT", "PATCH", "DELETE"} & rule.methods)
            if mutating and rule.endpoint in web_auth.AUTH_BYPASS_ENDPOINTS:
                credential_paths.append(f"{rule.endpoint} {path}")

        assert not credential_paths, (
            "these credential-affecting routes are in the auth bypass set, which is the "
            f"unauthenticated recovery path FR-12 forbids: {credential_paths}"
        )

    def test_the_reset_route_is_not_bypassed(self):
        import web_auth

        assert "identity_admin.reset_credential" not in web_auth.AUTH_BYPASS_ENDPOINTS


class TestCreatingWithAChosenPassword:
    """The operator picks the password; the browser derives the credential from it.

    The point is where the password exists. In *chosen* mode it never reaches this process — the
    client sends a salt and verifier, exactly as SRP does for a login. A plaintext `password` field
    is refused rather than hashed server-side, because accepting it would put a live credential in a
    request body in a system built to keep it out of one.
    """

    def test_a_chosen_credential_authenticates(self, admin_api):
        password = "an-operator-chosen-password"
        salt, verifier = srp_mod.enroll("chosen", password)

        created = admin_api.post(
            "/api/identity/users",
            json={
                "login": "chosen",
                "email": "chosen@example.com",
                "salt": salt,
                "verifier": verifier,
            },
        )
        assert created.status_code == 201

        srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
        user = srp_lib.User(
            "chosen", password, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
        )
        _name, a = user.start_authentication()
        begin = admin_api.post(
            "/api/auth/srp/begin", json={"login": "chosen", "A": a.hex()}
        ).get_json()
        proof = user.process_challenge(bytes.fromhex(begin["salt"]), bytes.fromhex(begin["B"]))
        finish = admin_api.post(
            "/api/auth/srp/finish", json={"token": begin["token"], "M": proof.hex()}
        )
        assert finish.status_code == 200

    def test_no_password_is_returned_for_a_chosen_credential(self, admin_api):
        """Omitted, not null — a client must not render an empty "here is the password" panel."""
        salt, verifier = srp_mod.enroll("chosen2", "pw")
        body = admin_api.post(
            "/api/identity/users",
            json={"login": "chosen2", "salt": salt, "verifier": verifier},
        ).get_json()
        assert "password" not in body

    def test_a_plaintext_password_is_refused(self, admin_api):
        resp = admin_api.post(
            "/api/identity/users", json={"login": "plain", "password": "hunter2"}
        )
        assert resp.status_code == 400
        assert "srp" in resp.get_json()["error"].lower()

    @pytest.mark.parametrize("payload", [
        {"login": "half1", "salt": "aabb"},
        {"login": "half2", "verifier": "aabb"},
    ])
    def test_half_a_credential_is_refused(self, admin_api, payload):
        assert admin_api.post("/api/identity/users", json=payload).status_code == 400

    @pytest.mark.parametrize("bad", ["zz", "not-hex", "abc"])
    def test_a_non_hex_credential_is_refused(self, admin_api, bad):
        """A non-hex verifier in the store is a credential nobody can authenticate against."""
        resp = admin_api.post(
            "/api/identity/users", json={"login": "bad", "salt": "aabb", "verifier": bad}
        )
        assert resp.status_code == 400
        assert admin_api.get("/api/identity/users/bad").status_code == 404

    def test_the_login_charset_stays_ascii_so_the_client_can_canonicalise(self):
        """Pins a coupling the frontend depends on and cannot see.

        `identityService.createUserWithPassword` derives the verifier against
        `login.toLowerCase()`, because SRP hashes the identity into `x` and the client must use the
        same string the server stores. The server uses `casefold()`, which differs from
        `toLowerCase()` only outside ASCII — and `_LOGIN_RE` is validated *before* the fold, so every
        login that reaches it is ASCII.

        Widening that regex to accept non-ASCII would silently break chosen-password enrolment for
        those logins, presenting as "wrong password" at first sign-in. This fails instead.
        """
        from identity.models import _LOGIN_RE

        assert _LOGIN_RE.pattern == r"^[A-Za-z0-9](?:[A-Za-z0-9._@+-]*[A-Za-z0-9])?$", (
            "the login charset changed; if it now admits non-ASCII, casefold() and the client's "
            "toLowerCase() can disagree and chosen-password enrolment breaks silently"
        )
        for non_ascii in ("Ünter", "İstanbul", "straße"):
            assert not _LOGIN_RE.match(non_ascii), f"{non_ascii!r} is now a valid login"


class TestYouCannotLockEveryoneOut:
    """FR-12 makes recovery from losing every credential a **redeploy of the identity store**, which
    on a customer cluster discards every user, group and membership. A misclick must not cost that.

    The pattern is borrowed from the Workshop Studio portal's users page, which disables the row
    menu on your own row. Enforced on the server rather than only in the UI, because a guard in the
    client is a guard against accidents and nothing else.
    """

    def test_the_last_enabled_account_cannot_be_disabled(self, admin_api):
        _create(admin_api, "only-one")
        # The bootstrap admin exists too, so remove it from contention first.
        admin_api.put("/api/identity/users/admin/disabled", json={"disabled": True})

        resp = admin_api.put("/api/identity/users/only-one/disabled", json={"disabled": True})
        assert resp.status_code == 409
        assert "last account" in resp.get_json()["error"]

    def test_the_last_enabled_account_cannot_be_deleted(self, admin_api):
        _create(admin_api, "only-two")
        admin_api.put("/api/identity/users/admin/disabled", json={"disabled": True})

        resp = admin_api.delete("/api/identity/users/only-two")
        assert resp.status_code == 409

    def test_re_enabling_is_never_refused(self, admin_api):
        """Only the disabling direction can lock anyone out."""
        _create(admin_api, "only-three")
        admin_api.put("/api/identity/users/admin/disabled", json={"disabled": True})
        assert admin_api.put(
            "/api/identity/users/admin/disabled", json={"disabled": False}
        ).status_code == 200

    def test_a_second_account_makes_the_operation_safe_again(self, admin_api):
        """The guard is about the outcome, not about forbidding the operation outright."""
        _create(admin_api, "keeper")
        _create(admin_api, "spare")
        assert admin_api.delete("/api/identity/users/spare").status_code == 200

    def test_you_cannot_disable_yourself(self, admin_api, monkeypatch):
        """Checked against the *authenticated* caller, so it holds however many admins exist."""
        from flask import g as flask_g

        _create(admin_api, "self-target")
        _create(admin_api, "someone-else")

        import api.identity_admin as mod

        monkeypatch.setattr(mod, "_caller_login", lambda: "self-target")
        assert flask_g is not None  # the real path reads g; this pins the decision, not the plumbing

        resp = admin_api.put("/api/identity/users/self-target/disabled", json={"disabled": True})
        assert resp.status_code == 409
        assert "your own account" in resp.get_json()["error"]

    def test_you_cannot_delete_yourself(self, admin_api, monkeypatch):
        _create(admin_api, "self-delete")
        _create(admin_api, "bystander")

        import api.identity_admin as mod

        monkeypatch.setattr(mod, "_caller_login", lambda: "self-delete")

        resp = admin_api.delete("/api/identity/users/self-delete")
        assert resp.status_code == 409
        assert "your own account" in resp.get_json()["error"]

    def test_another_administrator_can_still_disable_you(self, admin_api, monkeypatch):
        """The guard is about self-action, not about making accounts undeletable."""
        _create(admin_api, "target")
        _create(admin_api, "other-admin")

        import api.identity_admin as mod

        monkeypatch.setattr(mod, "_caller_login", lambda: "other-admin")

        assert admin_api.put(
            "/api/identity/users/target/disabled", json={"disabled": True}
        ).status_code == 200


class TestNFR8EveryMutatingRouteIsSeparatelyNamed:
    def test_each_action_is_distinct(self):
        """A shared `identity:write` would make "may add members but may not create users"
        unsayable in whatever policy Phase 3 writes."""
        import audit.actions as A
        from app import app

        registry = A.build_registry(app)
        actions = [
            e.action for e in registry.entries if e.endpoint.startswith("identity_admin.")
        ]
        assert len(actions) == len(set(actions)), f"duplicated action names: {actions}"
        assert len(actions) >= 8, f"expected one per mutating route, found {actions}"

    def test_the_privilege_escalation_pair_is_named_for_policy(self):
        """Membership editing confers grants, so it must be addressable on its own."""
        import audit.actions as A
        from app import app

        registry = A.build_registry(app)
        names = {e.action for e in registry.entries}
        assert "identity.group:add_member" in names
        assert "identity.group:remove_member" in names
        assert "identity.user:create" in names


class TestMissingThingsAreNotFound:
    @pytest.mark.parametrize("path,method", [
        ("/api/identity/users/ghost", "get"),
        ("/api/identity/users/ghost", "delete"),
        ("/api/identity/groups/ghost", "delete"),
    ])
    def test_absent_records_are_404_not_500(self, admin_api, path, method):
        assert getattr(admin_api, method)(path).status_code == 404

    def test_creating_without_a_login_is_refused(self, admin_api):
        assert admin_api.post("/api/identity/users", json={}).status_code == 400

    def test_a_reserved_login_is_refused(self, admin_api):
        """`anonymous` is what an unauthenticated request records; a real user holding it would
        make the audit trail ambiguous."""
        assert _create(admin_api, "anonymous").status_code == 400
