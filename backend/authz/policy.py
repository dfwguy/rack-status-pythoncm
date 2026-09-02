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
"""The policy engine (Phase 3).

Fills the seam `backend/authz/__init__.py` left. No call site changes: every route already asks the
same question through `authorize()`, and this makes the answer mean something.

THE EVALUATION RULES, AND WHY EACH ONE IS THE CONSERVATIVE CHOICE
------------------------------------------------------------------
1. **Default deny.** A request matching no policy is refused. The alternative — permit unless
   forbidden — means every action added after a policy was written is silently allowed, so the
   product becomes more permissive as it grows and nobody edits a file to make that happen.
2. **Deny overrides allow.** An explicit deny cannot be outvoted by any number of allows. Someone
   writing "never let anyone power-cycle rack 7" must be able to rely on it holding regardless of
   what else is granted, including grants written later by someone who has not read theirs.
3. **A malformed policy denies, loudly.** It is not skipped. Skipping means a typo in a `deny`
   silently widens access, which is the failure you would least want to be quiet.

WILDCARDS ARE DELIBERATE HERE, UNLIKE IN `redirect_uri`
--------------------------------------------------------
`oidc_clients` matches redirect URIs by exact string because a permissive match there hands an
attacker an authorization code. Policies are the opposite case: `rack.power:*` is the whole point,
and forcing an operator to enumerate 124 action names would produce a policy nobody maintains.

The safety comes from direction. A wildcard in an **allow** widens access, so `*` alone is refused
outright — it is almost always a mistake and, when it isn't, `AllowAllEngine` already exists and says
so honestly in the audit trail. A wildcard in a **deny** narrows access and needs no such guard.

GROUPS ARE THE UNIT OF GRANT
----------------------------
Policies attach to `group:<name>` as well as `user:<login>`. Phase 2 built groups precisely so this
phase would not have to invent a second grouping mechanism, and a grant attached to a person leaves
with them in a way nobody notices until an audit.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from authz import Decision

_logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_POLICY_FILE = REPO_ROOT / "var" / "store" / "policies.json"

ALLOW = "allow"
DENY = "deny"


class PolicyError(Exception):
    """A policy document that cannot be trusted to mean what it says."""


@dataclass(frozen=True)
class Policy:
    """One statement: these principals may (or may not) do these actions to these resources."""

    policy_id: str
    effect: str
    principals: tuple[str, ...]
    actions: tuple[str, ...]
    resources: tuple[str, ...] = ("*",)
    description: str = ""

    def matches(self, subjects: frozenset[str], action: str, resource: str) -> bool:
        return (
            any(_glob_any(s, self.principals) for s in subjects)
            and _glob_any(action, self.actions)
            and _glob_any(resource, self.resources)
        )


def _glob_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


def _parse(raw: dict, index: int) -> Policy:
    policy_id = str(raw.get("id") or f"policy[{index}]")
    effect = str(raw.get("effect") or "").strip().lower()
    if effect not in (ALLOW, DENY):
        raise PolicyError(f"{policy_id}: effect must be {ALLOW!r} or {DENY!r}, got {effect!r}")

    principals = tuple(str(p) for p in (raw.get("principals") or []))
    actions = tuple(str(a) for a in (raw.get("actions") or []))
    if not principals:
        raise PolicyError(f"{policy_id}: names no principals, so it can never match")
    if not actions:
        raise PolicyError(f"{policy_id}: names no actions, so it can never match")

    if effect == ALLOW:
        # A bare `*` in an allow grants everything the product will ever add, including actions
        # written years after the policy. Refused because it is nearly always a mistake — and when
        # it genuinely is not, `AllowAllEngine` exists and records `policy_engine: allow-all`, which
        # tells an auditor the truth instead of hiding a blanket grant inside a policy file.
        for field_name, values in (("principals", principals), ("actions", actions)):
            if "*" in values:
                raise PolicyError(
                    f"{policy_id}: allow policies may not use a bare '*' for {field_name}. "
                    f"Narrow it (e.g. 'rack.power:*'), or run the allow-all engine, which is "
                    f"honest about being permissive."
                )

    return Policy(
        policy_id=policy_id,
        effect=effect,
        principals=principals,
        actions=actions,
        resources=tuple(str(r) for r in (raw.get("resources") or ["*"])) or ("*",),
        description=str(raw.get("description") or ""),
    )


@dataclass
class PolicySet:
    """The loaded policies, split by effect because denies are evaluated first."""

    denies: tuple[Policy, ...] = ()
    allows: tuple[Policy, ...] = ()
    source: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.denies) + len(self.allows)


def load(path: Path) -> PolicySet:
    """Parse a policy file. A single bad statement fails the whole load.

    Deliberately all-or-nothing. Loading the valid statements and dropping the rest would mean a
    typo in a `deny` silently widens access -- the load looks successful, the audit trail shows
    allows, and nothing anywhere says a rule was discarded.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return PolicySet(source=str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"{path} is unreadable: {exc}") from exc

    entries = raw.get("policies") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise PolicyError(f"{path} must hold a list of policies, or an object with 'policies'")

    parsed = [_parse(entry, i) for i, entry in enumerate(entries)]
    seen: set[str] = set()
    for policy in parsed:
        if policy.policy_id in seen:
            raise PolicyError(f"{path} defines {policy.policy_id!r} twice")
        seen.add(policy.policy_id)

    return PolicySet(
        denies=tuple(p for p in parsed if p.effect == DENY),
        allows=tuple(p for p in parsed if p.effect == ALLOW),
        source=str(path),
    )


class PolicyEngine:
    """Default-deny RBAC with deny-overrides, over the Phase 2 group model."""

    name = "rbac-v1"

    def __init__(self, policy_file: Path | None = None, *, group_lookup=None):
        self._path = policy_file
        self._lock = threading.RLock()
        self._set: PolicySet | None = None
        self._key: tuple[str, float] | None = None
        #: Injected so the engine can be tested without an identity store, and so the store is not
        #: a hard import of this module.
        self._group_lookup = group_lookup or _groups_for

    def _file(self) -> Path:
        if self._path is not None:
            return self._path
        raw = (os.environ.get("RACK_WEB_POLICY_FILE") or "").strip()
        return Path(raw) if raw else DEFAULT_POLICY_FILE

    def policies(self) -> PolicySet:
        """The current policy set, reloaded when the file changes.

        Keyed on (path, mtime) so an operator editing policy takes effect without a restart. The
        read performs no write, which matters because a write would change the mtime this cache
        keys on -- the Phase 2 revocation cache invalidated itself exactly that way.
        """
        path = self._file()
        try:
            key = (str(path), path.stat().st_mtime)
        except OSError:
            key = (str(path), 0.0)

        with self._lock:
            if self._set is not None and self._key == key:
                return self._set
            loaded = load(path)
            self._set, self._key = loaded, key
            _logger.info("authz: loaded %d policy statement(s) from %s", loaded.total, path)
            return loaded

    def subjects_for(self, principal: str) -> frozenset[str]:
        """Everything a policy may name this principal by: itself, plus its groups."""
        subjects = {principal}
        for group in self._group_lookup(principal):
            subjects.add(f"group:{group}")
        return frozenset(subjects)

    def evaluate(self, principal: str, action: str, resource: str) -> Decision:
        """The verdict. Never raises -- a failing evaluator is a denial, not an exception."""
        try:
            policies = self.policies()
        except PolicyError as exc:
            # A policy file we cannot parse is not a reason to fall back to permitting. It is a
            # reason to refuse everything and be extremely obvious about why.
            _logger.error("authz: policy file unusable, denying all requests: %s", exc)
            return Decision(
                allowed=False,
                engine=self.name,
                reason="the policy file could not be loaded; all requests are refused",
            )

        subjects = self.subjects_for(principal)

        # Denies first, and they are final. An explicit prohibition must not be outvotable by an
        # allow written later by someone who never read it.
        for policy in policies.denies:
            if policy.matches(subjects, action, resource):
                return Decision(
                    allowed=False,
                    engine=self.name,
                    reason=f"denied by policy {policy.policy_id}",
                )

        for policy in policies.allows:
            if policy.matches(subjects, action, resource):
                return Decision(
                    allowed=True,
                    engine=self.name,
                    reason=f"allowed by policy {policy.policy_id}",
                )

        # Default deny. The reason names the action but never the resource: a refusal that confirms
        # a resource exists is an existence oracle for anyone probing.
        return Decision(
            allowed=False,
            engine=self.name,
            reason=f"no policy grants {action}",
        )


def _groups_for(principal: str) -> tuple[str, ...]:
    """Group names for a principal, from the Phase 2 identity store.

    Any failure yields no groups rather than raising, which means the caller falls back to whatever
    is granted to them personally -- fewer permissions, never more. A store outage must not become
    an escalation.
    """
    try:
        from identity import principals as principals_mod
        from identity import store as identity_store
    except ImportError:  # pragma: no cover - identity is always present in the app
        return ()

    try:
        kind, login = principals_mod.parse_principal(principal)
    except (ValueError, TypeError) as exc:
        _logger.debug("authz: unparseable principal %r (%s); no groups", principal, exc)
        return ()
    if kind != "user":
        return ()

    store = None
    try:
        store = identity_store.IdentityStore()
        return tuple(store.groups_of(login))
    except Exception:
        _logger.exception("authz: could not resolve groups for %s; treating as none", principal)
        return ()
    finally:
        if store is not None:
            store.close()
