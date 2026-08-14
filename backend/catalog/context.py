# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Active project/cluster context resolution and env overlays.

This module is the seam that makes the rest of the backend platform- and project-aware
without rewriting it. It resolves the *active* project/cluster (or any explicitly named
one) and produces two env overlays consumed by ``app.py``:

* :func:`high_overlay` -- authoritative identity/naming/connection values derived from
  the cluster doc (``NVIS_*``, ``RACK_PLATFORM``, SSH/exec, site prefix, customer,
  product line). These **win** over ``job_env_overrides`` and ``os.environ`` so that
  switching the active cluster actually re-scopes the app.
* :func:`low_overlay` -- platform-geometry-derived *defaults* (validation CT range,
  GPUs/tray, nodes/rack) plus the platform's ``tool_param_defaults``. These are used
  only when nothing more specific is set (lowest precedence), so an explicit env/Settings
  value always wins.

It also builds the AI context envelope (:func:`build_ai_context_block`) and the
compatibility-filtered tool list for a cluster (:func:`tools_for_cluster`).

No import of ``app`` here (avoids cycles); ``app.py`` calls into this module.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from . import models
from .store import (
    TABLE_PLATFORMS,
    TABLE_PROJECTS,
    TABLE_TOOLS,
    Repo,
    current_generation,
    get_repo,
)

_logger = logging.getLogger(__name__)

# Cache of (high, low) overlays for the active context, keyed by the store generation.
# ``_eget`` calls this on every config lookup, so resolving TinyDB each time is avoided.
_overlay_cache: dict[str, Any] = {"gen": -1, "sel": None, "high": {}, "low": {}}

# Hooks invoked when the catalog changes (active switch or doc edits). ``app.py`` uses
# this to re-materialize the active overlay into ``os.environ`` and clear naming caches
# so ``os.environ``-direct readers (nvis_id_templates, host_exec) stay consistent.
_change_hooks: list[Callable[[], None]] = []


def add_change_hook(fn: Callable[[], None]) -> None:
    _change_hooks.append(fn)


def notify_changed() -> None:
    # Iterate a copy: a hook may register or drop another hook while running, and mutating the
    # list mid-iteration would raise. Not a redundant cast.
    for fn in list(_change_hooks):  # noqa: PERF101
        try:
            fn()
        except Exception:  # pragma: no cover - hooks must never break a request
            _logger.debug("notify_changed: ignoring error", exc_info=True)


def overlays_for_active() -> tuple[dict[str, str], dict[str, str]]:
    """Return cached ``(high, low)`` overlays for the active context.

    Invalidated automatically whenever the catalog changes (store generation bump).
    """
    gen = current_generation()
    if _overlay_cache["gen"] == gen:
        return _overlay_cache["high"], _overlay_cache["low"]
    try:
        project, cluster, platform = resolve()
        high = high_overlay(project, cluster, platform)
        low = low_overlay(platform, cluster)
    except Exception:  # never let catalog issues break config resolution
        _logger.debug("overlays_for_active: ignoring error", exc_info=True)
        high, low = {}, {}
    _overlay_cache.update({"gen": gen, "high": high, "low": low})
    return high, low


# ---------------------------------------------------------------------------
# Active context selection
# ---------------------------------------------------------------------------


def get_active(repo: Repo | None = None) -> dict[str, str]:
    """Return ``{"project": id, "cluster": id}`` for the active context.

    Falls back to the first project/cluster when ``meta.active`` is unset or stale.
    """
    repo = repo or get_repo()
    active = repo.get_meta("active") or {}
    project_id = (active.get("project") or "").strip() if isinstance(active, dict) else ""
    cluster_id = (active.get("cluster") or "").strip() if isinstance(active, dict) else ""

    projects = repo.list(TABLE_PROJECTS)
    if not projects:
        return {"project": "", "cluster": ""}

    proj = next((p for p in projects if p.get("id") == project_id), None) or projects[0]
    clusters = proj.get("clusters") or []
    cluster = next((c for c in clusters if c.get("id") == cluster_id), None)
    if cluster is None and clusters:
        cluster = clusters[0]
    return {
        "project": proj.get("id", ""),
        "cluster": (cluster or {}).get("id", "") if cluster else "",
    }


def set_active(project_id: str, cluster_id: str, repo: Repo | None = None) -> dict[str, str]:
    """Validate and persist the active project/cluster. Returns the stored selection."""
    repo = repo or get_repo()
    proj_raw = repo.get(TABLE_PROJECTS, project_id)
    if proj_raw is None:
        raise ValueError(f"unknown project: {project_id!r}")
    proj = models.Project.from_dict(proj_raw)
    if cluster_id and proj.get_cluster(cluster_id) is None:
        raise ValueError(f"unknown cluster {cluster_id!r} in project {project_id!r}")
    if not cluster_id and proj.clusters:
        cluster_id = proj.clusters[0].id
    sel = {"project": project_id, "cluster": cluster_id}
    repo.set_meta("active", sel)
    notify_changed()
    return sel


def resolve(
    project_id: str | None = None,
    cluster_id: str | None = None,
    repo: Repo | None = None,
) -> tuple[models.Project | None, models.Cluster | None, models.Platform | None]:
    """Resolve (project, cluster, platform). Uses active context when args omitted."""
    repo = repo or get_repo()
    if not project_id or not cluster_id:
        active = get_active(repo)
        project_id = project_id or active.get("project")
        cluster_id = cluster_id or active.get("cluster")

    proj_raw = repo.get(TABLE_PROJECTS, project_id) if project_id else None
    if proj_raw is None:
        return None, None, None
    project = models.Project.from_dict(proj_raw)
    cluster = project.get_cluster(cluster_id) if cluster_id else (
        project.clusters[0] if project.clusters else None
    )
    platform = None
    if cluster and cluster.platform_id:
        plat_raw = repo.get(TABLE_PLATFORMS, cluster.platform_id)
        if plat_raw is not None:
            platform = models.Platform.from_dict(plat_raw)
    return project, cluster, platform


# ---------------------------------------------------------------------------
# Env overlays
# ---------------------------------------------------------------------------


def _put(d: dict[str, str], key: str, value: str | None) -> None:
    if value is not None and str(value).strip():
        d[key] = str(value).strip()


def high_overlay(
    project: models.Project | None = None,
    cluster: models.Cluster | None = None,
    platform: models.Platform | None = None,
    repo: Repo | None = None,
) -> dict[str, str]:
    """Authoritative identity/naming/connection env from the active (or given) cluster."""
    if cluster is None:
        project, cluster, platform = resolve(repo=repo)
    if cluster is None:
        return {}
    out: dict[str, str] = {}
    n = cluster.naming
    _put(out, "NVIS_RACK_ID", n.rack_id_template)
    _put(out, "NVIS_CT_ID", n.ct_id_template)
    _put(out, "NVIS_NVSW_ID", n.nvsw_id_template)
    _put(out, "NVIS_CT_JOINER", n.ct_joiner)
    _put(out, "NVIS_SLURM_HOSTLIST", n.slurm_hostlist)
    _put(out, "RACK_PLATFORM", cluster.cluster_manager)
    _put(out, "RACK_MULTIRACK_SITE_PREFIX", cluster.site_prefix)
    c = cluster.connection
    _put(out, "RACK_EXEC_MODE", c.exec_mode)
    _put(out, "RACK_SSH_HOST", c.ssh_host)
    _put(out, "RACK_SSH_USER", c.ssh_user)
    _put(out, "RACK_SSH_KEY", c.ssh_key)
    _put(out, "RACK_REMOTE_BASE_PATH", c.remote_base_path)
    _put(out, "RACK_CLUSTER_NAME", cluster.name)
    if project is not None:
        _put(out, "RACK_CLUSTER_CUSTOMER_NAME", project.customer)
    if platform is not None:
        product = platform.tool_param_defaults.get("RACK_CLUSTER_PRODUCT_LINE") or platform.gpu_model
        _put(out, "RACK_CLUSTER_PRODUCT_LINE", product)
        _put(out, "RACK_CLUSTER_HW_VENDOR", platform.system_vendor)
    return out


def geometry_defaults(platform: models.Platform, cluster: models.Cluster | None = None) -> dict[str, str]:
    """Derive low-precedence geometry env from the platform (cluster may override trays)."""
    g = platform.geometry
    trays = g.trays_per_rack
    if cluster is not None and cluster.geometry.trays_per_rack > 0:
        trays = cluster.geometry.trays_per_rack
    out: dict[str, str] = {}
    if trays > 0:
        out["RACK_VALIDATION_CT_RANGE"] = f"01-{trays:02d}"
        out["RACK_EXPORT_NODES_PER_RACK"] = str(trays)
        if g.gpus_per_rack > 0:
            per_tray = max(1, round(g.gpus_per_rack / trays))
            out["RACK_CLUSTER_GPU_PER_TRAY"] = str(per_tray)
    # GPUs per compute node (NVL=4, HGX=8, …) so launchers don't assume a generation.
    # Lowest precedence: an explicit env or on-node nvidia-smi still wins.
    if g.gpus_per_node > 0:
        out["GPUS_PER_NODE"] = str(g.gpus_per_node)
    return out


def fabric_defaults(platform: models.Platform) -> dict[str, str]:
    """Derive low-precedence fabric env from the platform's expected fabric shape.

    Feeds ``bin/scripts/lib/fabric.inc.sh``:
      * ``NVIS_RDMA_LINK_TECH`` selects IB vs RoCE rail classification.
      * ``NVIS_FABRIC_EXPECT_RAILS`` / ``NVIS_FABRIC_EXPECT_RATE`` let the fabric
        probe flag detected-vs-expected discrepancies (e.g. an 800G RA at 400G).
    The per-node ``ns_ifname`` is intentionally NOT forced here so the on-node
    auto-detect stays authoritative (the profile value is a documentation hint).
    """
    f = platform.fabric
    out: dict[str, str] = {}
    if f.scaleout in ("ib", "roce"):
        _put(out, "NVIS_RDMA_LINK_TECH", f.scaleout)
    gpn = platform.geometry.gpus_per_node
    if f.rails_per_gpu > 0 and gpn > 0:
        _put(out, "NVIS_FABRIC_EXPECT_RAILS", str(f.rails_per_gpu * gpn))
    if f.rail_rate_gbps > 0:
        _put(out, "NVIS_FABRIC_EXPECT_RATE", str(f.rail_rate_gbps))
    return out


def low_overlay(
    platform: models.Platform | None = None,
    cluster: models.Cluster | None = None,
    repo: Repo | None = None,
) -> dict[str, str]:
    """Geometry + fabric defaults + platform ``tool_param_defaults`` (lowest precedence)."""
    if platform is None:
        _project, cluster, platform = resolve(repo=repo)
    if platform is None:
        return {}
    out: dict[str, str] = {}
    out.update(geometry_defaults(platform, cluster))
    out.update(fabric_defaults(platform))
    # platform tool_param_defaults are defaults too (env/Settings/per-call win).
    for k, v in platform.tool_param_defaults.items():
        _put(out, k, v)
    return out


def tool_param_defaults(
    platform: models.Platform | None = None, repo: Repo | None = None
) -> dict[str, str]:
    """Just the platform's tool_param_defaults (for injecting into subprocess env)."""
    if platform is None:
        _p, _c, platform = resolve(repo=repo)
    return dict(platform.tool_param_defaults) if platform else {}


# ---------------------------------------------------------------------------
# Compatibility-filtered tools
# ---------------------------------------------------------------------------


def tools_for_cluster(
    cluster_id: str | None = None,
    project_id: str | None = None,
    repo: Repo | None = None,
    *,
    surface: str | None = None,
) -> list[dict[str, Any]]:
    """Return tool docs compatible with the cluster's platform, newest schema shape.

    Optionally filter to a UI ``surface`` (e.g. ``rackDetail.hardware``). Each returned
    dict gains ``compatible_with_platform: True`` for clarity.
    """
    repo = repo or get_repo()
    _project, _cluster, platform = resolve(project_id, cluster_id, repo=repo)
    out: list[dict[str, Any]] = []
    for raw in repo.list(TABLE_TOOLS):
        tool = models.Tool.from_dict(raw)
        if not tool.compatible.matches(platform):
            continue
        if surface and surface not in tool.surfaces:
            continue
        d = tool.to_dict()
        d["compatible_with_platform"] = True
        out.append(d)
    out.sort(key=lambda d: (d.get("category", ""), d.get("name", "")))
    return out


# ---------------------------------------------------------------------------
# AI context envelope
# ---------------------------------------------------------------------------


def build_ai_context_block(
    cluster_id: str | None = None,
    project_id: str | None = None,
    repo: Repo | None = None,
) -> str:
    """Compact, authoritative platform+cluster+customer block for AI prompts.

    Always safe to call; returns "" only when no project exists at all.
    """
    project, cluster, platform = resolve(project_id, cluster_id, repo=repo)
    if project is None and cluster is None and platform is None:
        return ""
    lines: list[str] = ["Deployment context (authoritative — use these facts):"]
    if project is not None:
        if project.customer:
            lines.append(f"- Customer: {project.customer}")
        lines.append(f"- Project: {project.name}")
    if cluster is not None:
        lines.append(
            f"- Cluster: {cluster.name} (manager: {cluster.cluster_manager or 'unknown'})"
        )
        if cluster.naming.rack_id_template:
            joiner = cluster.naming.ct_joiner or "-ct"
            lines.append(
                f"- Naming: rack='{cluster.naming.rack_id_template}', "
                f"node='{cluster.naming.ct_id_template}' (tray joiner '{joiner}')"
            )
    if platform is not None:
        g = platform.geometry
        lines.append(
            f"- Platform: {platform.name} "
            f"[{platform.gpu_model or '?'} GPU, {platform.cpu_vendor or '?'} CPU, "
            f"{platform.system_vendor or '?'} {platform.form_factor or ''}]".rstrip()
        )
        lines.append(
            f"- Geometry: {g.gpus_per_rack} GPUs/rack across {g.trays_per_rack} trays "
            f"({max(1, round(g.gpus_per_rack / g.trays_per_rack)) if g.trays_per_rack else '?'} GPUs/tray); "
            f"NVLink domain {g.nvlink_domain}."
        )
        ic = platform.interconnect or {}
        if ic:
            lines.append(
                f"- Interconnect: scale-up {ic.get('scaleup', '?')}, "
                f"scale-out {ic.get('scaleout', '?')}."
            )
        if platform.capabilities:
            lines.append(f"- Capabilities: {', '.join(platform.capabilities)}.")
        if platform.status and platform.status != "ga":
            lines.append(f"- NOTE: platform status is '{platform.status}'.")
        lines.append(
            "- When interpreting logs/results or recommending tools, use this platform's "
            "GPU model, geometry and topology; do not assume a different generation."
        )
    return "\n".join(lines)
