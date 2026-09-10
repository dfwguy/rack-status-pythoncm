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
"""The request chokepoint: one place that observes every request, exactly once.

``after_request`` is the only Flask hook holding both the outcome and the elapsed time, which
``result`` and ``latency_ms`` need. ``before_request`` has neither; ``teardown_request`` never sees
the response. Verified rather than assumed: ``after_request`` *does* run for a request the auth
gate refuses in ``before_request``, so a refusal costs the same path as a success and FR-10 needs
no separate mechanism.

**The scope is the action registry, not a path prefix.** A route is audited because it declared an
action, which is what makes FR-6 hold — a new endpoint is covered the moment it carries the
declaration the build already requires, with no audit-specific code anywhere. It also means the
scope cannot drift from the vocabulary Phase 3 will bind policy to.

One structural exception: ``/ws/terminal`` is answered by WSGI middleware *above* Flask's router,
so no Flask hook can see it. That is why FR-10b exists and why ``api/terminal.py`` calls
``audit.emit`` directly from its refusal branch. Two call sites is the honest minimum; a third
would mean this chokepoint had failed.

Traces to FR-6, FR-9, FR-10, FR-12, NFR-2, NFR-11, NFR-13.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from flask import g, request

import audit
from audit import actions as _actions
from audit import sink

_logger = logging.getLogger("rack_web.audit_hook")

#: Paths that must cost nothing at all (NFR-2). A page load is roughly 33 asset requests, so the
#: check has to come before any settings read, any stat and any registry lookup — which is why it
#: is a prefix tuple rather than something cleverer.
STATIC_PREFIXES = ("/assets/", "/spa/", "/static/", "/favicon", "/vite", "/robots.txt")

#: Methods FR-12 audits by default. Reads are excluded unless NVIS_AUDIT_READS is set, because
#: `/api/status` polling would otherwise dominate the record and bury the signal.
MUTATING = frozenset(_actions.MUTATING_METHODS)

_TIMER = "_nvis_audit_t0"
_REQID = "_nvis_audit_reqid"
_TARGET = "_nvis_audit_target"


def name_target(target: str) -> None:
    """Record the *object* this action was about, when the URL cannot express it.

    For almost every route the resolved resource is the whole story: `POST /api/power-off` with
    `rack=p17-r01` acts on `rack.power:p17-r01` and the trail says so. Attachment is the exception,
    and it is the one place where the missing half matters most. `POST /api/policies/ops/attachments`
    resolves to `policy:ops` — the policy — while the principal being granted it arrives in the
    body. So the trail recorded *which policy was attached* and never *to whom*, which is the only
    question an attachment audit exists to answer (AC-15g).

    Deliberately not solved by making the resolver return the subject as a second target. That
    reads tidier and would be a silent authorization change: `resolve_all` authorizes **every**
    target, so naming the principal there would start requiring the caller to hold authority over
    that principal as well as over the policy. That may even be the better rule — granting somebody
    a policy is arguably acting on them — but it is a policy decision that would refuse callers who
    are permitted today, and it must be made deliberately rather than as a side effect of improving
    an audit field.

    The field is **absent unless a handler names one**, rather than an empty string on every event.
    `EVENT_FIELDS` is a pinned contract (`test_audit_event.py::TestTC6EventSchema`) and `target` is
    not part of it; an optional extra is exactly what `build_event(**extra)` is for. Same reasoning
    as the OIDC claim release, where an unset claim is omitted rather than sent blank.
    """
    cleaned = str(target or "").strip()
    if not cleaned:
        return
    try:
        setattr(g, _TARGET, cleaned)
    except RuntimeError:  # outside a request context; nothing to annotate
        pass


def _target() -> dict[str, str]:
    """The `target` extra, or nothing at all."""
    try:
        named = str(getattr(g, _TARGET, "") or "").strip()
    except RuntimeError:
        return {}
    return {"target": named} if named else {}


def is_static(path: str) -> bool:
    return path.startswith(STATIC_PREFIXES)


def _result_for(status: int) -> str:
    if status in (401, 403):
        return audit.Result.REFUSED
    if status >= 500:
        return audit.Result.ERROR
    if status >= 400:
        return audit.Result.FAILURE
    return audit.Result.SUCCESS


def _principal() -> tuple[str, str]:
    """``(principal, auth_method)`` from the verified token — never from a header (FR-9, FR-16).

    ``X-Rack-Operator`` is self-reported display metadata and is deliberately not consulted: a
    principal a caller can choose turns the ledger into decoration.

    Phase 2 fills this in for real. The value is read from ``g``, where the auth gate put the
    subject of the access token it had *already verified* — not re-derived here. One verification
    per request means the trail cannot disagree with the gate about who the caller was, which is the
    property that makes an audit record usable as evidence rather than as a second opinion.

    ``anonymous`` remains correct for a request that reached a bypassed route or ran with the gate
    disabled, and the ``auth_method`` says which.
    """
    try:
        subject = str(getattr(g, "auth_principal", "") or "").strip()
        method = str(getattr(g, "auth_method", "") or "").strip()
    except RuntimeError:  # outside a request context
        return "anonymous", "none"
    if subject:
        return subject, method or "token"
    return "anonymous", "none"


def _authz() -> tuple[str, str]:
    """``(decision, policy_engine)`` for this request.

    Absent for a request the gate never authorised — a bypassed route, or a disabled gate — and the
    record then keeps Phase 1's honest ``not-evaluated``/``none`` rather than implying an engine
    weighed in.
    """
    try:
        decision = getattr(g, "authz_decision", None)
    except RuntimeError:
        return audit.Decision.NOT_EVALUATED, audit.POLICY_ENGINE_NONE
    if decision is None:
        return audit.Decision.NOT_EVALUATED, audit.POLICY_ENGINE_NONE
    return (
        audit.Decision.ALLOW if decision.allowed else audit.Decision.DENY,
        decision.engine,
    )


def _resource(path: str) -> str:
    """The resource the authorization gate judged, falling back to the request path.

    **Prefer the resolved resource, because the path is not the thing acted on.** The gate resolves
    a `RESOURCE_MODEL` identifier — `rack.power:p17-r01` — and until now computed it and threw it
    away, so the trail recorded `/api/power-off` for every rack in the fleet. A reader asking "what
    ran on p17-r01" had to infer the answer from a body the trail does not keep, and a policy
    attachment recorded `/api/policies/ops/attachments` without naming the principal it granted.

    A fan-out records the target the verdict was about: the refused one when refused, else the
    first, with the full set alongside it. Recording only one member of a multi-target request was
    the *reason* the all-of bypass was invisible for so long.

    The path remains the fallback for a route the resolver does not cover — reads outside the
    governed set, the SPA shell — and the query string is still dropped rather than filtered
    (NFR-11): a credential travelling as `?token=...` is invisible to key-name redaction, and there
    is nothing in a query the trail needs that the path and the action do not already carry.
    """
    resolved = str(getattr(g, "authz_resource", "") or "").strip()
    if not resolved:
        return path

    targets = getattr(g, "authz_resources", ()) or ()
    if len(targets) > 1:
        # `+N more` rather than the whole list: the trail is one line per event, and a 64-rack
        # campaign would otherwise bury every other field. The count is what tells a reader this
        # was a fan-out at all.
        return f"{resolved} (+{len(targets) - 1} more)"
    return resolved


#: Cluster label, keyed on the catalog store's generation counter. See _cluster().
_cluster_cache: dict[str, Any] = {"gen": -1, "label": ""}


def _cluster() -> str:
    """The active cluster's id, cached until the catalog changes.

    Resolving this per request cost 4.74 ms of the chokepoint's 5.22 ms, because
    ``catalog_context.resolve()`` reads the TinyDB store -- seven table reads and their JSON parses
    -- and it was doing that on every audited request to attach a label that only changes when an
    operator switches the active project. That alone blew NFR-2a's 5 ms budget, and TC-34 is what
    surfaced it.

    Keyed on ``store.current_generation()``, which exists for exactly this and is bumped on every
    catalog write, so a project switch is picked up on the next request without a TTL to tune.
    ``catalog.context.overlays_for_active`` already uses the same pattern.
    """
    try:
        from catalog import context as catalog_context
        from catalog.store import current_generation

        gen = current_generation()
        if _cluster_cache["gen"] == gen:
            return str(_cluster_cache["label"])
        _project, cluster, _platform = catalog_context.resolve()
        label = str(getattr(cluster, "id", "") or getattr(cluster, "name", "") or "")
        _cluster_cache.update({"gen": gen, "label": label})
        return label
    except Exception:  # noqa: BLE001 - the trail must not depend on the catalog being healthy
        return ""


def _read_action(endpoint: str) -> str:
    """A derived name for an opt-in read event.

    Reads carry no declared action, because the vocabulary covers the mutating surface that Phase 3
    will govern. When read auditing is switched on the event still needs a name, so one is derived
    from the blueprint — and it is deliberately shaped so a reader can tell it apart from the 89
    declared actions rather than mistaking it for one.
    """
    service = endpoint.split(".", 1)[0] if "." in endpoint else (endpoint or "app")
    return f"{service}:read"


def install(app: Any) -> None:
    """Register the timer and the emitting hook on *app*.

    **Registration order is load-bearing.** Flask runs ``after_request`` handlers in reverse
    registration order, so registering this first makes it run *last* — after the security-headers
    hook — which is what lets it observe the fully decorated response rather than a partial one.
    Call this before the other ``after_request`` handlers are declared.
    """

    @app.before_request
    def _audit_timer():
        # An independent timestamp, deliberately not the lake middleware's ``g._lake_t0``: an audit
        # field whose correctness depends on an unrelated optional feature is a field nobody can
        # trust (Resolved Design Point 2).
        if not is_static(request.path):
            setattr(g, _TIMER, time.perf_counter())
            setattr(g, _REQID, request.headers.get("X-Request-ID") or uuid.uuid4().hex)

    @app.after_request
    def _audit_emit(response):
        try:
            _record(response)
        except Exception:
            _logger.debug("audit hook swallowed an error", exc_info=True)
        return response


def _record(response) -> bool:
    """Build and emit the event for the request in flight. Returns whether one was written."""
    path = request.path or ""
    # NFR-2 / AC-17: assets short-circuit before any settings read, stat or registry lookup.
    if is_static(path):
        return False
    if not sink.AUDIT_ENABLED.resolve():
        return False

    method = (request.method or "").upper()
    entry = _actions.resolve(request.endpoint or "", method)

    # Two separate questions, and they were one until reads entered the vocabulary in Phase 3.
    #
    # `entry is not None` used to imply "audit this", because only mutations were declared. Now that
    # ~118 read routes are declared so policy can govern them, that implication would make every
    # `/api/status` poll an audit row and silently override `NVIS_AUDIT_READS=0` — burying the
    # signal the trail exists to carry. So: the METHOD decides whether to audit, and the registry
    # only decides what the thing is called.
    always = entry is not None and entry.action in _actions.ALWAYS_AUDITED
    if method not in MUTATING and not sink.AUDIT_READS.resolve() and not always:
        return False  # a read, and read auditing is off (FR-12)

    if entry is not None:
        action, variant = entry.action, entry.variant
    else:
        action, variant = _read_action(request.endpoint or ""), "sync"
        if method in MUTATING:
            # A mutating route with no declaration. The build gate (FR-14b) exists so this cannot
            # happen, but recording it unnamed beats not recording a privileged action at all.
            _logger.warning("auditing %s %s with a derived name: no @action declaration",
                            method, path)

    from api import _common

    src_ip, src_ip_trust = _common.client_addr()
    t0 = getattr(g, _TIMER, None)
    principal, auth_method = _principal()
    decision, policy_engine = _authz()

    return audit.emit(
        action=action,
        variant=variant,
        result=_result_for(response.status_code),
        principal=principal,
        auth_method=auth_method,
        decision=decision,
        policy_engine=policy_engine,
        resource=_resource(path),
        src_ip=src_ip,
        src_ip_trust=src_ip_trust,
        user_agent=request.headers.get("User-Agent", ""),
        request_id=getattr(g, _REQID, ""),
        cluster=_cluster(),
        latency_ms=None if t0 is None else round((time.perf_counter() - t0) * 1000, 2),
        **_target(),
    )
