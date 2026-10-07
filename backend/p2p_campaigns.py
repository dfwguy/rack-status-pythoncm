# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Per-cluster NVIS-P2P campaigns -- read/write helpers over the ``Repo``.

A campaign is a saved distribution: a catalog image paired with a roster and optional pacing, named
once and launched many times. The set is stored whole as one document per ``cluster_id`` in
``TABLE_P2P_CAMPAIGNS``; launching one enqueues a ``distribute`` operation (see
``backend/p2p_operations.py``) -- the definition itself holds no run state.
"""
from __future__ import annotations

from catalog import models
from catalog.store import TABLE_P2P_CAMPAIGNS, Repo, get_repo


def get_campaigns(cluster_id: str, repo: Repo | None = None) -> models.P2pCampaigns:
    """This cluster's saved campaigns, or an empty set if none have been defined yet."""
    repo = repo or get_repo()
    raw = repo.get(TABLE_P2P_CAMPAIGNS, cluster_id)
    if raw is None:
        return models.P2pCampaigns(cluster_id=cluster_id)
    # Force the id: a stored document describes the cluster it is keyed under, never another.
    return models.P2pCampaigns.from_dict({**raw, "cluster_id": cluster_id})


def save_campaigns(campaigns: models.P2pCampaigns, repo: Repo | None = None) -> models.P2pCampaigns:
    """Persist a cluster's campaigns. Raises ``ValueError`` on an invalid document."""
    problems = campaigns.validate()
    if problems:
        raise ValueError("; ".join(problems))
    repo = repo or get_repo()
    saved = repo.upsert(TABLE_P2P_CAMPAIGNS, campaigns.to_dict())
    return models.P2pCampaigns.from_dict({**saved, "cluster_id": campaigns.cluster_id})
