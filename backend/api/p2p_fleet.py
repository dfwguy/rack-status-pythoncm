# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike
"""NVIS-P2P (artifact distribution) gateway — the `/api/p2p/*` surface.

P2P is a per-cluster capability (`Cluster.p2p_enabled`), single-tenant: one Dragonfly instance per
cluster. The browser talks only to this gateway, never to Dragonfly Manager directly, so there is
one auth gate and one session — the `p2p:*` actions govern it, and the enabled cluster's Manager
endpoint/credentials (once it has one) are resolved server-side, never handed to the client.

**This is the read/status surface.** It answers the Artifact Fleet view. On a *greenfield* cluster —
P2P enabled but no Dragonfly instance provisioned yet — it reports the shipped catalog with an empty
coverage/fleet state and the object store unavailable, so the view renders the catalog it *will*
distribute rather than a bare error. When an instance exists, its live fleet snapshot is proxied
from the cluster's operations API (added with the provisioning workflow).

The mutating operations (`POST /api/p2p/operations`: preflight/persist/sync/distribute/verify/
repair, and provisioning) are declared here so the vocabulary and the UI have a stable contract, but
they return 501 until the reconcile workflow lands — an honest "not yet", not a 404 the SPA cannot
explain.
"""

from __future__ import annotations

import json
import logging
import pathlib

import p2p_campaigns
import p2p_catalog
import p2p_config
import p2p_operations as p2p_ops
import p2p_provision  # noqa: F401 -- imported for its side effect: registers the p2p_stage_assets native step
import p2p_rosters
import p2p_snapshot
import paths
import throttle
import validation_presets
import workflow_engine
from audit.actions import action
from catalog import context as catalog_context
from catalog import models
from flask import Blueprint, jsonify, request

_logger = logging.getLogger("rack_web.api.p2p_fleet")

bp = Blueprint("p2p_fleet", __name__, url_prefix="/api/p2p")

#: The shipped reconcile workflow (var/workflows/nvis-p2p-provision.yaml). One instance per cluster;
#: a reconcile leaves a healthy instance alone and creates only what is missing.
_PROVISION_WORKFLOW = "nvis-p2p-provision"

#: The shipped catalog release manifest. A per-cluster manifest overrides this later; this is the
#: default that ships in the image (deploy/p2p/catalog/images.json).
_CATALOG_MANIFEST = paths.REPO_ROOT / "deploy" / "p2p" / "catalog" / "images.json"


def _authored_catalog(cluster_id: str) -> dict | None:
    """The cluster's edited catalog as the frontend `Catalog` shape, or None if none authored.

    Rack Status is the catalog's source of truth, so an operator-authored catalog is what the view
    shows even before a sync projects it onto the cluster file; runtime observations stay blank until
    an instance reports them.
    """
    if not cluster_id or not p2p_catalog.has_catalog(cluster_id):
        return None
    cat = p2p_catalog.get_catalog(cluster_id)
    images = [{**img, "canonical_present": False, "persistent_import": None}
              for img in cat.to_dict()["images"]]
    return {"schema_version": 1, "catalog_name": "authored", "canonical_directory": "",
            "generated_at_utc": None, "images": images}


def _load_catalog(catalog_path: str = "", cluster_id: str = "") -> dict:
    """The catalog manifest as the frontend `Catalog` shape.

    Resolves per cluster, most-authoritative first: the operator's edited catalog document, then the
    cluster's configured manifest file (``P2pSettings.catalog_path``) when readable, then the shipped
    default in the image. A configured-but-unreadable path (common when the manifest lives on the
    cluster's app server and is not mounted here) falls back rather than showing an empty catalog.

    `canonical_present` and `persistent_import` are runtime observations, not manifest facts — on a
    cluster with no provisioned instance nothing has been observed, so they read false/null. When an
    instance exists these come from the live snapshot instead of this loader.
    """
    authored = _authored_catalog(cluster_id)
    if authored is not None:
        return authored
    source = pathlib.Path(catalog_path) if catalog_path.strip() else _CATALOG_MANIFEST
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if source != _CATALOG_MANIFEST:
            return _load_catalog()  # configured path absent here -> shipped default
        return {"canonical_directory": "", "images": []}
    except (OSError, ValueError):
        _logger.exception("p2p: could not read catalog manifest %s", source)
        if source != _CATALOG_MANIFEST:
            return _load_catalog()
        return {"canonical_directory": "", "images": []}

    images = []
    for img in raw.get("images", []):
        images.append(
            {
                "order": img.get("order", 0),
                "name": img.get("name", ""),
                "filename": img.get("filename", ""),
                "bytes": img.get("bytes", 0),
                "sha256": img.get("sha256", ""),
                "required_by": img.get("required_by", []),
                "canonical_present": False,
                "persistent_import": None,
            }
        )
    return {
        "schema_version": raw.get("schema_version"),
        "catalog_name": raw.get("catalog_name"),
        "canonical_directory": raw.get("canonical_directory", ""),
        "generated_at_utc": raw.get("generated_at_utc"),
        "images": images,
    }


def _not_provisioned_status(catalog_path: str = "", cluster_id: str = "") -> dict:
    """A valid `FleetStatus` for an enabled-but-unprovisioned cluster.

    The catalog is real (the operator's edited catalog, else the cluster's own ``catalog_path`` when
    readable); everything observed from a running instance is empty because there is no instance yet.
    The view renders the catalog and a zeroed coverage rather than an error.
    """
    return {
        "catalog": _load_catalog(catalog_path, cluster_id),
        "object_store_healthy": False,
        "fleet": [],
        "coverage": {
            "generated_at": None,
            "required_count": 0,
            "required_images": [],
            "by_su": [],
            "nodes": [],
        },
        "sync": None,
        "catalog_exact_duplicates": [],
        "preflight_generated_at": None,
        "operations": [],
        # Non-typed hint the view may surface; harmless to a client that ignores it.
        "provisioned": False,
    }


@bp.route("/status", methods=["GET"])
@throttle.throttle(
    30,
    rationale=(
        "reads the active cluster record and a small manifest; no subprocess, no fan-out. The "
        "Artifact Fleet view polls this on an interval, so the limit is generous."
    ),
)
@action(get="p2p:read")
def p2p_status():
    """Fleet snapshot for the active cluster's P2P instance.

    404 when P2P is not enabled for the active cluster — the nav group is already gated on the same
    flag, so this is the defensive backstop for a hand-typed URL, and it deliberately says nothing
    about a cluster that has not opted in.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404

    # A provisioned instance publishes a live FleetStatus snapshot the producer keeps current; serve
    # it when it is fresh. Absent or stale -> the greenfield state, with the catalog still resolved
    # from the cluster's own settings so the view reflects the per-cluster manifest where readable.
    settings = p2p_config.get_settings(cluster.id)
    snapshot = p2p_snapshot.load_snapshot(cluster.id)
    if snapshot is not None:
        return jsonify(snapshot)
    return jsonify(_not_provisioned_status(settings.catalog_path, cluster.id))


@bp.route("/operations", methods=["POST"])
@throttle.throttle(
    20,
    rationale="mutating P2P operations; also SRP-free but serialized behind the reconcile workflow once it lands.",
)
@action(post="p2p:operate")
def p2p_operations():
    """Start a fleet operation (preflight/persist/distribute/verify/repair) on the active cluster.

    A fleet operation acts on an *already provisioned* instance and is a different capability from
    standing the instance up, so it carries its own ``p2p:operate`` action rather than sharing
    ``p2p:provision``. The operation is enqueued for the instance's worker, which runs it with the
    cluster's own campaign pacing; the run shows up in the fleet snapshot the status endpoint serves.
    202 with the accepted ``Operation``; 400 when settings are incomplete or the request is invalid.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    settings = p2p_config.get_settings(cluster.id)
    blockers = settings.provisioning_blockers()
    if blockers:
        return (
            jsonify({"ok": False, "message": "P2P settings are incomplete: " + "; ".join(blockers),
                     "provisioning_blockers": blockers}),
            400,
        )
    body = request.get_json(silent=True) or {}
    pacing = body.get("pacing") if isinstance(body.get("pacing"), dict) else None
    try:
        operation = p2p_ops.enqueue(cluster.id, settings, str(body.get("action", "")),
                                    body.get("images"), body.get("sus"), str(body.get("roster", "")),
                                    pacing=pacing, campaign_name=str(body.get("campaign", "")))
    except p2p_ops.OperationError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify(operation), 202


@bp.route("/operations/<op_id>/log", methods=["GET"])
@throttle.throttle(60, rationale="reads one operation's log file for the viewer dialog; no subprocess.")
@action(get="p2p:read")
def p2p_operation_log(op_id: str):
    """The worker's log for one operation, for the in-app log dialog.

    Same response shape as the scheduler job-output dialog (`ok`/`content`/`too_large`), so the P2P
    viewer is the scheduler's log construct pointed at the operations directory rather than a job
    stdout path.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "error": "P2P is not enabled for the active cluster."}), 404
    result = p2p_ops.read_log(cluster.id, op_id)
    return jsonify(result), 200 if result.get("ok") else 404


@bp.route("/catalog", methods=["GET"])
@action(get="p2p:read")
def p2p_catalog_get():
    """The active cluster's editable catalog (the task list).

    The authored document when one exists; otherwise the current manifest (file or shipped) so the
    editor opens populated with reality rather than empty. ``authored`` says which it is.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    if p2p_catalog.has_catalog(cluster.id):
        images = p2p_catalog.get_catalog(cluster.id).to_dict()["images"]
        authored = True
    else:
        settings = p2p_config.get_settings(cluster.id)
        loaded = _load_catalog(settings.catalog_path)  # no cluster_id: the file/shipped, not the authored branch
        keep = ("order", "name", "filename", "bytes", "sha256", "required_by")
        images = [{k: img.get(k) for k in keep} for img in loaded["images"]]
        authored = False
    # The names an image's Required by may carry: each is a launch that stages the image naming it.
    return jsonify({"ok": True, "cluster_id": cluster.id, "authored": authored, "images": images,
                    "consumers": validation_presets.image_consumers()})


@bp.route("/catalog", methods=["PUT"])
@action(put="p2p:catalog")
def p2p_catalog_put():
    """Replace the active cluster's catalog (the task list). 400 on an invalid document."""
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    body = request.get_json(silent=True) or {}
    catalog = models.P2pCatalog.from_dict({"cluster_id": cluster.id, "images": body.get("images", [])})
    try:
        saved = p2p_catalog.save_catalog(catalog)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    # Launchers check node images against the catalog beside the scripts, a projection separate
    # from the P2P origin's manifest (which the P2P sync owns). Keep it current with every save; a
    # failure is reported, not fatal -- the document is saved and the next save or deploy retries.
    published = p2p_catalog.publish_to_scripts(cluster.id)
    return jsonify({"ok": True, "cluster_id": cluster.id, "authored": True,
                    "images": saved.to_dict()["images"], "scripts_catalog": published})


@bp.route("/catalog/publish", methods=["POST"])
@action(post="p2p:publish")
def p2p_catalog_publish():
    """Re-publish the active cluster's catalog beside the scripts (``<scripts it runs>/config``).

    Saves and in-app script deploys already do this; this is for a script tree pushed some other
    way (``bin/deploy-scripts.sh`` from a workstation), which never carries the catalog.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    published = p2p_catalog.publish_to_scripts(cluster.id)
    return jsonify(published), (200 if published.get("ok") else 502)


@bp.route("/rosters", methods=["GET"])
@action(get="p2p:read")
def p2p_rosters_get():
    """The active cluster's named rosters (node lists an operation can target by name)."""
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    rosters = p2p_rosters.get_rosters(cluster.id).to_dict()["rosters"]
    return jsonify({"ok": True, "cluster_id": cluster.id, "rosters": rosters})


@bp.route("/rosters", methods=["PUT"])
@action(put="p2p:roster")
def p2p_rosters_put():
    """Replace the active cluster's named rosters. 400 on an invalid document."""
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    body = request.get_json(silent=True) or {}
    rosters = models.P2pRosters.from_dict({"cluster_id": cluster.id, "rosters": body.get("rosters", [])})
    try:
        saved = p2p_rosters.save_rosters(rosters)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "cluster_id": cluster.id, "rosters": saved.to_dict()["rosters"]})


@bp.route("/campaigns", methods=["GET"])
@action(get="p2p:read")
def p2p_campaigns_get():
    """The active cluster's saved campaigns (task × roster × pacing definitions)."""
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    campaigns = p2p_campaigns.get_campaigns(cluster.id).to_dict()["campaigns"]
    return jsonify({"ok": True, "cluster_id": cluster.id, "campaigns": campaigns})


@bp.route("/campaigns", methods=["PUT"])
@action(put="p2p:campaign")
def p2p_campaigns_put():
    """Replace the active cluster's saved campaigns. 400 on an invalid document.

    A campaign is a definition, not a run: saving one never touches the fleet. Launching it is a
    separate ``POST /operations`` (a ``distribute`` carrying the campaign's image, roster, pacing,
    and name), so the same campaign re-runs idempotently and its runs are the operations it tagged.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    body = request.get_json(silent=True) or {}
    campaigns = models.P2pCampaigns.from_dict(
        {"cluster_id": cluster.id, "campaigns": body.get("campaigns", [])}
    )
    try:
        saved = p2p_campaigns.save_campaigns(campaigns)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "cluster_id": cluster.id, "campaigns": saved.to_dict()["campaigns"]})


@bp.route("/provision", methods=["POST"])
@throttle.throttle(
    10,
    rationale="starts a durable reconcile run (a background thread); cheap to call but not a poll target.",
)
@action(post="p2p:provision")
def p2p_start_provision():
    """Start (or reconcile) this cluster's Dragonfly control plane.

    Runs the shipped reconcile workflow on the active cluster's app server. Returns immediately with
    a ``run_id``; progress is polled at ``GET /api/workflow-runs/<run_id>`` like any other workflow
    run, so the durable-run registry (citizen 4) is the single system of record for what a
    provision did and when — there is no second job table. The workflow's first step is a read-only
    preflight, so calling this on an unready host reports faults rather than half-building.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404

    settings = p2p_config.get_settings(cluster.id)
    blockers = settings.provisioning_blockers()
    if blockers:
        # Refuse before spawning a run: a workflow that can only fail its own render step is worse
        # than a clear 400 that names what the operator must fill in first (Dragonfly Settings).
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "P2P settings are incomplete: " + "; ".join(blockers),
                    "provisioning_blockers": blockers,
                }
            ),
            400,
        )

    # The render env carries the per-cluster facts the reconcile's steps need (install root,
    # addresses, paths). The P2P_APPSERVER_* params carry the connection every step runs over -- the
    # app server, not the cluster's control host. Secrets are file *paths* here, never values.
    conn = settings.appserver_connection()
    params = {
        "cluster_id": cluster.id,
        "cluster_name": cluster.name,
        "P2P_APPSERVER_HOST": conn["host"],
        "P2P_APPSERVER_USER": conn["user"],
        "P2P_APPSERVER_KEY": conn["key"],
        **settings.to_render_env(),
    }
    try:
        result = workflow_engine.start_workflow_run(_PROVISION_WORKFLOW, params)
    except ValueError as exc:
        # The workflow definition is missing from this deployment — a packaging fault, not a client
        # error, so it must be loud rather than a confusing 404 on the P2P surface.
        _logger.error("p2p provision: %s", exc)
        return jsonify({"ok": False, "message": f"provisioning workflow unavailable: {exc}"}), 500

    run_id = result.get("run_id", "")
    return (
        jsonify(
            {
                "ok": True,
                "run_id": run_id,
                "state": result.get("state"),
                "poll": f"/api/workflow-runs/{run_id}",
            }
        ),
        202,
    )


@bp.route("/settings", methods=["GET"])
@throttle.throttle(
    30,
    rationale="reads one small per-cluster document; the Dragonfly Settings view loads it on open.",
)
@action(get="p2p:read")
def p2p_get_settings():
    """This cluster's P2P instance settings, defaulted when never configured."""
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    settings = p2p_config.get_settings(cluster.id)
    body = settings.to_dict()
    body["ok"] = True
    body["provisioning_blockers"] = settings.provisioning_blockers()
    # Which image directories the cluster record dictates: the Settings view shows those read-only
    # and points at the cluster editor, since an edit here would be overridden on the next read.
    body["image_dirs_from_cluster"] = {
        "container_path": bool(cluster.connection.shared_container_dir),
        "peer_root": bool(cluster.connection.node_container_dir),
    }
    return jsonify(body)


@bp.route("/settings", methods=["PUT"])
@throttle.throttle(
    20,
    rationale="writes one small per-cluster document; not a poll target.",
)
@action(put="p2p:configure")
def p2p_put_settings():
    """Save this cluster's P2P instance settings.

    A valid document may still be *incomplete* for provisioning (the addresses blank), so this
    accepts the draft and reports the remaining blockers rather than refusing it -- the reconcile is
    where an underspecified instance is turned away.
    """
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404
    payload = request.get_json(silent=True) or {}
    settings = models.P2pSettings.from_dict({**payload, "cluster_id": cluster.id})
    try:
        saved = p2p_config.save_settings(settings)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    body = saved.to_dict()
    body["ok"] = True
    body["provisioning_blockers"] = saved.provisioning_blockers()
    return jsonify(body)
