# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Live fleet snapshot for a provisioned P2P instance.

The ``/api/p2p/status`` gateway cannot reach a cluster's Dragonfly services across the network, and
re-auditing every node on every poll would be ruinous. Instead a snapshot *producer* that runs where
the instance lives (``deploy/p2p/scripts/fleet-snapshot.py`` on the app server) assembles a
``FleetStatus`` document periodically and writes it into a directory this process can read -- on a
co-located deployment that is the app's own state directory, shared with the producer. This module
reads the per-cluster file with a short in-process cache and a staleness guard, so the view sees
live coverage and campaign progress without a fan-out, and never ancient data from a producer that
has stopped.

Absent, stale or malformed -> ``None``, and the caller serves the not-provisioned state instead.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import time

import config_env

_logger = logging.getLogger("rack_web.p2p_snapshot")

#: Where the producer drops ``<cluster_id>.json``. Defaults under the app's state dir, which on a
#: co-located deployment is the same mounted directory the producer writes to.
_SNAPSHOT_DIR = pathlib.Path(os.environ.get("RACK_P2P_SNAPSHOT_DIR", "/app/var/p2p-snapshots"))
#: A snapshot older than this is treated as absent: a stopped producer must degrade to greenfield,
#: not pin the view to a stale coverage forever. Generous enough to tolerate a missed producer run.
#: Read at import, so it goes through `env_int`: a bare `int(...)` here turned a typo such as "30m"
#: into an app that would not start. Zero or negative is meaningless and falls back to the default.
_MAX_AGE_SECONDS = config_env.env_int("RACK_P2P_SNAPSHOT_MAX_AGE", 900, min_valid=1)
#: The view polls faster than the producer runs; re-reading the file every few seconds is enough.
_CACHE_TTL_SECONDS = 10

#: The keys a document must carry to be a usable FleetStatus. A partial file is treated as absent.
_REQUIRED_KEYS = frozenset({"catalog", "coverage", "object_store_healthy"})

_cache: dict[str, tuple[float, dict | None]] = {}


def _parse_generated_at(value: object) -> float | None:
    """Epoch seconds for an ISO-8601 ``generated_at``, or None if it is missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _read(cluster_id: str) -> dict | None:
    path = _SNAPSHOT_DIR / f"{cluster_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _logger.warning("p2p snapshot %s is unreadable or not JSON", path)
        return None
    if not isinstance(raw, dict) or not _REQUIRED_KEYS <= set(raw):
        _logger.warning("p2p snapshot %s is missing required keys", path)
        return None
    return raw


def _is_fresh(snapshot: dict, now: float) -> bool:
    generated = _parse_generated_at(snapshot.get("generated_at"))
    if generated is None:
        # No timestamp: cannot vouch for freshness, so do not serve it as live.
        return False
    return (now - generated) <= _MAX_AGE_SECONDS


def load_snapshot(cluster_id: str, *, now: float | None = None) -> dict | None:
    """The active cluster's live FleetStatus, or None if there is no fresh one.

    None means "no live instance to show": absent file, a producer that stopped (stale), or a
    malformed document. The caller renders the greenfield state rather than guessing.
    """
    if not cluster_id:
        return None
    now = time.time() if now is None else now
    hit = _cache.get(cluster_id)
    if hit is not None and (now - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]
    snapshot = _read(cluster_id)
    if snapshot is not None and not _is_fresh(snapshot, now):
        snapshot = None
    _cache[cluster_id] = (now, snapshot)
    return snapshot


def invalidate_cache() -> None:
    """Drop the in-process cache. For tests and for an explicit refresh after an operation."""
    _cache.clear()
