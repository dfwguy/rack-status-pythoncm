# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Per-cluster NVIS-P2P catalog -- read/write helpers over the ``Repo``.

The catalog is the list of distributable images ("tasks") for a cluster. It is edited here as a
Rack Status document, keyed by ``cluster_id`` in ``TABLE_P2P_CATALOG``, and a sync projects it onto
the instance's ``catalog_path`` so the fabric distributes what the operator authored. The document
is the source of truth; the file on the cluster is downstream of it.
"""
from __future__ import annotations

from catalog import models
from catalog.store import TABLE_P2P_CATALOG, Repo, get_repo


def get_catalog(cluster_id: str, repo: Repo | None = None) -> models.P2pCatalog:
    """This cluster's edited catalog, or an empty one if none has been authored yet."""
    repo = repo or get_repo()
    raw = repo.get(TABLE_P2P_CATALOG, cluster_id)
    if raw is None:
        return models.P2pCatalog(cluster_id=cluster_id)
    # Force the id: a stored document describes the cluster it is keyed under, never another.
    return models.P2pCatalog.from_dict({**raw, "cluster_id": cluster_id})


def has_catalog(cluster_id: str, repo: Repo | None = None) -> bool:
    """Whether an edited catalog exists for this cluster (vs. falling back to the shipped/file one)."""
    repo = repo or get_repo()
    return repo.get(TABLE_P2P_CATALOG, cluster_id) is not None


def save_catalog(catalog: models.P2pCatalog, repo: Repo | None = None) -> models.P2pCatalog:
    """Persist a cluster's catalog. Raises ``ValueError`` on an invalid document."""
    problems = catalog.validate()
    if problems:
        raise ValueError("; ".join(problems))
    repo = repo or get_repo()
    saved = repo.upsert(TABLE_P2P_CATALOG, catalog.to_dict())
    return models.P2pCatalog.from_dict({**saved, "cluster_id": catalog.cluster_id})
