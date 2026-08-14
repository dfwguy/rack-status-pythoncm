# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
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

# How an endpoint is reached. "local" is the app's own host; "ssh" is everything
# else, including each leg of a multi-hop chain.
ENDPOINT_TYPES = ("local", "ssh")

# Roles a site is expected to define. The set is open on purpose -- a deployment can
# invent any role name and bind it to an endpoint -- but these four are what the
# shipped call sites ask for, so they are seeded and surfaced in the UI:
#   control  cluster reads (cmsh, sinfo/squeue); the default route
#   scripts  where bin/scripts lives and runs
#   logs     where validation logs are written and read
#   bcm      the cluster manager, when it is not the control host
# "script" stays as the historical alias of the privileged route (see
# docs/EXECUTION_ARCHITECTURE.md §1.2).
CONNECTION_ROLES = ("control", "scripts", "logs", "bcm")
DEFAULT_CONNECTION_ROLE = "control"
LEGACY_PRIVILEGED_ROLE = "script"
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

# --- NPI Knowledge Portal vocabulary (mirrors reports/site/portal_model.py) ---
# These drive the imported reference catalog (see catalog.npi_import). Kept as tuples
# for UI hints; unknown values are tolerated (the upstream catalog may add more).
NPI_PORTFOLIO_ROLES = (
    "test",
    "test-framework",
    "validation-plane",
    "execution-orchestrator",
    "telemetry-analytics",
    "failure-attribution",
    "reference-catalog",
    "workload-recovery",
    "health-monitoring",
)
NPI_CAPABILITIES = (
    "contractual-scope",
    "config-functional",
    "screen-test-execution",
    "benchmark-performance",
    "building-block-diagnostic",
    "continuous-health",
    "remediation",
    "telemetry-analytics",
    "failure-attribution",
    "error-catalog",
    "workload-recovery",
)
NPI_MATURITIES = ("active", "emerging", "research", "legacy", "unknown")

# How an NPI capability maps onto our runnable-tool categories (for promotion + filters).
_NPI_CAPABILITY_TO_CATEGORY = {
    "benchmark-performance": "performance",
    "screen-test-execution": "validation",
    "config-functional": "validation",
    "contractual-scope": "validation",
    "building-block-diagnostic": "validation",
    "continuous-health": "maintenance",
    "remediation": "maintenance",
    "telemetry-analytics": "maintenance",
    "failure-attribution": "maintenance",
    "error-catalog": "maintenance",
    "workload-recovery": "maintenance",
}
# NPI maturity -> our status vocabulary.
_NPI_MATURITY_TO_STATUS = {
    "active": "ga",
    "emerging": "preview",
    "research": "preview",
    "legacy": "deprecated",
    "unknown": "future",
}


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


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _as_dict(v: Any) -> dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _as_dict_list(v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, (list, tuple)):
        return []
    return [dict(x) for x in v if isinstance(x, dict)]


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
    def from_dict(cls, d: Any) -> PlatformGeometry:
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
    def from_dict(cls, d: Any) -> PlatformFabric:
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
    def from_dict(cls, d: dict[str, Any]) -> Platform:
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
    def from_dict(cls, d: Any) -> ToolParam:
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
    def from_dict(cls, d: Any) -> ToolCompatibility:
        d = _as_dict(d)
        return cls(
            match=_as_str(d.get("match")) or "all",
            capabilities=_as_str_list(d.get("capabilities")),
            platform_ids=_as_str_list(d.get("platform_ids")),
        )

    def matches(self, platform: Platform | None) -> bool:
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
    def from_dict(cls, d: dict[str, Any]) -> Tool:
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
    def from_dict(cls, d: Any) -> ClusterNaming:
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
    def from_dict(cls, d: Any) -> ClusterGeometry:
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
class Endpoint:
    """One place the app can execute: a host, or a hop *through* another endpoint.

    ``via`` names another endpoint in the same cluster. Resolution chains them, so
    ``app -> bastion -> head -> bcm`` is four endpoints where each names its
    predecessor; each leg runs on the previous endpoint using *that* host's
    credentials and reachability, exactly like ``Connection.hop``. Depth is not
    limited: N hops is N ``via`` links.
    """

    id: str
    type: str = "ssh"  # local | ssh
    host: str = ""
    user: str = ""
    key: str = ""
    port: str = ""
    via: str = ""  # id of the endpoint this one is reached through ("" == from the app)
    sudo: bool = False  # run payloads under `sudo -n -E` on this endpoint
    opts: list[str] = field(default_factory=list)  # extra ssh -o flags
    description: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> Endpoint:
        d = _as_dict(d)
        return cls(
            id=_as_str(d.get("id")),
            type=_as_str(d.get("type")).lower() or "ssh",
            host=_as_str(d.get("host")),
            user=_as_str(d.get("user")),
            key=_as_str(d.get("key")),
            port=_as_str(d.get("port")),
            via=_as_str(d.get("via")),
            sudo=_as_bool(d.get("sudo")),
            opts=_as_str_list(d.get("opts")),
            description=_as_str(d.get("description")),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid endpoint id: {self.id!r}")
        if self.type not in ENDPOINT_TYPES:
            problems.append(f"unknown endpoint type {self.type!r} (expected {ENDPOINT_TYPES})")
        if self.type == "ssh" and not self.host:
            problems.append(f"endpoint {self.id!r}: ssh endpoints need a host")
        if self.type == "local" and self.via:
            problems.append(f"endpoint {self.id!r}: a local endpoint cannot be reached via a hop")
        return problems


@dataclass
class ConnectionRole:
    """What a call site asks for, decoupled from where that happens to live.

    A role binds a name (``logs``, ``scripts``, ``bcm``, or anything a site invents)
    to an endpoint plus an optional ``root`` directory on it. Code says "list the
    logs" and the deployment decides which host that means, so scripts, logs and the
    cluster manager can each sit somewhere different.
    """

    id: str
    endpoint: str = ""  # endpoint id; "" == the endpoint bound to the control role
    root: str = ""  # base directory for this role on that endpoint
    sudo: bool = False  # escalate on top of whatever the endpoint does
    description: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> ConnectionRole:
        d = _as_dict(d)
        return cls(
            id=_as_str(d.get("id")),
            endpoint=_as_str(d.get("endpoint")),
            root=_as_str(d.get("root")),
            sudo=_as_bool(d.get("sudo")),
            description=_as_str(d.get("description")),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid role id: {self.id!r}")
        return problems


@dataclass
class ClusterConnection:
    """How this cluster is reached.

    The flat ``exec_mode``/``ssh_*``/``remote_base_path`` fields are the original
    single-hop form and stay authoritative when no ``endpoints`` are declared, so
    existing clusters keep working untouched. Declaring ``endpoints``/``roles``
    unlocks multi-hop and per-role placement; the flat fields are then derived from
    the control role for the env-var contract that shell scripts still read.
    """

    exec_mode: str = ""  # local | ssh
    ssh_host: str = ""
    ssh_user: str = ""
    ssh_key: str = ""
    remote_base_path: str = ""
    endpoints: list[Endpoint] = field(default_factory=list)
    roles: list[ConnectionRole] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> ClusterConnection:
        d = _as_dict(d)
        return cls(
            exec_mode=_as_str(d.get("exec_mode")),
            ssh_host=_as_str(d.get("ssh_host")),
            ssh_user=_as_str(d.get("ssh_user")),
            ssh_key=_as_str(d.get("ssh_key")),
            remote_base_path=_as_str(d.get("remote_base_path")),
            endpoints=[Endpoint.from_dict(x) for x in _as_dict_list(d.get("endpoints"))],
            roles=[ConnectionRole.from_dict(x) for x in _as_dict_list(d.get("roles"))],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def endpoint(self, endpoint_id: str) -> Endpoint | None:
        for e in self.endpoints:
            if e.id == endpoint_id:
                return e
        return None

    def role(self, role_id: str) -> ConnectionRole | None:
        for r in self.roles:
            if r.id == role_id:
                return r
        return None

    def validate(self) -> list[str]:
        problems: list[str] = []
        seen: set[str] = set()
        for e in self.endpoints:
            problems.extend(e.validate())
            if e.id in seen:
                problems.append(f"duplicate endpoint id: {e.id!r}")
            seen.add(e.id)
        for e in self.endpoints:
            if e.via and e.via not in seen:
                problems.append(f"endpoint {e.id!r}: via references unknown endpoint {e.via!r}")
        problems.extend(self._cycle_problems())
        role_ids: set[str] = set()
        for r in self.roles:
            problems.extend(r.validate())
            if r.id in role_ids:
                problems.append(f"duplicate role id: {r.id!r}")
            role_ids.add(r.id)
            if r.endpoint and r.endpoint not in seen:
                problems.append(f"role {r.id!r}: unknown endpoint {r.endpoint!r}")
        if self.endpoints and "control" not in role_ids:
            problems.append("a 'control' role is required once endpoints are declared")
        return problems

    def _cycle_problems(self) -> list[str]:
        """Report any endpoint whose ``via`` chain loops (which would never resolve)."""
        problems: list[str] = []
        by_id = {e.id: e for e in self.endpoints}
        for start in self.endpoints:
            seen = {start.id}
            cur = start
            while cur.via:
                if cur.via in seen:
                    problems.append(f"endpoint {start.id!r}: via chain forms a cycle")
                    break
                seen.add(cur.via)
                nxt = by_id.get(cur.via)
                if nxt is None:
                    break
                cur = nxt
        return problems


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
    def from_dict(cls, d: dict[str, Any]) -> Cluster:
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
        problems.extend(self.connection.validate())
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
    def from_dict(cls, d: dict[str, Any]) -> Project:
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

    def get_cluster(self, cluster_id: str) -> Cluster | None:
        for c in self.clusters:
            if c.id == cluster_id:
                return c
        return None


# ---------------------------------------------------------------------------
# NpiTool (reference entry imported from the NVIDIA NPI Knowledge Portal)
# ---------------------------------------------------------------------------


@dataclass
class NpiTool:
    """A validation/perf tool as curated by the NVIDIA NPI Knowledge Portal.

    This is a *reference/taxonomy* record (source of truth for which tools exist and how
    they are classified), distinct from :class:`Tool` which is a runnable local script.
    The full NPI envelope (portfolio_role, capability, phases, layers, primitives,
    relationships, per-check ``tests``) is preserved faithfully so nothing is lost on
    round-trip. Selected entries can later be *promoted* into a runnable :class:`Tool`
    via :meth:`to_tool_stub`.

    Populated by :mod:`catalog.npi_import` from the portal's ``reports/catalog/tools/*.toml``.
    """

    id: str
    name: str
    summary: str = ""
    aliases: list[str] = field(default_factory=list)
    portfolio_role: str = ""
    component_kind: str = ""
    capability: str = ""
    evidence_depth: str = ""
    maturity: str = ""
    deployment: str = ""
    personas: list[str] = field(default_factory=list)
    stacks: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    phase_note: str = ""
    npi_model_status: str = ""
    execution_surfaces: list[str] = field(default_factory=list)
    owners: list[dict[str, Any]] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    repositories: list[dict[str, Any]] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    # Provenance: which upstream commit/file this was imported from.
    source: str = "npi-catalog"
    source_ref: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NpiTool:
        d = _as_dict(d)
        return cls(
            id=_as_str(d.get("id")),
            name=_as_str(d.get("name")) or _as_str(d.get("id")),
            summary=_as_str(d.get("summary")),
            aliases=_as_str_list(d.get("aliases")),
            portfolio_role=_as_str(d.get("portfolio_role")),
            component_kind=_as_str(d.get("component_kind")),
            capability=_as_str(d.get("capability")),
            evidence_depth=_as_str(d.get("evidence_depth")),
            maturity=_as_str(d.get("maturity")),
            deployment=_as_str(d.get("deployment")),
            personas=_as_str_list(d.get("personas")),
            stacks=_as_str_list(d.get("stacks")),
            scopes=_as_str_list(d.get("scopes")),
            phases=_as_str_list(d.get("phases")),
            layers=_as_str_list(d.get("layers")),
            phase_note=_as_str(d.get("phase_note")),
            npi_model_status=_as_str(d.get("npi_model_status")),
            execution_surfaces=_as_str_list(d.get("execution_surfaces")),
            owners=_as_dict_list(d.get("owners")),
            inputs=_as_str_list(d.get("inputs")),
            outputs=_as_str_list(d.get("outputs")),
            repositories=_as_dict_list(d.get("repositories")),
            reports=_as_str_list(d.get("reports")),
            tests=_as_dict_list(d.get("tests")),
            relationships=_as_dict_list(d.get("relationships")),
            review=_as_dict(d.get("review")),
            source=_as_str(d.get("source")) or "npi-catalog",
            source_ref=_as_str(d.get("source_ref")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid npi tool id: {self.id!r}")
        if not self.name:
            problems.append("npi tool name is required")
        return problems

    def category(self) -> str:
        """Best-fit runnable-tool category for filtering / promotion."""
        return _NPI_CAPABILITY_TO_CATEGORY.get(self.capability, "validation")

    def status(self) -> str:
        """Map NPI maturity onto our tool status vocabulary."""
        return _NPI_MATURITY_TO_STATUS.get(self.maturity, "future")

    def is_runnable_kind(self) -> bool:
        """True for entries that are actual tests/workloads (vs frameworks/telemetry planes)."""
        return self.portfolio_role in ("test", "test-framework")

    def to_tool_stub(self, *, id_prefix: str = "npi-") -> dict[str, Any]:
        """Produce a :class:`Tool`-shaped dict for later promotion into the runnable registry.

        The ``script`` is intentionally left blank — an operator wires the local launcher
        when promoting. Ids are namespaced (default ``npi-<slug>``) so imported reference
        entries never collide with hand-authored runnable tools (e.g. our ``nvloom``).
        """
        surfaces = ["catalog.npi"]
        target = "multirack" if any(
            "multi" in s.lower() for s in self.scopes
        ) else "rack"
        ai = self.summary
        if self.phase_note:
            ai = f"{ai} (NPI phase {self.phase_note})".strip()
        return {
            "id": f"{id_prefix}{self.id}",
            "name": self.name,
            "category": self.category(),
            "script": "",
            "env_key": "",
            "params": [],
            "compatible": {"match": "all"},
            "surfaces": surfaces,
            "execution": {"mode": "async", "target": target},
            "description": self.summary,
            "ai_context": ai,
            "status": self.status(),
            "source": "npi-catalog",
        }
