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
"""Contact attributes on a user record — `.plc/briefs/user-contact-attributes-l0.md` R1–R5.

The class names below map one-to-one onto the L0 requirement bullets, so a failure names the
requirement it broke rather than the function it broke.
"""

from __future__ import annotations

import identity.srp as srp_mod
import pytest
from identity import bootstrap
from identity.models import (
    MAX_EMAIL,
    IdentityError,
    User,
    canonical_email,
    canonical_phone_number,
)
from identity.store import IdentityStore


@pytest.fixture()
def store(tmp_path):
    s = IdentityStore(str(tmp_path / "identity.json"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin_api(monkeypatch, tmp_path):
    """The admin API over a private store holding the bootstrap administrator.

    Same shape as ``test_identity_admin_api.py``'s fixture and for the same reason: an empty store
    makes the lockout guards refuse operations the test is not trying to exercise.
    """
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    srp_mod.reset_for_tests()

    s = IdentityStore(str(tmp_path / "identity.json"))
    try:
        bootstrap.ensure_admin(s, emit=False)
    finally:
        s.close()

    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    srp_mod.reset_for_tests()


def _create(client, login: str, **extra):
    body = {"login": login, "email": f"{login}@example.com"}
    body.update(extra)
    return client.post("/api/identity/users", json=body)


class TestR1StoredExposedAndBackwardCompatible:
    def test_the_attributes_survive_a_round_trip(self, store):
        store.create_user(
            User(login="rkoike", email="R.Koike@Nvidia.COM", phone_number="+1 (555) 010-4477")
        )
        back = store.get_user("rkoike")
        assert back is not None
        # Normalised on the way in, so the stored form is the comparable one.
        assert back.email == "r.koike@nvidia.com"
        assert back.phone_number == "+15550104477"

    def test_a_row_written_before_this_field_existed_still_loads(self):
        """R1's no-migration claim. ``from_dict`` filters to declared fields, so an old row simply
        takes the defaults — this is the assertion that keeps an upgrade from needing a migration."""
        legacy = {
            "login": "legacy",
            "display_name": "Legacy",
            "srp_salt": "aa",
            "srp_verifier": "bb",
            "auth_source": "local",
            "disabled": False,
            "must_change_password": False,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "",
        }
        user = User.from_dict(legacy)
        assert user.email == ""
        assert user.phone_number == ""
        assert user.email_verified is False
        assert user.phone_number_verified is False

    def test_the_public_shape_carries_them_and_still_hides_the_credential(self, store):
        store.create_user(
            User(login="ada", email="ada@example.com", srp_salt="aa", srp_verifier="bb")
        )
        row = store.get_user("ada").public_dict()
        assert row["email"] == "ada@example.com"
        assert {"phone_number", "email_verified", "phone_number_verified"} <= set(row)
        assert "srp_salt" not in row and "srp_verifier" not in row

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  Alice@X.COM  ", "alice@x.com"),
            ("", ""),
            (None, ""),
            ("first.last+tag@sub.domain.example", "first.last+tag@sub.domain.example"),
        ],
    )
    def test_email_normalisation(self, raw, expected):
        assert canonical_email(raw) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "no-at-sign",
            "undotted@domain",
            "two@@at.com",
            "has space@example.com",
            "@no-local.com",
            "no-domain@.com",
            "x" * (MAX_EMAIL + 1) + "@example.com",
        ],
    )
    def test_a_malformed_email_is_refused(self, bad):
        with pytest.raises(IdentityError):
            canonical_email(bad)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("+1 (555) 010-4477", "+15550104477"),
            ("555.010.4477", "5550104477"),
            ("", ""),
        ],
    )
    def test_phone_normalisation_strips_what_people_type(self, raw, expected):
        assert canonical_phone_number(raw) == expected

    @pytest.mark.parametrize("bad", ["not-a-number", "+", "12", "+1-555-CALL-NOW"])
    def test_a_malformed_phone_is_refused(self, bad):
        with pytest.raises(IdentityError):
            canonical_phone_number(bad)


class TestR2RequiredAtTheBoundaryPermittedInTheModel:
    def test_the_model_permits_an_empty_email(self):
        """Not a gap. The bootstrap admin and every pre-existing account have none, and a type that
        refused them would make valid records unconstructable."""
        assert User(login="nobody").email == ""

    def test_the_bootstrap_admin_has_no_email_and_is_still_valid(self, tmp_path):
        s = IdentityStore(str(tmp_path / "identity.json"))
        try:
            bootstrap.ensure_admin(s, emit=False)
            admin = next(u for u in s.list_users())
            assert admin.email == ""
        finally:
            s.close()

    def test_creating_a_user_without_an_email_is_refused(self, admin_api):
        got = admin_api.post("/api/identity/users", json={"login": "noemail"})
        assert got.status_code == 400
        assert "email" in got.get_json()["error"].lower()

    def test_creating_a_user_with_a_malformed_email_is_refused(self, admin_api):
        got = admin_api.post(
            "/api/identity/users", json={"login": "bademail", "email": "not-an-address"}
        )
        assert got.status_code == 400

    def test_creating_a_user_with_an_email_succeeds_and_reports_it(self, admin_api):
        got = _create(admin_api, "withemail", phone_number="+15550104477")
        assert got.status_code == 201, got.get_json()
        assert got.get_json()["user"]["email"] == "withemail@example.com"


class TestR3AnEmailIdentifiesAtMostOneUser:
    def test_a_second_user_cannot_claim_the_same_address(self, store):
        store.create_user(User(login="first", email="shared@example.com"))
        with pytest.raises(IdentityError, match="already used by"):
            store.create_user(User(login="second", email="shared@example.com"))

    def test_uniqueness_ignores_case(self, store):
        store.create_user(User(login="first", email="shared@example.com"))
        with pytest.raises(IdentityError, match="already used by"):
            store.create_user(User(login="second", email="SHARED@EXAMPLE.COM"))

    def test_many_users_may_have_no_address(self, store):
        """The bug the obvious implementation ships: every pre-existing account has ``email == ""``,
        so a plain "is anyone using this value" test lets the first through and then refuses every
        other one — reporting a duplicate address to an operator who supplied no address at all."""
        for login in ("a", "b", "c"):
            store.create_user(User(login=login))
        assert len(store.list_users()) == 3

    def test_an_update_cannot_steal_another_users_address(self, store):
        store.create_user(User(login="first", email="first@example.com"))
        store.create_user(User(login="second", email="second@example.com"))
        second = store.get_user("second")
        with pytest.raises(IdentityError, match="already used by"):
            store.update_user(second.edited(email="first@example.com"))

    def test_a_user_may_keep_its_own_address_through_an_unrelated_edit(self, store):
        """The self-collision that a naive check gets wrong: updating a display name re-writes the
        row, and the address it already owns must not read as somebody else's."""
        store.create_user(User(login="first", email="first@example.com"))
        user = store.get_user("first")
        updated = store.update_user(user.edited(display_name="First Person"))
        assert updated.email == "first@example.com"

    def test_the_api_reports_a_duplicate_rather_than_writing_one(self, admin_api):
        """409 rather than 400: it is a conflict with another record, not a malformed request."""
        assert _create(admin_api, "one", email="dup@example.com").status_code == 201
        got = _create(admin_api, "two", email="dup@example.com")
        assert got.status_code == 409
        assert "already used by" in got.get_json()["error"]

    def test_an_update_that_collides_answers_the_same_status_as_a_create(self, admin_api):
        """One condition, one status. A client should not need two code paths for it."""
        _create(admin_api, "holder", email="taken@example.com")
        _create(admin_api, "mover2")
        got = admin_api.patch("/api/identity/users/mover2", json={"email": "taken@example.com"})
        assert got.status_code == 409, got.get_json()

    def test_a_malformed_address_is_still_a_400_not_a_conflict(self, admin_api):
        _create(admin_api, "mover3")
        got = admin_api.patch("/api/identity/users/mover3", json={"email": "not-an-address"})
        assert got.status_code == 400


class TestR4AVerifiedFlagNeverOutlivesItsAddress:
    def test_changing_the_address_clears_the_flag(self):
        """The account-linking hazard, in one assertion. A relying party that links accounts by
        verified email would otherwise accept a freshly-typed address as proven."""
        user = User(login="a", email="old@example.com", email_verified=True)
        moved = user.edited(email="new@example.com")
        assert moved.email == "new@example.com"
        assert moved.email_verified is False

    def test_rewriting_the_same_address_is_not_a_change(self):
        user = User(login="a", email="same@example.com", email_verified=True)
        assert user.edited(email="SAME@example.com").email_verified is True

    def test_the_same_rule_applies_to_the_phone_number(self):
        user = User(login="a", phone_number="+15550104477", phone_number_verified=True)
        assert user.edited(phone_number="+15550104488").phone_number_verified is False

    def test_verified_cannot_be_true_without_an_address(self):
        assert User(login="a", email="", email_verified=True).email_verified is False

    def test_changing_and_asserting_in_one_call_is_refused(self):
        """Refused rather than resolved, because either resolution would quietly ignore one of the
        two arguments the caller passed."""
        user = User(login="a", email="old@example.com")
        with pytest.raises(IdentityError, match="in one call"):
            user.edited(email="new@example.com", email_verified=True)

    def test_the_admin_assertion_route_sets_the_flag(self, admin_api):
        _create(admin_api, "assertme")
        got = admin_api.put("/api/identity/users/assertme/email-verified", json={"verified": True})
        assert got.status_code == 200, got.get_json()
        assert got.get_json()["user"]["email_verified"] is True

    def test_the_assertion_route_can_withdraw_it(self, admin_api):
        _create(admin_api, "withdrawme")
        admin_api.put("/api/identity/users/withdrawme/email-verified", json={"verified": True})
        got = admin_api.put(
            "/api/identity/users/withdrawme/email-verified", json={"verified": False}
        )
        assert got.get_json()["user"]["email_verified"] is False

    def test_a_profile_edit_cannot_assert_verification(self, admin_api):
        """R4's "own action" half. Folding this into the generic update would make the audit trail
        unable to distinguish asserting an identity claim from renaming somebody."""
        _create(admin_api, "sneaky")
        got = admin_api.patch(
            "/api/identity/users/sneaky", json={"email_verified": True}
        )
        assert got.status_code == 400
        assert "email-verified" in got.get_json()["error"]

    def test_an_address_change_through_the_api_drops_the_assertion(self, admin_api):
        _create(admin_api, "mover")
        admin_api.put("/api/identity/users/mover/email-verified", json={"verified": True})
        got = admin_api.patch("/api/identity/users/mover", json={"email": "moved@example.com"})
        assert got.status_code == 200, got.get_json()
        assert got.get_json()["user"]["email_verified"] is False

    def test_asserting_an_address_that_does_not_exist_is_refused(self, admin_api):
        """The bootstrap admin has no address, so there is nothing to assert about."""
        users = admin_api.get("/api/identity/users").get_json()["users"]
        admin_login = next(u["login"] for u in users if not u["email"])
        got = admin_api.put(
            f"/api/identity/users/{admin_login}/email-verified", json={"verified": True}
        )
        assert got.status_code == 400
        assert "no email" in got.get_json()["error"].lower()


class TestR5ManageableThroughTheApi:
    def test_the_profile_route_edits_both_attributes(self, admin_api):
        _create(admin_api, "editme")
        got = admin_api.patch(
            "/api/identity/users/editme",
            json={"email": "edited@example.com", "phone_number": "+1 555 010 9999"},
        )
        assert got.status_code == 200, got.get_json()
        user = got.get_json()["user"]
        assert user["email"] == "edited@example.com"
        assert user["phone_number"] == "+15550109999"

    def test_the_profile_route_still_edits_a_display_name_alone(self, admin_api):
        _create(admin_api, "renameme")
        got = admin_api.patch("/api/identity/users/renameme", json={"display_name": "Renamed"})
        assert got.status_code == 200
        assert got.get_json()["user"]["display_name"] == "Renamed"

    def test_an_empty_patch_is_still_refused(self, admin_api):
        _create(admin_api, "nochange")
        assert admin_api.patch("/api/identity/users/nochange", json={}).status_code == 400

    def test_the_list_carries_the_attributes_for_the_table(self, admin_api):
        _create(admin_api, "listed")
        users = admin_api.get("/api/identity/users").get_json()["users"]
        listed = next(u for u in users if u["login"] == "listed")
        assert listed["email"] == "listed@example.com"
        assert listed["email_verified"] is False
