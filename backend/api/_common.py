"""Flask-dependent helpers shared across API modules.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``core.py`` deliberately imports no Flask, so anything that builds a response cannot live there.
This module can, and the loader skips it: underscore-prefixed modules are helpers, not endpoints.

Importable from ``app.py`` too, without a cycle -- ``app`` already imports the ``api`` package, and
nothing under ``api`` imports ``app``.
"""

from __future__ import annotations

from flask import jsonify


def history_unavailable_response():
    """The 503 returned when the run-history store is absent or disabled."""
    return jsonify({
        "ok": False,
        "error": "run history is unavailable (results_history not installed or disabled)",
    }), 503
