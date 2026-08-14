# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""NVIS catalog: data-driven Platforms, Tools, and Customer Projects.

This package decouples NVIS Rack Status from a single hardware generation (GB200)
by storing three schemas as documents in a small, swappable key/value store:

* **Platforms** (``models.Platform``) -- hardware/system SKUs (GPU + CPU + vendor +
  topology + geometry + capabilities). NOT the same as ``backend/platforms/`` which
  models the *cluster manager* (BCM/MAAS); that becomes a per-cluster field here.
* **Tools** (``models.Tool``) -- each validation/performance/maintenance script,
  described as a document declaring which platforms it supports and where it surfaces
  in the UI. Replaces the hard-wired ``RACK_*_SCRIPT`` registry.
* **Projects/Clusters** (``models.Project`` / ``models.Cluster``) -- customer/site
  definitions with naming convention + geometry + connection. Generalizes the
  scattered ``NVIS_*`` templates, ``cluster_dashboard_metadata.json`` and SSH env.

Storage is TinyDB behind the :class:`catalog.store.Repo` interface so a DynamoDB /
cloud backend can be dropped in later without touching callers.
"""
from __future__ import annotations

from .store import Repo, TinyDBRepo, get_repo, set_repo

__all__ = ["Repo", "TinyDBRepo", "get_repo", "set_repo"]
