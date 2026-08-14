# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Flask blueprint for the NVIS catalog (platforms, tools, projects, active context).

Routes (all under ``/api/catalog``)::

    GET                      /overview            platforms + tools + projects + active + vocab
    GET                      /vocab               enum vocabularies for UI dropdowns
    GET    POST              /platforms           list / create-or-update
    GET    PUT    DELETE     /platforms/<id>      fetch / update / delete
    GET    POST              /tools               list (optional ?cluster=) / create-or-update
    GET    PUT    DELETE     /tools/<id>          fetch / update / delete
    GET    POST              /projects            list / create-or-update
    GET    PUT    DELETE     /projects/<id>       fetch / update / delete
    GET    PUT               /active              get / set active {project, cluster}

Validation is done with the model ``validate()`` methods; invalid payloads return 400 with
the list of problems. Mutations call :func:`catalog.context.notify_changed` so the app
re-materializes the active overlay.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from . import context, models
from .store import (
    TABLE_NPI_TOOLS,
    TABLE_PLATFORMS,
    TABLE_PROJECTS,
    TABLE_TOOLS,
    get_repo,
)

_logger = logging.getLogger(__name__)

catalog_bp = Blueprint("catalog", __name__, url_prefix="/api/catalog")


def _err(message: str | list[str], code: int = 400):
    problems = message if isinstance(message, list) else [message]
    return jsonify({"ok": False, "errors": problems}), code


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Generic CRUD helper bound to a model class + table
# ---------------------------------------------------------------------------


def _crud_list(table: str, model_cls):
    repo = get_repo()
    rows = [model_cls.from_dict(r).to_dict() for r in repo.list(table)]
    rows.sort(key=lambda d: (d.get("name") or d.get("id") or "").lower())
    return jsonify({"ok": True, "items": rows})


def _crud_get(table: str, model_cls, doc_id: str):
    raw = get_repo().get(table, doc_id)
    if raw is None:
        return _err(f"{table[:-1]} not found: {doc_id}", 404)
    return jsonify({"ok": True, "item": model_cls.from_dict(raw).to_dict()})


def _crud_upsert(table: str, model_cls, *, doc_id: str | None = None):
    body = _body()
    if doc_id:
        body = {**body, "id": doc_id}
    if not (body.get("id") or "").strip():
        # Derive an id slug from the name for create-by-POST convenience.
        name = (body.get("name") or "").strip()
        if name:
            body["id"] = models.slugify(name)
    obj = model_cls.from_dict(body)
    problems = obj.validate()
    if problems:
        return _err(problems, 400)
    saved = get_repo().upsert(table, obj.to_dict())
    context.notify_changed()
    return jsonify({"ok": True, "item": model_cls.from_dict(saved).to_dict()})


def _crud_delete(table: str, doc_id: str):
    removed = get_repo().delete(table, doc_id)
    if not removed:
        return _err(f"{table[:-1]} not found: {doc_id}", 404)
    context.notify_changed()
    return jsonify({"ok": True, "deleted": doc_id})


# ---------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------


@catalog_bp.get("/platforms")
def list_platforms():
    return _crud_list(TABLE_PLATFORMS, models.Platform)


@catalog_bp.post("/platforms")
def create_platform():
    return _crud_upsert(TABLE_PLATFORMS, models.Platform)


@catalog_bp.get("/platforms/<doc_id>")
def get_platform(doc_id: str):
    return _crud_get(TABLE_PLATFORMS, models.Platform, doc_id)


@catalog_bp.put("/platforms/<doc_id>")
def update_platform(doc_id: str):
    return _crud_upsert(TABLE_PLATFORMS, models.Platform, doc_id=doc_id)


@catalog_bp.delete("/platforms/<doc_id>")
def delete_platform(doc_id: str):
    return _crud_delete(TABLE_PLATFORMS, doc_id)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@catalog_bp.get("/tools")
def list_tools():
    cluster = (request.args.get("cluster") or "").strip()
    surface = (request.args.get("surface") or "").strip() or None
    if cluster:
        items = context.tools_for_cluster(cluster_id=cluster, surface=surface)
        return jsonify({"ok": True, "items": items})
    return _crud_list(TABLE_TOOLS, models.Tool)


@catalog_bp.post("/tools")
def create_tool():
    return _crud_upsert(TABLE_TOOLS, models.Tool)


@catalog_bp.get("/tools/<doc_id>")
def get_tool(doc_id: str):
    return _crud_get(TABLE_TOOLS, models.Tool, doc_id)


@catalog_bp.put("/tools/<doc_id>")
def update_tool(doc_id: str):
    return _crud_upsert(TABLE_TOOLS, models.Tool, doc_id=doc_id)


@catalog_bp.delete("/tools/<doc_id>")
def delete_tool(doc_id: str):
    return _crud_delete(TABLE_TOOLS, doc_id)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@catalog_bp.get("/projects")
def list_projects():
    return _crud_list(TABLE_PROJECTS, models.Project)


@catalog_bp.post("/projects")
def create_project():
    return _crud_upsert(TABLE_PROJECTS, models.Project)


@catalog_bp.get("/projects/<doc_id>")
def get_project(doc_id: str):
    return _crud_get(TABLE_PROJECTS, models.Project, doc_id)


@catalog_bp.put("/projects/<doc_id>")
def update_project(doc_id: str):
    return _crud_upsert(TABLE_PROJECTS, models.Project, doc_id=doc_id)


@catalog_bp.delete("/projects/<doc_id>")
def delete_project(doc_id: str):
    return _crud_delete(TABLE_PROJECTS, doc_id)


# ---------------------------------------------------------------------------
# NPI reference catalog (imported from the NVIDIA NPI Knowledge Portal)
# ---------------------------------------------------------------------------


@catalog_bp.get("/npi-tools")
def list_npi_tools():
    """List imported NPI reference tools. Filter with ?role= / ?capability= / ?runnable=1."""
    role = (request.args.get("role") or "").strip()
    capability = (request.args.get("capability") or "").strip()
    runnable = (request.args.get("runnable") or "").strip() in ("1", "true", "yes")
    rows = [models.NpiTool.from_dict(r) for r in get_repo().list(TABLE_NPI_TOOLS)]
    if role:
        rows = [t for t in rows if t.portfolio_role == role]
    if capability:
        rows = [t for t in rows if t.capability == capability]
    if runnable:
        rows = [t for t in rows if t.is_runnable_kind()]
    items = [t.to_dict() for t in rows]
    items.sort(key=lambda d: (d.get("name") or d.get("id") or "").lower())
    return jsonify({"ok": True, "items": items, "count": len(items)})


@catalog_bp.get("/npi-tools/<doc_id>")
def get_npi_tool(doc_id: str):
    raw = get_repo().get(TABLE_NPI_TOOLS, doc_id)
    if raw is None:
        return _err(f"npi tool not found: {doc_id}", 404)
    tool = models.NpiTool.from_dict(raw)
    item = tool.to_dict()
    item["derived"] = {
        "category": tool.category(),
        "status": tool.status(),
        "runnable": tool.is_runnable_kind(),
        "tool_stub": tool.to_tool_stub(),
    }
    return jsonify({"ok": True, "item": item})


@catalog_bp.post("/npi-tools/import")
def import_npi_tools():
    """Re-sync the NPI reference catalog from the configured snapshot/source dir."""
    from .npi_import import import_npi_catalog

    body = _body()
    source_dir = (body.get("source_dir") or "").strip() or None
    prune = body.get("prune", True)
    summary = import_npi_catalog(get_repo(), source_dir, prune=bool(prune))
    if not summary.get("source_dir"):
        return _err(
            "no NPI catalog source found (set NVIS_NPI_CATALOG_DIR or run "
            "bin/scripts/fetch_npi_catalog.sh)",
            404,
        )
    context.notify_changed()
    return jsonify({"ok": True, "summary": summary})


# ---------------------------------------------------------------------------
# Active context + vocab + overview
# ---------------------------------------------------------------------------


@catalog_bp.get("/active")
def get_active():
    sel = context.get_active()
    project, cluster, platform = context.resolve()
    return jsonify(
        {
            "ok": True,
            "active": sel,
            "project": project.to_dict() if project else None,
            "cluster": cluster.to_dict() if cluster else None,
            "platform": platform.to_dict() if platform else None,
            "ai_context": context.build_ai_context_block(),
        }
    )


@catalog_bp.put("/active")
def set_active():
    body = _body()
    project_id = (body.get("project") or "").strip()
    cluster_id = (body.get("cluster") or "").strip()
    if not project_id:
        return _err("'project' is required")
    try:
        sel = context.set_active(project_id, cluster_id)
    except ValueError as exc:
        return _err(str(exc), 400)
    return jsonify({"ok": True, "active": sel})


@catalog_bp.get("/topology")
def get_topology():
    """The active cluster's connection topology, as declared and as resolved.

    ``declared`` is what the cluster document says; ``resolved`` is what the
    transport actually built from it (per role: endpoint, hop path, root), which is
    the useful thing when diagnosing "which host did that command run on?". They
    differ when the topology is absent or invalid and the flat env contract is
    serving instead — ``source`` says which.
    """
    _project, cluster, _platform = context.resolve()
    if cluster is None:
        return _err("no active cluster", 404)
    conn = cluster.connection
    declared = {
        "endpoints": [e.__dict__ | {"opts": list(e.opts)} for e in conn.endpoints],
        "roles": [r.__dict__ for r in conn.roles],
    }
    resolved: dict[str, Any] = {}
    try:
        import host_exec

        resolved = {
            "roles": host_exec.describe_roles(),
            "names": host_exec.role_names(),
            "kind": host_exec.exec_kind(),
        }
    except Exception as exc:  # diagnostics must not fail with the transport
        _logger.debug("get_topology: ignoring error", exc_info=True)
        resolved = {"error": str(exc)}
    return jsonify(
        {
            "ok": True,
            "cluster": cluster.id,
            "source": "topology" if conn.endpoints else "environment",
            "declared": declared,
            "resolved": resolved,
            "problems": conn.validate(),
        }
    )


@catalog_bp.put("/topology")
def set_topology():
    """Replace the active cluster's ``endpoints``/``roles`` and rebuild the transport."""
    body = _body()
    _project, cluster, _platform = context.resolve()
    if cluster is None:
        return _err("no active cluster", 404)
    active = context.get_active()
    raw = get_repo().get(TABLE_PROJECTS, active.get("project") or "")
    if raw is None:
        return _err("active project not found", 404)

    updated = models.ClusterConnection.from_dict(
        cluster.connection.to_dict()
        | {"endpoints": body.get("endpoints") or [], "roles": body.get("roles") or []}
    )
    problems = updated.validate()
    if problems:
        return _err(problems, 400)

    project = models.Project.from_dict(raw)
    for c in project.clusters:
        if c.id == cluster.id:
            c.connection = updated
            break
    else:
        return _err(f"cluster not found in project: {cluster.id}", 404)

    get_repo().upsert(TABLE_PROJECTS, project.to_dict())
    context.notify_changed()
    return jsonify({"ok": True, "cluster": cluster.id, "connection": updated.to_dict()})


@catalog_bp.get("/vocab")
def vocab():
    return jsonify(
        {
            "ok": True,
            "endpoint_types": list(models.ENDPOINT_TYPES),
            "connection_roles": list(models.CONNECTION_ROLES),
            "gpu_models": list(models.GPU_MODELS),
            "cpu_vendors": list(models.CPU_VENDORS),
            "system_vendors": list(models.SYSTEM_VENDORS),
            "form_factors": list(models.FORM_FACTORS),
            "cluster_managers": list(models.CLUSTER_MANAGERS),
            "tool_categories": list(models.TOOL_CATEGORIES),
            "exec_modes": list(models.EXEC_MODES),
            "exec_targets": list(models.EXEC_TARGETS),
            "statuses": list(models.STATUSES),
            "npi_portfolio_roles": list(models.NPI_PORTFOLIO_ROLES),
            "npi_capabilities": list(models.NPI_CAPABILITIES),
            "npi_maturities": list(models.NPI_MATURITIES),
        }
    )


@catalog_bp.get("/overview")
def overview():
    repo = get_repo()
    platforms = [models.Platform.from_dict(r).to_dict() for r in repo.list(TABLE_PLATFORMS)]
    tools = [models.Tool.from_dict(r).to_dict() for r in repo.list(TABLE_TOOLS)]
    projects = [models.Project.from_dict(r).to_dict() for r in repo.list(TABLE_PROJECTS)]
    npi_tools = [models.NpiTool.from_dict(r).to_dict() for r in repo.list(TABLE_NPI_TOOLS)]
    return jsonify(
        {
            "ok": True,
            "platforms": sorted(platforms, key=lambda d: d["name"].lower()),
            "tools": sorted(tools, key=lambda d: (d.get("category", ""), d["name"].lower())),
            "projects": sorted(projects, key=lambda d: d["name"].lower()),
            "npi_tools": sorted(npi_tools, key=lambda d: (d.get("name") or d.get("id") or "").lower()),
            "active": context.get_active(),
        }
    )
