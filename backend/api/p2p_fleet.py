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

import p2p_config
import p2p_provision  # noqa: F401 -- imported for its side effect: registers the p2p_stage_assets native step
import paths
import throttle
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


def _load_catalog() -> dict:
    """The catalog manifest as the frontend `Catalog` shape.

    `canonical_present` and `persistent_import` are runtime observations, not manifest facts — on a
    cluster with no provisioned instance nothing has been observed, so they read false/null. When an
    instance exists these come from the live snapshot instead of this loader.
    """
    try:
        raw = json.loads(_CATALOG_MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"canonical_directory": "", "images": []}
    except (OSError, ValueError):
        _logger.exception("p2p: could not read catalog manifest %s", _CATALOG_MANIFEST)
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


def _not_provisioned_status() -> dict:
    """A valid `FleetStatus` for an enabled-but-unprovisioned cluster.

    The catalog is real (it is what P2P will distribute); everything observed from a running
    instance is empty because there is no instance yet. The view renders the catalog and a zeroed
    coverage rather than an error.
    """
    return {
        "catalog": _load_catalog(),
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

    # Greenfield today: no instance endpoints live on the cluster record yet, so there is nothing to
    # proxy. When provisioning lands, resolve the instance's operations endpoint here and proxy its
    # live snapshot instead of the not-provisioned state.
    return jsonify(_not_provisioned_status())


@bp.route("/operations", methods=["POST"])
@throttle.throttle(
    20,
    rationale="mutating P2P operations; also SRP-free but serialized behind the reconcile workflow once it lands.",
)
@action(post="p2p:operate")
def p2p_operations():
    """Start a fleet operation (preflight/persist/sync/distribute/verify/repair).

    A fleet operation acts on an *already provisioned* instance and is a different capability from
    standing the instance up, so it carries its own ``p2p:operate`` action rather than sharing
    ``p2p:provision``.

    501 until the reconcile workflow lands: the contract (and the `p2p:provision` action that
    governs it) is declared now so the UI and the vocabulary are stable, but there is nothing behind
    it yet. A 501 with a message the SPA can render beats a 404 it cannot explain.
    """
    return (
        jsonify(
            {
                "ok": False,
                "message": (
                    "Fleet operations are not available until this cluster's P2P instance is "
                    "provisioned. Provision it with POST /api/p2p/provision."
                ),
            }
        ),
        501,
    )


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
    # addresses, paths). Passed as run params so a step can write the cluster.env and the native
    # stage step knows where to put the assets. Secrets are file *paths* here, never values.
    params = {"cluster_id": cluster.id, "cluster_name": cluster.name, **settings.to_render_env()}
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
