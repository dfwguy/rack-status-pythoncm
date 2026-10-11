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

import json
from typing import Any

from catalog import models
from catalog.store import TABLE_P2P_CATALOG, TABLE_PROJECTS, Repo, get_repo


def get_catalog(cluster_id: str, repo: Repo | None = None) -> models.P2pCatalog:
    """This cluster's edited catalog, or an empty one if none has been authored yet."""
    repo = repo or get_repo()
    raw = repo.get(TABLE_P2P_CATALOG, cluster_id)
    if raw is None:
        return models.P2pCatalog(cluster_id=cluster_id)
    # Force the id: a stored document describes the cluster it is keyed under, never another.
    return models.P2pCatalog.from_dict({**raw, "cluster_id": cluster_id})


def image_for_tool(cluster_id: str, tool_id: str, repo: Repo | None = None) -> models.P2pCatalogImage | None:
    """The image this cluster's catalog names for *tool_id* through ``required_by``, or ``None``.

    ``required_by`` is how a tool names the image it runs (ROADMAP: a job names the catalog image it
    needs rather than taking whichever file sorts first), so the choice is data an operator edits in
    the Catalog Editor, not a path in a script or a launch body. Two images naming the same tool is
    a catalog that has not decided, and raises ``ValueError`` rather than picking one.
    """
    want = (tool_id or "").strip()
    if not want:
        return None
    named = [img for img in get_catalog(cluster_id, repo).images if want in img.required_by]
    if len(named) > 1:
        names = ", ".join(img.filename for img in named)
        raise ValueError(f"more than one catalog image is required by {want!r} ({names}); keep exactly one")
    return named[0] if named else None


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


# ---------------------------------------------------------------------------
# The catalog the launchers read
# ---------------------------------------------------------------------------

#: The catalog's files in the ``config/`` of the scripts the cluster runs (``script_tree.config_dir``). Read by
#: ``bin/scripts/lib/node_images.inc.sh`` to check a node's image before a launch. Separate from the
#: P2P origin's ``images.json``/``images.tsv``, which the P2P sync owns.
CONFIG_JSON = "image-catalog.json"
CONFIG_TSV = "image-catalog.tsv"


def manifest_files(catalog: models.P2pCatalog, canonical_directory: str = "") -> dict[str, bytes]:
    """The catalog as JSON and TSV, in exactly the operations worker's manifest format.

    The worker's sync (``deploy/p2p/scripts/operations-worker.py:_sync_catalog``) writes the same two
    shapes at the P2P origin, so a reader of either copy parses one format.
    """
    # The same projection the operations request carries (``P2pCatalog.to_dict()["images"]``).
    images = catalog.to_dict()["images"]
    doc = {"schema_version": 1, "catalog_name": "authored",
           "canonical_directory": canonical_directory, "images": images}
    rows = ["order\tname\tfilename\tbytes\tsha256"]
    rows += [f"{i.get('order', 0)}\t{i.get('name', '')}\t{i.get('filename', '')}\t"
             f"{i.get('bytes', 0)}\t{i.get('sha256', '')}" for i in images]
    return {
        CONFIG_JSON: json.dumps(doc, indent=2).encode("utf-8"),
        CONFIG_TSV: ("\n".join(rows) + "\n").encode("utf-8"),
    }


def _placement(cluster_id: str, repo: Repo) -> tuple[str, str] | None:
    """``(project_id, platform_id)`` of the cluster, which decide whose scripts it runs."""
    for raw in repo.list(TABLE_PROJECTS):
        project = models.Project.from_dict(raw)
        cluster = project.get_cluster(cluster_id)
        if cluster is not None:
            return project.id, cluster.platform_id
    return None


def publish_to_scripts(cluster_id: str, *, repo: Repo | None = None, profile: str = "script") -> dict[str, Any]:
    """Publish this cluster's authored catalog into the ``config/`` of the scripts it runs.

    The authored document stays the source of truth; this is a projection of it for script
    execution, re-published on every catalog save and script deploy.
    """
    import node_config
    import p2p_config

    repo = repo or get_repo()
    if not has_catalog(cluster_id, repo):
        return {"ok": False, "error": "no authored P2P catalog for this cluster"}
    placement = _placement(cluster_id, repo)
    if placement is None:
        return {"ok": False, "error": f"cluster {cluster_id!r} belongs to no project"}
    project_id, platform_id = placement
    catalog = get_catalog(cluster_id, repo)
    settings = p2p_config.get_settings(cluster_id, repo)
    summary = node_config.publish(manifest_files(catalog, settings.container_path),
                                  project_id=project_id, platform_id=platform_id, profile=profile)
    summary["images"] = len(catalog.images)
    return summary
