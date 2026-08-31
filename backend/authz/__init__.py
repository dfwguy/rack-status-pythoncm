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
"""Authorization: the seam now, the policy engine later.

**This package exists to be replaced from the inside.** Phase 3 wires a real evaluator — the
pydantic-schema engine in `docs/PHASE3_POLICY_ENGINE_SPIKE.md`, or Cedar — and the point of landing
the interface first is that doing so changes no call site. Every route asks the same question
through the same function today as it will then; only the answer gets smarter.

The question is fixed and deliberately small: **may this principal perform this action on this
resource?** All three are already first-class strings elsewhere in the product, which is what makes
this cheap to land — `identity.principals` owns the principal grammar (`user:admin`), the Phase 1
`@action` vocabulary owns the 110 action names, and `docs/RESOURCE_MODEL.md` owns the 36 resource
types. There is nothing to invent here, only to connect.

## The default engine allows everything, and that is not the same as no engine

`AllowAllEngine` is an explicit, named, registered decision to permit — recorded in the audit trail
as `decision: allow, policy_engine: allow-all`, which is an honest statement that *something*
evaluated the request and said yes.

An **unregistered** engine is refused instead. That distinction is the whole design:

- "the deployment chose a permissive policy" is a configuration, visible in every audit record;
- "nobody wired an engine" is a bug, and defaulting it to permit would turn a missing line of
  startup code into a silent, product-wide authorization bypass.

Phase 1 supplied this precedent twice, and both times the failure was invisible: the config
materialise hook, which when unregistered produced wrong values on some clusters and an error on
none; and `tokens.verify`, which refuses every token until `revocation.install()` runs. Refusing
loudly is the correct direction for a security control, because the alternative is discovering the
gap from an incident rather than from the first request.

## What this is not

Not a policy *store*, not a policy *language*, and not a group-membership resolver. Those arrive
with the real engine. Adding them here would mean designing the schema twice — once against an
allow-all stub that cannot exercise it, and again when the evaluator makes its real constraints
known.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

_logger = logging.getLogger(__name__)

#: Recorded in the audit event's `policy_engine` field when nothing is registered. Distinct from
#: `audit.POLICY_ENGINE_NONE` ("none", meaning Phase 1 had no engine at all) because "an engine was
#: asked for and none was found" is a different, and worse, condition than "this build predates
#: authorization".
ENGINE_UNSET = "unset"


class AuthzError(Exception):
    """Raised only for programming errors — a malformed action, a missing engine at import time."""


@dataclass(frozen=True, slots=True)
class Decision:
    """A verdict, plus enough context to explain it in an audit record and to a user.

    Frozen because a decision that can be edited after the fact is not evidence. `reason` is written
    for a human reading a refusal, so it must never name a resource the caller was not already
    allowed to know exists — see the existence-oracle note in `AUTH_PROGRAMME_SCOPE.md`.
    """

    allowed: bool
    engine: str
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Engine(Protocol):
    """What Phase 3 has to implement, and all of it.

    Kept to one method on purpose. A wider surface — `list_permissions`, `explain`, `simulate` —
    invites the stub to grow features the real evaluator will implement differently, and every one
    of those is a call site to migrate later.
    """

    @property
    def name(self) -> str:
        """Short identifier recorded in the audit trail's `policy_engine` field."""

    def evaluate(self, principal: str, action: str, resource: str) -> Decision:
        """The verdict. Must not raise; a failing evaluator is a denial, never an exception."""


class AllowAllEngine:
    """Permits everything, and says so in every record.

    The honest interim: authentication is enforced (a caller must be who they claim), authorization
    is not yet (any authenticated caller may do anything). That is a real reduction in exposure over
    a shared token, and pretending otherwise in the audit trail would be worse than the gap itself —
    `decision: allow` from `policy_engine: allow-all` tells a reader exactly how much the verdict is
    worth.
    """

    name = "allow-all"

    def evaluate(self, principal: str, action: str, resource: str) -> Decision:
        return Decision(allowed=True, engine=self.name, reason="permissive policy")


class DenyAllEngine:
    """Refuses everything. For tests, and for an operator who needs to stop the product now.

    Worth having as a named engine rather than only as a test double: "deny everything while we work
    out what happened" is a real incident response, and it should not require code.
    """

    name = "deny-all"

    def evaluate(self, principal: str, action: str, resource: str) -> Decision:
        return Decision(allowed=False, engine=self.name, reason="restrictive policy")


_lock = threading.Lock()
_engine: Engine | None = None


def set_engine(engine: Engine | None) -> None:
    """Register the evaluator. `app.py` does this once at startup."""
    global _engine
    with _lock:
        _engine = engine


def current_engine() -> Engine | None:
    with _lock:
        return _engine


def engine_name() -> str:
    engine = current_engine()
    return engine.name if engine is not None else ENGINE_UNSET


def authorize(principal: str, action: str, resource: str) -> Decision:
    """The one question. Fails closed on every path that is not an explicit permit.

    Three ways to be refused, and they are deliberately indistinguishable to the caller while being
    distinguishable in the log:

    - no engine registered — a deployment bug, logged at ERROR because nothing else will notice it;
    - the engine said no — the ordinary case;
    - the engine raised — treated as a denial, because an evaluator that cannot decide has not
      permitted anything. Letting the exception escape would surface as a 500, which reads to a
      caller as "try again" rather than "you may not".
    """
    engine = current_engine()
    if engine is None:
        _logger.error(
            "no authorization engine registered; refusing %s on %s for %s. "
            "app.py should call authz.set_engine() at startup",
            action, resource, principal,
        )
        return Decision(
            allowed=False, engine=ENGINE_UNSET, reason="no authorization engine registered"
        )
    try:
        decision = engine.evaluate(principal, action, resource)
    except Exception:
        _logger.exception(
            "authorization engine %r raised evaluating %s on %s; refusing",
            engine_name(), action, resource,
        )
        return Decision(allowed=False, engine=engine_name(), reason="evaluation failed")
    if not isinstance(decision, Decision):
        # A engine returning the wrong type is a programming error, and "truthy" is not a verdict.
        _logger.error("authorization engine %r returned %r, not a Decision", engine_name(), decision)
        return Decision(allowed=False, engine=engine_name(), reason="evaluation failed")
    return decision


def install_default() -> None:
    """Register the permissive engine. Called by `app.py`; explicit so it appears in a diff.

    Deliberately not done at import time. An engine that registers itself on import means the
    permissive default is impossible to *not* have, and the whole point of the unregistered-refuses
    rule is that choosing a policy is a visible act.
    """
    set_engine(AllowAllEngine())


def reset_for_tests() -> None:
    set_engine(None)
