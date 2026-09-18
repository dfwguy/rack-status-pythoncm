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

import re
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
        values = self.all_of(*names)
        return values[0] if values else None

    def all_of(self, *names: str) -> tuple[str, ...]:
        """**Every** target this request names, in order, de-duplicated.

        `first()` exists for the single-target case and for a caller that only needs a label. This
        is what authorization must use, because judging a fan-out by its first element is an
        authorization bypass: an operator granted `rack.power:p17-r01` could post
        `racks: ["p17-r01", "p17-r02"]` and act on both, with the gate having examined only the
        first. Measured, not theorised — the verdict came back `allow`.

        The previous defence was that the multirack dispatchers refuse a group that is not a clean
        partition of the reviewed target. That is a real control and it is not this one: it checks
        the *selection* is coherent, never that the caller holds a grant for each member.

        Order is preserved and duplicates dropped so a refusal can name the offending target
        without repeating it, and so `["a01", "a01"]` costs one evaluation rather than two.
        """
        for source in (self.view_args, self.query, self.body):
            for name in names:
                values = _as_identifiers(source.get(name))
                if values:
                    # **The FIRST matching key only, expanded.** `names` is a fallback chain, not a
                    # set of aliases for one thing, and treating it as a set was a real bug: the
                    # `policy` resolver reads `("policy_id", "id", "subject")`, so sweeping every
                    # key turned a detach request into two targets — the policy and the principal
                    # `user:alice`, whose colon the id grammar then refused. Every detach 400ed.
                    #
                    # Expanding one key is still exactly what closes the fan-out bypass, because a
                    # multi-target request names its targets in ONE key (`racks: [a01, a02]`), not
                    # by spreading them across the chain.
                    return values
        return ()


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
#: **`create` is the one mutation in this set, and it is a deliberate exception rather than a
#: loosening of the rule above.** A create that *names* its target still resolves to the instance —
#: `identity.group:create` with `name: ops` gives `identity.group:ops`, so "may create the ops
#: group" stays expressible. Only a create that names nothing falls back, and for that request the
#: collection is the honest answer: asking which platform a "create a platform" call targets has no
#: answer, because the id is derived server-side from the display name. `s3:CreateBucket` names the
#: bucket for the same reason.
#:
#: It does not weaken the warning above. `rack.power:off` is absent from this set, so a power-off
#: naming no rack is still refused — a create acting on "the collection" adds one member, whereas a
#: power-off on "the collection" would act on the fleet.
#:
#: Masked until 2026-09-08: the catalog resolvers read `name` from the body, so a create resolved to
#: `catalog.platform:Test Plat` — a display name used as an identifier. Removing `name` turned that
#: into a 400 and surfaced the gap.
COLLECTION_FALLBACK_VERBS = frozenset({"read", "list", "create"})

#: Exact `<type>:<verb>` pairs whose target genuinely IS the whole set, listed one at a time.
#:
#: **An allowlist of exact actions rather than more verbs**, because the question is never "is
#: `generate` safe" — it is "does *this* operation have an instance to name". Widening a verb answers
#: for every type that shares it, and the entry that matters is the one nobody thought about. Every
#: member here cites the reason it has no target, taken from the view rather than assumed.
#:
#: The bar: acting on "the collection" must mean *the set as one object* — one new member, one
#: document about all of them — and never *each member in turn*. `rack.power:off` can never appear
#: here: powering off the collection is powering off the fleet.
COLLECTION_FALLBACK_ACTIONS: frozenset[str] = frozenset({
    # `/api/node-report/start` is a fleet sweep by construction — its own docstring says "a second
    # fleet-wide sweep" over "~2000 nodes". There is no per-node target to name.
    "report.node:generate",
    # `/api/workflows/generate-mermaid` is a pure transformation: it turns a step list from the
    # request body into mermaid text and reads or writes no stored workflow, so there is no
    # instance for it to name. Checked against the view, which touches only `body["steps"]`.
    "workflow:generate",
    # `POST /api/workflows` creates a workflow whose id the server assigns — the test reads it back
    # from the response — so the request cannot name it. An *update* carries its id in the path and
    # still resolves to that instance, because the fallback only fires when nothing is nameable.
    "workflow:save",
    # `GET /api/audit-trail/export` serialises whatever the *filter* matched. There is no instance
    # to name -- the export IS the collection, narrowed -- so it takes the collection resource.
    # Listed here rather than by adding `export` to COLLECTION_FALLBACK_VERBS, because that would
    # make every future `<type>:export` collection-level by default, including on a type where an
    # export of one instance is the sensible reading.
    "audit.trail:export",
    # `GET /api/catalog/projects/<doc_id>/scripts` answers two things at once: which layer of the
    # script tree each of *this project's* scripts resolves from, and the deployment-wide layout
    # (tree root, layers, var dir) that decides it. The second half is the collection as one
    # object, which is what a request naming no project is asking for.
    #
    # In practice the fallback does not fire -- `doc_id` is in the path on the only route that
    # declares this action -- so it is a safety net for the case the parameter is ever renamed,
    # which is precisely the regression `test_every_read_action_resolves` was written after. The
    # *write* (`catalog.project:bootstrap_scripts`) is deliberately absent: composing a directory
    # acts on one project, and "all of them in turn" is exactly what this list must never permit.
    "catalog.project:read_scripts",
})


def _as_identifiers(raw: object) -> tuple[str, ...]:
    """The usable identifiers in one request value, in order, de-duplicated.

    Shared by `first()` and `all_of()` so the two cannot disagree about what counts as an id — a
    divergence there would mean the gate authorized a different set than it reported.
    """
    candidates = raw if isinstance(raw, list | tuple) else [raw]
    found: list[str] = []
    for value in candidates:
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
        elif isinstance(value, int | float) and not isinstance(value, bool):
            cleaned = str(value)
        else:
            continue
        if cleaned not in found:
            found.append(cleaned)
    return tuple(found)


def _singleton(_facts: RequestFacts) -> str:
    return SINGLETON


def _by(*names: str) -> Extractor:
    """An extractor reading the first of `names` that the request supplies.

    The names are recorded on the returned callable as `.keys`, so `resolve_all` can ask the *same*
    declaration which keys to sweep for multiple targets. The alternative — a second table mapping
    type to keys — is two sources of truth for one fact, and the copy that drifts is the one that
    decides authorization.
    """
    def extract(facts: RequestFacts) -> str | None:
        return facts.first(*names)

    extract.keys = names  # type: ignore[attr-defined]
    return extract


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
    "scheduler.job": _by("jid", "job_id"),
    "scheduler.node": _by("node", "nodes", "host"),
    "report": _by("report_id", "id"),
    "report.node": _by("node", "host"),
    # -- catalog documents, keyed by operator-chosen slug. `doc_id` is the URL parameter the
    #    catalog routes actually use; omitting it made every per-document read resolve to the
    #    collection instead of the instance, silently disabling per-document grants.
    # `name` is deliberately NOT a key here. It is a human display name — "Test Plat", spaces and
    # all — and using it as a resource id meant a policy would have to name
    # `catalog.platform:Test Plat`, which is not an identifier. It also made a *create* resolve to
    # the name in the request body, when a create has no instance yet and belongs to the
    # collection. Surfaced by the FR-4 grammar refusing the space; the modelling error predates it.
    "catalog.platform": _by("doc_id", "id", "platform"),
    "catalog.project": _by("doc_id", "id", "project"),
    "catalog.tool": _by("doc_id", "id", "tool"),
    # -- validation runs are scoped to the rack they occupy: the rack is the contended thing, and a
    #    grant to run validation on rack 7 must not be a grant to run it on rack 8.
    # `nodes` and `node` are read as well as the rack keys: a preset launched with
    # `scope: "node"` names its target in `nodes` and nothing else, so without them a legitimate
    # node-scoped launch was refused as unnameable. `first()` takes the head of a list, so a
    # multi-node launch authorizes against its first node — narrower than the collection, and the
    # dispatcher already refuses a target that is not a clean partition of the reviewed selection.
    "validation": _by("rack", "rack_id", "racks", "nodes", "node"),
    "tools": _by("rack", "rack_id", "racks"),
    # -- one instance per deployment. Scoping these would invent an id nobody could write a policy
    #    against, and `settings:default` reads better in an audit line than a bare type.
    "about": _singleton,
    "ai": _singleton,
    "analytics": _singleton,
    "auth": _singleton,
    "auth.password": _singleton,
    "auth.srp": _singleton,
    "catalog": _singleton,
    # Importing a shared board is an instance-level act: the question is "may this principal
    # apply a peer's board here", not "may they apply it to board X". The per-item questions
    # *are* scoped, but they are asked against the resource being written
    # (`catalog.tool:<id>`, `workflow:<id>`) inside the handler, so they reuse those types
    # rather than needing one of their own.
    "catalog.board": _singleton,
    "cluster": _singleton,
    "comments": _singleton,
    # One documentation site per deployment; there is no id to narrow a grant to.
    "docs": _singleton,
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
    # A policy is itself a governable resource: "may edit the rack-operator policy" has to be
    # expressible, and it is the one resource where a grant is meta -- whoever can edit policies
    # can grant themselves anything, so it needs to be nameable individually rather than only as
    # `policy:*`.
    "policy": _by("policy_id", "id", "subject"),
    "audit.deep": _by("run_id", "id"),
    # The security trail. Distinct from `audit.deep`, which is NVSwitch hardware auditing and
    # shares nothing with it — two unrelated meanings under one prefix, so the names must not
    # be allowed to converge (see the reasoning on the `audit.trail:*` action names).
    "audit.trail": _by("event_id", "id"),
    "history": _by("run_id", "id"),
    "metrics": _by("chart_id", "id", "metric"),
    "observability": _by("view", "dashboard", "id"),
    "help": _by("content_id", "id"),
    "export": _singleton,
    "job": _singleton,
    "openapi": _singleton,
    "scheduler.queue": _singleton,
}


#: Resource types whose handlers canonicalise the id to lower case before acting on it, so the
#: resolver must too (FR-4, AC-5).
#:
#: **This is a real bypass, not hygiene.** The gate judges the string the caller sent; the handler
#: then folds it. Measured 2026-09-08 against `rack.power`: with the commonest policy shape — a
#: broad allow plus a narrow deny — a request for `P17-R01` is judged as `rack.power:P17-R01`,
#: which `fnmatchcase` does not match against `deny rack.power:p17-r01`, and is then acted on by
#: `_normalize_rack_id` as `p17-r01`. The deny is evaded and the denied rack is powered off.
#:
#: Folded here rather than refused because the handler provably folds, so folding makes the two
#: agree exactly. The types *absent* from this set keep their case deliberately: `help` content ids
#: are camelCase (`singleRack`, `rackTable`), and lower-casing them would refuse every help read
#: while fixing nothing — their handlers do not fold either, so gate and handler already agree.
_FOLDED_TYPES = frozenset({
    # `_normalize_rack_id` -> `canonical_rack_id`, which lower-cases.
    "rack", "rack.power", "rack.gpu", "rack.bmc", "rack.services", "rack.lock",
    "fabric", "fabric.imex", "fabric.nvswitch", "diag", "bringup", "node.issues",
    "validation", "tools",
    # `canonical_login` / `canonical_group_name`, which case-fold.
    "identity.user", "identity.group",
    # Hostnames. DNS is case-insensitive, so two nodes cannot differ only in case and folding
    # cannot collapse two distinct resources into one.
    "scheduler.node", "report.node",
})

#: Characters a resource id may never contain, and the reason each one is here.
#:
#: `*?[]` are `fnmatch` metacharacters: a caller-supplied `*` is matched *literally* against a
#: policy pattern rather than acting as a wildcard, so it cannot widen a grant by itself — but an
#: id that matches a pattern by accident is a grant nobody wrote, and there is no legitimate id
#: containing one. `:` would re-split the `<type>:<id>` rendering into a different type. `/`, `\`
#: and `..` are path shapes, and several ids reach a filesystem or an `scp` target downstream.
#: Whitespace cannot appear mid-identifier in any of our namespaces, and a control character in an
#: audit line is a log-injection primitive.
_FORBIDDEN_IN_ID = re.compile(r"[*?\[\]:/\\\s]|\.\.|[\x00-\x1f\x7f]")


def clean_identifier(resource_type: str, raw: str) -> str:
    """Validate a caller-supplied id, and canonicalise it the way its handler will (FR-4).

    Raises :class:`Unresolvable`, which the gate answers as a 400 — the same treatment as an id it
    could not find at all, because in both cases we cannot name the resource the request targets
    and must not guess.
    """
    if _FORBIDDEN_IN_ID.search(raw):
        raise Unresolvable(
            f"invalid {resource_type} identifier: a resource id may not contain "
            "whitespace, ':', '/', '\\\\', '..', or the wildcard characters * ? [ ]"
        )
    return raw.lower() if resource_type in _FOLDED_TYPES else raw


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
        if verb in COLLECTION_FALLBACK_VERBS or action in COLLECTION_FALLBACK_ACTIONS:
            # A read that names no instance is a read over the collection. See
            # COLLECTION_FALLBACK_VERBS for why this is narrower, not broader.
            return f"{resource_type}:{COLLECTION}"
        raise Unresolvable(f"could not determine which {resource_type} this request targets")

    return f"{resource_type}:{clean_identifier(resource_type, identifier)}"


#: A request may not name more targets than this.
#:
#: All-of evaluation is linear in the number of targets, so an unbounded list is a way to spend the
#: server's CPU from an authenticated session — 2,000 racks is 2,000 evaluations before the handler
#: is even reached. The ceiling is well above any real launch: the largest reviewed campaign in this
#: product is a few dozen racks, and a fleet-wide operation names nothing and resolves to the
#: collection instead. Exceeding it is refused as a malformed request, not silently truncated,
#: because truncation is how the bypass this bound protects would come back.
MAX_TARGETS = 256


def resolve_all(action: str, facts: RequestFacts) -> tuple[str, ...]:
    """Every resource this request acts on. **This is what the gate must authorize against.**

    `resolve()` returns one resource, which is right for a label and wrong for a decision: a
    request naming several targets was judged by its first, so a grant for one rack authorized an
    operation on two. Verified before fixing — the verdict was `allow` and the second rack had no
    grant at all.

    All-of, deliberately: every named target must be permitted, and one refusal refuses the
    request. Any-of would be the same bypass with extra steps, and "authorize the first and let the
    handler sort it out" is what was already happening.

    A collection resolution (`<type>:_all`) is returned as a single element, because a request that
    names nothing has exactly one target — the set — and multiplying that out would be meaningless.
    """
    resource_type, _, verb = action.partition(":")

    if verb in COLLECTION_VERBS:
        # Unchanged: the collection is one resource however many members it has.
        return (resolve(action, facts),)

    extractor = _RESOLVERS.get(resource_type)
    if extractor is None or extractor is _singleton:
        # A singleton has a fixed id, and a missing resolver must refuse — `resolve` says both
        # better than a duplicate of its logic would.
        return (resolve(action, facts),)

    keys = getattr(extractor, "keys", ())
    if not keys:
        # A hand-written extractor with no declared keys cannot be swept for multiple targets.
        # Falling back to the single resolution is correct rather than lax: such an extractor reads
        # one specific place, so there is no list for it to have missed.
        return (resolve(action, facts),)

    identifiers = facts.all_of(*keys)
    if len(identifiers) > MAX_TARGETS:
        raise Unresolvable(
            f"this request names {len(identifiers)} targets; the limit is {MAX_TARGETS}. "
            f"Split it, or omit the target to act on the collection if the action supports it."
        )
    if len(identifiers) <= 1:
        # Nothing named, or exactly one: `resolve` already handles the fallbacks and the refusal.
        return (resolve(action, facts),)

    return tuple(
        f"{resource_type}:{clean_identifier(resource_type, identifier)}"
        for identifier in identifiers
    )
