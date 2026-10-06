# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The write path behind ``POST /api/p2p/operations``: enqueue a fleet operation.

A catalog campaign runs for minutes over hundreds of peers, and the gateway can neither block a
request on it nor reach the instance's hosts to run it directly. So an operation is *enqueued*, not
executed here: this writes a request document into a directory shared with the host where the
instance lives, and a worker there (``deploy/p2p/scripts/operations-worker.py``) claims it, runs the
matching catalog script with the cluster's own campaign pacing, and writes state back. The snapshot
the status gateway already serves carries the operations list, so the UI sees the request accepted
and then its state advance -- a distribute shows up as the live sync wave it drives.

Enqueue is cheap and side-effect-light (one file); the blast radius lives in the worker, out-of-band
with the fleet's own access.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import uuid

from catalog.models import P2pSettings

#: Shared with the worker. On a co-located deployment this is under the app's state directory, which
#: the worker reads from the same filesystem.
_OPS_DIR = pathlib.Path(os.environ.get("RACK_P2P_OPERATIONS_DIR", "/app/var/p2p-operations"))

#: The actions a worker knows how to run. "deploy"/"sync" are deliberately absent until their worker
#: handlers land, so the gateway rejects them cleanly rather than enqueuing a request nothing claims.
SUPPORTED_ACTIONS = frozenset({"preflight", "persist", "distribute", "verify", "repair"})
#: Actions that act on a specific image and cannot proceed without one.
IMAGE_REQUIRED = frozenset({"persist", "distribute", "repair"})


class OperationError(ValueError):
    """A request the gateway should reject with 400 and this message."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ops_dir(cluster_id: str) -> pathlib.Path:
    return _OPS_DIR / cluster_id


def enqueue(cluster_id: str, settings: P2pSettings, action: str,
            images: list[str] | None, sus: list[int] | None) -> dict:
    """Validate and queue one operation. Returns the ``Operation`` the UI renders.

    Raises :class:`OperationError` for a request the worker could not run: an unknown action, or an
    image-scoped action with no image. The caller maps that to a 400.
    """
    images = [i for i in (images or []) if i]
    sus = [int(s) for s in (sus or [])]
    if action not in SUPPORTED_ACTIONS:
        raise OperationError(f"unsupported operation: {action!r}")
    if action in IMAGE_REQUIRED and not images:
        raise OperationError(f"operation {action!r} requires at least one image")

    op_id = f"{action}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    request = {
        "id": op_id,
        "action": action,
        "sus": sus,
        "images": images,
        "state": "running",  # queued-for-the-worker reads as running to the UI until it resolves
        "started_at": _now(),
        "finished_at": None,
        "return_code": None,
        "log": f"/nvis/api/p2p/operations/{op_id}/log",
        # What the worker needs to run it without re-reading the settings store: paths and pacing.
        "request": {
            "catalog_path": settings.catalog_path,
            "container_path": settings.container_path,
            "install_root": settings.install_root,
            "inventory_source": settings.resolved_inventory_source(),
            "rollout": {
                "wave_size": settings.rollout_wave_size,
                "parallelism": settings.rollout_parallelism,
                "poll_seconds": settings.rollout_poll_seconds,
                "max_launches": settings.rollout_max_launches,
                "max_unreachable": settings.rollout_max_unreachable,
                "timeout_seconds": settings.rollout_timeout_seconds,
            },
        },
    }
    directory = _ops_dir(cluster_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{op_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(request, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    # The UI's Operation shape: the private "request" block is for the worker, not the client.
    return {k: v for k, v in request.items() if k != "request"}
