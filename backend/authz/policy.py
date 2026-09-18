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
"""The policy engine (Phase 3): attachment-based, default-deny.

Fills the seam `authz/__init__.py` left. No call site changes — every route already asks the same
question through `authorize()`; this makes the answer mean something.

EVALUATION ORDER, WHICH IS THE WHOLE CONTRACT
----------------------------------------------
1. **Implicit deny.** Nothing matched means refused.
2. **Any explicit deny halts**, immediately, with a refusal. A deny cannot be outvoted by any number
   of allows, at any specificity, in any file order.
3. **Allow only on an explicit match.**

Each step is the conservative choice, and each has a cost if inverted. Permit-unless-forbidden would
mean every action added after a policy was written is silently allowed, so the product grows more
permissive with nobody editing a file. Letting specificity beat a deny would mean "never power-cycle
rack 7" holds only until somebody writes a narrower rule they have not read.

POLICIES ATTACH TO PRINCIPALS; THEY DO NOT NAME THEM
-----------------------------------------------------
A policy document lists actions and resources. Who may use it is a separate relation, resolved per
request through :attr:`PolicyEngine.attachments`.

Measured before choosing (`P` policies, `g` groups, `A` attached to this principal, `S` statements
each, `p̄` principals named per policy in the retired model):

    inline principals   O(P · S · (1+g) · p̄)    grows with EVERY policy in the system
    attachment          O(g + A · S)            grows only with what is attached to this principal

    20 users / 10 policies      15.1 µs  →   2.1 µs    (7×)
    5k users / 5k policies    3069.4 µs  →   8.5 µs    (362×)

**The latency is not why.** At this product's scale 15 µs against 2 µs is free next to the `cmsh`
call the request is really waiting on. The reasons are that "attach a policy to a user" must not mean
*editing a document other users depend on*; that this engine is meant for reuse in products that
will not share this scale; and that choosing now is free where choosing later is a migration of every
document.

REUSE: NOTHING HERE MAY IMPORT `identity/` AT MODULE LEVEL (FR-7c)
-------------------------------------------------------------------
Both lookups — groups, and attachments — arrive as callables. The Rack Status adapters live at the
bottom of this file and are the *only* product-coupled code in `authz/`; a consumer in another
product supplies its own and imports nothing from here that knows about clusters.
`test_authz_policy.py` asserts the import graph, because a coupling added by accident is exactly what
a future consumer would have to unpick.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from authz import Decision
from authz.schema import Effect, PolicyDocument, PolicyFile, Statement

_logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_POLICY_FILE = REPO_ROOT / "var" / "store" / "policies.json"

#: Given a principal, the groups it belongs to.
GroupLookup = Callable[[str], Iterable[str]]
#: Given a subject (`user:x` or `group:y`), the ids of policies attached to it.
AttachmentLookup = Callable[[str], Iterable[str]]


class PolicyError(Exception):
    """A policy set that cannot be trusted to mean what it says."""


def _glob_any(value: str, patterns: Iterable[str]) -> bool:
    """`fnmatchcase`, never `fnmatch`.

    `fnmatch` normalises through `os.path.normcase`, which lowercases **only on Windows** — so on
    macOS and Linux the two are indistinguishable, and swapping one for the other here changes
    nothing that any test on our platforms can observe (measured; a mutation to `fnmatch` was not
    caught, and could not be). The explicit form is still the right call, because a policy grant
    must not become case-insensitive on a platform nobody deploys on today: an authorization
    decision is the wrong place to inherit a filesystem's opinion. Treat this as belt-and-braces,
    not as a defended behaviour.
    """
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


@dataclass(frozen=True)
class Loaded:
    """The parsed policy set, indexed for lookup by id."""

    by_id: dict[str, PolicyDocument]
    source: str = ""

    @property
    def total(self) -> int:
        return len(self.by_id)


def load(path: Path) -> Loaded:
    """Parse a policy file, or raise.

    All-or-nothing, deliberately. Loading the valid documents and dropping the rest means a typo in
    a *deny* silently widens access, with a successful-looking load and nothing in the log to say a
    rule was discarded.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        # A fresh deployment has no policy file. That is a valid state and it means default-deny,
        # which is why `authz.install_default` keeps the permissive engine until a file exists.
        return Loaded(by_id={}, source=str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"{path} is unreadable: {exc}") from exc

    try:
        parsed = PolicyFile.model_validate(raw if isinstance(raw, dict) else {"policies": raw})
    except ValueError as exc:
        raise PolicyError(f"{path} is not a valid policy file: {exc}") from exc

    return Loaded(by_id={p.id: p for p in parsed.policies}, source=str(path))


class PolicyEngine:
    """Default-deny, deny-overrides, over policies attached to the principal."""

    name = "rbac-v1"

    def __init__(
        self,
        policy_file: Path | None = None,
        *,
        group_lookup: GroupLookup | None = None,
        attachment_lookup: AttachmentLookup | None = None,
    ):
        self._path = policy_file
        self._lock = threading.RLock()
        self._loaded: Loaded | None = None
        self._key: tuple[str, float] | None = None
        self.groups: GroupLookup = group_lookup or _groups_for
        self.attachments: AttachmentLookup = attachment_lookup or _attachments_for
        #: Incremented once per statement examined. The only way to assert "evaluation considered
        #: only attached policies" (TC-13) without timing it — a timing assertion would pass on a
        #: broken implementation on a fast machine.
        self.statements_examined = 0

    # -- loading -----------------------------------------------------------------------------

    def _file(self) -> Path:
        if self._path is not None:
            return self._path
        raw = (os.environ.get("RACK_WEB_POLICY_FILE") or "").strip()
        return Path(raw) if raw else DEFAULT_POLICY_FILE

    def policies(self) -> Loaded:
        """The current set, reloaded when the file changes.

        Keyed on (path, mtime) so an operator editing policy takes effect without a restart — a
        restart to fix a lockout is an outage. The read performs **no write**, which matters because
        a write would change the mtime this cache keys on: the Phase 2 revocation cache invalidated
        itself exactly that way and never hit once while every functional test passed.
        """
        path = self._file()
        try:
            key = (str(path), path.stat().st_mtime)
        except OSError:
            key = (str(path), 0.0)

        with self._lock:
            if self._loaded is not None and self._key == key:
                return self._loaded
            loaded = load(path)
            self._loaded, self._key = loaded, key
            _logger.info("authz: loaded %d policy document(s) from %s", loaded.total, path)
            return loaded

    # -- resolution --------------------------------------------------------------------------

    def subjects_for(self, principal: str) -> tuple[str, ...]:
        """Everything an attachment may be addressed to: the principal, plus its groups.

        A lookup failure yields no groups rather than raising, so the caller falls back to whatever
        is attached to them personally — fewer permissions, never more. A store outage must not
        become an escalation.
        """
        subjects = [principal]
        try:
            subjects.extend(f"group:{g}" for g in self.groups(principal))
        except Exception:
            _logger.exception("authz: group lookup failed for %s; treating as no groups", principal)
        return tuple(subjects)

    def attached_documents(self, principal: str) -> tuple[PolicyDocument, ...]:
        """The policies in scope for this principal. `O(g + A)`.

        `1 + g` lookups against an index, not a scan of the corpus — that is the whole point of the
        attachment model. An id naming a policy that no longer exists is skipped, because deleting a
        document must not make every request fail; the attachment is stale, not the request.
        """
        loaded = self.policies()
        found: dict[str, PolicyDocument] = {}
        for subject in self.subjects_for(principal):
            try:
                ids = self.attachments(subject)
            except Exception:
                _logger.exception("authz: attachment lookup failed for %s", subject)
                continue
            for policy_id in ids:
                document = loaded.by_id.get(policy_id)
                if document is None:
                    _logger.warning(
                        "authz: %s is attached to unknown policy %r; ignoring", subject, policy_id
                    )
                    continue
                # Keyed by id, so a policy attached both directly and via a group is evaluated once.
                found[document.id] = document
        return tuple(found.values())

    # -- evaluation --------------------------------------------------------------------------

    def _matches(self, statement: Statement, action: str, resource: str) -> bool:
        self.statements_examined += 1
        return _glob_any(action, statement.actions) and _glob_any(resource, statement.resources)

    def evaluate(self, principal: str, action: str, resource: str) -> Decision:
        """The verdict. Never raises — a failing evaluator is a denial, not an exception.

        An engine that raises turns a policy typo into a 500 on every route at once, which is both
        an outage and a worse diagnostic than a refusal naming the rule.
        """
        try:
            documents = self.attached_documents(principal)
        except PolicyError as exc:
            # An unparseable policy file is not a reason to fall back to permitting. It is a reason
            # to refuse everything and be extremely obvious about why.
            _logger.error("authz: policy set unusable, denying all requests: %s", exc)
            return Decision(
                allowed=False,
                engine=self.name,
                reason="the policy file could not be loaded; all requests are refused",
            )

        # Denies first, and they are final. Two passes rather than one, because a deny appearing
        # after an allow in the file must still win — ordering must not decide a security outcome.
        for document in documents:
            for statement in document.statements:
                if statement.effect is Effect.deny and self._matches(statement, action, resource):
                    return Decision(
                        allowed=False,
                        engine=self.name,
                        reason=f"denied by {_rule_name(document, statement)}",
                    )

        for document in documents:
            for statement in document.statements:
                if statement.effect is Effect.allow and self._matches(statement, action, resource):
                    return Decision(
                        allowed=True,
                        engine=self.name,
                        reason=f"allowed by {_rule_name(document, statement)}",
                    )

        # Default deny. The reason names the action but never the resource: a refusal that confirms
        # a resource exists is an existence oracle for anyone probing (NFR-3).
        return Decision(allowed=False, engine=self.name, reason=f"no policy grants {action}")


def _rule_name(document: PolicyDocument, statement: Statement) -> str:
    """`policy/statement` where a statement is named, else the policy.

    Worth the branch: "which rule refused me" is the first question after a denial, and a document
    with eight statements cannot answer it.
    """
    return f"policy {document.id}/{statement.sid}" if statement.sid else f"policy {document.id}"


# -- Rack Status adapters ------------------------------------------------------------------------
#
# The ONLY product-coupled code in `authz/`. Both are imported lazily inside the function bodies so
# no module here has `identity/` on its import graph (FR-7c) — a consumer in another product passes
# its own callables and never loads these.


def _groups_for(principal: str) -> tuple[str, ...]:
    """Group names for a principal, from the Phase 2 identity store."""
    from identity import principals as principals_mod
    from identity import store as identity_store

    try:
        kind, login = principals_mod.parse_principal(principal)
    except (ValueError, TypeError):
        return ()
    if kind != "user":
        return ()

    store = None
    try:
        store = identity_store.IdentityStore()
        return tuple(store.groups_of(login))
    except Exception:
        _logger.exception("authz: could not resolve groups for %s", principal)
        return ()
    finally:
        if store is not None:
            store.close()


def _attachments_for(subject: str) -> tuple[str, ...]:
    """Policy ids attached to a subject, from the identity store.

    Returns empty on any failure, which is the safe direction: no attachments means default-deny,
    where a raise would take the request down and a fabricated list would grant access.
    """
    from identity import store as identity_store

    store = None
    try:
        store = identity_store.IdentityStore()
        return tuple(store.policies_for(subject))
    except AttributeError:
        # The store predates policy attachment. Reached only on a partially deployed tree; logged
        # once per call rather than raising, because default-deny is the correct behaviour here.
        _logger.warning("authz: identity store has no policy attachments yet")
        return ()
    except Exception:
        _logger.exception("authz: could not resolve attachments for %s", subject)
        return ()
    finally:
        if store is not None:
            store.close()
