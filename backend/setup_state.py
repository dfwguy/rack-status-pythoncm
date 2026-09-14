# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Whether this deployment still needs to be set up.

Author: Rafael M. Koike & Cursor Agent ("Ada")

A first boot creates an administrator, a policy and a certificate, and then lands the operator
on a dashboard describing a cluster nobody has configured. The wizard fills that gap, and this
module answers the only question it needs: should it run?

**"No project exists" is not that question, and that is the surprise here.** `catalog.seed`
creates a `default` project on first boot from whatever environment it finds and marks it
active, so the projects table is never empty -- a wizard waiting for an empty table would never
appear. What distinguishes a fresh deployment is that nobody has *confirmed* a project:

- a project an operator created (``source != "seed"``) means setup happened, even if this flag
  was never written -- which is what stops an upgraded deployment showing the wizard;
- ``setup_complete`` in the catalog's own meta table records a wizard that finished, so a
  deployment whose only project is still the seeded one does not get asked twice;
- ``NVIS_FORCE_SETUP_WIZARD`` re-opens it deliberately, which is what ``cli.py serve
  --setup-wizard`` sets.

The flag lives in the catalog store rather than on the administrator's identity record, because
what it describes is the catalog: a second administrator should not be asked to configure a
cluster the first one already configured.
"""

from __future__ import annotations

import logging

from catalog import models
from catalog.store import TABLE_PROJECTS, get_repo
from config_env import _eget

_logger = logging.getLogger(__name__)

#: Meta key in the catalog store. Beside `active` and `schema_version`, which is where the rest
#: of the catalog's own state already lives.
META_KEY = "setup_complete"


def _forced() -> bool:
    raw = (_eget("NVIS_FORCE_SETUP_WIZARD", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def operator_projects() -> list[models.Project]:
    """Projects somebody configured, as opposed to the one seeding invented from the environment."""
    out: list[models.Project] = []
    for raw in get_repo().list(TABLE_PROJECTS):
        project = models.Project.from_dict(raw)
        if (project.source or "").strip().lower() != "seed":
            out.append(project)
    return out


def mark_complete(project_id: str = "") -> None:
    """Record that setup finished, so the wizard stops opening on every login."""
    get_repo().set_meta(META_KEY, {"done": True, "project": project_id})


def reopen() -> None:
    """Forget the flag. The 'run the wizard again' half of the Projects screen entry point."""
    get_repo().set_meta(META_KEY, {"done": False, "project": ""})


def status() -> dict[str, object]:
    """Why the wizard is or is not required, in the shape `/api/auth-check` returns.

    The reason is part of the answer on purpose: "required" with no explanation is the kind of
    thing that gets debugged by reading source, and the three causes are genuinely different.
    """
    try:
        repo = get_repo()
        meta = repo.get_meta(META_KEY) or {}
        recorded = bool(meta.get("done")) if isinstance(meta, dict) else bool(meta)
        configured = operator_projects()
    except Exception:
        # A catalog that cannot be read is not a reason to trap an operator in a wizard that
        # also cannot write. Report "not required" and let the dashboard's own error paths say
        # what is wrong -- the same call the password-change flag makes one function over.
        _logger.exception("setup: could not read catalog state")
        return {"required": False, "reason": "catalog-unreadable", "forced": False}

    if _forced():
        return {
            "required": True,
            "reason": "forced",
            "forced": True,
            "projects": [p.id for p in configured],
        }
    if configured:
        return {
            "required": False,
            "reason": "project-configured",
            "forced": False,
            "projects": [p.id for p in configured],
        }
    if recorded:
        return {"required": False, "reason": "already-completed", "forced": False, "projects": []}
    return {"required": True, "reason": "no-project-configured", "forced": False, "projects": []}


def required() -> bool:
    return bool(status().get("required"))
