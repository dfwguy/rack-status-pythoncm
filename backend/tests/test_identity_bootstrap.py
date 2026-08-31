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
"""First-boot administrator bootstrap — FR-10, FR-10a, FR-10b, FR-3, FR-11.

Covers AC-18 (one admin, password emitted once), AC-19 (a second boot re-emits nothing and leaves
the existing admin alone), AC-20 (the credential is flagged for change and the flag clears), and
AC-28c (a deployment upgrading from shared-token auth has no identity store, so the same start that
removes the legacy path must mint a credential — otherwise the upgrade is a lockout).

Two properties get more attention than their line count suggests, because both fail silently:

**Idempotency.** A credential re-printed on every restart is a shared password sitting in the
deployment's logs. The failure is invisible — the product works perfectly — so it is asserted
directly rather than inferred from "the user already exists".

**Non-recoverability.** FR-3 stores only the SRP verifier, so nothing can print the password again.
That is a deliberate property, not a limitation, and it is what makes "we lost the admin password" a
redeploy rather than a support backdoor. The test therefore asserts the password is absent from the
persisted bytes rather than merely absent from the `User` object.
"""

from __future__ import annotations

import json
import logging
import re
import string

import pytest
from identity import bootstrap, srp
from identity.models import canonical_login
from identity.store import IdentityStore


@pytest.fixture
def store(tmp_path):
    """A store this test owns. Never the real one — bootstrap writes a real credential."""
    s = IdentityStore(str(tmp_path / "identity.json"))
    yield s
    s.close()


class TestTheStoreStartsEmpty:
    def test_a_fresh_store_needs_bootstrapping(self, store):
        assert bootstrap.needed(store) is True

    def test_a_store_holding_a_user_does_not(self, store):
        bootstrap.ensure_admin(store, emit=False)
        assert bootstrap.needed(store) is False


class TestAC18FirstBootCreatesExactlyOneAdmin:
    def test_one_user_exists_and_it_is_admin(self, store):
        password = bootstrap.ensure_admin(store, emit=False)

        assert password, "the caller must receive the generated credential"
        users = store.list_users()
        assert len(users) == 1, f"expected exactly one user, got {[u.login for u in users]}"
        assert users[0].login == bootstrap.ADMIN_LOGIN

    def test_the_login_is_canonical(self, store):
        bootstrap.ensure_admin(store, emit=False)
        assert store.list_users()[0].login == canonical_login(bootstrap.ADMIN_LOGIN)

    def test_the_credential_verifies_against_srp(self, store):
        """The bootstrap is worthless if the password it prints does not actually authenticate.

        This is the end-to-end property — enrolment and verification agreeing — rather than
        "a salt and a verifier are non-empty", which a wrong-but-consistent implementation passes.
        """
        password = bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None

        # Re-deriving the verifier from the stored salt and the emitted password must reproduce
        # exactly what was stored. `verifier_for_salt` works in bytes; the store keeps hex, which
        # `enroll` chose so the file stays readable while debugging.
        recomputed = srp.verifier_for_salt(
            bootstrap.ADMIN_LOGIN, password, bytes.fromhex(user.srp_salt)
        )
        assert recomputed.hex() == user.srp_verifier

    def test_a_wrong_password_does_not_reproduce_the_verifier(self, store):
        """Guard the guard: the check above must be able to fail."""
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None
        wrong = srp.verifier_for_salt(
            bootstrap.ADMIN_LOGIN, "not-the-password", bytes.fromhex(user.srp_salt)
        )
        assert wrong.hex() != user.srp_verifier


class TestFR3ThePasswordIsNotRecoverable:
    def test_it_is_absent_from_the_persisted_store(self, store, tmp_path):
        """Asserted against the bytes on disk, not the object.

        A `User` with no password attribute proves nothing about what was written — the interesting
        failure is a field that reaches the file, and only reading the file can see it.
        """
        password = bootstrap.ensure_admin(store, emit=False)

        raw = (tmp_path / "identity.json").read_text(encoding="utf-8")
        assert password not in raw, "the generated password reached the identity file"

        # And the structured form, in case an encoder escapes it differently than a substring scan
        # would match.
        assert password not in json.dumps(store.raw())

    def test_only_salt_and_verifier_are_kept(self, store):
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None
        assert user.srp_salt and user.srp_verifier
        assert not hasattr(user, "password")

    def test_the_public_view_never_carries_the_verifier(self, store):
        """`public_dict` is what an API response would serialise; the verifier is a credential."""
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None
        public = user.public_dict()
        assert "srp_verifier" not in public
        assert "srp_salt" not in public


class TestAC19ASecondBootChangesNothing:
    def test_it_returns_none_and_does_not_re_emit(self, store, caplog):
        first = bootstrap.ensure_admin(store, emit=False)
        assert first

        with caplog.at_level(logging.WARNING, logger="identity.bootstrap"):
            second = bootstrap.ensure_admin(store)  # emit=True on purpose

        assert second is None, "a second bootstrap must not mint a second credential"
        assert not caplog.records, (
            "a credential was emitted on a restart. Re-printing it on every start turns a one-time "
            "password into a shared one sitting in the deployment's logs."
        )

    def test_the_existing_admin_is_untouched(self, store):
        bootstrap.ensure_admin(store, emit=False)
        before = store.get_user(bootstrap.ADMIN_LOGIN)
        assert before is not None

        bootstrap.ensure_admin(store, emit=False)

        after = store.get_user(bootstrap.ADMIN_LOGIN)
        assert after is not None
        assert after.srp_verifier == before.srp_verifier, "the stored credential was replaced"
        assert after.srp_salt == before.srp_salt
        assert after.created_at == before.created_at

    def test_it_does_not_reactivate_a_deliberately_changed_credential(self, store):
        """The realistic restart: the operator has already logged in and changed the password.

        If the bootstrap fired again it would overwrite a real credential with a printed one, which
        is a privilege-escalation path for anyone who can read the logs and restart the container.
        """
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None
        salt, verifier = srp.enroll(bootstrap.ADMIN_LOGIN, "an-operator-chosen-password")
        store.update_user(user.edited(srp_salt=salt, srp_verifier=verifier,
                                      must_change_password=False))

        assert bootstrap.ensure_admin(store, emit=False) is None

        after = store.get_user(bootstrap.ADMIN_LOGIN)
        assert after is not None
        assert after.srp_verifier == verifier, "a restart overwrote the operator's own password"
        assert after.must_change_password is False


class TestAC20TheCredentialIsFlaggedForChange:
    def test_the_bootstrap_user_must_change_it(self, store):
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None
        assert user.must_change_password is True, (
            "FR-10a: a bootstrap credential that lives forever is a shared password"
        )

    def test_the_flag_clears_when_the_password_is_changed(self, store):
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(bootstrap.ADMIN_LOGIN)
        assert user is not None

        salt, verifier = srp.enroll(bootstrap.ADMIN_LOGIN, "a-new-password")
        store.update_user(
            user.edited(srp_salt=salt, srp_verifier=verifier, must_change_password=False)
        )

        assert store.get_user(bootstrap.ADMIN_LOGIN).must_change_password is False


class TestAC28cTheUpgradeFromSharedTokenIsNotALockout:
    """FR-10b: the condition is an empty store, whatever the history behind it.

    A deployment upgrading from shared-token auth has no identity file at all. If the bootstrap
    only fired on a pristine install, that upgrade would remove the working credential and mint no
    replacement — the failure mode this requirement exists to prevent.
    """

    def test_a_store_file_that_exists_but_holds_no_users_still_bootstraps(self, tmp_path):
        path = tmp_path / "identity.json"
        first = IdentityStore(str(path))
        first.close()
        assert path.is_file(), "the store file exists but has no users — the upgrade case"

        store = IdentityStore(str(path))
        try:
            assert bootstrap.needed(store) is True
            assert bootstrap.ensure_admin(store, emit=False)
            assert len(store.list_users()) == 1
        finally:
            store.close()

    def test_groups_without_users_do_not_count_as_bootstrapped(self, store):
        """A partly-completed earlier run can leave groups behind. Nobody can log in with a group."""
        from identity.models import Group

        store.create_group(Group(name="operators", description="left over"))
        assert bootstrap.needed(store) is True


class TestThePasswordItself:
    def test_it_is_the_configured_length(self):
        assert len(bootstrap.generate_password()) == bootstrap.PASSWORD_LENGTH

    def test_it_is_alphanumeric_only(self):
        """No punctuation, deliberately.

        This string is copied out of a startup log, through a terminal, possibly a chat window, and
        into a browser. Shell metacharacters and quotes in that path produce mangled-paste tickets
        that are indistinguishable from a wrong password. The entropy is bought with length.
        """
        allowed = set(string.ascii_letters + string.digits)
        assert set(bootstrap.generate_password()) <= allowed

    def test_it_carries_enough_entropy(self):
        """~119 bits at the default length. Asserted as a property, not a magic number."""
        import math

        bits = bootstrap.PASSWORD_LENGTH * math.log2(len(string.ascii_letters + string.digits))
        assert bits >= 100, f"only {bits:.0f} bits of entropy in the bootstrap credential"

    def test_successive_passwords_differ(self):
        generated = {bootstrap.generate_password() for _ in range(50)}
        assert len(generated) == 50, "the generator repeated itself"

    def test_it_uses_the_system_csprng_not_the_random_module(self):
        """`secrets`, never `random`.

        `random` is a Mersenne Twister seeded from the clock; observing a few outputs recovers its
        state, which for a credential generator is the whole ballgame. Asserted structurally
        because the outputs of the two are indistinguishable by inspection — a statistical test
        would not catch it, and that is exactly why this is worth pinning.
        """
        source = (
            __import__("pathlib").Path(bootstrap.__file__).read_text(encoding="utf-8")
        )
        assert "import secrets" in source
        assert not re.search(r"^\s*import random\b", source, re.MULTILINE), (
            "bootstrap.py imports `random`; a credential must come from the system CSPRNG"
        )
        assert "secrets.choice" in source


class TestTheEmission:
    def test_the_password_is_written_once_at_warning_level(self, store, caplog):
        """WARNING, not INFO, and the reason is operational.

        A deployment running at WARNING would drop the only copy of the only credential at INFO,
        and the operator would find out at the login screen with nothing to type. It is also
        genuinely warning-shaped: there is an unchanged bootstrap password on the system.
        """
        with caplog.at_level(logging.WARNING, logger="identity.bootstrap"):
            password = bootstrap.ensure_admin(store)

        emitted = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(emitted) == 1, f"expected exactly one emission, got {len(emitted)}"
        message = emitted[0].getMessage()
        assert password in message
        assert bootstrap.ADMIN_LOGIN in message

    def test_the_message_says_it_cannot_be_shown_again(self, store, caplog):
        """The operator has one chance to copy it; the message has to say so."""
        with caplog.at_level(logging.WARNING, logger="identity.bootstrap"):
            bootstrap.ensure_admin(store)
        message = caplog.records[0].getMessage().lower()
        assert "cannot be shown again" in message or "not stored" in message

    def test_emit_false_stays_silent_but_still_creates_the_user(self, store, caplog):
        with caplog.at_level(logging.DEBUG, logger="identity.bootstrap"):
            password = bootstrap.ensure_admin(store, emit=False)
        assert password
        assert not caplog.records
        assert len(store.list_users()) == 1
