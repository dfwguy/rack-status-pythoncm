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
import re
from typing import Any

from audit.actions import action
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


@action(get="catalog.platform:list")
@catalog_bp.get("/platforms")
@action(get="catalog.platform:list")
def list_platforms():
    return _crud_list(TABLE_PLATFORMS, models.Platform)


@catalog_bp.post("/platforms")
@action("catalog.platform:create")
def create_platform():
    return _crud_upsert(TABLE_PLATFORMS, models.Platform)


@action(get="catalog.platform:read")
@catalog_bp.get("/platforms/<doc_id>")
@action(get="catalog.platform:read")
def get_platform(doc_id: str):
    return _crud_get(TABLE_PLATFORMS, models.Platform, doc_id)


@catalog_bp.put("/platforms/<doc_id>")
@action("catalog.platform:update")
def update_platform(doc_id: str):
    return _crud_upsert(TABLE_PLATFORMS, models.Platform, doc_id=doc_id)


@catalog_bp.delete("/platforms/<doc_id>")
@action("catalog.platform:delete")
def delete_platform(doc_id: str):
    return _crud_delete(TABLE_PLATFORMS, doc_id)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


# A P2P catalog image encodes the libnccl build it carries as a `-nccl<MAJOR.MINOR.PATCH>.sqsh`
# filename suffix (e.g. `nvidia+nemo+26.04.01-arm-nccl2.32.3.sqsh`). The direct-NCCL
# microbenchmark can load any of those builds, so the launch dialog's version list is derived
# from the catalog the operator curated rather than a string hard-coded in a component that
# drifts the moment a new image is staged.
_NCCL_IMAGE_RE = re.compile(r"-nccl(\d+\.\d+\.\d+)\.sqsh$")


def _nccl_versions_from_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive the NCCL-version dropdown from P2P catalog images, newest (lowest ``order``) first.

    Each entry is ``{"value": <x.y.z>, "label": <image name or version>, "order": <int>}``.
    ``order`` is the operator's catalog position, so the latest build is the one they gave the
    lowest ``order`` -- sorting ascending puts it first. Images whose filename carries no libnccl
    suffix (plain containers, benchmarks) are skipped, and a version seen on more than one image
    keeps its lowest-``order`` entry so the dropdown never lists the same build twice. Returns
    ``[]`` when nothing matches, the frontend's signal to fall back to its shipped default list.
    """
    by_version: dict[str, dict[str, Any]] = {}
    for img in images or []:
        if not isinstance(img, dict):
            continue
        match = _NCCL_IMAGE_RE.search(str(img.get("filename", "")))
        if not match:
            continue
        version = match.group(1)
        entry = {"value": version, "label": img.get("name") or version, "order": img.get("order", 0)}
        prior = by_version.get(version)
        if prior is None or entry["order"] < prior["order"]:
            by_version[version] = entry
    return sorted(by_version.values(), key=lambda e: (e["order"], e["value"]))


def _catalog_nccl_versions(cluster_id: str) -> list[dict[str, Any]]:
    """NCCL versions offered by a cluster's *served* P2P catalog (authored → file → shipped).

    Reads the same manifest the P2P fleet view serves, so the dialog offers exactly the builds a
    cluster has staged. The import is function-local on purpose: ``api.p2p_fleet`` is a sibling
    blueprint, and importing it at module load would couple catalog registration to it.
    """
    from api.p2p_fleet import _load_catalog  # lazy: avoid a cross-blueprint import cycle

    catalog = _load_catalog(cluster_id=cluster_id)
    return _nccl_versions_from_images(catalog.get("images", []))


def _preset_version_subtitle(preset_id: str) -> str | None:
    """The ``NCCL X · CUDA Y`` subtitle for a validation-preset tool, read from the code-owned
    preset the tool registers — so the card can never drift from what the launcher actually runs.

    The version lives in ``validation_presets.py`` precisely because it must not be weakenable from
    a browser; the catalog tool only registers the preset by id and *displays* it. Deriving the
    subtitle here, on every read, is what closes the drift the seed warned about (a stored
    ``NCCL 2.31.2`` card while the recipe moved to 2.30.7). Only a preset that pins BOTH a NCCL and
    a CUDA version gets a derived subtitle — today just the acceptance sweep — so recipes whose
    subtitle is prose (``NCCL + LLMB, all trays``) keep it. The import is function-local for the
    same reason as above: catalog registration must not load the launch stack at module time.
    """
    try:
        import validation_presets  # lazy: avoid a module-load cycle through the catalog
        preset = validation_presets.resolve_preset(preset_id)
    except Exception:  # noqa: BLE001 - an unknown/odd preset simply keeps its stored subtitle
        return None
    nccl = (getattr(preset, "nccl_version", "") or "").strip()
    cuda = (getattr(preset, "cuda_version", "") or "").strip()
    return f"NCCL {nccl} · CUDA {cuda}" if nccl and cuda else None


@action(get="catalog.tool:list")
@catalog_bp.get("/tools")
@action(get="catalog.tool:list")
def list_tools():
    cluster = (request.args.get("cluster") or "").strip()
    surface = (request.args.get("surface") or "").strip() or None
    if cluster:
        items = context.tools_for_cluster(cluster_id=cluster, surface=surface)
        # Inject the catalog-derived NCCL version list into every opted-in tool. Derived once;
        # only applied when non-empty so the frontend keeps its shipped fallback otherwise.
        nccl_versions = _catalog_nccl_versions(cluster)
        if nccl_versions:
            for item in items:
                launch = item.get("launch")
                body = launch.get("body") if isinstance(launch, dict) else None
                if isinstance(body, dict) and body.get("show_nccl_version"):
                    body["nccl_versions"] = nccl_versions
        # Show the version the code-owned preset will actually run on the card, derived from the
        # registered preset so the catalog tool and the launcher can never disagree.
        for item in items:
            launch = item.get("launch")
            if not isinstance(launch, dict) or launch.get("kind") != "validation-preset":
                continue
            subtitle = _preset_version_subtitle(str(launch.get("preset_id") or ""))
            if subtitle:
                item["subtitle"] = subtitle
        return jsonify({"ok": True, "items": items})
    return _crud_list(TABLE_TOOLS, models.Tool)


@catalog_bp.post("/tools")
@action("catalog.tool:create")
def create_tool():
    return _crud_upsert(TABLE_TOOLS, models.Tool)


@action(get="catalog.tool:read")
@catalog_bp.get("/tools/<doc_id>")
@action(get="catalog.tool:read")
def get_tool(doc_id: str):
    return _crud_get(TABLE_TOOLS, models.Tool, doc_id)


@catalog_bp.put("/tools/<doc_id>")
@action("catalog.tool:update")
def update_tool(doc_id: str):
    return _crud_upsert(TABLE_TOOLS, models.Tool, doc_id=doc_id)


@catalog_bp.delete("/tools/<doc_id>")
@action("catalog.tool:delete")
def delete_tool(doc_id: str):
    return _crud_delete(TABLE_TOOLS, doc_id)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@action(get="catalog.project:list")
@catalog_bp.get("/projects")
@action(get="catalog.project:list")
def list_projects():
    return _crud_list(TABLE_PROJECTS, models.Project)


@catalog_bp.post("/projects")
@action("catalog.project:create")
def create_project():
    return _crud_upsert(TABLE_PROJECTS, models.Project)


@action(get="catalog.project:read")
@catalog_bp.get("/projects/<doc_id>")
@action(get="catalog.project:read")
def get_project(doc_id: str):
    return _crud_get(TABLE_PROJECTS, models.Project, doc_id)


@catalog_bp.put("/projects/<doc_id>")
@action("catalog.project:update")
def update_project(doc_id: str):
    return _crud_upsert(TABLE_PROJECTS, models.Project, doc_id=doc_id)


@catalog_bp.delete("/projects/<doc_id>")
@action("catalog.project:delete")
def delete_project(doc_id: str):
    return _crud_delete(TABLE_PROJECTS, doc_id)


# ---------------------------------------------------------------------------
# NPI reference catalog (imported from the NVIDIA NPI Knowledge Portal)
# ---------------------------------------------------------------------------


@action(get="catalog.tool:list")
@catalog_bp.get("/npi-tools")
@action(get="catalog.tool:list")
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


@action(get="catalog.tool:read")
@catalog_bp.get("/npi-tools/<doc_id>")
@action(get="catalog.tool:read")
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
@action("catalog.tool:import")
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


@action(get="catalog:read")
@catalog_bp.get("/active")
@action(get="catalog:read")
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
@action("catalog:set_active")
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


@action(get="catalog:read")
@catalog_bp.get("/topology")
@action(get="catalog:read")
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
@action("catalog:set_topology")
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


@action(get="catalog:read")
@catalog_bp.get("/vocab")
@action(get="catalog:read")
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
            "schedulers": list(models.SCHEDULERS),
            "clusterkit_tests": list(models.CLUSTERKIT_TESTS),
            "tool_categories": list(models.TOOL_CATEGORIES),
            "exec_modes": list(models.EXEC_MODES),
            "exec_targets": list(models.EXEC_TARGETS),
            "statuses": list(models.STATUSES),
            "npi_portfolio_roles": list(models.NPI_PORTFOLIO_ROLES),
            "npi_capabilities": list(models.NPI_CAPABILITIES),
            "npi_maturities": list(models.NPI_MATURITIES),
        }
    )


@action(get="catalog:read")
@catalog_bp.get("/overview")
@action(get="catalog:read")
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
