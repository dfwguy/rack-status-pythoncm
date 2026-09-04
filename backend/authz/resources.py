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
"""Turning a request into the resource a policy can name (FR-5, FR-6).

THE BUG THIS EXISTS TO FIX
--------------------------
The gate passed `request.path` as the resource — a URL, `/api/rack-power/on`. Policies are written
against `docs/RESOURCE_MODEL.md` identifiers, `rack.power:a01`. So every resource-scoped statement
matched nothing, every request fell through to default-deny, and **the failure was silent**: with no
policy file the engine was permissive, so nothing looked wrong. A resource-scoped policy would have
appeared to work right up until it was the only thing standing between an operator and a rack.

Worse than a resolver that is missing is one that guesses. `rack.power:*` matches
`rack.power:<anything>`, so a placeholder for "could not work out which rack" is matched by every
wildcard grant an operator writes — a resolution failure would silently become a *grant*. That is
why an unresolvable scoped resource is refused before evaluation rather than handed to the engine
with a filler id.

HOW A RESOURCE IS BUILT
-----------------------
`<type>:<id>`, where the **type is the action's own namespace** — `rack.power:on` is an action on a
`rack.power`, and taking the type from anywhere else would let the two disagree. Only the id has to
be found, and where to find it is *declared* per type, never sniffed:

    SCOPED     an id is required. Not found -> `Unresolvable`, and the gate refuses.
    SINGLETON  one instance per deployment; the id is fixed, so `settings:default`.

The declaration is exhaustive and `test_authz_resource_coverage.py` fails the build if an action's
namespace has no entry. That check is the point: a new endpoint whose type nobody declared would
otherwise ship, and the first person to notice would be an operator whose policy silently stopped
applying. A build failure naming the type is a five-minute fix; a silent one is an incident.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

#: A resolver reads the request's already-parsed parts. It is handed plain mappings rather than the
#: Flask request, so this module stays importable — and testable — without an app context.
Extractor = Callable[["RequestFacts"], str | None]


class Unresolvable(Exception):
    """A scoped resource whose id could not be determined. The gate turns this into a refusal."""


@dataclass(frozen=True)
class RequestFacts:
    """Everything a resolver may look at, and nothing else.

    Deliberately not the Flask request. A resolver that could reach `request` could reach the
    session, the headers, or `g` — and a resource identity derived from the *caller* rather than the
    *target* is how you get a policy that grants access to whatever the requester says it does.
    """

    view_args: Mapping[str, object]
    query: Mapping[str, str]
    body: Mapping[str, object]

    def first(self, *names: str) -> str | None:
        """The first non-empty value among `names`, checked path → query → body.

        Path before body on purpose: the URL is what the route matched and what the audit log
        records, so when a request carries both, the path is the one that actually selected the
        target.

        **A list yields its first element**, which is what makes the multirack endpoints work:
        `/api/launch/prepare` and the six `racks: [...]` call sites in `validation_runs.py` name
        several racks in one request. Taking the first is deliberately a *narrowing* — the resource
        that gets authorized is one of the named racks, never a wildcard — and the multirack
        dispatchers already refuse a group that is not a clean partition of the reviewed target
        (`dcgm-mndiag-mnubergemm.mdc`), so a caller cannot smuggle an extra rack past a grant for
        the first one. Discovered by turning real authentication on: every multirack launch test
        answered 403 because this only looked for the singular `rack`.
        """
        for source in (self.view_args, self.query, self.body):
            for name in names:
                value = source.get(name)
                if isinstance(value, list | tuple) and value:
                    value = value[0]
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, int | float) and not isinstance(value, bool):
                    return str(value)
        return None


SINGLETON = "default"

#: The id of a *collection*, as distinct from any instance in it. A `:list` verb resolves here.
#:
#: A collection genuinely is a different resource from its members — `s3:ListBucket` acts on the
#: bucket, `s3:GetObject` on the object — and pretending otherwise is what makes reads unresolvable:
#: 103 of the 118 read routes carry no id at all, so a `<type>:<id>` resolver would refuse every
#: one of them.
#:
#: The leading underscore is load-bearing. Cluster ids are site slugs and catalog ids are
#: operator-chosen names; neither can begin with `_`, so this cannot collide with a real instance.
#: `<type>:*` does match it, which is correct: "may read all racks" should include listing them.
#: A grant of one specific rack does **not** confer listing, which is right but worth knowing —
#: a UI showing the rack table needs `rack:_all` or a wildcard, not `rack:a01`.
COLLECTION = "_all"

#: Verbs that act on a collection rather than an instance. Verb-driven rather than a per-type flag
#: because the same type has both — `catalog.platform:list` and `catalog.platform:read`.
COLLECTION_VERBS = frozenset({"list"})

#: Verbs that may fall back to the collection when no instance is named, and **only** these.
#:
#: Many read routes take their target from a query parameter (`?jid=`, `?node=`, `?rack=`) and some
#: have no target at all — `/api/metrics/health` is a deployment-level probe. Refusing those would
#: mean 403 on a health check, so an unnamed read is treated as a read *over the collection*, which
#: is the honest description of the request.
#:
#: This is safe because `<type>:_all` is a **separate resource that must be granted separately**: a
#: grant of `rack:a01` does not match it, so the fallback is strictly narrower than any invented
#: instance id. It must never extend to a mutation — a power-on with no rack named would become
#: "power on the collection", which is the single worst thing this file could do. `resolve()`
#: therefore keys off the verb, and `test_a_mutation_never_falls_back_to_the_collection` pins it.
COLLECTION_FALLBACK_VERBS = frozenset({"read", "list"})


def _singleton(_facts: RequestFacts) -> str:
    return SINGLETON


def _by(*names: str) -> Extractor:
    return lambda facts: facts.first(*names)


# The declaration. One row per resource type in `audit.actions`, which is what the build gate
# checks. A type is SCOPED only where instances are genuinely separately governable — the whole
# point of a resource in a policy is to be able to write a rule about *one* of them.
_RESOLVERS: dict[str, Extractor] = {
    # -- racks and the hardware under them. `<rack>` is a URL parameter on 19 routes.
    "rack": _by("rack", "rack_id", "racks"),
    "rack.power": _by("rack", "rack_id", "racks"),
    "rack.gpu": _by("rack", "rack_id", "racks"),
    "rack.bmc": _by("rack", "rack_id", "racks"),
    "rack.services": _by("rack", "rack_id", "racks"),
    "rack.lock": _by("rack", "rack_id", "racks"),
    "fabric": _by("rack", "rack_id", "racks"),
    "fabric.imex": _by("rack", "rack_id", "racks"),
    "fabric.nvswitch": _by("rack", "rack_id", "racks"),
    "diag": _by("rack", "rack_id", "racks"),
    "bringup": _by("rack", "rack_id", "racks"),
    "node.issues": _by("rack", "rack_id", "racks"),
    # -- identity. Note the resource rendering, `identity.user:rkoike`, which is NOT the principal
    #    rendering `user:rkoike`; a policy about who may administer users needs the first while the
    #    actor is the second (RESOURCE_MODEL.md).
    "identity.user": _by("login", "user"),
    "identity.group": _by("name", "group"),
    # -- work with its own durable id. `run_id` included because a workflow *run* is reached at
    #    /api/workflow-runs/<run_id>: without it the run reads resolved to the collection.
    "workflow": _by("workflow_id", "run_id", "id"),
    "validation.activity": _by("activity_id", "id"),
    "slurm.job": _by("jid", "job_id"),
    "slurm.node": _by("node", "nodes", "host"),
    "report": _by("report_id", "id"),
    "report.node": _by("node", "host"),
    # -- catalog documents, keyed by operator-chosen slug. `doc_id` is the URL parameter the
    #    catalog routes actually use; omitting it made every per-document read resolve to the
    #    collection instead of the instance, silently disabling per-document grants.
    "catalog.platform": _by("doc_id", "id", "name", "platform"),
    "catalog.project": _by("doc_id", "id", "name", "project"),
    "catalog.tool": _by("doc_id", "id", "name", "tool"),
    # -- validation runs are scoped to the rack they occupy: the rack is the contended thing, and a
    #    grant to run validation on rack 7 must not be a grant to run it on rack 8.
    "validation": _by("rack", "rack_id", "racks"),
    "tools": _by("rack", "rack_id", "racks"),
    # -- one instance per deployment. Scoping these would invent an id nobody could write a policy
    #    against, and `settings:default` reads better in an audit line than a bare type.
    "ai": _singleton,
    "analytics": _singleton,
    "auth": _singleton,
    "auth.password": _singleton,
    "auth.srp": _singleton,
    "catalog": _singleton,
    "cluster": _singleton,
    "comments": _singleton,
    "logs": _singleton,
    "nvftp": _singleton,
    "oidc.token": _singleton,
    "results": _singleton,
    "settings": _singleton,
    "support": _singleton,
    "vault": _singleton,
    # -- types introduced with the read vocabulary (Phase 3). Instance reads that carry an id are
    #    scoped; the rest are one surface per deployment and their `:list` verb resolves to the
    #    collection regardless.
    "audit.deep": _by("run_id", "id"),
    "history": _by("run_id", "id"),
    "metrics": _by("chart_id", "id", "metric"),
    "observability": _by("view", "dashboard", "id"),
    "help": _by("content_id", "id"),
    "export": _singleton,
    "job": _singleton,
    "openapi": _singleton,
    "slurm.queue": _singleton,
}


def declared_types() -> frozenset[str]:
    """What the build gate compares the action registry against."""
    return frozenset(_RESOLVERS)


def is_singleton(resource_type: str) -> bool:
    return _RESOLVERS.get(resource_type) is _singleton


def resolve(action: str, facts: RequestFacts) -> str:
    """`<type>:<id>` for this request, or raise `Unresolvable`.

    Raises rather than returning a filler, because every filler is matched by the `type:*` grant an
    operator would reasonably write. A refusal is recoverable and legible; a silent grant is not.
    """
    resource_type, _, verb = action.partition(":")

    # A collection read needs no id, and must not be refused for lacking one.
    if verb in COLLECTION_VERBS:
        if resource_type not in _RESOLVERS:
            raise Unresolvable(f"no resource resolver declared for {resource_type!r}")
        return f"{resource_type}:{COLLECTION}"

    extractor = _RESOLVERS.get(resource_type)
    if extractor is None:
        # Only reachable if the build gate was bypassed or an action was added without one. Refusing
        # is the sole safe answer: we cannot name the resource, so we cannot know a policy meant to
        # permit it.
        raise Unresolvable(
            f"no resource resolver declared for {resource_type!r} "
            f"(add one in authz/resources.py; the build gate should have caught this)"
        )

    identifier = extractor(facts)
    if not identifier:
        if verb in COLLECTION_FALLBACK_VERBS:
            # A read that names no instance is a read over the collection. See
            # COLLECTION_FALLBACK_VERBS for why this is narrower, not broader.
            return f"{resource_type}:{COLLECTION}"
        raise Unresolvable(f"could not determine which {resource_type} this request targets")

    return f"{resource_type}:{identifier}"
