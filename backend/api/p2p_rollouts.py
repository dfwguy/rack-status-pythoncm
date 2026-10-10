# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""NVIS-P2P pull rollouts -- ``/api/p2p/rollouts``.

A rollout puts one catalog image onto racks through the node agents' desired-state documents,
rung by rung, and reads its progress from the agents' metrics (``p2p_rollout``). It is a
workflow-engine run, so it has a durable record, survives a restart (``install`` resumes it), and can
be steered while it runs: cancel, retry nodes, change the pacing. Reads are ``p2p:read``; each
control has its own action (``p2p:rollout_start|_cancel|_retry|_pace``), so a site can let someone
cancel or retry a rollout without letting them start one.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import p2p_catalog
import p2p_config
import p2p_rollout
import throttle
import workflow_engine
from audit.actions import action
from catalog import context as catalog_context
from flask import Blueprint, Flask, jsonify, request
from paths import _resolve_log_dir
from rack_status_engine import get_cluster_snapshot

from ._common import operator_from_request

_logger = logging.getLogger("rack_web.api.p2p_rollouts")

bp = Blueprint("p2p_rollouts", __name__, url_prefix="/api/p2p")


def _cluster():
    """The active cluster when P2P is enabled on it, else ``(None, response)``."""
    _project, cluster, _platform = catalog_context.resolve()
    if cluster is None or not getattr(cluster, "p2p_enabled", False):
        return None, (jsonify({"ok": False, "message": "P2P is not enabled for the active cluster."}), 404)
    return cluster, None


def _inventory() -> list[dict] | None:
    payload = get_cluster_snapshot(_resolve_log_dir(None), wait=False).payload or {}
    nodes = payload.get("nodes")
    return nodes if isinstance(nodes, list) else None


def _rollout_run(cluster_id: str, run_id: str):
    run = workflow_engine.get_run_state(run_id)
    if not run or run.get("workflow_id") != p2p_rollout.WORKFLOW_ID \
            or run.get("params", {}).get("cluster_id") != cluster_id:
        return None
    return run


@bp.route("/rollouts", methods=["GET"])
@action(get="p2p:read")
def p2p_rollouts_list():
    """Recent pull rollouts on the active cluster, newest first, with per-state node counts."""
    cluster, err = _cluster()
    if err:
        return err
    out = []
    for row in workflow_engine.list_runs(workflow_id=p2p_rollout.WORKFLOW_ID, limit=100):
        run = workflow_engine.get_run_state(row["run_id"])
        if run and run.get("params", {}).get("cluster_id") == cluster.id:
            out.append(p2p_rollout.describe(run))
    settings = p2p_config.get_settings(cluster.id)
    counts: dict[str, int] = {}
    for n in _inventory() or []:
        if isinstance(n, dict) and n.get("rack"):
            counts[str(n["rack"])] = counts.get(str(n["rack"]), 0) + 1
    racks = [{"rack": r, "nodes": counts[r]} for r in sorted(counts, key=p2p_rollout.natural_key)]
    return jsonify({"ok": True, "cluster_id": cluster.id, "rollouts": out, "racks": racks,
                    "blockers": settings.rollout_blockers(),
                    "defaults": p2p_rollout.default_pacing(settings)})


@bp.route("/rollouts/plan", methods=["GET"])
@action(get="p2p:read")
def p2p_rollouts_plan():
    """Preview a rollout: each rack's members in tray order, and which have a live agent.

    ``?image=<catalog filename>&racks=Q5,Q23``. Nothing is written; it is the same resolution a
    start does, plus one metrics read so the operator sees agentless nodes before launching.
    """
    cluster, err = _cluster()
    if err:
        return err
    settings = p2p_config.get_settings(cluster.id)
    racks = [r for r in (request.args.get("racks") or "").split(",") if r.strip()]
    try:
        plan = p2p_rollout.plan_rollout(
            cluster_id=cluster.id, settings=settings,
            catalog_images=p2p_catalog.get_catalog(cluster.id).to_dict()["images"],
            inventory=_inventory(), filename=(request.args.get("image") or "").strip(), racks=racks)
    except p2p_rollout.RolloutError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    nodes = [n for members in plan["racks"].values() for n in members]
    try:
        obs = p2p_rollout.vm_observer(plan.get("metrics_url") or "")(nodes, plan["image"]["filename"])
        metrics_error = ""
    except Exception as exc:  # noqa: BLE001 -- a preview still answers without metrics
        obs, metrics_error = {}, f"{type(exc).__name__}: {exc}"
    racks_out = []
    for rack, members in plan["racks"].items():
        rows = [{"node": n, "agent": bool(obs.get(n) and obs[n].alive),
                 "state": obs[n].state if obs.get(n) else ""} for n in members]
        racks_out.append({"rack": rack, "nodes": rows,
                          "live_agents": sum(r["agent"] for r in rows),
                          "holders": sum(r["state"] == "ready" for r in rows)})
    return jsonify({"ok": True, "image": plan["image"], "racks": racks_out, "metrics_error": metrics_error,
                    "pacing": p2p_rollout.default_pacing(settings)})


@bp.route("/rollouts", methods=["POST"])
@throttle.throttle(10, rationale="starts a long-running rollout run; cheap, but not something to call in a loop.")
@action(post="p2p:rollout_start")
def p2p_rollouts_start():
    """Start a pull rollout of one catalog image onto racks.

    Body ``{image, racks: [...], pacing?: {stages, budget_seconds, max_active_racks}}``. A second start
    of the same image on the same racks while the first is live joins it (``joined: true``).
    """
    cluster, err = _cluster()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    settings = p2p_config.get_settings(cluster.id)
    racks = body.get("racks") if isinstance(body.get("racks"), list) else []
    try:
        plan = p2p_rollout.plan_rollout(
            cluster_id=cluster.id, settings=settings,
            catalog_images=p2p_catalog.get_catalog(cluster.id).to_dict()["images"],
            inventory=_inventory(), filename=str(body.get("image") or "").strip(), racks=[str(r) for r in racks])
        pacing = p2p_rollout.normalize_pacing(body.get("pacing"), p2p_rollout.default_pacing(settings))
    except p2p_rollout.RolloutError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    started = p2p_rollout.start(plan, pacing, operator_from_request())
    return jsonify({"ok": True, **started}), 200 if started.get("joined") else 201


@bp.route("/rollouts/<run_id>", methods=["GET"])
@action(get="p2p:read")
def p2p_rollouts_get(run_id: str):
    """One rollout in full: per rack its rung and cuts, per node its state and reason, and the log."""
    cluster, err = _cluster()
    if err:
        return err
    run = _rollout_run(cluster.id, run_id)
    if run is None:
        return jsonify({"ok": False, "message": "rollout not found"}), 404
    return jsonify({"ok": True, "rollout": p2p_rollout.describe(run, detail=True)})


@bp.route("/rollouts/<run_id>/cancel", methods=["POST"])
@throttle.throttle(20, rationale="operator control of a live rollout.")
@action(post="p2p:rollout_cancel")
def p2p_rollouts_cancel(run_id: str):
    """Cancel a rollout: nodes still downloading are told to stop; installed nodes keep the image."""
    cluster, err = _cluster()
    if err:
        return err
    if _rollout_run(cluster.id, run_id) is None:
        return jsonify({"ok": False, "message": "rollout not found"}), 404
    return jsonify({"ok": True, **workflow_engine.cancel_run(run_id)})


@bp.route("/rollouts/<run_id>/retry", methods=["POST"])
@throttle.throttle(20, rationale="operator control of a live rollout.")
@action(post="p2p:rollout_retry")
def p2p_rollouts_retry(run_id: str):
    """Retry nodes on a live rollout. Body ``{nodes: [...]}``; an empty list retries every deferred node."""
    cluster, err = _cluster()
    if err:
        return err
    if _rollout_run(cluster.id, run_id) is None:
        return jsonify({"ok": False, "message": "rollout not found"}), 404
    nodes = (request.get_json(silent=True) or {}).get("nodes") or []
    try:
        run = p2p_rollout.request_retry(run_id, nodes if isinstance(nodes, list) else [])
    except p2p_rollout.RolloutError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "rollout": p2p_rollout.describe(run)})


@bp.route("/rollouts/<run_id>/pacing", methods=["PUT"])
@throttle.throttle(20, rationale="operator control of a live rollout.")
@action(put="p2p:rollout_pace")
def p2p_rollouts_pacing(run_id: str):
    """Change a live rollout's pacing (stages, budget_seconds, max_active_racks); applies next tick."""
    cluster, err = _cluster()
    if err:
        return err
    if _rollout_run(cluster.id, run_id) is None:
        return jsonify({"ok": False, "message": "rollout not found"}), 404
    try:
        run = p2p_rollout.set_pacing(run_id, request.get_json(silent=True) or {})
    except p2p_rollout.RolloutError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "rollout": p2p_rollout.describe(run)})


def _resume_later() -> None:
    # After the app has settled (the active cluster resolved, the snapshot warming): a rollout's first
    # resumed tick reads metrics and documents, not anything the first request needs.
    time.sleep(20)
    try:
        resumed = workflow_engine.resume_interrupted_runs()
        if resumed:
            _logger.info("p2p rollouts: resumed %d interrupted run(s): %s", len(resumed), ", ".join(resumed))
    except Exception:  # a failed sweep must not take the app down
        _logger.warning("p2p rollouts: resume sweep failed", exc_info=True)


def install(app: Flask) -> None:
    """Resume rollouts a restart interrupted. Not in demo mode or tests, which run no real rollouts."""
    if os.environ.get("RACK_STATUS_DEMO", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    threading.Thread(target=_resume_later, name="p2p-rollout-resume", daemon=True).start()
