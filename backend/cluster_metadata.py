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
"""Operator-authored cluster dashboard metadata: the facts no command can report.

A small JSON file holding what a human knows and the cluster does not say about itself -- site, customer,
contract or campaign labels that the dashboard shows beside the machine-derived rows.

Separate from the catalog's Project/Cluster record on purpose: this is display metadata for one
deployment's dashboard, whereas the catalog record is the resolved identity the whole app runs on. If a
field here starts driving behaviour rather than captions, it belongs on the catalog record instead
(core-designs.mdc citizen 3), because that is what the cluster switcher and the AI context read.

Read by the validation launcher (to label a run), the metadata route and the cluster overview, which is
why it is here rather than beside any one of them.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path

from config_env import _env_int
from core import rpath as _rpath
from paths import REPO_ROOT

_logger = logging.getLogger(__name__)


_CLUSTER_DM_KEYS: tuple[str, ...] = ("product_line", "hw_vendor", "customer_name")


def _cluster_dashboard_metadata_path() -> Path:
    raw = (os.environ.get("RACK_CLUSTER_DASHBOARD_METADATA_FILE") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return (REPO_ROOT / "var" / "cache" / "cluster_dashboard_metadata.json").resolve()


def read_cluster_dashboard_metadata() -> dict[str, str]:
    """Load dashboard-saved cluster labels (product line, HW vendor, project/customer).

    Returns ``product_line``, ``hw_vendor``, ``customer_name`` (empty string when unset).
    """
    defaults = {k: "" for k in _CLUSTER_DM_KEYS}
    p = _cluster_dashboard_metadata_path()
    if not p.is_file():
        return dict(defaults)
    max_len = max(1, _env_int("RACK_CLUSTER_DASHBOARD_META_MAX_LEN", 512))
    try:
        with open(p, "r", encoding="utf-8") as f:
            locked = False
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                pass
            try:
                try:
                    raw = json.load(f)
                except json.JSONDecodeError:
                    raw = {}
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except (OSError, UnicodeDecodeError, TypeError):
        return dict(defaults)
    if not isinstance(raw, dict):
        return dict(defaults)
    out = dict(defaults)
    for k in _CLUSTER_DM_KEYS:
        v = raw.get(k)
        if isinstance(v, str):
            t = v.replace("\n", " ").replace("\r", " ").strip()
            if t:
                out[k] = t[:max_len]
    return out
