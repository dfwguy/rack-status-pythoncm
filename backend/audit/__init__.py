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
"""The operator audit trail: the event schema, ``emit()``, and the flood guard.

``actions`` owns the vocabulary an event is named with; ``sink`` owns the durable file. This module
owns the event itself and the one control that stops the trail eating itself.

**The flood guard is a security control, not an optimisation.** FR-10 says refusals are recorded,
and against the bounded budget of FR-7 that creates the mirror image of a known cloud failure: a
denial loop evicts the very history it should be recorded in. The same invisibility as an S3
cross-region replication denial discovered only through the bill, in a different currency.

Its semantics were pinned after the test-plan verifier found four cases contradicting each other:
**individual until the threshold, then collapse.** The first T refusals for a key are written
immediately, so a lone refusal — a ``/ws/terminal`` rejection, say — is visible when it happens
rather than at process exit. Only the collapsed remainder is deferred, flushed on the next
``emit()`` for that key plus an ``atexit`` hook. A buffer-from-first reading would have made a
single refusal unobservable until the process ended, and unobservable *at all* for a container
stopped with SIGTERM, since ``atexit`` does not run then.

Traces to FR-5, FR-9, FR-10, FR-10a, FR-12, NFR-10, NFR-11, NFR-13.
"""

from __future__ import annotations

import atexit
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from audit import sink
from audit.actions import action, build_registry, registry, resolve

__all__ = [
    "Decision",
    "Result",
    "action",
    "build_registry",
    "configure",
    "emit",
    "flush",
    "registry",
    "resolve",
]


class Result:
    """What became of the action (FR-10)."""

    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"
    REFUSED = "refused"


class Decision:
    """The authorization verdict.

    ``NOT_EVALUATED`` was the only value Phase 1 could honestly record: writing ``allow`` would have
    asserted that a policy engine permitted the action, which was false while none existed, and a
    record that overstates its own authority is worse than one that admits its limit (NFR-13,
    threat T-52).

    Phase 2 registers an engine, so ``allow`` and ``deny`` are now reachable — but the claim is only
    as strong as the ``policy_engine`` beside it. ``allow`` from ``allow-all`` means "an explicitly
    permissive policy was consulted and said yes", not "a policy authorised this". Keep the two
    fields together when reading a record; either alone is misleading.
    """

    NOT_EVALUATED = "not-evaluated"
    ALLOW = "allow"
    DENY = "deny"


#: No policy engine exists yet, and the field says so rather than being omitted, so the record
#: shape does not change when Phase 3 wires one in.
POLICY_ENGINE_NONE = "none"

#: The FR-5 field set, plus ``policy_engine`` from NFR-13. Fixed here so Phase 2 and Phase 3 enrich
#: values without reshaping the record — a trail whose schema drifts cannot be compared over time.
EVENT_FIELDS = (
    "event_id", "ts", "principal", "auth_method", "service", "action", "variant", "resource",
    "decision", "policy_engine", "result", "src_ip", "src_ip_trust", "user_agent", "request_id",
    "cluster", "latency_ms",
)

_lock = threading.Lock()
#: key -> [collapsed_count, window_start_monotonic, template_event, seen_in_window]
#:
#: ``seen`` counts every refusal in the window, ``collapsed`` only those past the threshold. Both
#: are needed: ``seen`` decides whether to write individually, ``collapsed`` is the summary's count.
_flood: dict[tuple[str, str, str], list[Any]] = {}
_atexit_registered = False


def configure(*, force: bool = False) -> bool:
    """Install the audit sink. Thin passthrough so callers need one import."""
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(flush)
        _atexit_registered = True
    return sink.configure(force=force)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_event(
    *,
    action: str,
    result: str,
    principal: str = "anonymous",
    auth_method: str = "none",
    variant: str = "sync",
    resource: str = "",
    src_ip: str = "",
    src_ip_trust: str = "peer",
    user_agent: str = "",
    request_id: str = "",
    cluster: str = "",
    latency_ms: float | None = None,
    decision: str = Decision.NOT_EVALUATED,
    policy_engine: str = POLICY_ENGINE_NONE,
    service: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Assemble one event with the exact FR-5 field set, plus any explicit extras.

    ``service`` is derived from the action name rather than from the blueprint, so it survives a
    module being renamed or a route moving between files (FR-14).
    """
    event = {
        "event_id": str(uuid.uuid4()),
        "ts": _now_iso(),
        "principal": principal,
        "auth_method": auth_method,
        "service": service if service is not None else action.split(":", 1)[0],
        "action": action,
        "variant": variant,
        "resource": resource,
        "decision": decision,
        # Was hard-coded to POLICY_ENGINE_NONE, correctly, while no engine existed. Now a parameter
        # so the record names which evaluator produced the verdict: `allow` from `allow-all` and
        # `allow` from a real policy are very different claims, and a reader must be able to tell
        # them apart without knowing which release they are looking at (NFR-13, threat T-52).
        "policy_engine": policy_engine,
        "result": result,
        "src_ip": src_ip,
        "src_ip_trust": src_ip_trust,
        "user_agent": user_agent,
        "request_id": request_id,
        "cluster": cluster,
        "latency_ms": latency_ms,
    }
    event.update(extra)
    return event


def _flood_key(event: dict[str, Any]) -> tuple[str, str, str]:
    """What makes two refusals "identical" (AC-14c).

    Principal, action and reason. Refusals differing in any of the three are distinct events and
    must not be collapsed together, or a single noisy client would mask a different one.
    """
    return (
        str(event.get("principal", "")),
        str(event.get("action", "")),
        str(event.get("reason", event.get("result", ""))),
    )


def _summary_event(template: dict[str, Any], count: int) -> dict[str, Any]:
    summary = dict(template)
    summary["event_id"] = str(uuid.uuid4())
    summary["ts"] = _now_iso()
    summary["count"] = count
    summary["collapsed"] = True
    return summary


def flush() -> int:
    """Write any pending flood summaries. Called on the next emit for a key, and at exit.

    Returns the number of summary events written, which is what lets a test assert the collapse
    happened rather than infer it from a file read that may race the flush.
    """
    written = 0
    with _lock:
        for key, state in list(_flood.items()):
            if state[0] > 0 and sink.write(_summary_event(state[2], state[0])):
                written += 1
            # Drop the key either way. A key whose window held only individually-written refusals
            # has nothing to summarise but should not linger: one entry per distinct
            # (principal, action, reason) triple is small, but it is still unbounded growth.
            _flood.pop(key, None)
    return written


def emit(event: dict[str, Any] | None = None, **kwargs: Any) -> bool:
    """Record one audit event. Never raises; returns whether something was written.

    Refusals pass through the flood guard: the first T for a key go straight to the trail, and
    beyond that they accumulate into one counted summary (FR-10a).
    """
    try:
        ev = event if event is not None else build_event(**kwargs)
        if ev.get("result") != Result.REFUSED:
            return sink.write(ev)

        key = _flood_key(ev)
        threshold = max(1, int(sink.AUDIT_FLOOD_THRESHOLD.resolve()))
        window = max(1, int(sink.AUDIT_FLOOD_WINDOW_SEC.resolve()))
        now = time.monotonic()

        with _lock:
            state = _flood.get(key)
            if state is not None and (now - state[1]) > window:
                # The window closed. Flush what accumulated before starting a new one, so a burst
                # that straddles a boundary is not silently merged into the next.
                if state[0] > 0:
                    sink.write(_summary_event(state[2], state[0]))
                state = None
                _flood.pop(key, None)

            if state is None:
                # First occurrence in this window. seen counts individually-written refusals.
                _flood[key] = [0, now, ev, 1]
                return sink.write(ev)

            state[3] += 1
            if state[3] <= threshold:
                return sink.write(ev)  # still under the threshold: write it individually
            state[0] += 1               # over it: collapse into the pending summary
            return True
    except Exception:  # noqa: BLE001 - audit is evidence, never a gate (NFR-1)
        return False


def reset_for_tests() -> None:
    with _lock:
        _flood.clear()
    sink.reset_for_tests()
