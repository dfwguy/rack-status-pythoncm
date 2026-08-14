# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Import the NVIDIA NPI Knowledge Portal tool catalog into the NVIS catalog.

The NPI team curates the canonical validation/performance test catalog as TOML records
(``reports/catalog/tools/*.toml``) in the ``NPI-Knowledge-Portal`` repo on
``gitlab-master.nvidia.com``. That catalog is our *source of truth* for which tools exist
and how they are classified. This module reads those TOML records and mirrors them into
the ``npi_tools`` table as :class:`catalog.models.NpiTool` documents.

Two things to keep straight:

* :class:`~catalog.models.NpiTool` is a **reference/taxonomy** record. It preserves the
  full NPI envelope (portfolio_role, capability, phases, layers, per-check ``tests``,
  relationships) and is never executed directly.
* :class:`~catalog.models.Tool` is a **runnable local script**. Selected NPI entries are
  *promoted* into runnable tools (namespaced ``npi-<slug>`` ids) via
  ``NpiTool.to_tool_stub`` once an operator wires a launcher; the import never touches the
  hand-authored ``tools`` table on its own.

Source resolution (first that exists wins), all pointing at a directory of ``*.toml``:

1. ``$NVIS_NPI_CATALOG_DIR``
2. ``var/cache/npi_catalog/tools`` (populated by ``bin/scripts/fetch_npi_catalog.sh``)
3. ``backend/catalog/npi_seed/tools`` (an optional vendored offline snapshot)

The import is a **mirror**: records present upstream are upserted; with ``prune=True``
(default) previously-imported records no longer present upstream are removed. Operator
edits are not expected on reference records, so they are intentionally overwritten.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomllib

from . import models
from .store import REPO_ROOT, TABLE_NPI_TOOLS, Repo

# Upstream provenance (for the fetch script + audit trail).
NPI_CATALOG_REPO = "https://gitlab-master.nvidia.com/pshamis/NPI-Knowledge-Portal"
NPI_CATALOG_SUBPATH = "reports/catalog/tools"


def candidate_dirs() -> list[Path]:
    """Ordered list of directories that may hold the NPI ``*.toml`` records."""
    dirs: list[Path] = []
    env_dir = (os.environ.get("NVIS_NPI_CATALOG_DIR") or "").strip()
    if env_dir:
        dirs.append(Path(env_dir))
    dirs.append(REPO_ROOT / "var" / "cache" / "npi_catalog" / "tools")
    dirs.append(Path(__file__).resolve().parent / "npi_seed" / "tools")
    return dirs


def resolve_source_dir(source_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the first existing catalog directory containing at least one ``*.toml``."""
    if source_dir:
        p = Path(source_dir)
        return p if _has_toml(p) else None
    for p in candidate_dirs():
        if _has_toml(p):
            return p
    return None


def _has_toml(p: Path) -> bool:
    try:
        return p.is_dir() and any(p.glob("*.toml"))
    except OSError:
        return False


def load_records(source_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Parse every ``*.toml`` in ``source_dir`` into raw record dicts.

    Each file is a single flat tool table. Files that fail to parse are skipped (the
    upstream catalog is validated in its own CI, but we degrade gracefully here). The
    originating filename is stamped onto ``source_ref`` for provenance.
    """
    root = Path(source_dir)
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.toml")):
        try:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if not isinstance(data, dict) or not (data.get("id") or "").strip():
            continue
        data.setdefault("source", "npi-catalog")
        data["source_ref"] = f"{NPI_CATALOG_SUBPATH}/{path.name}"
        records.append(data)
    return records


def to_npi_tool(record: dict[str, Any]) -> models.NpiTool:
    """Normalize a raw upstream record into an :class:`~catalog.models.NpiTool`."""
    return models.NpiTool.from_dict(record)


def import_npi_catalog(
    repo: Repo,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    prune: bool = True,
) -> dict[str, Any]:
    """Mirror the NPI catalog into ``npi_tools``. Idempotent. Returns a log summary."""
    summary: dict[str, Any] = {
        "source_dir": "",
        "found": 0,
        "added": 0,
        "updated": 0,
        "pruned": 0,
        "skipped": 0,
    }
    resolved = resolve_source_dir(source_dir)
    if resolved is None:
        return summary
    summary["source_dir"] = str(resolved)

    existing = {
        (row.get("id") or ""): row
        for row in repo.list(TABLE_NPI_TOOLS)
        if (row.get("source") or "npi-catalog") == "npi-catalog"
    }
    seen: set[str] = set()
    for record in load_records(resolved):
        tool = to_npi_tool(record)
        if tool.validate():
            summary["skipped"] += 1
            continue
        doc = tool.to_dict()
        prior = existing.get(tool.id)
        if prior is None:
            summary["added"] += 1
        elif prior != doc:
            summary["updated"] += 1
        repo.upsert(TABLE_NPI_TOOLS, doc)
        seen.add(tool.id)
        summary["found"] += 1

    if prune:
        for stale_id in set(existing) - seen:
            if repo.delete(TABLE_NPI_TOOLS, stale_id):
                summary["pruned"] += 1
    return summary
