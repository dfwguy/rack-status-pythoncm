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
"""The declared action vocabulary: what an audit record calls the thing that happened.

An audit key must not be a refactorable identifier. The obvious choice was ``request.endpoint``,
and it was wrong for a specific reason: ``test_route_parity.py`` snapshots URL paths and methods,
**not** endpoint names, so renaming a view function would keep every URL identical, pass the whole
suite, and silently rewrite the ``action`` string in every future audit record. For a trail whose
value is longitudinal, that severs history from the present with no signal anywhere.

So an action is declared, in the form ``service[.subservice]:verb`` — ``rack.power:on``,
``fabric.nvswitch:reset_sdn``, ``auth:login``. The name is code-adjacent and versioned with the
code that implements it; the *policy* attached to that name is deployment-adjacent and will live in
catalog documents (Phase 3), which keeps the two on the axes they actually change along.

**Two endpoints may share an action, and that is the point.** Many routes are one operation reached
by a different transport: seven of the fourteen ``tools`` endpoints are ``-stream`` twins of the
other seven, and ``gpu-reset`` has an ``-async`` sibling. Giving those distinct action names would
force Phase 3 to grant the same capability twice, and granting the synchronous route while
forgetting the streaming one is an authorization bypass that reads as a complete policy. They share
one action and differ by ``variant``, which makes that class of gap unexpressible rather than
merely unlikely.

Traces to FR-14, FR-14a, FR-14b, FR-14c of ``.plc/briefs/auth-audit-logging-brief.md``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

#: ``service[.subservice]:verb``. The dotted sub-service is part of the service, which gives Phase 3
#: a prefix to write one rule against everything under ``fabric.nvswitch``.
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?:[a-z][a-z0-9_]*$")

#: HTTP methods whose routes must carry a declaration (FR-14b) and which are audited by default
#: (FR-12).
MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")

#: Recognised transports. Validated rather than free-form on purpose: an unrecognised variant would
#: look like a *different* variant to the duplicate check below, so a typo would silently defeat the
#: one guard that catches an accidentally shared action name.
VARIANT_SYNC = "sync"
VARIANT_ASYNC = "async"
VARIANT_STREAM = "stream"
VARIANT_LEGACY = "legacy"
VARIANT_FORM = "form"
VARIANTS = (VARIANT_SYNC, VARIANT_ASYNC, VARIANT_STREAM, VARIANT_LEGACY, VARIANT_FORM)

#: Attribute the decorator leaves on the view function. It cannot record the endpoint name itself:
#: at decoration time the function has not been registered on a blueprint, so its endpoint does not
#: exist yet. Resolution therefore happens in :func:`build_registry`, after registration.
_DECLS = "__nvis_action_decls__"

F = TypeVar("F", bound=Callable[..., Any])


class ActionRegistryError(RuntimeError):
    """A malformed or colliding declaration. Raised at startup, never logged and continued.

    A collision discovered by reading audit records is discovered after the evidence is already
    ambiguous, which is too late to be worth anything (FR-14a).
    """


@dataclass(frozen=True)
class Declaration:
    action: str
    variant: str = VARIANT_SYNC


@dataclass(frozen=True)
class Entry:
    """One resolved ``(endpoint, method)`` pair and the action it names."""

    endpoint: str
    method: str
    rule: str
    action: str
    variant: str

    @property
    def service(self) -> str:
        return service_of(self.action)

    @property
    def verb(self) -> str:
        return verb_of(self.action)


def service_of(action: str) -> str:
    """Everything left of the colon, sub-service included."""
    return action.split(":", 1)[0]


def verb_of(action: str) -> str:
    return action.split(":", 1)[1] if ":" in action else ""


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def action(name: str | None = None, *, variant: str = VARIANT_SYNC, **per_method: str) -> Callable[[F], F]:
    """Declare the action a route performs.

    Applied directly beneath ``@bp.route``::

        @bp.route("/gpu-reset-async", methods=["POST"])
        @action("rack.gpu:reset", variant="async")
        def api_gpu_reset_async(): ...

    Of 103 endpoints serving a mutating method, exactly two serve more than one, so a single name
    per route is right for 101 of them and the per-method form is an escape hatch rather than the
    general shape::

        @action(put="comments:update", delete="comments:delete")

    The two forms do not mix: a route either has one action or one per method, and accepting both
    at once would leave the precedence between them for a reader to guess.
    """
    if name is not None and per_method:
        raise ActionRegistryError(
            f"action({name!r}) also passed per-method names {sorted(per_method)}; declare either "
            "one action for the route or one per method, not both"
        )
    if name is None and not per_method:
        raise ActionRegistryError("action() needs an action name, or one per method")

    if name is not None:
        decls: dict[str | None, Declaration] = {None: Declaration(name, variant)}
    else:
        decls = {m.upper(): Declaration(a, variant) for m, a in per_method.items()}

    def decorate(fn: F) -> F:
        existing: dict[str | None, Declaration] = dict(getattr(fn, _DECLS, {}))
        for key, decl in decls.items():
            if key in existing and existing[key] != decl:
                raise ActionRegistryError(
                    f"{fn.__name__} already declares {existing[key]} for {key or 'all methods'}; "
                    f"refusing to replace it with {decl}"
                )
            existing[key] = decl
        setattr(fn, _DECLS, existing)
        return fn

    return decorate


def declarations_of(fn: Callable[..., Any]) -> dict[str | None, Declaration]:
    return dict(getattr(fn, _DECLS, {}))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Registry:
    """Resolved ``(endpoint, method)`` → action for one Flask application."""

    def __init__(self, entries: tuple[Entry, ...]) -> None:
        self._entries = entries
        self._by_key = {(e.endpoint, e.method): e for e in entries}

    @property
    def entries(self) -> tuple[Entry, ...]:
        return self._entries

    def resolve(self, endpoint: str, method: str) -> Entry | None:
        return self._by_key.get((endpoint, (method or "").upper()))

    def by_action(self) -> dict[str, tuple[Entry, ...]]:
        """action → every route that implements it.

        The inverse is what makes "no action is reachable by only a subset of its routes"
        checkable, which is the property that keeps a transport twin from being governed on one
        leg and ungoverned on the other (AC-29b).
        """
        out: dict[str, list[Entry]] = {}
        for e in self._entries:
            out.setdefault(e.action, []).append(e)
        return {k: tuple(v) for k, v in out.items()}

    def actions(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_action()))

    def services(self) -> tuple[str, ...]:
        return tuple(sorted({service_of(a) for a in self.by_action()}))


_registry: Registry | None = None


def registry() -> Registry:
    if _registry is None:
        raise ActionRegistryError("build_registry(app) has not run yet")
    return _registry


def resolve(endpoint: str, method: str) -> Entry | None:
    """Convenience resolver against the process registry, for the request chokepoint."""
    return None if _registry is None else _registry.resolve(endpoint, method)


def _iter_mutating_rules(app: Any):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in sorted(set(rule.methods or ()) & set(MUTATING_METHODS)):
            yield rule, method


def build_registry(app: Any, *, install: bool = True) -> Registry:
    """Resolve and validate every declaration on *app*, after blueprints are registered.

    Raises :class:`ActionRegistryError` on a malformed name, an unrecognised variant, or a repeated
    ``(action, variant)`` pair — at startup, so an ambiguous vocabulary cannot reach a running
    deployment (FR-14a).

    Coverage is deliberately *not* enforced here. FR-14b makes a missing declaration fail the
    **build**, which is a test's job (see :func:`undeclared_mutating_routes`); making it fail
    startup instead would mean a half-migrated tree could not boot at all, and the declarations
    land over 38 modules.
    """
    global _registry

    entries: list[Entry] = []
    claimed: dict[tuple[str, str], Entry] = {}

    for rule, method in _iter_mutating_rules(app):
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        decls = declarations_of(view)
        decl = decls.get(method) or decls.get(None)
        if decl is None:
            continue

        if not ACTION_RE.match(decl.action):
            raise ActionRegistryError(
                f"{rule.endpoint} declares action {decl.action!r}, which does not match "
                f"service[.subservice]:verb ({ACTION_RE.pattern})"
            )
        if decl.variant not in VARIANTS:
            raise ActionRegistryError(
                f"{rule.endpoint} declares unknown variant {decl.variant!r}; known: {list(VARIANTS)}"
            )

        entry = Entry(rule.endpoint, method, str(rule), decl.action, decl.variant)
        key = (decl.action, decl.variant)
        prior = claimed.get(key)
        if prior is not None and prior.endpoint != entry.endpoint:
            raise ActionRegistryError(
                f"{entry.endpoint} and {prior.endpoint} both claim action {decl.action!r} with "
                f"variant {decl.variant!r}. Two routes may share an action only when their "
                f"variants differ — that is how a transport twin stays one capability."
            )
        claimed.setdefault(key, entry)
        entries.append(entry)

    built = Registry(tuple(entries))
    if install:
        _registry = built
    return built


def undeclared_mutating_routes(app: Any) -> tuple[tuple[str, str, str], ...]:
    """``(endpoint, method, rule)`` for every mutating route with no declaration.

    The coverage gate of FR-14b. A route added without an action would be invisible to Phase 3
    policy, so a capability governed on one route would be ungoverned on another — which is a
    coverage failure rather than an attack on the name schema, and is why it fails the build.
    """
    missing: list[tuple[str, str, str]] = []
    for rule, method in _iter_mutating_rules(app):
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        decls = declarations_of(view)
        if decls.get(method) is None and decls.get(None) is None:
            missing.append((rule.endpoint, method, str(rule)))
    return tuple(sorted(missing))


def reset_for_tests() -> None:
    global _registry
    _registry = None
