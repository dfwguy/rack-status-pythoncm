# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Which dashboards are currently open: a TTL heartbeat map of browser client ids.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Two callers with nothing else in common, which is why this is its own module: the status/overview
polls report a connected-user count, and ``rack_locks`` needs the same map to decide whether a manual
claim is *abandoned* -- a claim whose owner's browser stopped heartbeating can be cleared, while one
whose owner is still polling cannot. Those live in different API modules now, so the map cannot sit in
either.

In-process and deliberately not persisted: it answers "who is looking right now", so a restart
starting from empty is correct rather than a loss.

The dict and its lock are shared **objects**. Mutate them through the functions here, never rebind
them -- a second dict would not be covered by this lock, and the two callers would then disagree
about who is online.
"""

from __future__ import annotations

import threading
import time

from config_env import _env_int
from core import APP_CLIENT_ID_RE

_app_clients_lock = threading.Lock()
_app_clients_last_seen: dict[str, float] = {}


def _app_client_ttl_sec() -> int:
    return max(30, _env_int("RACK_WEB_ACTIVE_CLIENT_TTL_SEC", 120))


def _touch_app_client(client_id: str, now_ts: float | None = None) -> int:
    """Track active dashboard clients and return currently-active count."""
    cid = (client_id or "").strip()
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - float(_app_client_ttl_sec())
    with _app_clients_lock:
        stale = [k for k, ts in _app_clients_last_seen.items() if float(ts) < cutoff]
        for k in stale:
            _app_clients_last_seen.pop(k, None)
        if APP_CLIENT_ID_RE.fullmatch(cid):
            _app_clients_last_seen[cid] = now
        return len(_app_clients_last_seen)


def _active_clients_snapshot() -> dict[str, float]:
    """Copy of the heartbeat map for ``rack_locks`` (offline-claim detection)."""
    with _app_clients_lock:
        return dict(_app_clients_last_seen)
