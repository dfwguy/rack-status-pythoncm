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
"""The deployment's own OpenAPI 3.1 description, in JSON and YAML.

Named ``openapi_spec`` rather than ``openapi`` on purpose: this module imports the top-level
``openapi`` generator, and a sibling of the same name would be ambiguous to read even where it
resolves.
"""
from __future__ import annotations

import json

import app_version
from flask import Blueprint, Response, current_app, jsonify

# Empty prefix: these routes carry the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("openapi_spec", __name__, url_prefix="")


def _spec() -> dict:
    """Build the document for the *live* app object.

    ``current_app`` is a proxy; the generator walks ``url_map``, so it needs the real app.
    """
    import openapi as _openapi

    return _openapi.build_spec(current_app._get_current_object(), version=app_version.app_version())


@bp.route("/api/openapi.json", methods=["GET"])
def api_openapi_json():
    """OpenAPI 3.1 description of this deployment's `/api/*` surface.

    Generated from the app's own route table on each request, so it describes exactly what this
    build serves and cannot drift from the code. Cheap enough to generate per call (introspection
    over ~163 rules) and always correct, which a cached copy would not be after a hot reload.

    Behind the normal auth gate: it enumerates the whole surface of a tool that operates a
    customer cluster, and any system integrating with it authenticates anyway.
    """
    try:
        spec = _spec()
    except ImportError:
        return jsonify({"ok": False, "error": "openapi generator unavailable"}), 503
    return Response(json.dumps(spec, indent=2), mimetype="application/json")


@bp.route("/api/openapi.yaml", methods=["GET"])
def api_openapi_yaml():
    """The same document as YAML, for tooling that prefers it."""
    try:
        spec = _spec()
    except ImportError:
        return jsonify({"ok": False, "error": "openapi generator unavailable"}), 503
    try:
        import yaml

        body = yaml.safe_dump(spec, sort_keys=False, width=100)
    except ImportError:
        return jsonify({"ok": False, "error": "pyyaml unavailable; use /api/openapi.json"}), 503
    return Response(body, mimetype="application/yaml")
