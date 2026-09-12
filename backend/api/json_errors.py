# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Every API error is JSON -- including the ones nobody wrote a handler for.

Author: Rafael M. Koike & Cursor Agent ("Ada")

**Not a Blueprint.** Error handlers are not routes, so this module exposes ``install(app)``, the
loader's escape hatch for things Flask routing cannot express (see ``backend/api/__init__.py``).
It adds no URL, which is why the route snapshot is unchanged.

Why this exists
---------------
The product's convention is already "an error is JSON with ``ok: false``" -- 100 sites answer a
reported failure with JSON carrying status 500, against 14 using 502, and
``test_endpoint_smoke._assert_did_not_crash`` asserts that *any* 5xx has a JSON body. The single
documented exception is the "Frontend not built" 503, which is HTML because its audience is a
person in a browser.

What was missing was the last resort. No error handler was registered anywhere in the backend, so
an exception nobody anticipated fell through to **Flask's default HTML 500 page** -- and the SPA
calls ``res.json()`` on every reply, so the operator saw a JSON parse error instead of the cause.

That is not hypothetical. Running outside demo mode without a head node configured,
``GET /api/status`` raised ``RuntimeError: No SSH host is configured. Point RACK_SSH_HOST at the
head-node hostname or IP.`` from ``transport/ssh.ssh_argv``; the browser got 265 bytes of HTML and
reported a parse failure. The message was perfectly actionable and the UI could not show it.

The smoke test anticipated exactly this and says so: it is "what would catch the crash if TESTING
were ever turned off". In production TESTING *is* off, and nothing caught it. This module is that
missing half.

Two decisions worth keeping
---------------------------
**Scoped to callers that want JSON.** Browser-facing paths must keep rendering HTML, or the SPA
shell and the deliberate "Frontend not built" 503 would start answering machine-readable errors to
a human. So the handler converts ``/api/``, ``/oidc/`` and ``/.well-known/`` (plus a caller that
explicitly asks for JSON and not HTML), and leaves every other path to Flask.

**Propagation is preserved where it is load-bearing.** Registering a handler for ``Exception``
normally defeats Flask's own re-raise under ``TESTING``/``DEBUG``, and that re-raise is real test
signal: a genuine crash currently fails a test on its traceback, which is how the ``{"steps": 1}``
``TypeError`` in ``api/workflows.py`` was found. Swallowing it into a tidy 500 would have made
``_assert_did_not_crash`` pass for exactly the bug it exists to catch. So the handler re-raises
whenever Flask would have, and the behaviour under test is reached by turning propagation off
explicitly -- which is what ``test_api_json_errors.py`` does.
"""

from __future__ import annotations

import logging

from flask import current_app, jsonify, request
from transport import NotConfigured
from werkzeug.exceptions import HTTPException

_logger = logging.getLogger(__name__)

#: Paths whose callers are programs, not people. ``/oidc/`` and ``/.well-known/`` are included
#: because a conforming OIDC client parses every response as JSON, so an HTML error there is as
#: broken as it is in the SPA.
JSON_PATH_PREFIXES = ("/api/", "/oidc/", "/.well-known/")


def wants_json() -> bool:
    """Should a failure on this request be reported as JSON?

    Path first, because it is unambiguous. The ``Accept`` fallback deliberately requires JSON to
    be preferred *and* HTML not to be accepted: a browser sends ``*/*``, which would otherwise
    make every page a JSON candidate.
    """
    if (request.path or "").startswith(JSON_PATH_PREFIXES):
        return True
    accept = request.accept_mimetypes
    return accept.best == "application/json" and not accept.accept_html


def _flask_would_propagate() -> bool:
    """Flask's own rule for re-raising, reimplemented because our handler bypasses it.

    ``Flask.handle_exception`` consults ``PROPAGATE_EXCEPTIONS`` (defaulting to testing/debug) and
    re-raises, but registering a handler for ``Exception`` means control never reaches it. Copying
    the rule keeps "does a crash raise?" answered the same way with and without this module.
    """
    configured = current_app.config.get("PROPAGATE_EXCEPTIONS")
    if configured is None:
        return bool(current_app.testing or current_app.debug)
    return bool(configured)


def _scrubbed(text: str) -> str:
    """Remove known secret values from a message before it leaves the process.

    An exception message is arbitrary text from anywhere in the process, so it is not a safe
    channel by construction. ``logging_config`` already owns which values are sensitive; failing
    open here would be worse than the leak it guards, so a resolution failure returns the text
    unchanged and is logged rather than swallowed silently.
    """
    try:
        import logging_config

        return logging_config.scrub_text(text)
    except Exception:
        _logger.warning("json_errors: secret scrubbing unavailable", exc_info=True)
        return text


def _status_for(exc: Exception) -> int:
    """503 for "this deployment cannot do that yet", 500 for a genuine fault.

    Worth the distinction rather than answering 500 for everything. Walking every GET route on
    a deployment with no head node produced **21** endpoints reporting ``NotConfigured`` -- all
    of them a configuration state, none of them a bug -- and a monitoring system or a UI cannot
    tell those apart from a crash if both are 500. 503 is also what the product already returns
    for expected unavailability (``api/_common.history_unavailable_response``).
    """
    return 503 if isinstance(exc, NotConfigured) else 500


def install(app) -> None:  # type: ignore[no-untyped-def]
    """Register the JSON error handlers on *app*."""

    @app.errorhandler(HTTPException)
    def _json_http_error(exc: HTTPException):
        """Report an HTTP error as JSON for API callers.

        Covers the ones Flask raises itself and that no route sees: 405 on a wrong method, 400 on
        a malformed JSON body, 413, 415. Those rendered as HTML too, and broke the SPA the same
        way an unhandled exception did.
        """
        if not wants_json():
            return exc.get_response()
        # `description` is the human-readable half of werkzeug's own errors, and is what an
        # `abort(400, "...")` call site supplies.
        return jsonify({
            "ok": False,
            "error": exc.description or exc.name,
            "status": exc.code,
        }), (exc.code or 500)

    @app.errorhandler(Exception)
    def _json_unhandled_error(exc: Exception):
        """Last resort: an exception nobody anticipated, as JSON rather than an HTML page."""
        # A more specific handler normally wins, but Flask routes an HTTPException here when it is
        # raised from inside another handler, so do not let one become a 500.
        if isinstance(exc, HTTPException):
            return _json_http_error(exc)

        _logger.exception("unhandled exception on %s %s", request.method, request.path)

        if not wants_json() or _flask_would_propagate():
            raise exc

        # The type and message, never the traceback: the message is usually the fix ("Point
        # RACK_SSH_HOST at the head-node hostname"), while a traceback in a response body is an
        # information leak that helps no operator. The traceback is in the log above.
        return jsonify({
            "ok": False,
            "error": _scrubbed(f"{type(exc).__name__}: {exc}"),
        }), _status_for(exc)

    @app.errorhandler(NotConfigured)
    def _json_not_configured(exc: NotConfigured):
        """A deployment that has not been told where to execute is unavailable, not broken.

        Registered separately from the catch-all so the *log* level differs too: this is an
        expected state on a half-configured deployment, and a full traceback per request on a
        polled endpoint buries everything else in the log.
        """
        if not wants_json() or _flask_would_propagate():
            raise exc
        _logger.warning("%s %s: %s", request.method, request.path, exc)
        return jsonify({"ok": False, "error": str(exc), "status": 503}), 503
