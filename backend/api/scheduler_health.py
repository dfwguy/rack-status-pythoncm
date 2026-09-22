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
"""Can this cluster's scheduler accept work right now?

Separate from `slurm_queries` because the subject is the *provider*, not the queue: this
answers before a launch what the queue views can only tell you after one has failed.

It earns its place on a `pythoncm` cluster, where three independent prerequisites -- the
helper being deployed to the head, a `pythoncm` matching that head's CMDaemon, and usable
certificates -- all fail identically from the outside, as a launch that did not start. One
probe separates them and names which.

The probe is non-mutating: nothing is submitted, nothing is queued, and a provider with no
cheap readiness check says so rather than inventing one.
"""
from __future__ import annotations

import logging

import scheduler_watch
from audit.actions import action
from catalog import context as catalog_context
from flask import Blueprint, jsonify
from schedulers.factory import build_scheduler

_logger = logging.getLogger(__name__)

bp = Blueprint("scheduler_health", __name__, url_prefix="")


@bp.route("/api/scheduler-watch", methods=["GET"])
@action(get="scheduler.queue:list")
def api_scheduler_watch():
    """Recent readiness samples for the active cluster's scheduler.

    The probe below answers "is it ready now". This answers "since when", which is the
    question a failed launch actually raises — a single reading cannot distinguish a
    scheduler that has been unreachable for forty minutes from one that blinked once.
    """
    return jsonify({"ok": True, **scheduler_watch.summary(), "history": scheduler_watch.history()})


@bp.route("/api/scheduler-probe", methods=["GET"])
@action(get="scheduler.queue:list")
def api_scheduler_probe():
    """Report whether the active cluster's scheduler backend is reachable and ready."""
    kind = ""
    try:
        scheduler = build_scheduler()
        kind = scheduler.kind
        probe = getattr(scheduler, "probe", None)
        if probe is None:
            # Not a failure. A batch scheduler has no readiness call that is cheaper than
            # just submitting, and saying "ready" on no evidence would be worse than saying
            # we did not look.
            return jsonify({
                "ok": True,
                "scheduler": kind,
                "ready": None,
                "detail": f"{kind} has no readiness probe; reachability is proven by a launch",
            })
        result = probe()
    except Exception as exc:  # noqa: BLE001 - every backend fails differently; all are reportable
        _logger.warning("scheduler probe failed for %s: %s", kind or "?", exc)
        # 200, deliberately: an unreachable scheduler is a fact about the cluster this
        # endpoint exists to report, not a fault in answering the question.
        return jsonify({
            "ok": False,
            "scheduler": kind or _active_scheduler_kind(),
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}"[:400],
        })
    return jsonify({
        "ok": True,
        "scheduler": kind,
        "ready": bool(result.get("ready", True)),
        "detail": str(result.get("detail") or ""),
    })


def _active_scheduler_kind() -> str:
    """What the active cluster record says, for when building the provider itself failed."""
    try:
        _project, cluster, _platform = catalog_context.resolve()
        return str(getattr(cluster, "scheduler", "") or "")
    except Exception:  # noqa: BLE001 - naming the scheduler is a nicety, not the answer
        return ""
