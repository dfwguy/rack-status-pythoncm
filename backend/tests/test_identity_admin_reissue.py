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
"""The bootstrap credential is issued when there is no `admin`, and otherwise only on an explicit
reset. A plain restart never touches an existing admin's password.

**Why this is the contract.** An earlier arrangement re-issued the password on *every* start while
the admin was still "unclaimed" (`must_change_password=True`). It was meant to make a missed
credential recoverable, but it made every restart of a not-yet-claimed deployment a lockout: the
password an operator copied from the startup banner stopped working the next time the container
cycled, with nothing to say why. Confirmed on the Nscale NC deployment — four boots, four passwords,
each invalidating the last.

**The corrected rule.** Mint only when no `admin` account exists (a fresh install, or a store with
other accounts but no administrator). Once an `admin` exists its password is left exactly as it is,
claimed or not, until the holder changes it — or until an operator *explicitly* asks for a reset via
``cli.py reset-admin-password`` (``force=True`` here). Recoverability is a deliberate act, not a
side effect of restarting; the restart path cannot set `force`, so cycling the process grants nobody
access it did not already have.

**The decision is read from the store, never the environment.** "Does an `admin` exist?" is a
durable fact the store answers, which is what lets the several entry points that call `ensure_admin`
in one real start converge without any boot-scoped marker — see `test_identity_bootstrap.py` and
`test_identity_startup_wiring.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import bootstrap, srp
from identity.models import Group, User
from identity.store import IdentityStore


@pytest.fixture
def store(tmp_path):
    """A private, empty store. Nothing here touches the deployment's own identity file."""
    handle = IdentityStore(tmp_path / "identity.json")
    try:
        yield handle
    finally:
        handle.close()


def _verifies(store_handle: IdentityStore, password: str) -> bool:
    """Whether `password` is the admin's current credential, checked through the real SRP enrol.

    Recomputing the verifier from the stored salt is what an authentication actually turns on, so
    this asserts the credential rather than a flag beside it.
    """
    user = store_handle.get_user(bootstrap.ADMIN_LOGIN)
    assert user is not None
    # `verifier_for_salt`, not `enroll`: enrol generates a fresh salt, so it can never reproduce a
    # stored verifier. This recomputes against the salt on the record, which is what an SRP
    # exchange does.
    # `.hex()` because the function returns bytes and the store keeps hex — comparing the two
    # directly is always False, which would make every assertion here pass or fail for the wrong
    # reason.
    recomputed = srp.verifier_for_salt(
        bootstrap.ADMIN_LOGIN, password, bytes.fromhex(user.srp_salt)
    ).hex()
    return recomputed == user.srp_verifier


class TestTheAdminIsCreatedWhenAbsent:
    def test_the_first_boot_creates_the_admin(self, store):
        password = bootstrap.ensure_admin(store, emit=False)
        assert password
        assert _verifies(store, password)

    def test_a_store_with_other_users_but_no_admin_still_gets_one(self, store):
        """The condition is the *admin account's* absence, not the store's emptiness: a store with
        operators but no administrator still has no way in and still gets one."""
        salt, verifier = srp.enroll("operator", "whatever")
        store.create_user(
            User(login="operator", display_name="Operator", srp_salt=salt, srp_verifier=verifier)
        )

        password = bootstrap.ensure_admin(store, emit=False)
        assert password and _verifies(store, password)
        assert store.get_user(bootstrap.ADMIN_LOGIN) is not None
        assert len(store.list_users()) == 2

    def test_the_created_credential_must_be_changed(self, store):
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None and user.must_change_password


class TestARestartLeavesTheAdminAlone:
    """The regression this file exists for: a second call with the admin already present is a no-op,
    so the password an operator copied survives every restart until they change it."""

    def test_a_restart_issues_nothing(self, store):
        bootstrap.ensure_admin(store, emit=False)
        second = bootstrap.ensure_admin(store, emit=False)

        assert second is None, (
            "a restart re-issued the credential, so the password the operator copied from the "
            "startup banner was silently invalidated — the Nscale NC lockout"
        )

    def test_the_first_password_still_works_after_a_restart(self, store):
        first = bootstrap.ensure_admin(store, emit=False)
        bootstrap.ensure_admin(store, emit=False)

        assert _verifies(store, first), "the one-time password stopped working on a plain restart"

    def test_many_restarts_keep_one_account_and_one_credential(self, store):
        first = bootstrap.ensure_admin(store, emit=False)
        for _ in range(4):
            assert bootstrap.ensure_admin(store, emit=False) is None
        assert len(store.list_users()) == 1
        assert _verifies(store, first)

    def test_an_unclaimed_admin_is_not_re_issued(self, store):
        """Still `must_change_password=True`, yet untouched — "unclaimed" is no longer a reason to
        re-mint. Only the account's absence, or an explicit force, issues a password."""
        first = bootstrap.ensure_admin(store, emit=False)
        admin = store.get_user(bootstrap.ADMIN_LOGIN)
        assert admin is not None and admin.must_change_password

        assert bootstrap.ensure_admin(store, emit=False) is None
        assert _verifies(store, first)

    def test_a_claimed_password_is_not_replaced(self, store):
        """The operator has signed in and chosen their own credential; a restart must preserve it."""
        bootstrap.ensure_admin(store, emit=False)
        chosen = "the-operators-own-password"
        admin = store.get_user(bootstrap.ADMIN_LOGIN)
        assert admin is not None
        salt, verifier = srp.enroll(bootstrap.ADMIN_LOGIN, chosen)
        store.update_user(
            admin.edited(srp_salt=salt, srp_verifier=verifier, must_change_password=False)
        )

        assert bootstrap.ensure_admin(store, emit=False) is None
        assert _verifies(store, chosen), "a claimed administrator's password was replaced"


class TestAForcedResetReIssues:
    """`force=True` is the deliberate recovery path — `cli.py reset-admin-password`. It re-issues
    regardless of claimed state, which is the only thing that should ever replace a live credential.
    """

    def test_force_issues_a_different_password(self, store):
        first = bootstrap.ensure_admin(store, emit=False)
        forced = bootstrap.ensure_admin(store, emit=False, force=True)

        assert forced and forced != first
        assert _verifies(store, forced)

    def test_the_previous_password_stops_working(self, store):
        first = bootstrap.ensure_admin(store, emit=False)
        bootstrap.ensure_admin(store, emit=False, force=True)

        assert not _verifies(store, first)

    def test_a_forced_reset_is_still_a_bootstrap_credential(self, store):
        """`must_change_password` must come back on, or a forced reset would hand out a permanent
        password and FR-10a would be defeated by resetting."""
        bootstrap.ensure_admin(store, emit=False)
        admin = store.get_user(bootstrap.ADMIN_LOGIN)
        assert admin is not None
        store.update_user(admin.edited(must_change_password=False))

        bootstrap.ensure_admin(store, emit=False, force=True)
        after = store.get_user(bootstrap.ADMIN_LOGIN)
        assert after is not None and after.must_change_password

    def test_force_recovers_a_claimed_but_lost_credential(self, store):
        """The real reason force exists: the operator changed the password and then lost it. A
        restart will not help them (it leaves the claimed credential in place); an explicit reset
        does."""
        bootstrap.ensure_admin(store, emit=False)
        admin = store.get_user(bootstrap.ADMIN_LOGIN)
        assert admin is not None
        salt, verifier = srp.enroll(bootstrap.ADMIN_LOGIN, "lost-forever")
        store.update_user(
            admin.edited(srp_salt=salt, srp_verifier=verifier, must_change_password=False)
        )

        forced = bootstrap.ensure_admin(store, emit=False, force=True)
        assert forced and _verifies(store, forced)
        assert not _verifies(store, "lost-forever")

    def test_force_edits_rather_than_duplicates(self, store):
        """A forced reset edits the record; a bug here would leave two accounts and an ambiguous
        login."""
        bootstrap.ensure_admin(store, emit=False)
        for _ in range(3):
            bootstrap.ensure_admin(store, emit=False, force=True)
        assert len(store.list_users()) == 1

    def test_force_preserves_the_rest_of_the_record(self, store):
        """`edited` rather than a replacement, so nothing else on the record is silently reset."""
        bootstrap.ensure_admin(store, emit=False)
        before = store.get_user(bootstrap.ADMIN_LOGIN)
        assert before is not None
        store.update_user(before.edited(email="admin@example.com"))

        bootstrap.ensure_admin(store, emit=False, force=True)
        after = store.get_user(bootstrap.ADMIN_LOGIN)
        assert after is not None
        assert after.display_name == "Administrator"
        assert after.email == "admin@example.com"

    def test_force_creates_the_admin_when_absent(self, store):
        """Force on a store with no admin is still a create, not an error — the recovery command
        must work even if the account was removed."""
        salt, verifier = srp.enroll("operator", "whatever")
        store.create_user(
            User(login="operator", display_name="Operator", srp_salt=salt, srp_verifier=verifier)
        )
        password = bootstrap.ensure_admin(store, emit=False, force=True)
        assert password and _verifies(store, password)


class TestASecondAccountDoesNotFreezeAMissingAdmin:
    def test_a_second_account_does_not_stop_a_missing_admin_being_created(self, store):
        """Earlier, any second account marked the deployment "claimed" and froze the bootstrap. The
        rule now keys on the admin account alone: other accounts are irrelevant to whether an
        administrator gets created."""
        salt, verifier = srp.enroll("operator", "whatever")
        store.create_user(
            User(login="operator", display_name="Operator", srp_salt=salt, srp_verifier=verifier)
        )
        assert bootstrap.ensure_admin(store, emit=False) is not None

    def test_a_group_alone_does_not_count_as_an_admin(self, store):
        """Groups are not credentials. A seeded group must not be mistaken for an administrator."""
        store.create_group(Group(name="ops", description="seeded"))
        assert bootstrap.ensure_admin(store, emit=False) is not None


class TestUnclaimedAdminIsStillAQuery:
    """`unclaimed_admin` no longer drives minting, but it remains a truthful read-only query of
    whether a sole, not-yet-claimed `admin` is present."""

    def test_it_reports_the_admin_then_none_once_claimed(self, store):
        bootstrap.ensure_admin(store, emit=False)
        assert bootstrap.unclaimed_admin(store) is not None

        admin = store.get_user(bootstrap.ADMIN_LOGIN)
        assert admin is not None
        store.update_user(admin.edited(must_change_password=False))
        assert bootstrap.unclaimed_admin(store) is None

    def test_a_differently_named_sole_user_is_not_the_admin(self, store):
        salt, verifier = srp.enroll("someone", "whatever")
        store.create_user(
            User(
                login="someone", display_name="Someone", srp_salt=salt,
                srp_verifier=verifier, must_change_password=True,
            )
        )
        assert bootstrap.unclaimed_admin(store) is None


class TestTheOperatorIsToldWhatHappened:
    def test_the_first_banner_does_not_claim_a_re_issue(self, store, caplog):
        with caplog.at_level("WARNING"):
            bootstrap.ensure_admin(store)
        assert "IDENTITY STORE INITIALISED" in caplog.text
        assert "RE-ISSUED" not in caplog.text

    def test_the_forced_reset_banner_says_the_old_password_is_dead(self, store, caplog):
        """An operator holding a value from an older log line needs to learn it is dead here,
        rather than at the login screen."""
        bootstrap.ensure_admin(store, emit=False)
        with caplog.at_level("WARNING"):
            bootstrap.ensure_admin(store, force=True)

        assert "RE-ISSUED" in caplog.text
        assert "NO LONGER VALID" in caplog.text

    def test_the_password_is_in_the_banner(self, store, caplog):
        """The banner is the only copy; a formatting change that dropped it would be silent."""
        with caplog.at_level("WARNING"):
            password = bootstrap.ensure_admin(store)
        assert password and password in caplog.text
