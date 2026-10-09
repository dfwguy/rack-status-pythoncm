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

import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
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

#: How a compute node is placed on the floor (row/rack/slot).
#:
#: ``direct`` -- the rack id is embedded in the hostname, so the naming templates
#: (``NVIS_RACK_ID`` prefixing ``NVIS_CT_ID``) parse it out. This is every cluster whose
#: hostnames look like ``bos01-a05-11-s1-dgx-01-c01``.
#:
#: ``translate`` -- the hostname does NOT encode the rack (Nscale NC's
#: ``sys1-gso1-p-phy-gpu1-su19``). Per-node rack/slot comes live from the cluster-manager
#: provider (BCM's ``rack`` attribute, ``U19:11``), rows come from the rack-id template applied
#: to that provider rack id, and the SU grouping -- which neither the hostname nor the provider
#: rack attribute gives -- is supplied by the cluster's ``scalable_units`` list. In this mode
#: the rack-prefixes-CT constraint does not apply, because ``NVIS_CT_ID`` is not a placement
#: source.
#:
#: ``pool`` -- there is NO placement data anywhere: no rack objects in the cluster manager,
#: no rack attribute on the devices, no rack token in the hostname, no SU list. The Akamai/Reno
#: RTX 6000 cluster is the archetype -- ``cmsh rack; list`` returns nothing and every device's
#: ``rack`` attribute is empty. The node is the atom: the fleet renders as a flat pool of node
#: tiles ordered by hostname, with no rack rows and no fabricated rack. Node *shape* (GPUs per
#: node) still comes from the scheduler/provider (see node-shape enrichment); only *placement*
#: is absent. A cluster flips from ``pool`` to ``translate`` with no rework the day someone
#: populates rack/rackindex in the cluster manager.
RACK_MAPPING_MODES = ("direct", "translate", "pool")

#: Where work runs, as distinct from who manages the machines. Must stay in step with
#: ``schedulers.factory._BUILDERS`` -- a cluster naming a kind the factory cannot build is a
#: deployment that comes up and then fails on first launch, so `validate()` refuses it here.
#:
#: The default is ``slurm`` and the asymmetry is deliberate: defaulting to a native scheduler
#: on a Slurm cluster produces a queue view containing only work this product launched, which
#: looks plausible and is wrong, while defaulting to Slurm where there is none fails loudly on
#: the first submit. Fail loud wins.
SCHEDULERS = ("slurm", "pythoncm", "local", "runai", "slinky", "maas")

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

# How a widget on a launch surface actually starts a tool.
#
# Code owns this set; a document only *names* one and supplies its arguments. That is the
# whole point: an operator adds a button by writing a Tool document, and the frontend gains
# no new code path -- and cannot be pointed at an arbitrary URL, because the launcher decides
# what it is allowed to call.
#
#   validation-preset  the audited recipe path (ValidationPresetDialog -> prepare -> launch)
#   async-job          POST <endpoint>, then poll <endpoint minus -async> + "-status"
#   stream             a streaming endpoint rendered in the live output dialog
#   dialog             hands off to a named frontend dialog that builds its own body
#   none               catalogued but not launchable from a widget (reference entries)
LAUNCH_KINDS = ("validation-preset", "async-job", "stream", "dialog", "none")

# Endpoints are same-origin API paths only. A document is operator-editable, so an absolute
# URL here would let a catalog edit aim a button at another host; the shape is pinned rather
# than trusted.
_ENDPOINT_RE = re.compile(r"^/api/[A-Za-z0-9][A-Za-z0-9._~/-]*$")

# ``script`` is a path *relative to* the scripts directory, and it reaches a shell.
#
# `workflow_engine` resolves a registered tool as ``bash <scripts_dir>/<script>`` and hands the
# string to the execution host, where a remote shell interprets it. So this field is not
# metadata: an unconstrained value is command injection (``a.sh; curl …|sh``) or traversal
# (``../../..``), and an *absolute* value escapes silently because ``Path("/dir") / "/abs"``
# discards the left side entirely.
#
# Hence a deliberately narrow alphabet: no whitespace, no shell metacharacters, no leading
# slash. Sub-directories are allowed because a site may group its scripts; ``.`` and ``..``
# segments are not. A site that keeps a script *outside* the scripts directory uses
# ``env_key``, which is the documented mechanism for relocating one.
_SCRIPT_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")

# An env var name, because ``env_key`` is looked up in the process environment.
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: A cluster data directory (image store, node cache). Launchers interpolate it into remote
#: ``pdsh``/``rsync`` command lines and Slurm ``--export`` lists, so it must be an absolute path
#: of plain path characters: no whitespace, quotes, ``$``, ``,`` (the ``--export`` separator) or
#: ``..`` segments.
_DATA_DIR_RE = re.compile(r"^/[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*/?$")


def valid_data_dir(path: str) -> bool:
    """Is *path* safe to hand a launcher as a cluster data directory?"""
    return bool(_DATA_DIR_RE.fullmatch(path)) and ".." not in path.split("/")


#: Where NVIS-P2P publishes distributed images on every compute node. Its node scripts
#: (``deploy/p2p/scripts/*-node.sh``) write ``/opt/containers/<filename>`` literally; only the peer
#: working root under it is configurable. So on a P2P cluster the node image cache IS this directory,
#: and a different ``node_container_dir`` would send launchers looking where P2P never writes.
P2P_NODE_CONTAINER_DIR = "/opt/containers"


def _parent_dir(path: str) -> str:
    path = path.rstrip("/")
    return path.rsplit("/", 1)[0] if "/" in path.lstrip("/") else ""


def node_image_dirs(cluster: Cluster, p2p: P2pSettings | None = None) -> tuple[str, str]:
    """``(shared, node)`` image directories for *cluster*; ``""`` = not stated, use the default.

    The cluster's connection fields are the one source of truth. A P2P cluster that has not stated
    them yet inherits what its P2P instance already uses -- the origin ``container_path`` and the
    parent of ``peer_root`` -- so a deployment that predates the fields keeps working unchanged,
    and P2P never disagrees with the launchers about where an image lives.
    """
    conn = cluster.connection
    shared = conn.shared_container_dir.rstrip("/") if conn.shared_container_dir else ""
    node = conn.node_container_dir.rstrip("/") if conn.node_container_dir else ""
    if cluster.p2p_enabled:
        p2p = p2p or P2pSettings(cluster_id=cluster.id)
        shared = shared or p2p.container_path.rstrip("/")
        node = node or _parent_dir(p2p.peer_root) or P2P_NODE_CONTAINER_DIR
    return shared, node


def safe_script_ref(script: str) -> bool:
    """Is *script* safe to interpolate into ``bash <scripts_dir>/<script>``?

    Public so the *consumption* site can re-check rather than trust the store: a catalog file
    can be edited by hand on the box, which skips the write-path validation entirely. Same
    reasoning as ``toolLaunch.isOwnApiPath`` on the frontend — cheap to check, and the failure
    it prevents is arbitrary command execution on the head node.
    """
    s = (script or "").strip()
    if not s or not _SCRIPT_RE.match(s):
        return False
    return not any(part in (".", "..") for part in s.split("/"))


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


def _as_int(v: Any, default: int) -> int:
    # bool is an int subclass; True would silently become 1 and look like a position.
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


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

    **The Scalable Unit (SU) lives here, not in geometry, because the fabric is what
    bounds it.** An SU is the group of racks one set of leaf and spine switches serves;
    its size is a property of the *platform*, so a GB300 NVL72 SU and a VR200 SU are
    different sizes and a newer generation may span more racks per SU. Geometry
    describes one rack; this describes how racks are grouped above it.

    ``racks_per_su == 0`` means the platform has no SU model yet, and callers must
    degrade to ungrouped racks rather than inventing a grouping.
    """

    scaleout: str = ""  # ib | roce (scale-out technology)
    rails_per_gpu: int = 1  # compute NICs per GPU
    rail_rate_gbps: int = 0  # expected per-rail rate (400, 800); 0 == unknown
    ns_ifname: str = ""  # north-south / bootstrap netdev hint (e.g. bond0)
    racks_per_su: int = 0  # racks one leaf/spine group serves; 0 == not modelled
    leaf_switches_per_su: int = 0  # expected leaf count in one SU; 0 == unknown
    spine_switches_per_su: int = 0  # expected spine count in one SU; 0 == unknown

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
            racks_per_su=_i("racks_per_su", 0),
            leaf_switches_per_su=_i("leaf_switches_per_su", 0),
            spine_switches_per_su=_i("spine_switches_per_su", 0),
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
        f = self.fabric
        for name in ("racks_per_su", "leaf_switches_per_su", "spine_switches_per_su"):
            if getattr(f, name) < 0:
                problems.append(f"fabric.{name} cannot be negative")
        # Switch counts without a rack count describe a group of unknown size, which reads
        # like an SU model and is not one. Refuse it rather than draw a wrong diagram.
        if f.racks_per_su == 0 and (f.leaf_switches_per_su or f.spine_switches_per_su):
            problems.append(
                "fabric declares SU switch counts without racks_per_su; set racks_per_su "
                "or drop the switch counts"
            )
        return problems

    def has_capability(self, cap: str) -> bool:
        c = (cap or "").strip().lower()
        return any(c == x.strip().lower() for x in self.capabilities)

    def gpus_per_su(self) -> int:
        """GPUs in one Scalable Unit, or 0 when the platform has no SU model.

        Derived rather than stored: it is exactly ``racks_per_su x gpus_per_rack``, and a
        second stored copy is a second thing to keep in step.
        """
        return self.fabric.racks_per_su * self.geometry.gpus_per_rack


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
class ToolLaunch:
    """What happens when an operator presses a tool's widget button.

    ``kind`` selects a launcher the frontend implements (see :data:`LAUNCH_KINDS`); the
    remaining fields are that launcher's arguments. ``body`` is merged into the request the
    launcher builds, so per-cluster knobs (a container tag, an iteration count) live on the
    document instead of in a component.

    ``dialog`` is *required* for ``kind="dialog"`` and *optional* everywhere else, where it
    names an options dialog to collect operator input before the request goes out -- which is
    how Stage 1 and Stage 2 keep their existing launch-review step.

    ``kind="none"`` means the tool is catalogued but has no button -- the default, so an
    existing document keeps rendering as reference material until someone binds it.
    """

    kind: str = "none"
    endpoint: str = ""  # async-job | stream
    preset_id: str = ""  # validation-preset
    dialog: str = ""  # dialog
    scopes: list[str] = field(default_factory=list)  # subset of EXEC_TARGETS; [] == the tool's own
    body: dict[str, Any] = field(default_factory=dict)
    confirm: bool = False  # force the confirm step even without a node selection
    # Selection size this widget accepts; 0 == unbounded. This is a *surface* constraint, not
    # the recipe's -- the engine still enforces its own. GPT-OSS is the worked example: the
    # rack tab has always offered it as a single-node benchmark while the multirack view fans
    # it out across hundreds, so the two are two documents with different bounds rather than
    # one document with an `if` in a component.
    min_nodes: int = 0
    max_nodes: int = 0

    @classmethod
    def from_dict(cls, d: Any) -> ToolLaunch:
        d = _as_dict(d)
        return cls(
            kind=_as_str(d.get("kind")).lower() or "none",
            endpoint=_as_str(d.get("endpoint")),
            preset_id=_as_str(d.get("preset_id")),
            dialog=_as_str(d.get("dialog")),
            scopes=[s.lower() for s in _as_str_list(d.get("scopes"))],
            body=_as_dict(d.get("body")),
            confirm=_as_bool(d.get("confirm")),
            min_nodes=_as_int(d.get("min_nodes"), 0),
            max_nodes=_as_int(d.get("max_nodes"), 0),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.kind not in LAUNCH_KINDS:
            problems.append(f"unknown launch.kind {self.kind!r} (expected one of {LAUNCH_KINDS})")
            return problems
        if self.kind in ("async-job", "stream"):
            if not self.endpoint:
                problems.append(f"launch.kind={self.kind!r} requires launch.endpoint")
            elif not _ENDPOINT_RE.match(self.endpoint) or ".." in self.endpoint:
                problems.append(
                    f"launch.endpoint {self.endpoint!r} must be a same-origin /api/... path"
                )
        if self.kind == "validation-preset" and not self.preset_id:
            problems.append("launch.kind='validation-preset' requires launch.preset_id")
        if self.kind == "dialog" and not self.dialog:
            problems.append("launch.kind='dialog' requires launch.dialog")
        for scope in self.scopes:
            if scope not in EXEC_TARGETS:
                problems.append(f"unknown launch.scope {scope!r} (expected one of {EXEC_TARGETS})")
        if self.min_nodes < 0 or self.max_nodes < 0:
            problems.append("launch.min_nodes / launch.max_nodes cannot be negative")
        elif self.max_nodes and self.min_nodes > self.max_nodes:
            problems.append(
                f"launch.min_nodes ({self.min_nodes}) exceeds launch.max_nodes ({self.max_nodes})"
            )
        return problems


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
    # --- widget presentation -------------------------------------------------
    # What the operator reads on the button. ``name`` is the title and ``description`` the
    # body, both already present; these are the parts a card needs that a registry entry did
    # not have. Keeping them on the document is what makes "we now ship NCCL 2.31.2" one edit
    # instead of a code change in every component that names the version.
    subtitle: str = ""  # short qualifier under the title (version, scope, duration)
    button_label: str = ""  # "" == the surface's default ("Run")
    icon: str = ""  # lucide icon name; unknown names fall back to the category default
    # Card banner. "" == the frontend's per-category artwork, which is the normal case: the
    # shipped set covers every category, so a document only names one to override it. A URL or
    # an app-relative path; the frontend does not fetch anything cross-origin for it.
    image: str = ""
    destructive: bool = False  # renders the danger variant and forces a confirm
    order: int = 100  # default board position; ties break on name
    launch: ToolLaunch = field(default_factory=ToolLaunch)

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
            subtitle=_as_str(d.get("subtitle")),
            button_label=_as_str(d.get("button_label")),
            icon=_as_str(d.get("icon")),
            image=_as_str(d.get("image")),
            destructive=_as_bool(d.get("destructive")),
            order=_as_int(d.get("order"), 100),
            launch=ToolLaunch.from_dict(d.get("launch")),
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
        elif not safe_script_ref(self.script):
            problems.append(
                f"invalid tool script {self.script!r}: expected a path relative to the scripts "
                "directory using letters, digits, '.', '_', '-' and '/' only (no '..', no "
                "leading '/'). To run a script kept elsewhere, set env_key instead."
            )
        if self.env_key and not _ENV_KEY_RE.match(self.env_key):
            problems.append(
                f"invalid tool env_key {self.env_key!r}: expected an environment variable name "
                "(upper-case letters, digits and underscores)"
            )
        problems.extend(self.launch.validate())
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
class ClusterScalableUnit:
    """One Scalable Unit of this cluster, and the racks in it.

    Explicit because derivation alone is wrong on real floors. Nscale TX 16K numbers
    SU1..SU14 across ``ink50-dh01`` rows a-c *and* ``ink50-dh02`` rows a-k, which no
    ``rows_per_pod`` arithmetic reproduces, and a bring-up routinely has a final SU that
    is only partly populated. So a site may state its own mapping; when it does not,
    :func:`derive_scalable_units` supplies one from the platform's ``racks_per_su``.
    """

    id: str
    label: str = ""  # display name; falls back to the id
    racks: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> ClusterScalableUnit:
        d = _as_dict(d)
        su_id = _as_str(d.get("id"))
        return cls(
            id=su_id,
            label=_as_str(d.get("label")) or su_id,
            racks=_as_str_list(d.get("racks")),
        )


@dataclass
class ClusterCampaign:
    """A pinned roster of exact compute nodes, for a reproducible acceptance run.

    Acceptance is a claim about *specific hardware*. "We validated rack h18" is only
    meaningful if the eighteen trays are the same eighteen every time, so a campaign names
    hostnames rather than a rack whose membership a later inventory refresh could change.
    That is the difference between evidence and an anecdote.

    **This belongs on the cluster record, which is the whole point.** The mechanism it
    replaces was two environment variables pointing at a newline-separated file on one
    host: not portable, invisible to the cluster switcher, uneditable from the UI, and
    impossible for a second deployment to reproduce. A campaign on the Cluster document
    travels with the project that owns it, so one container genuinely serves Mayo, Firmus,
    Boston and Nscale.

    **The version is derived, never declared.** See :meth:`fingerprint`.
    """

    id: str
    label: str = ""  # display name; falls back to the id
    nodes: list[str] = field(default_factory=list)  # exact compute hostnames
    #: Enforced nodes-per-rack, or 0 to not check. A bring-up that reviewed 16 trays per
    #: rack wants the 17th to be a refusal rather than a silently larger run.
    nodes_per_rack: int = 0
    created_at: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> ClusterCampaign:
        d = _as_dict(d)
        campaign_id = _as_str(d.get("id"))
        return cls(
            id=campaign_id,
            label=_as_str(d.get("label")) or campaign_id,
            nodes=_as_str_list(d.get("nodes")),
            nodes_per_rack=_as_int(d.get("nodes_per_rack"), 0),
            created_at=_as_str(d.get("created_at")),
            notes=_as_str(d.get("notes")),
        )

    def fingerprint(self) -> str:
        """A short digest of the roster itself.

        Derived rather than stored, and that is the design. A `version` field an operator
        maintains by hand is a value that can disagree with the thing it describes: edit
        the roster, forget the bump, and every consumer believes it is looking at the
        previous list. The failure is silent and it lands in an audit trail -- two
        different node sets recorded under one identifier.

        A digest of the content cannot drift from the content. This is the same repair as
        `identity-auth-traps.mdc` §1: when a comparison can be wrong, fix the *value* so
        it cannot be, rather than adding a check around it.

        Case-folded and sorted, because neither the spelling a site uses nor the order it
        lists nodes in changes which hardware was reviewed.
        """
        canonical = "\n".join(sorted(node.strip().casefold() for node in self.nodes if node.strip()))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.id):
            problems.append(f"invalid campaign id: {self.id!r}")
        if not self.nodes:
            # An empty campaign would pin nothing while looking like a control.
            problems.append(f"campaign {self.id!r} has no nodes")
        folded = [node.strip().casefold() for node in self.nodes if node.strip()]
        if len(set(folded)) != len(folded):
            # A duplicate would run the work twice on one node and inflate the node count
            # the acceptance record reports.
            problems.append(f"campaign {self.id!r} lists a node more than once")
        if self.nodes_per_rack < 0:
            problems.append(f"campaign {self.id!r}: nodes_per_rack cannot be negative")
        return problems


@dataclass
class ClusterEvidence:
    """Where this cluster's external evidence feeds live.

    Some sites run their own collector -- a pipeline that walks the fleet's logs and writes a
    snapshot of what passed -- and that snapshot is better evidence than anything this product
    can re-derive, because it has already applied the site's acceptance policy.

    On the cluster record rather than in the environment for the usual reason: these are paths
    on *this* cluster's hosts, and one instance serves several clusters. The implementation
    this was ported from hard-coded
    ``/cluster/OVA/logs/nscaletx-16k-validation/validated-racks.json``, which is one
    customer's filesystem compiled into the product.

    Empty means the cluster has no external feed, which is the common case: most clusters'
    evidence is the logs this product reads itself.
    """

    #: The collector's snapshot, read through the ``scripts`` role.
    snapshot_path: str = ""
    #: Root of the per-node health-check markers, one directory per node.
    healthcheck_root: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> ClusterEvidence:
        d = _as_dict(d)
        return cls(
            snapshot_path=_as_str(d.get("snapshot_path")),
            healthcheck_root=_as_str(d.get("healthcheck_root")),
        )


def derive_scalable_units(
    racks: Iterable[str],
    racks_per_su: int,
    *,
    label_prefix: str = "SU",
) -> list[ClusterScalableUnit]:
    """Chunk ordered racks into SUs of ``racks_per_su``.

    The default when a cluster states no mapping of its own. Order is the caller's --
    normally the inventory order -- because this cannot know a site's physical sequence.
    Returns ``[]`` when the platform has no SU model (``racks_per_su <= 0``) rather than
    inventing one SU containing everything, which would draw a diagram that is confidently
    wrong. A trailing partial SU is kept: a half-populated SU is a real thing on a floor.
    """
    ordered = [r for r in (str(x).strip() for x in racks) if r]
    if racks_per_su <= 0 or not ordered:
        return []
    out: list[ClusterScalableUnit] = []
    for index, start in enumerate(range(0, len(ordered), racks_per_su), start=1):
        out.append(
            ClusterScalableUnit(
                id=f"su{index:02d}",
                label=f"{label_prefix}{index}",
                racks=ordered[start : start + racks_per_su],
            )
        )
    return out


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


#: Scale-out technology a cluster may state; blank inherits the platform's ``fabric.scaleout``.
CLUSTER_FABRIC_SCALEOUT = ("", "ib", "roce")
#: RoCE address family a cluster may state; blank lets the scan read it off the compute GIDs.
CLUSTER_FABRIC_ADDR_FAMILIES = ("", "ipv4", "ipv6")
#: An ``NCCL_IB_HCA`` value: an optional ``^``/``=`` modifier, then comma-separated device names or
#: name prefixes, each with an optional ``:port``. Nothing else -- it is exported into a shell.
_NCCL_IB_HCA_RE = re.compile(r"^[\^=]?[A-Za-z0-9_.-]+(:[0-9]+)?(,[A-Za-z0-9_.-]+(:[0-9]+)?)*$")


@dataclass
class ClusterFabric:
    """How this deployment's compute fabric differs from what its platform implies.

    The platform says what the hardware is (``PlatformFabric``: IB or RoCE, rails per GPU, rate).
    A site decides how it is wired and addressed, and two sites with the same platform can
    disagree: NscaleTX-NC runs its GB300 Spectrum-X rails as SR-IOV VFs (``roce_vf_r1..4``) behind
    OVS uplink PFs (``roce_r1..4``), addressed over IPv6 only, while the 400G NICs a rate-based pick
    would choose are its management plane. ``bin/scripts/lib/fabric.inc.sh`` detects all of that
    unaided; every field here is an override for a site where detection is wrong, and blank means
    "detect". Projected per cluster (``catalog.context``) as ``NVIS_RDMA_LINK_TECH``,
    ``NVIS_FABRIC_ADDR_FAMILY`` and ``NVIS_FABRIC_HCA``, which forward over SSH by their prefix.
    """

    scaleout: str = ""  # "" (platform's) | ib | roce
    addr_family: str = ""  # "" (detect) | ipv4 | ipv6
    #: The ``NCCL_IB_HCA`` value naming the compute plane (e.g. ``roce_vf_r``); blank = detect.
    compute_hca: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> ClusterFabric:
        d = _as_dict(d)
        return cls(
            scaleout=_as_str(d.get("scaleout")).strip().lower(),
            addr_family=_as_str(d.get("addr_family")).strip().lower(),
            compute_hca=_as_str(d.get("compute_hca")).strip(),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.scaleout not in CLUSTER_FABRIC_SCALEOUT:
            problems.append(f"fabric.scaleout must be one of ib, roce or blank (got {self.scaleout!r})")
        if self.addr_family not in CLUSTER_FABRIC_ADDR_FAMILIES:
            problems.append(
                f"fabric.addr_family must be one of ipv4, ipv6 or blank (got {self.addr_family!r})"
            )
        if self.compute_hca and not _NCCL_IB_HCA_RE.match(self.compute_hca):
            problems.append(
                "fabric.compute_hca must be an NCCL_IB_HCA value: device names or name prefixes, "
                f"comma-separated, optionally led by ^ or = (got {self.compute_hca!r})"
            )
        return problems


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
    #: Where this cluster's nodes write the validation/job logs that Rack Status ingests. A
    #: per-cluster setting on purpose: a multi-tenant deployment must read each project's logs from
    #: its own cluster, not from one global directory shared by all of them (the single-tenant
    #: ``RACK_STATUS_LOG_DIR`` env var this supersedes). Blank falls back to that deprecated env var,
    #: then to ``remote_base_path/var/logs``.
    log_dir: str = ""
    #: The cluster's workspace: what is built or installed ON the cluster for its tests -- HPC-X,
    #: CUDA, host NCCL + nccl-tests, nvloom, the dgxc-benchmarking/LLMB install. Installers write
    #: under it and launchers derive their paths from it (bin/scripts/workspace-dir-common.sh), so
    #: the two cannot disagree. One of the cluster's three roots beside remote_base_path (scripts)
    #: and log_dir (logs); see docs/explanation/how-cluster-paths-reach-scripts.md. Exported as
    #: NVIS_WORKSPACE_DIR. Blank falls back to that env setting; there is no built-in default.
    workspace_dir: str = ""
    #: Where this cluster keeps its shared container images, as the launch host sees the mount.
    #: A mount point is a site fact (Nscale mounts ``/cluster/containers``; other sites differ),
    #: so it lives on the cluster rather than in a script. Only the directory is configurable --
    #: preset image basenames and digests stay code-owned. Blank falls back to the
    #: ``RACK_VALIDATION_CONTAINER_ROOT`` setting and then its default.
    shared_container_dir: str = ""
    #: Where each compute node keeps its local copy of those images (the node cache that staging
    #: copies into and that pyxis/enroot read from). Launchers used to hard-code ``/raid/data`` or
    #: ``/tmp/containers``; neither exists on every cluster -- NC's nodes cache under
    #: ``/opt/containers`` and have no ``/raid`` at all. Blank falls back to the
    #: ``NVIS_NODE_CONTAINER_DIR`` setting and then its default.
    node_container_dir: str = ""
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
            log_dir=_as_str(d.get("log_dir")),
            workspace_dir=_as_str(d.get("workspace_dir")),
            shared_container_dir=_as_str(d.get("shared_container_dir")),
            node_container_dir=_as_str(d.get("node_container_dir")),
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
        for name in ("workspace_dir", "shared_container_dir", "node_container_dir"):
            value = getattr(self, name)
            if value and not valid_data_dir(value):
                problems.append(f"{name} must be an absolute path of plain path characters: {value!r}")
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
    cluster_manager: str = "bcm"  # who manages the machines (BCM/MAAS)
    scheduler: str = "slurm"  # where work runs; see SCHEDULERS
    # How nodes map to the floor: hostname-embedded rack (direct), provider rack + SU list
    # (translate), or no placement at all -- a flat pool of nodes (pool). See RACK_MAPPING_MODES.
    rack_mapping_mode: str = "direct"
    site_prefix: str = ""  # pod/site prefix for multirack bracket expansion
    naming: ClusterNaming = field(default_factory=ClusterNaming)
    geometry: ClusterGeometry = field(default_factory=ClusterGeometry)
    # How this site's compute fabric is wired and addressed, where that differs from the platform.
    fabric: ClusterFabric = field(default_factory=ClusterFabric)
    connection: ClusterConnection = field(default_factory=ClusterConnection)
    # Empty means "derive from the platform's racks_per_su"; see scalable_units_for().
    scalable_units: list[ClusterScalableUnit] = field(default_factory=list)
    # Pinned acceptance rosters. A site may keep several -- one per bring-up wave -- and a
    # launch names which it ran against, so the record says what was actually validated.
    campaigns: list[ClusterCampaign] = field(default_factory=list)
    # Where an external collector writes this cluster's evidence, if it has one.
    evidence: ClusterEvidence = field(default_factory=ClusterEvidence)
    # NVIS-P2P (artifact distribution) is single-tenant: one Dragonfly instance per cluster. This
    # flag says whether this cluster has that capability. Off means the P2P nav group and gateway
    # are inert for it. The enabled instance's endpoints/credentials live on the connection overlay
    # and the token vault, not here -- this is only the on/off an operator sets per cluster.
    p2p_enabled: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Cluster:
        d = _as_dict(d)
        sus_raw = d.get("scalable_units")
        campaigns_raw = d.get("campaigns")
        return cls(
            id=_as_str(d.get("id")),
            name=_as_str(d.get("name")) or _as_str(d.get("id")),
            platform_id=_as_str(d.get("platform_id")),
            cluster_manager=_as_str(d.get("cluster_manager")) or "bcm",
            scheduler=_as_str(d.get("scheduler")).lower() or "slurm",
            rack_mapping_mode=_as_str(d.get("rack_mapping_mode")).lower() or "direct",
            site_prefix=_as_str(d.get("site_prefix")),
            naming=ClusterNaming.from_dict(d.get("naming")),
            geometry=ClusterGeometry.from_dict(d.get("geometry")),
            fabric=ClusterFabric.from_dict(d.get("fabric")),
            connection=ClusterConnection.from_dict(d.get("connection")),
            scalable_units=(
                [ClusterScalableUnit.from_dict(s) for s in sus_raw]
                if isinstance(sus_raw, list)
                else []
            ),
            campaigns=(
                [ClusterCampaign.from_dict(c) for c in campaigns_raw]
                if isinstance(campaigns_raw, list)
                else []
            ),
            evidence=ClusterEvidence.from_dict(d.get("evidence")),
            p2p_enabled=_as_bool(d.get("p2p_enabled")),
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
        if self.scheduler and self.scheduler not in SCHEDULERS:
            problems.append(f"unknown scheduler {self.scheduler!r} (expected {SCHEDULERS})")
        if self.rack_mapping_mode and self.rack_mapping_mode not in RACK_MAPPING_MODES:
            problems.append(
                f"unknown rack_mapping_mode {self.rack_mapping_mode!r} (expected {RACK_MAPPING_MODES})"
            )
        if self.rack_mapping_mode == "translate" and not self.scalable_units:
            # translate mode places rows/racks from the provider, but the SU grouping has no
            # other source -- the hostname's suffix isn't read as an SU and the provider rack
            # attribute doesn't carry one. Without the list the floor has no SUs at all.
            problems.append(
                "rack_mapping_mode 'translate' requires a scalable_units (SU) translation list"
            )
        if self.rack_mapping_mode == "pool" and self.scalable_units:
            # pool mode asserts there is no placement at all, so an SU grouping is a
            # contradiction -- it would claim a geometry the mode exists to say does not exist.
            # Fail closed at the record rather than silently rendering phantom SUs.
            problems.append(
                "rack_mapping_mode 'pool' has no placement, so scalable_units must be empty"
            )
        problems.extend(self.connection.validate())
        node_dir = self.connection.node_container_dir.rstrip("/")
        if self.p2p_enabled and node_dir and node_dir != P2P_NODE_CONTAINER_DIR:
            problems.append(
                f"node_container_dir {node_dir!r} disagrees with P2P, which publishes images to "
                f"{P2P_NODE_CONTAINER_DIR} on every node; leave it blank or set {P2P_NODE_CONTAINER_DIR}"
            )
        problems.extend(self._validate_scalable_units())
        problems.extend(self.fabric.validate())
        problems.extend(self._validate_campaigns())
        return problems

    def _validate_campaigns(self) -> list[str]:
        problems: list[str] = []
        seen: set[str] = set()
        for campaign in self.campaigns:
            problems.extend(campaign.validate())
            if campaign.id in seen:
                # Two campaigns sharing an id makes "which roster ran" unanswerable, which
                # is the one question a campaign exists to answer.
                problems.append(f"duplicate campaign id {campaign.id!r}")
            seen.add(campaign.id)
        return problems

    def campaign(self, campaign_id: str) -> ClusterCampaign | None:
        wanted = (campaign_id or "").strip()
        for candidate in self.campaigns:
            if candidate.id == wanted:
                return candidate
        return None

    def _validate_scalable_units(self) -> list[str]:
        """An SU map must be a clean partition, for the same reason a multirack launch is.

        A rack in two SUs is not a warning: it makes the floor view double-count and, if a
        caller ever fans out per SU, sends two jobs to one rack. Fail closed at the record.
        """
        problems: list[str] = []
        seen_ids: set[str] = set()
        rack_owner: dict[str, str] = {}
        for su in self.scalable_units:
            if not valid_id(su.id):
                problems.append(f"invalid scalable unit id: {su.id!r} (expected slug like su01)")
                continue
            if su.id in seen_ids:
                problems.append(f"duplicate scalable unit id: {su.id!r}")
                continue
            seen_ids.add(su.id)
            if not su.racks:
                problems.append(f"scalable unit {su.id!r} lists no racks")
            for rack in su.racks:
                owner = rack_owner.get(rack)
                if owner is not None:
                    problems.append(
                        f"rack {rack!r} appears in scalable units {owner!r} and {su.id!r}"
                    )
                    continue
                rack_owner[rack] = su.id
        return problems

    def scalable_units_for(
        self,
        racks: Iterable[str],
        racks_per_su: int,
        *,
        label_prefix: str = "SU",
    ) -> list[ClusterScalableUnit]:
        """This cluster's SUs: its own mapping when it has one, else derived.

        ``racks_per_su`` comes from the platform's fabric. Explicit wins because a site
        that has written its mapping down knows something this cannot compute.
        """
        if self.scalable_units:
            return list(self.scalable_units)
        return derive_scalable_units(racks, racks_per_su, label_prefix=label_prefix)


#: How a distribute splits its roster into waves. ``sequential`` slices the roster in order, as it
#: is listed. ``rack-staged`` plans by placement instead: the first stage puts one node in every rack
#: (the only stage that draws on the app server's seeds), later stages grow every rack to the next
#: rung of the stage ladder, so each new node pulls from rack-mates the scheduler already prefers.
ROLLOUT_STRATEGIES = ("sequential", "rack-staged")
#: The default ladder: one node per rack, then half of every rack, then the rest. Halving first means
#: a rack's first copy feeds about as many nodes as there are rack-mates left to fill, and the last
#: stage pulls from half a rack of holders -- no single node serves the whole rack.
DEFAULT_ROLLOUT_STAGES = ("1", "50%", "100%")
_STAGE_RE = re.compile(r"^([1-9][0-9]*)(%?)$")


def _as_stages(v: Any) -> list[str]:
    """A stage ladder from a list or a comma-separated string, tokens trimmed and spaces dropped."""
    raw = v.split(",") if isinstance(v, str) else v
    return [t.replace(" ", "") for t in _as_str_list(raw)]


def validate_rollout_stages(stages: list[str], where: str = "rollout_stages") -> list[str]:
    """Problems with a rack-staged ladder; empty means usable.

    Each rung is a cumulative per-rack target: a node count (``"1"``, ``"2"``) or a share of the
    rack (``"50%"``). Counts must rise, shares must rise, and the ladder must end at ``"100%"`` so
    every node in every rack is reached. Counts and shares may be mixed (``1, 2, 4, 50%, 100%``): a
    rack smaller than a count simply reaches that rung early, which the planner handles by
    skipping the rungs it has already passed.
    """
    if not stages:
        return [f"{where}: at least one stage is required"]
    problems: list[str] = []
    counts: list[int] = []
    shares: list[int] = []
    for token in stages:
        m = _STAGE_RE.match(token)
        if not m:
            problems.append(f"{where}: {token!r} is not a node count (e.g. 1) or a share (e.g. 50%)")
            continue
        value = int(m.group(1))
        if m.group(2):
            if value > 100:
                problems.append(f"{where}: {token!r} is more than 100%")
            shares.append(value)
        else:
            counts.append(value)
    if problems:
        return problems
    if counts != sorted(set(counts)) or shares != sorted(set(shares)):
        problems.append(f"{where}: stages must rise ({', '.join(stages)})")
    if stages[-1] != "100%":
        problems.append(f"{where}: the last stage must be 100% so every node is reached")
    return problems


@dataclass
class P2pSettings:
    """Per-cluster NVIS-P2P instance configuration.

    What ``deploy/p2p/scripts/render-site-config.py`` needs to stand up one Dragonfly instance for a
    cluster, plus the campaign-pacing defaults the rollout engine reads when distributing a catalog
    image over that cluster's roster. Read and written whole by its ``cluster_id``; nothing joins it,
    so it is a document behind the ``Repo`` (citizen 15), not a relation. Secrets are **not** stored
    here -- render generates them into files under ``install_root`` and the paths are derived, so the
    document holds only topology, identity and pacing, nothing reversible.

    The defaults are a working baseline for a single-app-server cluster; ``manager_ip`` and
    ``appserver_ip`` have no safe default (they are the cluster's own addresses) and provisioning
    refuses until they are set.
    """

    cluster_id: str
    install_root: str = "/cluster/nvis-p2p"
    service_user: str = "nvis-p2p"
    service_group: str = "nvis-p2p"
    service_uid: int = 3100
    service_gid: int = 3100
    #: The app server's addresses. No default -- they are the cluster's own and must be supplied.
    manager_ip: str = ""
    appserver_ip: str = ""
    #: How the reconcile REACHES the P2P app server. This is not the cluster's control connection --
    #: on NC that is the BCM head (a different host) -- so P2P carries its own: an SSH host (defaults
    #: to appserver_ip when blank), a user, and a key path readable inside the container. This is the
    #: server the reconcile stages assets to and brings the control plane up on.
    appserver_ssh_host: str = ""
    appserver_ssh_user: str = "root"
    appserver_ssh_key: str = ""
    console_bind: str = "127.0.0.1:4300"
    #: The compute-peer inventory. INVENTORY_SOURCE is an absolute PATH to a file listing eligible
    #: nodes -- the renderer validates it as a path, not a scheduler name -- and INVENTORY_PATTERN is
    #: the hostname bracket pattern (e.g. ``sys1-gso1-p-phy-[001-224]``). Left blank, the source path
    #: defaults under install_root and the reconcile writes it from the cluster roster.
    inventory_source: str = ""
    inventory_pattern: str = ""
    container_path: str = "/cluster/containers"
    #: NVIS_CATALOG_PATH must name a ``.json`` file (the renderer enforces the suffix); the images
    #: directory is ``container_path``, a different thing.
    catalog_path: str = "/cluster/containers/catalog.json"
    peer_root: str = "/opt/containers/.nvis-p2p"
    object_store_data_path: str = "/cluster/nvis-p2p/object-store"
    seed_cache_path: str = "/cluster/nvis-p2p/seed-cache"
    systemd_dir: str = "/etc/systemd/system"
    #: Campaign defaults -- how a staged catalog rollout over this cluster's roster paces itself.
    #: Unlike the fields above these are *not* render inputs: they are the runtime knobs the rollout
    #: engine reads (the ``NVIS_P2P_ROLLOUT_*`` variables), carried per-cluster so a larger fabric can
    #: pace differently from a small one. A campaign splits the roster into waves of
    #: ``rollout_wave_size`` nodes; each wave launches with ``rollout_parallelism`` concurrent starts
    #: and is polled every ``rollout_poll_seconds`` until every node is ready or deferred; a node is
    #: re-launched up to ``rollout_max_launches`` times and dropped after ``rollout_max_unreachable``
    #: unreachable checks, and the whole campaign gives up after ``rollout_timeout_seconds``.
    rollout_wave_size: int = 50
    rollout_parallelism: int = 48
    rollout_poll_seconds: int = 60
    rollout_max_launches: int = 3
    rollout_max_unreachable: int = 3
    rollout_timeout_seconds: int = 1800
    #: How the roster becomes waves (``ROLLOUT_STRATEGIES``). Under ``rack-staged`` the waves follow
    #: ``rollout_stages`` over the cluster's topology inventory: the first stage loads the seeds and
    #: is paced by ``rollout_wave_size``; every later stage pulls inside racks and is paced by
    #: ``rollout_fill_wave_size`` (0 = the same as ``rollout_wave_size``), so it can run wider.
    rollout_strategy: str = "sequential"
    rollout_stages: list[str] = field(default_factory=lambda: list(DEFAULT_ROLLOUT_STAGES))
    rollout_fill_wave_size: int = 0
    #: A download whose written bytes have not moved for this many seconds is stopped and relaunched
    #: (deferred once it has used ``rollout_max_launches``) instead of holding its wave open until
    #: the timeout. 0 disables the check.
    rollout_stall_seconds: int = 600
    #: The hard limit on one node's download of one image. The node enforces it itself (dfget is
    #: killed when it passes), so a hung transfer ends even with no rollout watching; a launch also
    #: replaces a "running" download older than this rather than re-adopting it.
    rollout_download_timeout_seconds: int = 3600

    @classmethod
    def from_dict(cls, d: Any) -> P2pSettings:
        d = _as_dict(d)
        base = cls(cluster_id=_as_str(d.get("cluster_id")))
        return cls(
            cluster_id=base.cluster_id,
            install_root=_as_str(d.get("install_root")) or base.install_root,
            service_user=_as_str(d.get("service_user")) or base.service_user,
            service_group=_as_str(d.get("service_group")) or base.service_group,
            service_uid=_as_int(d.get("service_uid"), base.service_uid),
            service_gid=_as_int(d.get("service_gid"), base.service_gid),
            manager_ip=_as_str(d.get("manager_ip")),
            appserver_ip=_as_str(d.get("appserver_ip")),
            appserver_ssh_host=_as_str(d.get("appserver_ssh_host")),
            appserver_ssh_user=_as_str(d.get("appserver_ssh_user")) or base.appserver_ssh_user,
            appserver_ssh_key=_as_str(d.get("appserver_ssh_key")),
            console_bind=_as_str(d.get("console_bind")) or base.console_bind,
            inventory_source=_as_str(d.get("inventory_source")),
            inventory_pattern=_as_str(d.get("inventory_pattern")),
            container_path=_as_str(d.get("container_path")) or base.container_path,
            catalog_path=_as_str(d.get("catalog_path")) or base.catalog_path,
            peer_root=_as_str(d.get("peer_root")) or base.peer_root,
            object_store_data_path=_as_str(d.get("object_store_data_path")) or base.object_store_data_path,
            seed_cache_path=_as_str(d.get("seed_cache_path")) or base.seed_cache_path,
            systemd_dir=_as_str(d.get("systemd_dir")) or base.systemd_dir,
            rollout_wave_size=_as_int(d.get("rollout_wave_size"), base.rollout_wave_size),
            rollout_parallelism=_as_int(d.get("rollout_parallelism"), base.rollout_parallelism),
            rollout_poll_seconds=_as_int(d.get("rollout_poll_seconds"), base.rollout_poll_seconds),
            rollout_max_launches=_as_int(d.get("rollout_max_launches"), base.rollout_max_launches),
            rollout_max_unreachable=_as_int(d.get("rollout_max_unreachable"), base.rollout_max_unreachable),
            rollout_timeout_seconds=_as_int(d.get("rollout_timeout_seconds"), base.rollout_timeout_seconds),
            rollout_strategy=_as_str(d.get("rollout_strategy")).lower() or base.rollout_strategy,
            rollout_stages=_as_stages(d.get("rollout_stages")) or base.rollout_stages,
            rollout_fill_wave_size=_as_int(d.get("rollout_fill_wave_size"), base.rollout_fill_wave_size),
            rollout_stall_seconds=_as_int(d.get("rollout_stall_seconds"), base.rollout_stall_seconds),
            rollout_download_timeout_seconds=_as_int(d.get("rollout_download_timeout_seconds"),
                                                     base.rollout_download_timeout_seconds),
        )

    def to_dict(self) -> dict[str, Any]:
        # The store keys documents by "id"; carry both so a get-by-id and a read of cluster_id agree.
        out = asdict(self)
        out["id"] = self.cluster_id
        return out

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.cluster_id):
            problems.append(f"invalid cluster id: {self.cluster_id!r}")
        # Campaign pacing bounds. Parallelism matches the rollout engine's own 1..128 cap; the rest
        # must be positive -- a zero wave size or poll interval would spin without making progress.
        if not 1 <= self.rollout_parallelism <= 128:
            problems.append(f"rollout_parallelism must be between 1 and 128, got {self.rollout_parallelism}")
        for label, value in (
            ("rollout_wave_size", self.rollout_wave_size),
            ("rollout_poll_seconds", self.rollout_poll_seconds),
            ("rollout_max_launches", self.rollout_max_launches),
            ("rollout_max_unreachable", self.rollout_max_unreachable),
            ("rollout_timeout_seconds", self.rollout_timeout_seconds),
        ):
            if value < 1:
                problems.append(f"{label} must be at least 1, got {value}")
        if self.rollout_strategy not in ROLLOUT_STRATEGIES:
            problems.append(f"rollout_strategy must be one of {', '.join(ROLLOUT_STRATEGIES)}, got {self.rollout_strategy!r}")
        problems.extend(validate_rollout_stages(self.rollout_stages))
        if self.rollout_fill_wave_size < 0:
            problems.append(f"rollout_fill_wave_size must be 0 (same as the wave size) or more, got {self.rollout_fill_wave_size}")
        if self.rollout_stall_seconds < 0:
            problems.append(f"rollout_stall_seconds must be 0 (off) or more, got {self.rollout_stall_seconds}")
        if self.rollout_download_timeout_seconds < 60:
            problems.append("rollout_download_timeout_seconds must be at least 60, "
                            f"got {self.rollout_download_timeout_seconds}")
        return problems

    def provisioning_blockers(self) -> list[str]:
        """What is missing before this instance can be provisioned. Empty means ready to try.

        Separate from :meth:`validate`: a settings document is *valid* to store with the addresses
        still blank (an operator saves it, then fills them in), but provisioning cannot start
        without them. Keeping the two apart means the editor accepts a draft while the reconcile
        still refuses to run against an underspecified instance.
        """
        blockers: list[str] = []
        # These have no safe default: the cluster's own addresses and the hostname pattern its
        # compute peers follow. The inventory *source path* defaults under install_root (the reconcile
        # writes it), so it is not a blocker; everything else defaults.
        required = (
            ("manager_ip", self.manager_ip),
            ("appserver_ip", self.appserver_ip),
            ("inventory_pattern", self.inventory_pattern),
        )
        for label, value in required:
            if not value.strip():
                blockers.append(f"{label} is not set")
        return blockers

    def resolved_inventory_source(self) -> str:
        """The inventory file path: explicit, or a default under install_root the reconcile writes."""
        return self.inventory_source.strip() or f"{self.install_root.rstrip('/')}/inventory/eligible.nodes"

    def appserver_connection(self) -> dict[str, str]:
        """SSH parameters the reconcile uses to reach the P2P app server.

        The host falls back to ``appserver_ip`` because on a single-app-server cluster they are the
        same address; a site whose app server answers on a different management address overrides it.
        The key is a path readable inside the container (e.g. the mounted deployment key).
        """
        return {
            "host": self.appserver_ssh_host.strip() or self.appserver_ip.strip(),
            "user": self.appserver_ssh_user.strip() or "root",
            "key": self.appserver_ssh_key.strip(),
        }

    def aligned_to(self, cluster: Cluster) -> P2pSettings:
        """These settings with the image directories taken from *cluster* where it states them.

        The cluster's ``shared_container_dir`` / ``node_container_dir`` are the source of truth for
        where images live; P2P derives its origin directory and peer root from them, so the two
        can never point at different places. Fields the cluster leaves blank keep P2P's own value.
        """
        conn = cluster.connection
        shared = conn.shared_container_dir.rstrip("/")
        node = conn.node_container_dir.rstrip("/")
        return replace(
            self,
            container_path=shared or self.container_path,
            peer_root=f"{node}/.nvis-p2p" if node else self.peer_root,
        )

    def to_render_env(self) -> dict[str, str]:
        """The ``NVIS_*`` environment ``render-site-config.py`` consumes.

        Secret *file paths* are derived under ``install_root/secrets`` rather than stored: render
        writes the secrets there, so the document never carries one.
        """
        secrets = f"{self.install_root}/secrets"
        return {
            "NVIS_INSTALL_ROOT": self.install_root,
            "NVIS_SERVICE_USER": self.service_user,
            "NVIS_SERVICE_GROUP": self.service_group,
            "NVIS_SERVICE_UID": str(self.service_uid),
            "NVIS_SERVICE_GID": str(self.service_gid),
            "NVIS_MANAGER_IP": self.manager_ip,
            "NVIS_APPSERVER_IP": self.appserver_ip,
            "NVIS_CONSOLE_BIND": self.console_bind,
            "NVIS_INVENTORY_SOURCE": self.resolved_inventory_source(),
            "NVIS_INVENTORY_PATTERN": self.inventory_pattern,
            "NVIS_CONTAINER_PATH": self.container_path,
            "NVIS_CATALOG_PATH": self.catalog_path,
            "NVIS_PEER_ROOT": self.peer_root,
            "NVIS_OBJECT_STORE_DATA_PATH": self.object_store_data_path,
            "NVIS_SEED_CACHE_PATH": self.seed_cache_path,
            "NVIS_POSTGRES_PASSWORD_FILE": f"{secrets}/postgres-password",
            "NVIS_MANAGER_JWT_KEY_FILE": f"{secrets}/manager-jwt-key",
            "NVIS_MINIO_ROOT_PASSWORD_FILE": f"{secrets}/minio-root-password",
            "NVIS_DFSTORE_READONLY_ENV_FILE": f"{secrets}/dfstore-readonly.env",
            "NVIS_SYSTEMD_DIR": self.systemd_dir,
        }


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: A catalog filename is a bare enroot squashfs name -- never a path, so an edit cannot point the
#: fabric at a file outside the canonical directory.
_CATALOG_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.sqsh$")


@dataclass
class P2pCatalogImage:
    """One catalog image -- a "task" an operator can distribute.

    ``filename`` is the canonical enroot squashfs name and the key within a catalog; ``sha256`` is
    the content pin every node verifies a download against, and may be blank for an image added but
    not yet imported/pinned.
    """

    order: int = 0
    name: str = ""
    filename: str = ""
    bytes: int = 0
    sha256: str = ""
    required_by: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> P2pCatalogImage:
        d = _as_dict(d)
        return cls(
            order=_as_int(d.get("order"), 0),
            name=_as_str(d.get("name")),
            filename=_as_str(d.get("filename")),
            bytes=_as_int(d.get("bytes"), 0),
            sha256=_as_str(d.get("sha256")).lower(),
            required_by=_as_str_list(d.get("required_by")),
        )


@dataclass
class P2pCatalog:
    """A cluster's editable catalog -- the list of distributable images ("tasks").

    Stored whole by ``cluster_id`` behind the ``Repo`` (a document, like :class:`P2pSettings`), and
    rendered to the instance's ``catalog_path`` by a sync so the fabric uses what the operator
    authored. Rack Status is the source of truth; the file on the cluster is a projection of it.
    """

    cluster_id: str
    images: list[P2pCatalogImage] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> P2pCatalog:
        d = _as_dict(d)
        images_raw = d.get("images")
        images = [P2pCatalogImage.from_dict(i) for i in images_raw] if isinstance(images_raw, list) else []
        return cls(cluster_id=_as_str(d.get("cluster_id")), images=images)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["id"] = self.cluster_id
        return out

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.cluster_id):
            problems.append(f"invalid cluster id: {self.cluster_id!r}")
        seen: set[str] = set()
        for i, img in enumerate(self.images):
            where = img.filename or f"image #{i + 1}"
            if not _CATALOG_FILENAME_RE.match(img.filename):
                problems.append(f"{where}: filename must be a bare *.sqsh name")
            elif img.filename in seen:
                problems.append(f"{where}: duplicate filename")
            else:
                seen.add(img.filename)
            if not img.name.strip():
                problems.append(f"{where}: name is required")
            if img.sha256 and not _SHA256_RE.match(img.sha256):
                problems.append(f"{where}: sha256 must be 64 hex characters")
            if img.bytes < 0:
                problems.append(f"{where}: bytes must not be negative")
        return problems


#: A node name is a bare hostname -- no spaces or path characters, so a roster cannot smuggle a
#: shell fragment to the fan-out that targets it.
_ROSTER_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class P2pRoster:
    """A named list of eligible nodes an operation can target."""

    name: str = ""
    description: str = ""
    nodes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> P2pRoster:
        d = _as_dict(d)
        return cls(
            name=_as_str(d.get("name")),
            description=_as_str(d.get("description")),
            nodes=[n.strip() for n in _as_str_list(d.get("nodes")) if n.strip()],
        )


@dataclass
class P2pRosters:
    """A cluster's named rosters -- node lists an operation targets by name instead of the single
    inventory. Stored whole by ``cluster_id`` behind the ``Repo``, like the other P2P documents."""

    cluster_id: str
    rosters: list[P2pRoster] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> P2pRosters:
        d = _as_dict(d)
        rosters_raw = d.get("rosters")
        rosters = [P2pRoster.from_dict(r) for r in rosters_raw] if isinstance(rosters_raw, list) else []
        return cls(cluster_id=_as_str(d.get("cluster_id")), rosters=rosters)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["id"] = self.cluster_id
        return out

    def get(self, name: str) -> P2pRoster | None:
        """The roster with this name, or None."""
        return next((r for r in self.rosters if r.name == name), None)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.cluster_id):
            problems.append(f"invalid cluster id: {self.cluster_id!r}")
        seen: set[str] = set()
        for i, roster in enumerate(self.rosters):
            where = roster.name or f"roster #{i + 1}"
            if not roster.name.strip():
                problems.append(f"{where}: name is required")
            elif roster.name in seen:
                problems.append(f"{where}: duplicate name")
            else:
                seen.add(roster.name)
            if not roster.nodes:
                problems.append(f"{where}: at least one node is required")
            bad = next((n for n in roster.nodes if not _ROSTER_NODE_RE.match(n)), None)
            if bad is not None:
                problems.append(f"{where}: invalid node name {bad!r}")
        return problems


#: A pacing override of 0 means "inherit the cluster's rollout default"; a positive value overrides
#: it for this campaign only. These are the keys the operations enqueue reads off a request's
#: ``rollout`` block, so the names match ``_rollout_env`` in the worker, minus the ``rollout_`` prefix.
_CAMPAIGN_PACING_FIELDS = (
    "wave_size",
    "parallelism",
    "poll_seconds",
    "max_launches",
    "max_unreachable",
    "timeout_seconds",
    "fill_wave_size",
    "stall_seconds",
    "download_timeout_seconds",
)


@dataclass
class P2pCampaign:
    """A saved distribution: a task (catalog image) paired with a roster and optional pacing.

    A campaign is the composable unit an operator names once and launches many times -- "roll
    *this image* to *this roster* at *this pace*". Launching it enqueues a ``distribute`` operation
    resolving the image, the roster, and any pacing overrides; the definition itself holds no run
    state, so the same campaign re-runs idempotently and its history lives in the operations log.
    """

    name: str = ""
    description: str = ""
    image: str = ""
    roster: str = ""
    #: Pacing overrides; 0 on any field means "use the cluster's rollout default for it".
    wave_size: int = 0
    parallelism: int = 0
    poll_seconds: int = 0
    max_launches: int = 0
    max_unreachable: int = 0
    timeout_seconds: int = 0
    fill_wave_size: int = 0
    #: Stall window override; 0 inherits (turning stall handling off is a cluster setting).
    stall_seconds: int = 0
    download_timeout_seconds: int = 0
    #: Wave planning overrides; blank / empty inherits the cluster's ``rollout_strategy`` and
    #: ``rollout_stages``.
    strategy: str = ""
    stages: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> P2pCampaign:
        d = _as_dict(d)
        return cls(
            name=_as_str(d.get("name")),
            description=_as_str(d.get("description")),
            image=_as_str(d.get("image")),
            roster=_as_str(d.get("roster")),
            wave_size=_as_int(d.get("wave_size"), 0),
            parallelism=_as_int(d.get("parallelism"), 0),
            poll_seconds=_as_int(d.get("poll_seconds"), 0),
            max_launches=_as_int(d.get("max_launches"), 0),
            max_unreachable=_as_int(d.get("max_unreachable"), 0),
            timeout_seconds=_as_int(d.get("timeout_seconds"), 0),
            fill_wave_size=_as_int(d.get("fill_wave_size"), 0),
            stall_seconds=_as_int(d.get("stall_seconds"), 0),
            download_timeout_seconds=_as_int(d.get("download_timeout_seconds"), 0),
            strategy=_as_str(d.get("strategy")).lower(),
            stages=_as_stages(d.get("stages")),
        )

    def pacing_overrides(self) -> dict[str, Any]:
        """The overridden pacing and planning fields, keyed as the enqueue's ``rollout`` block expects.

        Only overridden fields appear -- a positive number, a named strategy, a non-empty ladder --
        so the enqueue can layer them onto the cluster defaults and a campaign that overrides nothing
        inherits the cluster's pacing entirely.
        """
        out: dict[str, Any] = {f: v for f in _CAMPAIGN_PACING_FIELDS if (v := getattr(self, f)) > 0}
        if self.strategy:
            out["strategy"] = self.strategy
        if self.stages:
            out["stages"] = list(self.stages)
        return out


@dataclass
class P2pCampaigns:
    """A cluster's saved campaigns. Stored whole by ``cluster_id`` behind the ``Repo``, like the
    other P2P documents -- the catalog it draws images from and the rosters it targets."""

    cluster_id: str
    campaigns: list[P2pCampaign] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> P2pCampaigns:
        d = _as_dict(d)
        raw = d.get("campaigns")
        campaigns = [P2pCampaign.from_dict(c) for c in raw] if isinstance(raw, list) else []
        return cls(cluster_id=_as_str(d.get("cluster_id")), campaigns=campaigns)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["id"] = self.cluster_id
        return out

    def get(self, name: str) -> P2pCampaign | None:
        """The campaign with this name, or None."""
        return next((c for c in self.campaigns if c.name == name), None)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not valid_id(self.cluster_id):
            problems.append(f"invalid cluster id: {self.cluster_id!r}")
        seen: set[str] = set()
        for i, c in enumerate(self.campaigns):
            where = c.name or f"campaign #{i + 1}"
            if not c.name.strip():
                problems.append(f"{where}: name is required")
            elif c.name in seen:
                problems.append(f"{where}: duplicate name")
            else:
                seen.add(c.name)
            if not _CATALOG_FILENAME_RE.match(c.image):
                problems.append(f"{where}: image must be a bare *.sqsh name")
            # The roster is a reference, not a node list: blank means the whole fleet, and a name is
            # free-form (roster names allow spaces, e.g. "All fleet"). It is only ever a lookup key,
            # never interpolated into a fan-out, so it carries no charset constraint here; the enqueue
            # rejects a name that resolves to no roster at launch time.
            # Pacing overrides are optional (0 inherits the cluster default); when set they obey the
            # same bounds the settings document enforces, so a launch can't pace past the engine's cap.
            if c.parallelism and not 1 <= c.parallelism <= 128:
                problems.append(f"{where}: parallelism override must be between 1 and 128, got {c.parallelism}")
            for label in ("wave_size", "poll_seconds", "max_launches", "max_unreachable", "timeout_seconds",
                          "fill_wave_size", "stall_seconds"):
                if getattr(c, label) < 0:
                    problems.append(f"{where}: {label} override must not be negative")
            if c.download_timeout_seconds and c.download_timeout_seconds < 60:
                problems.append(f"{where}: download_timeout_seconds override must be at least 60 (or 0 to inherit)")
            if c.strategy and c.strategy not in ROLLOUT_STRATEGIES:
                problems.append(f"{where}: strategy must be one of {', '.join(ROLLOUT_STRATEGIES)} (or blank to inherit)")
            if c.stages:
                problems.extend(validate_rollout_stages(c.stages, where=f"{where}: stages"))
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
