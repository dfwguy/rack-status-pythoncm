# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Identity records: the login is the identity, and it cannot move (FR-2, FR-2a, FR-2c, FR-15a).

Author: Rafael M. Koike & Cursor Agent ("Ada")

Written as refusals. The login carries identity, so what matters is what the types will NOT let you
do -- rename it, collide on case, smuggle a separator through it, or read a credential out of an API
response. A test that only asserts the happy path would pass against a record that allowed all four.
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import principals
from identity.models import (
    AUTH_FEDERATED,
    MAX_LOGIN,
    Group,
    IdentityError,
    Membership,
    User,
    canonical_login,
)


class TestTheLoginIsTheIdentity:
    def test_the_login_cannot_be_renamed(self):
        """FR-2. `edited` takes no login, so this is a type error rather than a runtime check --
        a caller cannot express the rename and be told later."""
        user = User(login="rkoike")
        with pytest.raises(TypeError):
            user.edited(login="someone-else")  # type: ignore[call-arg]

    def test_the_record_is_frozen(self):
        user = User(login="rkoike")
        with pytest.raises(FrozenInstanceError):
            user.login = "someone-else"  # type: ignore[misc]

    def test_editing_a_display_name_does_not_touch_the_login(self):
        """AC-2d: the display name is not load-bearing."""
        user = User(login="rkoike", display_name="Rafael")
        moved = user.edited(display_name="Rafael M. Koike")
        assert moved.login == user.login == "rkoike"
        assert moved.display_name == "Rafael M. Koike"
        assert moved.principal == user.principal

    def test_the_principal_is_the_login(self):
        assert User(login="rkoike").principal == "user:rkoike"


class TestCanonicalisation:
    @pytest.mark.parametrize("raw", ["Admin", "ADMIN", "admin", "  admin  "])
    def test_case_and_whitespace_fold_to_one_form(self, raw):
        """AC-2c. Two logins differing only in case must not be two principals for one subject,
        or a policy matches one and misses the other."""
        assert canonical_login(raw) == "admin"
        assert User(login=raw).principal == "user:admin"

    def test_an_email_shaped_login_is_accepted(self):
        """Federation will supply these (Entra), so refusing them would break FR-7a."""
        assert canonical_login("Rafael.Koike@NVIDIA.com") == "rafael.koike@nvidia.com"

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_an_empty_login_is_refused(self, bad):
        with pytest.raises(IdentityError):
            canonical_login(bad)

    def test_a_login_may_not_contain_the_grammar_separator(self):
        """The one character that would break the principal grammar: `user:a:b` is ambiguous."""
        with pytest.raises(IdentityError, match="separates the principal grammar"):
            canonical_login("a:b")

    @pytest.mark.parametrize(
        "bad",
        ["has space", "tab\there", "new\nline", "-leading", "trailing-", ".dot", "sla/sh", "semi;colon"],
    )
    def test_structurally_invalid_logins_are_refused(self, bad):
        with pytest.raises(IdentityError):
            canonical_login(bad)

    def test_an_over_long_login_is_refused(self):
        """A bound, not a security boundary: the login is in every audit event that names it."""
        assert canonical_login("a" * MAX_LOGIN) == "a" * MAX_LOGIN
        with pytest.raises(IdentityError):
            canonical_login("a" * (MAX_LOGIN + 1))

    @pytest.mark.parametrize("reserved", ["anonymous", "ANONYMOUS", "system", "none", "null"])
    def test_reserved_logins_are_refused(self, reserved):
        """`user:anonymous` is distinct from bare `anonymous` in the grammar, but not to a human
        reading the trail, which is the audience that matters."""
        with pytest.raises(IdentityError, match="reserved"):
            canonical_login(reserved)


class TestCredentialsDoNotLeak:
    def test_public_dict_omits_the_verifier_and_salt(self):
        """NFR-4. The API shape is where this leaks if it leaks at all."""
        user = User(login="rkoike", srp_salt="aabb", srp_verifier="ccdd")
        pub = user.public_dict()
        assert "srp_salt" not in pub
        assert "srp_verifier" not in pub
        assert "aabb" not in str(pub) and "ccdd" not in str(pub)
        # but the caller can still tell whether a credential exists
        assert pub["has_credential"] is True
        assert pub["principal"] == "user:rkoike"

    def test_has_credential_is_false_until_both_parts_exist(self):
        assert User(login="a").has_credential is False
        assert User(login="a", srp_salt="aa").has_credential is False
        assert User(login="a", srp_salt="aa", srp_verifier="bb").has_credential is True

    def test_a_federated_user_needs_no_local_credential(self):
        """FR-7a: one principal model for both, so this must be a legal record."""
        user = User(login="rkoike@nvidia.com", auth_source=AUTH_FEDERATED)
        assert user.has_credential is False
        assert user.principal == "user:rkoike@nvidia.com"

    def test_an_unknown_auth_source_is_refused(self):
        with pytest.raises(IdentityError):
            User(login="a", auth_source="carrier-pigeon")


class TestRoundTrip:
    def test_a_user_survives_to_dict_and_back(self):
        user = User(login="rkoike", display_name="R", srp_salt="aa", srp_verifier="bb",
                    must_change_password=True)
        assert User.from_dict(user.to_dict()) == user

    def test_unknown_persisted_keys_are_ignored_rather_than_fatal(self):
        """A store written by a newer version must not crash an older one on read."""
        row = User(login="rkoike").to_dict() | {"a_field_from_the_future": 1}
        assert User.from_dict(row).login == "rkoike"

    def test_a_group_and_membership_survive_a_round_trip(self):
        group = Group(name="operators", description="on-call")
        assert Group.from_dict(group.to_dict()) == group
        edge = Membership(group="Operators", login="RKoike")
        assert Membership.from_dict(edge.to_dict()) == edge
        # canonicalised on the way in, so the edge matches the records it refers to
        assert edge.key == ("operators", "rkoike")


class TestGroupsAreFlat:
    def test_a_group_cannot_be_renamed(self):
        """FR-2b applies to groups too: a recycled name silently re-grants what the old one had."""
        with pytest.raises(TypeError):
            Group(name="operators").edited(name="others")  # type: ignore[call-arg]

    def test_a_group_principal_is_distinct_from_a_user_principal(self):
        assert Group(name="admin").principal == "group:admin"
        assert User(login="admin").principal != Group(name="admin").principal


class TestThePrincipalGrammar:
    """FR-15a: one module formats and parses, so there is one grammar rather than three regexes."""

    @pytest.mark.parametrize(
        ("kind", "ident", "expected"),
        [("user", "rkoike", "user:rkoike"),
         ("group", "operators", "group:operators"),
         ("service", "post-run-refresher", "service:post-run-refresher")],
    )
    def test_format_and_parse_round_trip(self, kind, ident, expected):
        assert principals.format_principal(kind, ident) == expected
        assert principals.parse_principal(expected) == (kind, ident)

    def test_anonymous_has_no_kind_prefix_and_round_trips(self):
        assert principals.parse_principal("anonymous") == ("anonymous", "")
        assert principals.is_anonymous("anonymous") is True
        assert principals.is_anonymous("user:rkoike") is False

    def test_an_unknown_kind_is_refused_both_ways(self):
        with pytest.raises(principals.PrincipalError):
            principals.format_principal("robot", "r2d2")
        with pytest.raises(principals.PrincipalError):
            principals.parse_principal("robot:r2d2")

    @pytest.mark.parametrize("bad", ["", "rkoike", "user:", ":rkoike", "user:a:b"])
    def test_malformed_principals_are_refused(self, bad):
        with pytest.raises(principals.PrincipalError):
            principals.parse_principal(bad)

    def test_formatting_refuses_an_identifier_containing_the_separator(self):
        """The second gate. models.canonical_login is the first; this catches identifiers arriving
        from anywhere else, e.g. a federated claim."""
        with pytest.raises(principals.PrincipalError, match="may not contain"):
            principals.format_principal("user", "a:b")

    def test_formatting_refuses_an_empty_identifier(self):
        with pytest.raises(principals.PrincipalError):
            principals.format_principal("user", "")
