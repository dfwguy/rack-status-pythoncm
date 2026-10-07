# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Per-cluster NVIS-P2P rosters -- read/write helpers over the ``Repo``.

A roster is a named list of eligible nodes an operation can target by name instead of the single
inventory. The set is stored whole as one document per ``cluster_id`` in ``TABLE_P2P_ROSTERS``.
"""
from __future__ import annotations

from catalog import models
from catalog.store import TABLE_P2P_ROSTERS, Repo, get_repo


def get_rosters(cluster_id: str, repo: Repo | None = None) -> models.P2pRosters:
    """This cluster's named rosters, or an empty set if none have been defined yet."""
    repo = repo or get_repo()
    raw = repo.get(TABLE_P2P_ROSTERS, cluster_id)
    if raw is None:
        return models.P2pRosters(cluster_id=cluster_id)
    # Force the id: a stored document describes the cluster it is keyed under, never another.
    return models.P2pRosters.from_dict({**raw, "cluster_id": cluster_id})


def save_rosters(rosters: models.P2pRosters, repo: Repo | None = None) -> models.P2pRosters:
    """Persist a cluster's rosters. Raises ``ValueError`` on an invalid document."""
    problems = rosters.validate()
    if problems:
        raise ValueError("; ".join(problems))
    repo = repo or get_repo()
    saved = repo.upsert(TABLE_P2P_ROSTERS, rosters.to_dict())
    return models.P2pRosters.from_dict({**saved, "cluster_id": rosters.cluster_id})
