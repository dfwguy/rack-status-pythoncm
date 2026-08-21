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

from flask import g, request, session

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
    """``(principal, auth_method)`` from the session — never from a header (FR-9).

    ``X-Rack-Operator`` is self-reported display metadata and is deliberately not consulted: a
    principal a caller can choose turns the ledger into decoration. Phase 1 has no user identity in
    the session yet, so an authenticated request is still ``anonymous`` with its auth method
    recorded; Phase 2 substitutes a real subject without changing the record's shape.
    """
    try:
        authed = session.get("rack_web_auth") == "1"
        subject = str(session.get("rack_web_user") or "").strip()
    except RuntimeError:  # outside a request context
        return "anonymous", "none"
    if subject:
        return subject, "session"
    return ("anonymous", "session" if authed else "none")


def _resource(path: str) -> str:
    """The path, without its query string (NFR-11).

    A credential travelling as ``?token=...`` is invisible to key-name redaction, so the query
    string is dropped rather than filtered — there is nothing in it the trail needs that the path
    and the action do not already carry.
    """
    return path


def _cluster() -> str:
    try:
        from catalog import context as catalog_context

        _project, cluster, _platform = catalog_context.resolve()
        return str(getattr(cluster, "id", "") or getattr(cluster, "name", "") or "")
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

    if entry is not None:
        action, variant = entry.action, entry.variant
    elif method in MUTATING:
        # A mutating route with no declaration. The build gate (FR-14b) exists so this cannot
        # happen, but recording it unnamed beats not recording a privileged action at all.
        action, variant = _read_action(request.endpoint or ""), "sync"
        _logger.warning("auditing %s %s with a derived name: no @action declaration",
                        method, path)
    elif sink.AUDIT_READS.resolve():
        action, variant = _read_action(request.endpoint or ""), "sync"
    else:
        return False  # a read, and read auditing is off (FR-12)

    from api import _common

    src_ip, src_ip_trust = _common.client_addr()
    t0 = getattr(g, _TIMER, None)
    principal, auth_method = _principal()

    return audit.emit(
        action=action,
        variant=variant,
        result=_result_for(response.status_code),
        principal=principal,
        auth_method=auth_method,
        resource=_resource(path),
        src_ip=src_ip,
        src_ip_trust=src_ip_trust,
        user_agent=request.headers.get("User-Agent", ""),
        request_id=getattr(g, _REQID, ""),
        cluster=_cluster(),
        latency_ms=None if t0 is None else round((time.perf_counter() - t0) * 1000, 2),
    )
