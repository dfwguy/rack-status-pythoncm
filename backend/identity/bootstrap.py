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


def ensure_admin(store: IdentityStore, *, emit: bool = True) -> str | None:
    """Create `admin` if and only if the store holds no users. Returns the password, or None.

    Idempotent by the condition above, which is what AC-19 pins: a credential re-printed on every
    restart would be a shared password sitting in the deployment's logs, so "did we already
    bootstrap" must be answered from the store and never from a marker file or an env var.

    Returning the password is for tests and for callers that want to present it themselves; the
    default `emit=True` also writes it to the log, because the operator reading startup output is
    the actual audience.
    """
    if not needed(store):
        return None

    password = generate_password()
    salt, verifier = srp.enroll(ADMIN_LOGIN, password)
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
    if emit:
        _emit(password)
    return password


def _emit(password: str) -> None:
    """Write the credential to the startup output, once, impossible to miss.

    Deliberately `logging.warning` and not `info`: a deployment running at WARNING would otherwise
    drop the only copy of the only credential, and the operator would discover the loss at the login
    screen with nothing to type. It is also genuinely warning-shaped — there is an unchanged
    bootstrap password on the system until someone acts.

    The banner exists because this competes with a few hundred startup lines for attention.
    """
    _logger.warning(
        "\n"
        "=========================================================================\n"
        "  IDENTITY STORE INITIALISED - ONE-TIME ADMINISTRATOR CREDENTIAL\n"
        "=========================================================================\n"
        "    login:    %s\n"
        "    password: %s\n"
        "\n"
        "  Copy this now. It is not stored and cannot be shown again - only its\n"
        "  SRP verifier is kept. You will be asked to change it at first login.\n"
        "  Losing every administrator credential means redeploying the identity\n"
        "  store; there is no reset endpoint and no recovery code.\n"
        "=========================================================================",
        ADMIN_LOGIN,
        password,
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
