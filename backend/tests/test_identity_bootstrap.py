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
    """**AC-19: a second boot changes nothing once an `admin` account exists.**

    This held universally, was amended in 2026-09 to "once CLAIMED" so an unclaimed deployment
    re-minted on every start, and the amendment caused a lockout in the field — a restart silently
    invalidated the password the operator had copied (the Nscale NC deployment, four boots, four
    passwords). The rule is back to its original form: once an administrator is present, no boot
    touches its credential. The one supported way to replace a live credential is an explicit
    ``force`` (``cli.py reset-admin-password``), which a restart cannot trigger —
    `test_identity_admin_reissue.py` holds that full boundary.
    """

    def test_a_second_boot_does_not_re_issue(self, store, caplog):
        first = bootstrap.ensure_admin(store, emit=False)
        assert first

        with caplog.at_level(logging.WARNING, logger="identity.bootstrap"):
            second = bootstrap.ensure_admin(store)  # emit=True on purpose

        assert second is None, "a restart re-issued the credential and invalidated the first password"
        assert not caplog.records, "a no-op boot must not print a banner"
        assert _verifies_admin(store, first), "the first password stopped working on a restart"

    def test_the_claimed_admin_is_untouched(self, store):
        """The property AC-19 exists for, asserted against a *claimed* administrator."""
        bootstrap.ensure_admin(store, emit=False)
        admin = store.get_user(bootstrap.ADMIN_LOGIN)
        assert admin is not None
        store.update_user(admin.edited(must_change_password=False))

        before = store.get_user(bootstrap.ADMIN_LOGIN)
        assert before is not None

        assert bootstrap.ensure_admin(store, emit=False) is None

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


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    """Point the policy store at a file this test owns.

    Never the real `var/store/policies.json` — seeding writes one, and a test that wrote the repo's
    own policy file would flip every other suite from the permissive engine to default-deny.
    """
    target = tmp_path / "store" / "policies.json"
    monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(target))
    return target


class TestABrandNewDeploymentEnforcesFromTheFirstBoot:
    """The administrator policy written alongside the administrator.

    Without it, a fresh install falls back to `AllowAllEngine` and every request is permitted while
    the audit trail says `policy_engine: allow-all`. Honest, but it means the product ships with
    authorization switched off and nothing in the UI saying so.
    """

    def test_it_writes_the_policy_and_attaches_it(self, store, policy_file):
        from authz import store as policy_store

        bootstrap.ensure_admin(store, emit=False)
        assert bootstrap.ensure_admin_policy(store) is True

        assert [d.id for d in policy_store.read_all()] == [bootstrap.ADMIN_POLICY_ID]
        assert store.policies_for("user:admin") == [bootstrap.ADMIN_POLICY_ID]

    def test_the_seeded_policy_actually_grants_the_administrator(self, store, policy_file):
        """The failure this guards is the worse one: seeding a policy that denies the admin turns
        first boot into a lockout, and there is no reset endpoint to recover through."""
        from authz.policy import PolicyEngine
        from authz.resources import RequestFacts, resolve

        bootstrap.ensure_admin(store, emit=False)
        bootstrap.ensure_admin_policy(store)

        engine = PolicyEngine(
            policy_file,
            group_lookup=lambda p: (),
            attachment_lookup=lambda s: store.policies_for(s),
        )
        for action, body in (
            ("rack.power:off", {"rack": "p17-r01"}),
            ("identity.user:create", {"login": "bob"}),
            ("policy:attach", {"policy_id": "administrators"}),
            ("vault:write", {}),
        ):
            resource = resolve(action, RequestFacts(view_args={}, query={}, body=body))
            assert engine.evaluate("user:admin", action, resource).allowed, action

    def test_everyone_else_is_still_denied(self, store, policy_file):
        """Default-deny is the point. A seeded policy that leaked to unattached principals would be
        allow-all with extra steps."""
        from authz.policy import PolicyEngine
        from authz.resources import RequestFacts, resolve

        bootstrap.ensure_admin(store, emit=False)
        bootstrap.ensure_admin_policy(store)
        engine = PolicyEngine(
            policy_file,
            group_lookup=lambda p: (),
            attachment_lookup=lambda s: store.policies_for(s),
        )
        resource = resolve("rack.power:off", RequestFacts(view_args={}, query={}, body={"rack": "p17-r01"}))
        assert not engine.evaluate("user:bob", "rack.power:off", resource).allowed

    def test_it_leaves_an_existing_policy_set_alone(self, store, policy_file):
        """A deployment that has written a policy has made a decision this must not overrule."""
        from authz import store as policy_store

        policy_file.parent.mkdir(parents=True, exist_ok=True)
        policy_file.write_text(json.dumps({"version": 1, "policies": [{
            "id": "site-rules",
            "statements": [{"effect": "deny", "actions": ["*"], "resources": ["*"]}],
        }]}))

        bootstrap.ensure_admin(store, emit=False)
        assert bootstrap.ensure_admin_policy(store) is False
        assert [d.id for d in policy_store.read_all()] == ["site-rules"]

    def test_ensure_admin_alone_writes_no_policy(self, store, policy_file):
        """The isolation property that keeps this out of every test fixture.

        Dozens of fixtures call `ensure_admin` to get a realistic store. If seeding lived inside it,
        all of them would silently acquire a policy file and switch from the permissive engine to
        default-deny as a side effect of creating a user.
        """
        from authz import store as policy_store

        bootstrap.ensure_admin(store, emit=False)
        assert policy_store.read_all() == []
        assert not policy_file.exists()


class TestTheAdminPolicyDocumentStaysValid:
    """Build gates. The document is generated, so it can outgrow the schema without anyone editing
    it — a new resource type is enough."""

    def test_it_covers_every_declared_resource_type(self):
        """A type absent here is an action the administrator cannot perform on a fresh install, and
        the symptom would be a 403 on a brand-new deployment for a feature that just shipped."""
        from authz.resources import declared_types

        actions = bootstrap.admin_policy_document()["statements"][0]["actions"]
        assert {a.split(":")[0] for a in actions} == set(declared_types())

    def test_it_fits_the_schema_pattern_cap(self):
        """`MAX_PATTERNS_PER_FIELD` is 64 and there are 50 types. This fails while there is still
        headroom to split the statement, rather than at the boot that first exceeds it."""
        from authz.schema import MAX_PATTERNS_PER_FIELD

        actions = bootstrap.admin_policy_document()["statements"][0]["actions"]
        assert len(actions) <= MAX_PATTERNS_PER_FIELD

    def test_it_validates_against_the_real_schema(self):
        """Written through `policy_store.create`, which validates — so an invalid document would
        fail at first boot, on a deployment, with the administrator not yet able to log in."""
        from authz.schema import PolicyDocument

        PolicyDocument.model_validate(bootstrap.admin_policy_document())

    def test_it_uses_no_bare_star_action(self):
        """`_no_bare_star_in_allow` refuses it, and the reason is worth preserving: a blanket grant
        buried in a policy file tells an auditor less than the allow-all engine does."""
        actions = bootstrap.admin_policy_document()["statements"][0]["actions"]
        assert "*" not in actions


class TestRedundantCallersInOneBootConverge:
    """The product calls `ensure_admin` from several entry points within one real start —
    `startup_gate.prepare()` in the gunicorn arbiter, `app._bootstrap_identity_at_startup()` in each
    worker it forks, `cli.py serve` before it hands off. They must mint exactly one credential
    between them.

    This used to need a boot-scoped env marker (`ensure_admin_once_per_boot`), because `ensure_admin`
    re-issued on every call and a second caller minted a second password — confirmed 2026-09-28 on
    the Nscale NC deployment, two banners two seconds apart with only the second valid. The marker is
    gone: `ensure_admin` now mints only when no `admin` exists, so the first caller creates the
    account and every later caller in the same boot finds it present and no-ops. The convergence is a
    property of the store, not of any coordinating state between the callers.
    """

    def test_a_second_caller_for_the_same_store_does_nothing(self, store):
        """The regression case: the second entry point must not mint a password that invalidates the
        one the first already printed."""
        first = bootstrap.ensure_admin(store, emit=False)
        second = bootstrap.ensure_admin(store, emit=False)

        assert second is None
        assert _verifies_admin(store, first)

    def test_several_callers_print_one_banner(self, store, caplog):
        """Only the first, creating caller emits — so an operator sees exactly one credential, not a
        stream of them each superseding the last."""
        with caplog.at_level(logging.WARNING, logger="identity.bootstrap"):
            first = bootstrap.ensure_admin(store)
            bootstrap.ensure_admin(store)
            bootstrap.ensure_admin(store)

        assert first and _verifies_admin(store, first)
        assert caplog.text.count("ONE-TIME ADMINISTRATOR CREDENTIAL") == 1

    def test_a_different_store_still_bootstraps(self, store, tmp_path):
        """No shared guard between stores: a second, independently-configured store in the same
        process still gets its own administrator."""
        other = IdentityStore(str(tmp_path / "other-identity.json"))
        try:
            bootstrap.ensure_admin(store, emit=False)
            other_password = bootstrap.ensure_admin(other, emit=False)
            assert other_password
            assert _verifies_admin(other, other_password)
        finally:
            other.close()


def _verifies_admin(store_handle: IdentityStore, password: str) -> bool:
    user = store_handle.get_user(bootstrap.ADMIN_LOGIN)
    assert user is not None
    recomputed = srp.verifier_for_salt(
        bootstrap.ADMIN_LOGIN, password, bytes.fromhex(user.srp_salt)
    ).hex()
    return recomputed == user.srp_verifier
