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
"""A restart re-issues the bootstrap credential while nobody has claimed the deployment.

**Why this exists.** The password is shown once and stored nowhere, so an operator who missed it —
a scrolled-past console, a container started detached, a restart nobody was watching — had a
deployment they could not sign into and no supported way back in. That happened during this work.

**Why it does not contradict AC-19.** AC-19 forbade re-printing because the printed value stayed
*valid*: one credential, accepted indefinitely, recoverable from any log written at any point in the
deployment's life. Re-issuing invalidates every earlier copy, so the standing shared secret is what
disappears — a stale log line becomes worthless rather than a way in.

**The boundary is the whole safety argument, so most of this file tests the refusals.** Once the
password is changed, or any second account exists, somebody holds real access; re-issuing then
would lock out a live operator and hand a back door to anyone who can restart the process.
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


class TestAnUnclaimedDeploymentGetsAFreshPassword:
    def test_the_first_boot_creates_the_admin(self, store):
        password = bootstrap.ensure_admin(store, emit=False)
        assert password
        assert _verifies(store, password)

    def test_a_restart_issues_a_different_password(self, store):
        first = bootstrap.ensure_admin(store, emit=False)
        second = bootstrap.ensure_admin(store, emit=False)

        assert second and second != first, (
            "a restart issued nothing, so an operator who missed the first password is locked out"
        )
        assert _verifies(store, second)

    def test_the_previous_password_stops_working(self, store):
        """The property that answers AC-19: an older log line is worthless, not a way in."""
        first = bootstrap.ensure_admin(store, emit=False)
        bootstrap.ensure_admin(store, emit=False)

        assert not _verifies(store, first)

    def test_it_stays_a_bootstrap_credential(self, store):
        """`must_change_password` must survive the re-issue, or a re-issued password becomes a
        permanent one and FR-10a is defeated by restarting."""
        bootstrap.ensure_admin(store, emit=False)
        bootstrap.ensure_admin(store, emit=False)

        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None and user.must_change_password

    def test_no_second_administrator_is_created(self, store):
        """A re-issue edits the record; a bug here would leave two accounts and an ambiguous login."""
        for _ in range(3):
            bootstrap.ensure_admin(store, emit=False)
        assert len(store.list_users()) == 1

    def test_the_display_name_and_record_survive(self, store):
        """`edited` rather than a replacement, so nothing else on the record is silently reset."""
        bootstrap.ensure_admin(store, emit=False)
        before = store.get_user(bootstrap.ADMIN_LOGIN)
        assert before is not None
        store.update_user(before.edited(email="admin@example.com"))

        bootstrap.ensure_admin(store, emit=False)
        after = store.get_user(bootstrap.ADMIN_LOGIN)
        assert after is not None
        assert after.display_name == "Administrator"
        assert after.email == "admin@example.com"


class TestAClaimedDeploymentIsNeverTouched:
    """The refusals. Each of these re-issuing would be a lockout, a back door, or both."""

    def test_a_changed_password_is_not_replaced(self, store):
        """The central case: the operator has signed in and chosen their own credential."""
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

    def test_a_second_account_makes_the_deployment_claimed(self, store):
        """Even with the admin still unclaimed. Somebody administering this deployment created that
        account, so it is provisioned — and re-issuing would hand its admin credential to anyone
        who can restart the process."""
        bootstrap.ensure_admin(store, emit=False)
        salt, verifier = srp.enroll("operator", "whatever")
        store.create_user(
            User(login="operator", display_name="Operator", srp_salt=salt, srp_verifier=verifier)
        )

        assert bootstrap.ensure_admin(store, emit=False) is None

    def test_a_group_alone_does_not_count_as_claimed(self, store):
        """Groups are not credentials. A seeded group must not freeze a recoverable deployment."""
        bootstrap.ensure_admin(store, emit=False)
        store.create_group(Group(name="ops", description="seeded"))

        assert bootstrap.ensure_admin(store, emit=False) is not None

    def test_unclaimed_admin_reports_none_once_claimed(self, store):
        bootstrap.ensure_admin(store, emit=False)
        assert bootstrap.unclaimed_admin(store) is not None

        admin = store.get_user(bootstrap.ADMIN_LOGIN)
        assert admin is not None
        store.update_user(admin.edited(must_change_password=False))
        assert bootstrap.unclaimed_admin(store) is None

    def test_a_differently_named_sole_user_is_not_the_admin(self, store):
        """A single unclaimed account that is not `admin` must not be adopted as one."""
        salt, verifier = srp.enroll("someone", "whatever")
        store.create_user(
            User(
                login="someone", display_name="Someone", srp_salt=salt,
                srp_verifier=verifier, must_change_password=True,
            )
        )
        assert bootstrap.unclaimed_admin(store) is None
        assert bootstrap.ensure_admin(store, emit=False) is None


class TestTheOperatorIsToldWhatHappened:
    def test_the_first_banner_does_not_claim_a_re_issue(self, store, caplog):
        with caplog.at_level("WARNING"):
            bootstrap.ensure_admin(store)
        assert "IDENTITY STORE INITIALISED" in caplog.text
        assert "RE-ISSUED" not in caplog.text

    def test_the_re_issue_banner_says_the_old_password_is_dead(self, store, caplog):
        """An operator holding a value from an older log line needs to learn it is dead here,
        rather than at the login screen."""
        bootstrap.ensure_admin(store, emit=False)
        with caplog.at_level("WARNING"):
            bootstrap.ensure_admin(store)

        assert "RE-ISSUED" in caplog.text
        assert "NO LONGER VALID" in caplog.text

    def test_the_password_is_in_the_banner(self, store, caplog):
        """The banner is the only copy; a formatting change that dropped it would be silent."""
        with caplog.at_level("WARNING"):
            password = bootstrap.ensure_admin(store)
        assert password and password in caplog.text
