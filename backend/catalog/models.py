"""Document models for the NVIS catalog (platforms, tools, projects/clusters).

These are intentionally light dataclasses with ``from_dict`` / ``to_dict`` round-trips
and a ``validate()`` that returns a list of human-readable problems (empty == valid).
They are stored as plain dicts in the :mod:`catalog.store` repository, so unknown keys
are tolerated on read (forward-compat) and normalized on write.

Naming note: a :class:`Platform` here is a *hardware/system SKU*. The cluster manager
(BCM/MAAS) lives on :class:`Cluster.cluster_manager`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# Vocabulary kept permissive on purpose -- new GPUs/vendors must not require a code
# change. These lists drive UI hints/validation warnings, not hard rejection.
GPU_MODELS = ("B200", "B300", "GB200", "GB300", "R200", "VR200", "H200", "H100")
CPU_VENDORS = ("Grace", "Intel", "AMD")
SYSTEM_VENDORS = ("NVIDIA", "DGX", "Dell", "SMC", "Supermicro")
FORM_FACTORS = ("NVL72", "NVL36", "HGX-8", "HGX-4", "MGX")
CLUSTER_MANAGERS = ("bcm", "maas")
TOOL_CATEGORIES = (
    "validation",
    "performance",
    "hardware",
    "networking",
    "maintenance",
    "setup",
)
EXEC_MODES = ("async", "sync", "stream")
EXEC_TARGETS = ("rack", "multirack", "cluster", "node")
STATUSES = ("ga", "preview", "future", "deprecated")


def slugify(value: str) -> str:
    """Turn a label into a stable id slug (lowercase, dash-separated)."""
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return s[:63] or "item"


def valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))


def _as_str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _as_str_list(v: Any) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


def _as_dict(v: Any) -> dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


# ---------------------------------------------------------------------------
# Platform (hardware / system SKU)
# ---------------------------------------------------------------------------


@dataclass
class PlatformGeometry:
    """Physical shape of one rack of this platform.

    ``trays_per_rack`` x ``gpus_per_node`` x ``nodes_per_tray`` normally equals
    ``gpus_per_rack``; ``nvlink_domain`` is the scale-up domain size (e.g. 72 for NVL72).
    """

    gpus_per_node: int = 4
    nodes_per_tray: int = 2
    trays_per_rack: int = 18
    gpus_per_rack: int = 72
    nvlink_domain: int = 72

    @classmethod
    def from_dict(cls, d: Any) -> "PlatformGeometry":
        d = _as_dict(d)

        def _i(key: str, default: int) -> int:
            try:
                return int(d.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            gpus_per_node=_i("gpus_per_node", 4),
            nodes_per_tray=_i("nodes_per_tray", 2),
            trays_per_rack=_i("trays_per_rack", 18),
            gpus_per_rack=_i("gpus_per_rack", 72),
            nvlink_domain=_i("nvlink_domain", 72),
        )


@dataclass
class PlatformFabric:
    """Expected scale-out fabric shape (drives NIC auto-map + validation).

    Consumed by ``bin/scripts/lib/fabric.inc.sh`` (via low-overlay env) to pick the
    compute rails and to flag detected-vs-expected discrepancies (e.g. an 800G RA
    that comes up 400G). ``scaleout`` == ``ib`` or ``roce``; ``rail_rate_gbps`` is
    the expected per-rail line rate (400 for NDR, 800 for XDR/CX-8).
    """

    scaleout: str = ""  # ib | roce (scale-out technology)
    rails_per_gpu: int = 1  # compute NICs per GPU
    rail_rate_gbps: int = 0  # expected per-rail rate (400, 800); 0 == unknown
    ns_ifname: str = ""  # north-south / bootstrap netdev hint (e.g. bond0)

    @classmethod
    def from_dict(cls, d: Any) -> "PlatformFabric":
        d = _as_dict(d)

        def _i(key: str, default: int) -> int:
            try:
                return int(d.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            scaleout=_as_str(d.get("scaleout")).lower(),
            rails_per_gpu=_i("rails_per_gpu", 1),
            rail_rate_gbps=_i("rail_rate_gbps", 0),
            ns_ifname=_as_str(d.get("ns_ifname")),
        )


@dataclass
class Platform:
    id: str
    name: str
    generation: str = ""  # Blackwell, Blackwell-Ultra, Rubin, Hopper
    gpu_model: str = ""  # B200, B300, GB200, GB300, R200, VR200
    superchip: str = ""  # GB200, GB300, VR200, or "" for HGX/x86 systems
    cpu_vendor: str = ""  # Grace, Intel, AMD
    cpu_arch: str = ""  # arm64, x86_64
    system_vendor: str = ""  # NVIDIA, DGX, Dell, SMC
    form_factor: str = ""  # NVL72, NVL36, HGX-8
    geometry: PlatformGeometry = field(default_factory=PlatformGeometry)
    fabric: PlatformFabric = field(default_factory=PlatformFabric)
    interconnect: dict[str, str] = field(default_factory=dict)  # {scaleup, scaleout}
    capabilities: list[str] = field(default_factory=list)  # nvlink, nvswitch, imex, mnnvl, roce
    tool_param_defaults: dict[str, str] = field(default_factory=dict)
    status: str = "ga"
    description: str = ""
    source: str = "user"  # "seed" for built-ins so user edits are distinguishable

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Platform":
        d = _as_dict(d)
        return cls(
            id=_as_str(d.get("id")),
            name=_as_str(d.get("name")) or _as_str(d.get("id")),
            generation=_as_str(d.get("generation")),
            gpu_model=_as_str(d.get("gpu_model")),
            superchip=_as_str(d.get("superchip")),
            cpu_vendor=_as_str(d.get("cpu_vendor")),
            cpu_arch=_as_str(d.get("cpu_arch")),
            system_vendor=_as_str(d.get("system_vendor")),
            form_factor=_as_str(d.get("form_factor")),
            geometry=PlatformGeometry.from_dict(d.get("geometry")),
            fabric=PlatformFabric.from_dict(d.get("fabric")),
            interconnect={k: _as_str(v) for k, v in _as_dict(d.get("interconnect")).items()},
            capabilities=_as_str_list(d.get("capabilities")),
            tool_param_defaults={
                k: _as_str(v) for k, v in _as_dict(d.get("tool_param_defaults")).items()
            },
            status=_as_str(d.get("status")) or "ga",
            description=_as_str(d.get("description")),
            source=_as_str(d.get("source")) or "user",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid platform id: {self.id!r} (expected slug like gb200-nvl72)")
        if not self.name:
            problems.append("platform name is required")
        if self.status and self.status not in STATUSES:
            problems.append(f"unknown status {self.status!r} (expected one of {STATUSES})")
        g = self.geometry
        if g.gpus_per_rack <= 0 or g.trays_per_rack <= 0:
            problems.append("geometry must have positive trays_per_rack and gpus_per_rack")
        return problems

    def has_capability(self, cap: str) -> bool:
        c = (cap or "").strip().lower()
        return any(c == x.strip().lower() for x in self.capabilities)


# ---------------------------------------------------------------------------
# Tool (a runnable script described as a document)
# ---------------------------------------------------------------------------


@dataclass
class ToolParam:
    key: str
    label: str = ""
    description: str = ""
    default: str = ""
    required: bool = False

    @classmethod
    def from_dict(cls, d: Any) -> "ToolParam":
        d = _as_dict(d)
        return cls(
            key=_as_str(d.get("key")),
            label=_as_str(d.get("label")) or _as_str(d.get("key")),
            description=_as_str(d.get("description")),
            default=_as_str(d.get("default")),
            required=bool(d.get("required")),
        )


@dataclass
class ToolCompatibility:
    """How a tool is matched to platforms.

    Exactly one mode is used, checked in order: ``platform_ids`` -> ``capabilities``
    -> ``match`` ("all"/"none"). Empty == match all platforms.
    """

    match: str = "all"  # "all" | "none"
    capabilities: list[str] = field(default_factory=list)
    platform_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> "ToolCompatibility":
        d = _as_dict(d)
        return cls(
            match=_as_str(d.get("match")) or "all",
            capabilities=_as_str_list(d.get("capabilities")),
            platform_ids=_as_str_list(d.get("platform_ids")),
        )

    def matches(self, platform: "Platform | None") -> bool:
        if self.platform_ids:
            return bool(platform) and platform.id in self.platform_ids
        if self.capabilities:
            return bool(platform) and all(platform.has_capability(c) for c in self.capabilities)
        return self.match != "none"


@dataclass
class Tool:
    id: str
    name: str
    category: str = "validation"
    script: str = ""  # basename under SCRIPTS_DIR (e.g. sysinfo.sh)
    env_key: str = ""  # override env var (e.g. RACK_SYSINFO_SCRIPT)
    params: list[ToolParam] = field(default_factory=list)
    compatible: ToolCompatibility = field(default_factory=ToolCompatibility)
    surfaces: list[str] = field(default_factory=list)  # e.g. rackDetail.hardware, tools
    execution: dict[str, str] = field(default_factory=dict)  # {mode, target}
    description: str = ""
    ai_context: str = ""
    status: str = "ga"
    source: str = "user"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Tool":
        d = _as_dict(d)
        params_raw = d.get("params")
        params = [ToolParam.from_dict(p) for p in params_raw] if isinstance(params_raw, list) else []
        return cls(
            id=_as_str(d.get("id")),
            name=_as_str(d.get("name")) or _as_str(d.get("id")),
            category=_as_str(d.get("category")) or "validation",
            script=_as_str(d.get("script")),
            env_key=_as_str(d.get("env_key")),
            params=params,
            compatible=ToolCompatibility.from_dict(d.get("compatible")),
            surfaces=_as_str_list(d.get("surfaces")),
            execution={k: _as_str(v) for k, v in _as_dict(d.get("execution")).items()},
            description=_as_str(d.get("description")),
            ai_context=_as_str(d.get("ai_context")),
            status=_as_str(d.get("status")) or "ga",
            source=_as_str(d.get("source")) or "user",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid tool id: {self.id!r}")
        if not self.name:
            problems.append("tool name is required")
        if not self.script:
            problems.append("tool script (basename) is required")
        if self.category and self.category not in TOOL_CATEGORIES:
            problems.append(f"unknown category {self.category!r} (expected one of {TOOL_CATEGORIES})")
        mode = self.execution.get("mode")
        if mode and mode not in EXEC_MODES:
            problems.append(f"unknown execution.mode {mode!r} (expected one of {EXEC_MODES})")
        target = self.execution.get("target")
        if target and target not in EXEC_TARGETS:
            problems.append(f"unknown execution.target {target!r} (expected one of {EXEC_TARGETS})")
        return problems


# ---------------------------------------------------------------------------
# Project / Cluster (customer + site definition)
# ---------------------------------------------------------------------------


@dataclass
class ClusterNaming:
    """Naming convention for a cluster (the NVIS_* templates, structured)."""

    rack_id_template: str = ""
    ct_id_template: str = ""
    nvsw_id_template: str = ""
    ct_joiner: str = "-ct"
    slurm_hostlist: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> "ClusterNaming":
        d = _as_dict(d)
        return cls(
            rack_id_template=_as_str(d.get("rack_id_template")),
            ct_id_template=_as_str(d.get("ct_id_template")),
            nvsw_id_template=_as_str(d.get("nvsw_id_template")),
            ct_joiner=d.get("ct_joiner", "-ct") if isinstance(d.get("ct_joiner"), str) else "-ct",
            slurm_hostlist=_as_str(d.get("slurm_hostlist")),
        )


@dataclass
class ClusterGeometry:
    """Site layout used to place/filter racks. Defaults derive from the platform."""

    pods: int = 1
    rows_per_pod: int = 0
    racks_per_row: int = 0
    trays_per_rack: int = 0  # 0 == inherit from platform

    @classmethod
    def from_dict(cls, d: Any) -> "ClusterGeometry":
        d = _as_dict(d)

        def _i(key: str, default: int) -> int:
            try:
                return int(d.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            pods=_i("pods", 1),
            rows_per_pod=_i("rows_per_pod", 0),
            racks_per_row=_i("racks_per_row", 0),
            trays_per_rack=_i("trays_per_rack", 0),
        )


@dataclass
class ClusterConnection:
    exec_mode: str = ""  # local | ssh
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_key: str = ""
    remote_base_path: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> "ClusterConnection":
        d = _as_dict(d)
        return cls(
            exec_mode=_as_str(d.get("exec_mode")),
            ssh_host=_as_str(d.get("ssh_host")),
            ssh_user=_as_str(d.get("ssh_user")),
            ssh_key=_as_str(d.get("ssh_key")),
            remote_base_path=_as_str(d.get("remote_base_path")),
        )


@dataclass
class Cluster:
    id: str
    name: str
    platform_id: str = ""
    cluster_manager: str = "bcm"
    site_prefix: str = ""  # pod/site prefix for multirack bracket expansion
    naming: ClusterNaming = field(default_factory=ClusterNaming)
    geometry: ClusterGeometry = field(default_factory=ClusterGeometry)
    connection: ClusterConnection = field(default_factory=ClusterConnection)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Cluster":
        d = _as_dict(d)
        return cls(
            id=_as_str(d.get("id")),
            name=_as_str(d.get("name")) or _as_str(d.get("id")),
            platform_id=_as_str(d.get("platform_id")),
            cluster_manager=_as_str(d.get("cluster_manager")) or "bcm",
            site_prefix=_as_str(d.get("site_prefix")),
            naming=ClusterNaming.from_dict(d.get("naming")),
            geometry=ClusterGeometry.from_dict(d.get("geometry")),
            connection=ClusterConnection.from_dict(d.get("connection")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid cluster id: {self.id!r}")
        if not self.name:
            problems.append("cluster name is required")
        if self.cluster_manager and self.cluster_manager not in CLUSTER_MANAGERS:
            problems.append(
                f"unknown cluster_manager {self.cluster_manager!r} (expected {CLUSTER_MANAGERS})"
            )
        return problems


@dataclass
class Project:
    id: str
    name: str
    customer: str = ""
    description: str = ""
    clusters: list[Cluster] = field(default_factory=list)
    source: str = "user"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        d = _as_dict(d)
        clusters_raw = d.get("clusters")
        clusters = (
            [Cluster.from_dict(c) for c in clusters_raw] if isinstance(clusters_raw, list) else []
        )
        return cls(
            id=_as_str(d.get("id")),
            name=_as_str(d.get("name")) or _as_str(d.get("id")),
            customer=_as_str(d.get("customer")),
            description=_as_str(d.get("description")),
            clusters=clusters,
            source=_as_str(d.get("source")) or "user",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid project id: {self.id!r}")
        if not self.name:
            problems.append("project name is required")
        seen: set[str] = set()
        for c in self.clusters:
            problems.extend(c.validate())
            if c.id in seen:
                problems.append(f"duplicate cluster id within project: {c.id!r}")
            seen.add(c.id)
        return problems

    def get_cluster(self, cluster_id: str) -> "Cluster | None":
        for c in self.clusters:
            if c.id == cluster_id:
                return c
        return None
