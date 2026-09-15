# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""On-disk cache for the analytics aggregate, and the fingerprint that keys it.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The aggregate is minutes of log scanning over the whole corpus, so it is computed rarely and read
often -- by the Analytics view, and by the Rack Status PDF, which needs the same numbers for its
rack-level charts but must never trigger a scan of its own. Those two now live in different API
modules, which is why the cache is here rather than in either.

Keyed by resolved log directory, with a fingerprint of the rack rows the caller sent, so the same
inputs hit and changed inputs miss. Persisted as JSON under var/cache: surviving a restart is the
point, since the alternative is re-scanning a 2 GB log directory to redraw a chart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from json_db import atomic_write_json
from paths import REPO_ROOT

_logger = logging.getLogger(__name__)


_analytics_aggregate_cache_lock = threading.Lock()


_ANALYTICS_AGG_CACHE_PATH = (REPO_ROOT / "var" / "cache" / "analytics_aggregate_cache.json").resolve()


def _analytics_rows_fingerprint(rows_in: object) -> str:
    """Stable hash of rack status rows sent with analytics (same inputs → same key)."""
    if not isinstance(rows_in, list):
        return hashlib.sha256(b"[]").hexdigest()
    pairs: list[tuple[str, str]] = []
    for ent in rows_in:
        if not isinstance(ent, dict):
            continue
        rk = str(ent.get("rack", "")).strip().lower()
        nm = str(ent.get("nemo", "")).strip().lower()
        if rk:
            pairs.append((rk, nm))
    pairs.sort()
    raw = json.dumps(pairs, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _analytics_aggregate_cache_get(log_dir_key: str) -> dict[str, object] | None:
    """Return last stored payload for *log_dir_key* (resolved absolute path string), or ``None``."""
    with _analytics_aggregate_cache_lock:
        try:
            with open(_ANALYTICS_AGG_CACHE_PATH, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
            return None
    if not isinstance(doc, dict) or int(doc.get("version") or 0) != 1:
        return None
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        return None
    hit = entries.get(log_dir_key)
    return hit if isinstance(hit, dict) else None


def _analytics_aggregate_cache_put(log_dir_key: str, payload: dict[str, object]) -> None:
    """Persist ``analyzed_at``, ``rows_fingerprint``, ``single_rack``, ``multirack`` for a log directory."""
    _ANALYTICS_AGG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    store = {
        k: payload[k]
        for k in ("analyzed_at", "rows_fingerprint", "single_rack", "multirack")
        if k in payload
    }
    with _analytics_aggregate_cache_lock:
        doc: dict[str, object] = {"version": 1, "entries": {}}
        try:
            if _ANALYTICS_AGG_CACHE_PATH.is_file():
                with open(_ANALYTICS_AGG_CACHE_PATH, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                if isinstance(prev, dict) and int(prev.get("version") or 0) == 1 and isinstance(prev.get("entries"), dict):
                    doc["entries"] = dict(prev["entries"])
        except (OSError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
            pass
        ent: dict[str, object] = {}
        prev_entries = doc.get("entries")
        if isinstance(prev_entries, dict):
            ent = dict(prev_entries)
        doc["entries"] = ent
        ent[log_dir_key] = store
        atomic_write_json(_ANALYTICS_AGG_CACHE_PATH, doc, sort_keys=True)
