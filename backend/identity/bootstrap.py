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
"""First-boot `admin` creation and one-time password emission (FR-10, FR-10a, FR-10b).

An empty identity store has no way in. This module is the *only* thing that creates a user without
an authenticated request, and it does so exactly once per store, which is why FR-11 can say every
other path requires authentication.

**The password is emitted once and is then unrecoverable.** Only the SRP verifier is stored (FR-3),
so nothing here or anywhere else can print it again on a later start. That is the intended
property, not a limitation: it is what makes "we lost the admin password" a redeploy (FR-12) rather
than a support backdoor. The corollary for operators is blunt — capture it from the startup output
on first boot or you will be redeploying the store.

Why it runs on *any* start that finds an empty store, not only a literally-first one (FR-10b): the
release that adds this removes shared-token and PAM authentication in the same breath (FR-18). A
deployment upgrading from a token has no identity store, so if the bootstrap only fired on a
pristine install, the upgrade would be a lockout — the old credential stops working and no new one
appears. An empty store is the condition, whatever the history behind it.
"""

from __future__ import annotations

import logging
import secrets
import string

from . import srp
from .models import User, canonical_login
from .store import IdentityStore

_logger = logging.getLogger(__name__)

#: The login is fixed rather than configurable. A configurable bootstrap name is a configuration
#: input that decides who is administrator, and FR-12 does not allow configuration to grant access.
ADMIN_LOGIN = "admin"

#: 20 characters from a 62-character alphabet is ~119 bits. Well past anything brute-forceable, and
#: still short enough to be read off a terminal and typed once, which is the whole life of this
#: credential (FR-10a marks it for immediate change).
PASSWORD_LENGTH = 20

#: Deliberately no punctuation. This string gets copied out of a startup log, through a terminal,
#: possibly a chat window, and into a browser; shell metacharacters and quotes in that path cause
#: mangled-paste support tickets that are indistinguishable from a wrong password. The entropy is
#: bought with length instead, which costs nothing.
_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A fresh credential from the system CSPRNG.

    `secrets`, never `random`: `random` is a Mersenne Twister seeded from the clock, and observing a
    few outputs recovers its state. That distinction is the entire security of this function.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def needed(store: IdentityStore) -> bool:
    """Whether this start has to bootstrap — true exactly when no user exists.

    A one-line delegation on purpose. `IdentityStore.is_empty()` was written for this caller and
    already answers *users, not the file*, which is the subtlety that makes the shared-token upgrade
    work: a store file that exists but holds no users still needs bootstrapping. A second
    implementation here would be a copy of that decision, free to drift from it.
    """
    return store.is_empty()


def unclaimed_admin(store: IdentityStore) -> User | None:
    """The bootstrap administrator, if it exists and nobody has claimed the deployment yet.

    "Claimed" is the distinction that matters, and it is not "does a user exist": FR-10 enrols an
    administrator on first boot, so a store is non-empty from the moment the product starts. What
    tells a *provisioned* deployment from a *pristine* one is whether anyone has set a password —
    the bootstrap admin ships with ``must_change_password=True`` and a one-time credential, and
    until that is changed there is no credential anyone is using and no access to protect.

    Answers ``None`` the moment a second account exists, even an unclaimed one. Somebody
    administering this deployment created it, which makes the deployment provisioned regardless of
    the admin's own state.
    """
    users = store.list_users()
    if len(users) != 1:
        return None
    only = users[0]
    if only.login != canonical_login(ADMIN_LOGIN) or not only.must_change_password:
        return None
    return only


def ensure_admin(store: IdentityStore, *, emit: bool = True, force: bool = False) -> str | None:
    """Make sure a usable administrator credential exists, and return it when one was issued.

    **A password is issued in exactly two cases, and only these two:**

    * **there is no `admin` user** — a fresh install, or a deployment upgrading from shared-token
      auth with no identity store yet (FR-10b); create `admin`, as FR-10 requires. The condition is
      the *admin account's* absence, not the store's emptiness: a store that holds other accounts
      but no administrator still has no way in, and still gets one.
    * **`force=True`** — an operator has deliberately asked for a reset (`cli.py
      reset-admin-password`), because the one-time password was missed or lost. This re-issues the
      admin credential and invalidates every earlier copy.

    **If an `admin` user already exists, its password is left exactly as it is** — whether or not it
    has been claimed (changed from the bootstrap credential). A plain restart issues nothing. This
    is the correction to the 2026-09/10 behaviour that re-minted on *every* start while the admin
    was still "unclaimed" (`must_change_password=True`). That turned every restart of a not-yet-
    claimed deployment into a lockout: the password an operator had copied from the startup banner
    stopped working the next time the container cycled, and nothing said why. Confirmed on the Nscale
    NC deployment — four boots, four different passwords, each invalidating the last. Recoverability
    does not require auto-reminting; it requires an *explicit* reset, which `force` provides and
    which cannot be triggered by merely restarting the process.

    **The condition is read from the store, never from the environment.** "Does an `admin` account
    exist?" is a durable fact the store answers directly; gating on it is idempotent against the
    store itself, so the several entry points that call this within one real start —
    `startup_gate.prepare` in the gunicorn arbiter, `app._bootstrap_identity_at_startup` in each
    forked worker, `cli.py serve` before it `execv`s — converge without coordinating: the first
    caller creates `admin`, and every later caller finds it present and returns `None`. No
    boot-scoped marker, no cross-`fork`/`execv` env state, nothing to drift. That machinery existed
    only to contain the re-mint-on-every-call behaviour this function no longer has.

    **Nothing is re-issued once the admin exists unless `force` is set.** Leaving a live credential
    in place is the point; regenerating it on a restart would lock out whoever holds it and hand a
    back door to anyone who can cycle the process. The restart path cannot set `force`, so the
    process gains nobody access it did not already have.
    """
    existing = store.get_user(canonical_login(ADMIN_LOGIN))
    if existing is not None and not force:
        return None

    password = generate_password()
    salt, verifier = srp.enroll(ADMIN_LOGIN, password)

    if existing is None:
        store.create_user(
            User(
                login=canonical_login(ADMIN_LOGIN),
                display_name="Administrator",
                srp_salt=salt,
                srp_verifier=verifier,
                # FR-10a. A bootstrap credential that lives forever is a shared password.
                must_change_password=True,
            )
        )
    else:
        # `edited` rather than a fresh User, so nothing else on the record is silently reset — and
        # `must_change_password` stays True, because a forced reset is still a bootstrap credential
        # and still has to be replaced by its holder.
        store.update_user(
            existing.edited(srp_salt=salt, srp_verifier=verifier, must_change_password=True)
        )

    if emit:
        _emit(password, reissued=existing is not None)
    return password


def _emit(password: str, *, reissued: bool = False) -> None:
    """Write the credential to the startup output, once, impossible to miss.

    Deliberately `logging.warning` and not `info`: a deployment running at WARNING would otherwise
    drop the only copy of the only credential, and the operator would discover the loss at the login
    screen with nothing to type. It is also genuinely warning-shaped — there is an unchanged
    bootstrap password on the system until someone acts.

    The banner exists because this competes with a few hundred startup lines for attention.
    """
    # The two headings differ so an operator can tell which situation they are in, and the
    # re-issue case says what happened to the previous value: an earlier password copied from an
    # older log line no longer works, and finding that out at the login screen would be worse than
    # never having seen it.
    heading = (
        "  ADMINISTRATOR CREDENTIAL RE-ISSUED - PREVIOUS PASSWORD NO LONGER VALID"
        if reissued
        else "  IDENTITY STORE INITIALISED - ONE-TIME ADMINISTRATOR CREDENTIAL"
    )
    why = (
        "  Re-issued by an explicit reset (reset-admin-password). The previous\n"
        "  password no longer works. A plain restart does NOT replace it - only\n"
        "  this deliberate reset does.\n"
        if reissued
        else "  You will be asked to change it at first login.\n"
    )
    _logger.warning(
        "\n"
        "=========================================================================\n"
        "%s\n"
        "=========================================================================\n"
        "    login:    %s\n"
        "    password: %s\n"
        "\n"
        "  Copy this now. It is not stored and cannot be shown again - only its\n"
        "  SRP verifier is kept.\n"
        "%s"
        "  Once it IS changed, nothing is re-issued: losing every administrator\n"
        "  credential then means redeploying the identity store, because there is\n"
        "  no reset endpoint and no recovery code.\n"
        "=========================================================================",
        heading,
        ADMIN_LOGIN,
        password,
        why,
    )


#: Id of the policy the first boot writes. Referenced by the startup preflight and by the tests
#: that assert a brand-new deployment enforces rather than falling back to allow-all.
ADMIN_POLICY_ID = "administrators"


def admin_policy_document() -> dict:
    """The policy a brand-new deployment starts with: the administrator may do anything.

    **Enumerated per resource type rather than written as a bare ``*``**, because
    ``Statement._no_bare_star_in_allow`` refuses `actions: ["*"]` in an allow — deliberately, since
    a blanket grant hidden in a policy file tells an auditor less than the allow-all engine, which
    at least records `policy_engine: allow-all` in the trail. Enumerating keeps the document
    readable: an auditor sees exactly which services the administrator may act on.

    Derived from :func:`authz.resources.declared_types` rather than a hand-written list, so the
    document cannot fall behind the product. A new resource type joins it automatically, and
    ``test_admin_policy_covers_every_declared_type`` fails the build if the enumeration ever
    outgrows the schema's pattern cap.
    """
    from authz.resources import declared_types

    return {
        "id": ADMIN_POLICY_ID,
        "description": (
            "Full administrative access. Written on first boot so the deployment enforces policy "
            "from the start instead of running permissively. Narrow or replace it once real roles "
            "exist; detaching it from the last administrator locks everyone out."
        ),
        "statements": [
            {
                "sid": "administer-everything",
                "effect": "allow",
                "actions": sorted(f"{t}:*" for t in declared_types()),
                "resources": ["*"],
            }
        ],
    }


def admin_policy_gaps() -> list[str]:
    """Declared resource types the **stored** administrators policy does not cover.

    **The gap this exists for is a real defect, found from the UI on 2026-09-10.** The policy
    enumerates `<type>:*` for every type in `declared_types()` — but it is written *once*, at first
    boot, from whatever types existed at that moment. `ensure_admin_policy` then returns early
    forever, because an existing policy set is a decision it must not overrule. So every resource
    type added afterwards is missing from every deployment that was installed before it, and the
    symptom is an administrator getting `not authorized` on a feature that just shipped, with
    nothing in the logs to connect the two. `audit.trail` was simply the first new type since the
    policy was seeded; the next one would have gone the same way.

    **The existing test could not have caught it.** `test_it_covers_every_declared_resource_type`
    compares `admin_policy_document()` with `declared_types()` — two values computed live, from the
    same source, in the same process. It is honest about its scope ("on a fresh install") and that
    is exactly the case that works. Drift lives in the *persisted* artifact, so a test that never
    reads the stored document cannot see it.

    Reporting rather than repairing, and that is deliberate. The obvious fix — top the policy up on
    boot — cannot distinguish "this type did not exist when we seeded" from "an operator removed
    it", and the document's own description invites the latter ("Narrow or replace it once real
    roles exist"). Silently re-widening a narrowed administrators policy would be a privilege
    escalation performed by a housekeeping routine. So the gap is surfaced and
    `cli.py reconcile-admin-policy` applies it as an explicit act.
    """
    from authz import resources as authz_resources
    from authz import store as policy_store

    try:
        document = policy_store.get(ADMIN_POLICY_ID)
    except Exception:  # noqa: BLE001 - an unreadable policy set is the preflight's business
        return []
    if document is None:
        return []

    granted: set[str] = set()
    for statement in getattr(document, "statements", []) or []:
        for pattern in getattr(statement, "actions", []) or []:
            if str(pattern).endswith(":*"):
                granted.add(str(pattern)[:-2])
            elif pattern == "*":
                return []  # a bare wildcard covers everything; nothing to report

    return sorted(authz_resources.declared_types() - granted)


def ensure_admin_policy(store: IdentityStore, *, login: str = ADMIN_LOGIN) -> bool:
    """Write the administrator policy and attach it to `login`. Returns whether it wrote anything.

    **Only ever called for a brand-new deployment**, and deliberately not from
    :func:`ensure_admin`. Seeding inside that function would give a policy file to every test
    fixture that bootstraps an administrator — flipping those suites from the permissive engine to
    default-deny as a side effect of creating a user, and writing into the repo's real
    ``var/store`` while doing it.

    Why a brand-new system is the only safe moment: on an existing deployment we cannot know which
    of its accounts should hold this policy, and guessing wrong either locks the operators out or
    hands somebody full access. So an upgrade is asked for a policy explicitly by the startup
    preflight rather than having one invented for it.

    Idempotent. An existing policy set is left exactly as it is, because a deployment that has
    written a policy has made a decision this function must not overrule.
    """
    from authz import store as policy_store

    if policy_store.read_all():
        return False

    policy_store.create(admin_policy_document())
    store.attach_policy(f"user:{canonical_login(login)}", ADMIN_POLICY_ID)
    _logger.warning(
        "identity: wrote the '%s' policy and attached it to %s. This deployment now ENFORCES "
        "policy (default-deny); every other principal is refused until a policy grants it.",
        ADMIN_POLICY_ID,
        login,
    )
    return True
