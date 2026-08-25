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
"""OpenAPI 3.1 description of the `/api/*` surface, generated from the running app.

Generated, never hand-written. There are 163 routes; a hand-maintained spec would be wrong
within a day of the first merge, and a wrong spec is worse than none because integrators trust
it. Everything here is derived from Flask's own ``url_map`` plus the view functions, so the
document cannot describe an endpoint that does not exist, and a new endpoint appears in it
without anyone remembering to update a file.

What is derived, and how honest each part is:

* **paths, methods, path parameters** — exact, from ``url_map``.
* **summary / description** — the view's docstring. This repo writes real docstrings, so this is
  the most valuable part of the document.
* **query parameters** — read out of the function source by finding ``request.args.get("x")``.
  Accurate for what the handler actually reads, and it will miss a parameter fetched indirectly;
  such a parameter is marked nowhere rather than guessed at.
* **request body** — declared as a free-form JSON object when the handler calls
  ``request.get_json``. The bodies are not schema'd: they are heterogeneous and a fabricated
  schema would be a lie an integrator would code against.
* **responses** — 200 plus any explicit status literal returned in the handler (``, 404``).

The result is deliberately a *map of the surface*, not a contract for every payload. Consumers
get every route, its verbs, its query parameters and its documented purpose; where we do not
genuinely know a shape, the document says ``object`` instead of inventing one.
"""
from __future__ import annotations

import inspect
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

OPENAPI_VERSION = "3.1.0"

#: Path prefixes worth publishing. The SPA's own page routes and static assets are not an API.
INCLUDED_PREFIXES = ("/api/",)

#: Flask converter -> (JSON type, format). Anything unlisted degrades to a plain string.
_CONVERTER_TYPES: dict[str, tuple[str, str | None]] = {
    "string": ("string", None),
    "int": ("integer", None),
    "float": ("number", None),
    "path": ("string", "path"),
    "uuid": ("string", "uuid"),
    "default": ("string", None),
}

_ARGS_GET_RE = re.compile(r"""request\.args\.get\(\s*["']([A-Za-z0-9_\-]+)["']""")
_ARGS_INDEX_RE = re.compile(r"""request\.args\[\s*["']([A-Za-z0-9_\-]+)["']""")
#: Status codes are read off `return ..., <code>` lines only, and must be a real HTTP status.
#: A bare "three digits after a comma" pattern also matches `timeout=900` and put a `900`
#: response on /api/universal/scan, which a real OpenAPI validator rejects — response keys must
#: match `^[1-5][0-9]{2}$`. Structural self-tests did not catch it; the validator did.
_STATUS_RE = re.compile(r""",\s*(\d{3})\s*\)?\s*$""")
_HTTP_STATUSES = frozenset({
    "200", "201", "202", "204", "206",
    "301", "302", "303", "304", "307", "308",
    "400", "401", "402", "403", "404", "405", "406", "408", "409", "410", "413", "415", "422",
    "423", "429",
    "500", "501", "502", "503", "504", "507",
})
_JSON_BODY_RE = re.compile(r"request\.get_json|request\.json\b")

#: Tag descriptions, so the grouping means something to someone who did not write the app.
#: A prefix with no entry still gets a tag — it just has no prose.
TAG_DESCRIPTIONS: dict[str, str] = {
    "analytics": "Fleet-wide aggregate analysis of NCCL and NeMo results.",
    "catalog": "Platforms, tools and projects/clusters — what the product can run, and where.",
    "cluster-overview": "Cluster-level summary facts and live section refresh.",
    "node-report": "Customer-schema validation report (Nscale) generation and collection.",
    "rack-locks": "Advisory rack claims: who is operating on which rack.",
    "validation-activities": "Durable registry of validation-family launches and their tasks.",
    "workflows": "Workflow definitions and runs — also the general long-job runner.",
    "workflow-runs": "State, output and cancellation for a workflow run.",
    "observability": "Grafana panel embeds and metric queries.",
    "bringup": "Bring-up fleet state and per-node test verdicts.",
}


def _summary_and_description(func: Any) -> tuple[str, str]:
    """First docstring line as the summary, the remainder as the description."""
    doc = inspect.getdoc(func) or ""
    if not doc:
        return "", ""
    lines = [line.rstrip() for line in doc.strip().splitlines()]
    summary = lines[0].strip()
    rest = "\n".join(lines[1:]).strip()
    return summary, rest


def _source_of(func: Any) -> str:
    try:
        return inspect.getsource(func)
    except (OSError, TypeError):
        return ""


def _query_parameters(source: str) -> list[dict[str, Any]]:
    """Query parameters the handler actually reads.

    Derived rather than declared, so it cannot claim a parameter the code ignores. Both
    ``request.args.get("x")`` and ``request.args["x"]`` are recognised; the second form is
    required, since indexing raises when absent.
    """
    optional = dict.fromkeys(_ARGS_GET_RE.findall(source))
    required = dict.fromkeys(_ARGS_INDEX_RE.findall(source))
    out = []
    for name in {**optional, **required}:
        out.append({
            "name": name,
            "in": "query",
            "required": name in required,
            "schema": {"type": "string"},
        })
    return sorted(out, key=lambda p: p["name"])


def _path_parameters(rule: Any) -> list[dict[str, Any]]:
    params = []
    for name in sorted(rule.arguments):
        # werkzeug exposes no public accessor for a rule's converters.
        converter = rule._converters.get(name)
        kind = type(converter).__name__.replace("Converter", "").lower() if converter else "default"
        json_type, fmt = _CONVERTER_TYPES.get(kind, _CONVERTER_TYPES["default"])
        schema: dict[str, Any] = {"type": json_type}
        if fmt:
            schema["format"] = fmt
        params.append({"name": name, "in": "path", "required": True, "schema": schema})
    return params


def _responses(source: str, has_doc: bool) -> dict[str, Any]:
    """200 plus every explicit status the handler returns.

    Only `return` lines are considered, and only real HTTP statuses are accepted, so a numeric
    literal that happens to sit after a comma cannot become a response code.
    """
    codes = {"200"}
    for line in source.splitlines():
        if "return" not in line:
            continue
        for code in _STATUS_RE.findall(line.rstrip()):
            if code in _HTTP_STATUSES:
                codes.add(code)
    out: dict[str, Any] = {}
    for code in sorted(codes):
        out[code] = {
            "description": _STATUS_TEXT.get(code, "See the endpoint description."),
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    if not has_doc:
        out["200"]["description"] = "Success (endpoint has no docstring)."
    return out


_STATUS_TEXT = {
    "200": "Success.",
    "202": "Accepted; work continues in the background.",
    "400": "Malformed request.",
    "401": "Authentication required.",
    "403": "Forbidden.",
    "404": "Not found, or the feature is disabled on this deployment.",
    "409": "Conflict — not ready, or a stale identifier.",
    "500": "Server error.",
    "503": "A dependency of this endpoint is unavailable.",
    "504": "Upstream timed out.",
}


#: Domain buckets for endpoints that do not group with anything else. Checked as substrings in
#: order, so `hw-check-presets` lands in diagnostics. Grouping by raw path segment alone produced
#: 106 tags for 201 operations — a tag per endpoint, which is no grouping at all.
_DOMAIN_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("bug-report", "diagnostics"),
    ("ds-diag", "diagnostics"),
    ("hw-check", "diagnostics"),
    ("sysinfo", "diagnostics"),
    ("fabric", "diagnostics"),
    ("nvlink", "diagnostics"),
    ("export-logs", "diagnostics"),
    ("parser", "results"),
    ("results", "results"),
    ("logs", "results"),
    ("report", "reports"),
    ("gpu-reset", "remediation"),
    ("bmc", "remediation"),
    ("power", "remediation"),
    ("reboot", "remediation"),
    ("drain", "remediation"),
    ("grafana", "observability"),
    ("metric", "observability"),
    ("login", "auth"),
    ("logout", "auth"),
    ("auth", "auth"),
    ("terminal", "terminal"),
    ("run-", "launch"),
    ("validation", "validation"),
    ("rack", "racks"),
    ("node", "cluster"),
    ("cluster", "cluster"),
)


def _segment_of(path: str) -> str:
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    return parts[1] if len(parts) >= 2 and parts[0] == "api" else (parts[0] if parts else "misc")


def _base_of(segment: str) -> str:
    """`gpu-reset-async` and `gpu-reset-status` are one endpoint family, so they share a base."""
    for suffix in ("-async", "-status", "-start", "-result", "-prepare"):
        if segment.endswith(suffix):
            return segment[: -len(suffix)]
    return segment


def _tag_for(path: str, family_counts: dict[str, int]) -> str:
    """A tag that groups: the endpoint family when it has siblings, else a domain bucket."""
    segment = _segment_of(path)
    base = _base_of(segment)
    if family_counts.get(base, 0) >= 2:
        return base
    for keyword, tag in _DOMAIN_KEYWORDS:
        if keyword in segment:
            return tag
    return "general"


def _flask_path_to_openapi(rule_path: str) -> str:
    """``/api/x/<int:job>`` -> ``/api/x/{job}``."""
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", rule_path)


def build_spec(app: Any, *, version: str = "", server_url: str = "") -> dict[str, Any]:
    """The OpenAPI document for *app*."""
    paths: dict[str, dict[str, Any]] = {}
    tags_seen: set[str] = set()

    # First pass: how many operations share each endpoint family, so a family with siblings gets
    # its own tag and a lone endpoint falls into a domain bucket instead of inventing one.
    family_counts: dict[str, int] = {}
    for rule in app.url_map.iter_rules():
        path = str(rule.rule)
        if path.startswith(INCLUDED_PREFIXES):
            base = _base_of(_segment_of(path))
            family_counts[base] = family_counts.get(base, 0) + 1

    for rule in app.url_map.iter_rules():
        path = str(rule.rule)
        if not path.startswith(INCLUDED_PREFIXES):
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue

        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        if not methods:
            continue

        summary, description = _summary_and_description(view)
        source = _source_of(view)
        query = _query_parameters(source)
        path_params = _path_parameters(rule)
        tag = _tag_for(path, family_counts)
        tags_seen.add(tag)

        openapi_path = _flask_path_to_openapi(path)
        entry = paths.setdefault(openapi_path, {})
        for method in methods:
            operation: dict[str, Any] = {
                "operationId": (
                    f"{method.lower()}_"
                    f"{re.sub(r'[^a-zA-Z0-9]+', '_', openapi_path).strip('_')}"
                ),
                "tags": [tag],
                "summary": summary or rule.endpoint.replace("_", " "),
                "responses": _responses(source, bool(summary)),
            }
            if description:
                operation["description"] = description
            params = path_params + (query if method == "GET" else [])
            if params:
                operation["parameters"] = params
            if method in ("POST", "PUT", "PATCH") and _JSON_BODY_RE.search(source):
                operation["requestBody"] = {
                    "required": False,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                    "description": (
                        "Free-form JSON. Bodies vary per endpoint and are described in the "
                        "endpoint description rather than schema'd, so an integrator is not "
                        "given a fabricated contract."
                    ),
                }
            entry[method.lower()] = operation

    spec: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "NVIS Rack Status API",
            "version": version or "0",
            "summary": "Cluster bring-up, validation and observability for GB200/GB300 fleets.",
            "description": (
                "Generated from the running application's route table, so it describes exactly "
                "the endpoints this deployment serves.\n\n"
                "Query parameters are those the handler actually reads. Request bodies are "
                "declared as free-form JSON: they are heterogeneous across endpoints, and a "
                "fabricated schema would be a contract we do not honour — read the endpoint "
                "description instead.\n\n"
                "Most endpoints sit behind the dashboard session gate; authenticate at "
                "`POST /api/login` first."
            ),
        },
        "servers": [{"url": server_url or "/", "description": "This deployment"}],
        "tags": [
            {"name": name, **({"description": TAG_DESCRIPTIONS[name]} if name in TAG_DESCRIPTIONS else {})}
            for name in sorted(tags_seen)
        ],
        "paths": dict(sorted(paths.items())),
        "components": {
            "securitySchemes": {
                "sessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "session",
                    "description": "Flask session cookie obtained from POST /api/login.",
                }
            }
        },
        "security": [{"sessionCookie": []}],
    }
    return spec


def spec_stats(spec: dict[str, Any]) -> dict[str, int]:
    """Counts worth logging or asserting on: paths, operations, documented operations."""
    operations = 0
    documented = 0
    for methods in (spec.get("paths") or {}).values():
        for operation in methods.values():
            operations += 1
            if operation.get("description") or operation.get("summary"):
                documented += 1
    return {
        "paths": len(spec.get("paths") or {}),
        "operations": operations,
        "documented": documented,
        "tags": len(spec.get("tags") or []),
    }
