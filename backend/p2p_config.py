# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike
"""Per-cluster NVIS-P2P instance configuration -- read/write helpers over the ``Repo``.

The gateway and the reconcile workflow both need one cluster's P2P settings, so the get/save logic
lives here (the module that owns the concern) rather than beside either caller. The document is
stored behind the catalog ``Repo`` in ``TABLE_P2P_SETTINGS``, keyed by cluster id -- a document,
not a relation (citizen 15).

A cluster that has never been configured reads back the defaults rather than ``None``: an operator
enabling P2P should see a sensible starting point to edit, and the reconcile should be able to say
precisely which addresses are still missing rather than "no settings".
"""

from __future__ import annotations

from catalog import models
from catalog.store import TABLE_P2P_SETTINGS, TABLE_PROJECTS, Repo, get_repo


def _cluster(cluster_id: str, repo: Repo) -> models.Cluster | None:
    for raw in repo.list(TABLE_PROJECTS):
        cluster = models.Project.from_dict(raw).get_cluster(cluster_id)
        if cluster is not None:
            return cluster
    return None


def get_settings(cluster_id: str, repo: Repo | None = None) -> models.P2pSettings:
    """This cluster's P2P settings, or the defaults if none are stored yet.

    The image directories come from the cluster record where it states them
    (``P2pSettings.aligned_to``): the cluster is the one source of truth for where images live, so
    the reconcile, the operations worker and the launchers can never disagree about it.
    """
    repo = repo or get_repo()
    raw = repo.get(TABLE_P2P_SETTINGS, cluster_id)
    if raw is None:
        settings = models.P2pSettings(cluster_id=cluster_id)
    else:
        # Force the id: a stored document must describe the cluster it is keyed under, never another.
        settings = models.P2pSettings.from_dict({**raw, "cluster_id": cluster_id})
    cluster = _cluster(cluster_id, repo)
    return settings.aligned_to(cluster) if cluster is not None else settings


def save_settings(settings: models.P2pSettings, repo: Repo | None = None) -> models.P2pSettings:
    """Persist a cluster's P2P settings. Raises ``ValueError`` on an invalid document."""
    problems = settings.validate()
    if problems:
        raise ValueError("; ".join(problems))
    repo = repo or get_repo()
    saved = repo.upsert(TABLE_P2P_SETTINGS, settings.to_dict())
    return models.P2pSettings.from_dict({**saved, "cluster_id": settings.cluster_id})
