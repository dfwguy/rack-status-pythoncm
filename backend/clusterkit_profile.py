# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""ClusterKit deployment policy, resolved through the existing Settings ladder.

This module has no Flask, database, scheduler or execution side effects. A profile
is administrator policy, never an ordinary run-request payload.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

SETTING = "RACK_CLUSTERKIT_SITE_PROFILE"
PAIRWISE_COUNTS = (2, 4, 8)  # Recommended legacy choices, not site geometry.
MAX_PAIRWISE_NODES = 64  # Reviewed bounded capability; administrator limits may be smaller.


def valid_pairwise_count(value):
    return type(value) is int and 2 <= value <= MAX_PAIRWISE_NODES and value % 2 == 0


HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def valid_node(value):
    return (isinstance(value, str) and len(value) <= 253
            and all(HOST.fullmatch(part) for part in value.split(".")))


def valid_path(value):
    return (isinstance(value, str) and value.startswith("/") and value != "/"
            and not any(c in value for c in (",", "%", "\n", "\r", "\0"))
            and not any(ord(c) < 32 for c in value)
            and ".." not in PurePosixPath(value).parts)


@dataclass(frozen=True)
class FunctionalParameters:
    bandwidth_iterations: int = 8
    bandwidth_bytes: int = 8388608
    latency_iterations: int = 256
    latency_bytes: int = 8
    test_limit_seconds: int = 30


@dataclass(frozen=True)
class PerformancePolicy:
    enabled: bool = False
    verified: bool = False
    reference: str = ""
    bandwidth_min_mbytes_per_sec: float | None = None
    bandwidth_max_mbytes_per_sec: float | None = None
    latency_max_usec: float | None = None


@dataclass(frozen=True)
class TopologyGroup:
    name: str
    nodes: tuple[str, ...]
    kind: str = "group"
    authoritative: bool = False
    source: str = "administrator mapping"


@dataclass(frozen=True)
class SiteProfile:
    enabled: bool = False
    partition: str = ""
    hpcx_init: str = ""
    results_root: str = ""
    allowed_node_counts: tuple[int, ...] = (2,)
    max_nodes: int = 2
    default_wall_seconds: int = 300
    max_wall_seconds: int = 300
    expected_architecture: str = ""
    hca_policy: str = "configured"
    hca: str = ""
    expected_link_layer: str = ""
    gid_policy: str = "global"
    ucx_transports: tuple[str, ...] = ("rc", "sm", "self")
    path_comparison: bool = False
    node_allowlist: tuple[str, ...] = ()
    node_regex: str = ""
    topology_groups: tuple[TopologyGroup, ...] = ()
    rack_inventory: dict | None = None
    whole_rack_enabled: bool = False
    functional: FunctionalParameters = FunctionalParameters()
    performance: PerformancePolicy = PerformancePolicy()

    def as_dict(self):
        values = asdict(self)
        if not values["whole_rack_enabled"]:
            values.pop("whole_rack_enabled")  # Existing plan/profile hashes stay stable.
        if values["rack_inventory"] is None:
            values.pop("rack_inventory")  # Preserve legacy profile/plan serialization.
        values["topology_groups"] = {
            group.name: {key: value for key, value in asdict(group).items() if key != "name"}
            for group in self.topology_groups
        }
        return json.loads(json.dumps(values))

    def whole_rack_reason(self, group):
        """One capability check shared by planning and the directory."""
        if not self.whole_rack_enabled:
            return "Whole-rack testing is disabled by the administrator"
        if group is None or group.kind != "rack" or not group.authoritative:
            return "Whole-rack testing requires authoritative physical rack membership"
        if not valid_pairwise_count(len(group.nodes)):
            return "Whole-rack pairwise testing requires an even supported node count"
        if len(group.nodes) > self.max_nodes:
            return "Rack exceeds the administrator node limit"
        if any(not self.allows(node) for node in group.nodes):
            return "Every mapped rack member must be permitted by the administrator"
        members = set(group.nodes)
        if any(other.kind == "rack" and other.name != group.name and members.intersection(other.nodes)
               for other in self.topology_groups):
            return "Ambiguous physical rack membership; resolve overlapping rack mappings first"
        return ""

    def allows(self, node):
        return (valid_node(node)
                and (not self.node_allowlist or node in self.node_allowlist)
                and (not self.node_regex or re.fullmatch(self.node_regex, node) is not None))


@dataclass(frozen=True)
class ProfileResolution:
    profile: SiteProfile | None
    errors: tuple[str, ...] = ()
    inventory: dict | None = None

    @property
    def ready(self):
        return bool(self.profile and self.profile.enabled and not self.errors)

    def public(self):
        profile = self.profile
        return {"schema_version": 1, "enabled": bool(profile and profile.enabled),
                "configuration_ready": self.ready, "errors": list(self.errors),
                "disabled_reason": "ClusterKit is disabled" if profile and not profile.enabled else None,
                "allowed_node_counts": list(profile.allowed_node_counts) if profile else [],
                "max_nodes": profile.max_nodes if profile else None,
                "performance_status": "UNVERIFIED",
                "compute_preflight_verified": False}


def parse_profile(raw) -> ProfileResolution:
    """Validate a single atomic profile; never merge fragments from different sites."""
    errors = []
    if raw is None or raw == "":
        raw = {}
    if isinstance(raw, str):
        if len(raw) > 8192:
            return ProfileResolution(None, ("Site profile exceeds the Settings 8192-character limit",))
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return ProfileResolution(None, ("Site profile must be valid JSON",))
    if not isinstance(raw, dict):
        return ProfileResolution(None, ("Site profile must be an object",))
    unknown = set(raw) - set(SiteProfile.__dataclass_fields__)
    if unknown:
        errors.append("Unknown site profile fields")
    defaults = SiteProfile()
    values = {}
    for key in SiteProfile.__dataclass_fields__:
        values[key] = raw.get(key, getattr(defaults, key))

    for key in ("enabled", "path_comparison", "whole_rack_enabled"):
        if type(values[key]) is not bool:
            errors.append(f"{key} must be a boolean")
    for key in ("partition", "hpcx_init", "results_root", "expected_architecture", "hca_policy",
                "hca", "expected_link_layer", "gid_policy", "node_regex"):
        if not isinstance(values[key], str):
            errors.append(f"{key} must be a string")
    for key in ("max_nodes", "default_wall_seconds", "max_wall_seconds"):
        if type(values[key]) is not int or values[key] < 1:
            errors.append(f"{key} must be a positive integer")
    if errors:
        return ProfileResolution(None, tuple(errors))
    if values["default_wall_seconds"] > values["max_wall_seconds"]:
        errors.append("default_wall_seconds exceeds max_wall_seconds")
    sizes = values["allowed_node_counts"]
    if not isinstance(sizes, (list, tuple)) or any(not valid_pairwise_count(n) for n in sizes):
        errors.append(f"allowed_node_counts must contain even sizes between 2 and {MAX_PAIRWISE_NODES}")
    elif len(set(sizes)) != len(sizes) or any(n > values["max_nodes"] for n in sizes):
        errors.append("allowed_node_counts must be unique and within max_nodes")
    else:
        values["allowed_node_counts"] = tuple(sizes)

    for key in ("hpcx_init", "results_root"):
        if values[key] and not valid_path(values[key]):
            errors.append(f"{key} must be an absolute safe POSIX path without traversal or export delimiters")
    if values["partition"] and not LABEL.fullmatch(values["partition"]):
        errors.append("Invalid partition")
    if values["expected_architecture"] not in ("", "aarch64", "x86_64"):
        errors.append("Unsupported expected_architecture")
    if values["expected_link_layer"] not in ("", "Ethernet", "InfiniBand"):
        errors.append("expected_link_layer must be Ethernet or InfiniBand")
    if values["hca_policy"] not in ("configured", "discover"):
        errors.append("hca_policy must be configured or discover")
    if values["hca"] and not re.fullmatch(r"[A-Za-z0-9_]+:[1-9][0-9]*", values["hca"]):
        errors.append("hca must be exactly one device:port")
    if values["gid_policy"] not in ("global", "auto"):
        errors.append("gid_policy must be global or auto")
    tls = values["ucx_transports"]
    if not isinstance(tls, (list, tuple)) or not tls or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_]+", v) for v in tls):
        errors.append("ucx_transports must contain transport names, not raw arguments")
    elif len(set(tls)) != len(tls):
        errors.append("ucx_transports must be unique")
    else:
        values["ucx_transports"] = tuple(tls)

    allowlist = values["node_allowlist"]
    if (not isinstance(allowlist, (list, tuple)) or len(allowlist) > 512
            or any(not valid_node(n) for n in allowlist)):
        errors.append("node_allowlist must contain literal valid hostnames")
    elif len({n.casefold() for n in allowlist}) != len(allowlist):
        errors.append("Duplicate node_allowlist entry")
    else:
        values["node_allowlist"] = tuple(allowlist)
    pattern = values["node_regex"]
    if pattern:
        # A constrained admin pattern: no captures/lookarounds/backreferences or
        # alternation. Node input itself is length/character limited first.
        try:
            if len(pattern) > 256 or any(c in pattern for c in "()|") or re.search(r"\\[1-9]", pattern):
                raise ValueError()
            re.compile(pattern)
        except (ValueError, re.error):
            errors.append("node_regex must be a valid simple pattern without groups or alternation")

    source = values["rack_inventory"]
    if source is not None:
        if not isinstance(source, dict) or set(source) - {"snapshot_db", "cluster_id", "log_root", "checkpoint_only", "max_age_seconds"}:
            errors.append("Invalid rack_inventory source")
        else:
            source = {"checkpoint_only": True, "max_age_seconds": 300, **source}
            if (not valid_path(source.get("snapshot_db")) or not valid_path(source.get("log_root"))
                    or not isinstance(source.get("cluster_id"), str) or not source["cluster_id"].strip()
                    or len(source["cluster_id"]) > 256 or any(ord(c) < 32 for c in source["cluster_id"])
                    or source["checkpoint_only"] is not True
                    or type(source["max_age_seconds"]) is not int or not 1 <= source["max_age_seconds"] <= 86400):
                errors.append("rack_inventory requires safe paths, an exact cluster identity and a bounded freshness limit")
            else:
                values["rack_inventory"] = source

    groups = values["topology_groups"]
    normalized = []
    if isinstance(groups, tuple) and not groups:
        groups = {}
    if not isinstance(groups, dict):
        errors.append("topology_groups must be a mapping")
    else:
        for name, group in groups.items():
            if (not isinstance(name, str) or not LABEL.fullmatch(name) or not isinstance(group, dict)
                    or set(group) - {"nodes", "kind", "authoritative", "source"}):
                errors.append("Invalid topology group")
                continue
            nodes = group.get("nodes", [])
            kind, authority = group.get("kind", "group"), group.get("authoritative", False)
            source = group.get("source", "administrator mapping")
            if (not isinstance(nodes, list) or not nodes or len(nodes) > 512
                    or any(not valid_node(n) for n in nodes)
                    or len({n.casefold() for n in nodes}) != len(nodes)
                    or kind not in ("group", "rack", "su") or type(authority) is not bool
                    or (kind in ("rack", "su") and not authority)
                    or not isinstance(source, str) or not source.strip() or len(source) > 256):
                errors.append(f"Invalid membership/authority for topology group {name}")
                continue
            normalized.append(TopologyGroup(name, tuple(nodes), kind, authority, source))
    values["topology_groups"] = tuple(normalized)

    functional = values["functional"]
    if isinstance(functional, FunctionalParameters):
        functional = asdict(functional)
    if not isinstance(functional, dict) or set(functional) - set(FunctionalParameters.__dataclass_fields__):
        errors.append("Invalid functional parameters")
    else:
        params = asdict(FunctionalParameters()) | functional
        if any(type(v) is not int or v < 1 for v in params.values()):
            errors.append("Functional parameters must be positive integers")
        elif (params["bandwidth_bytes"] > 64 * 1024 * 1024 or params["latency_bytes"] > 1024 * 1024
              or params["bandwidth_iterations"] > 100000 or params["latency_iterations"] > 1000000):
            errors.append("Functional recipe exceeds the bounded CPU/RoCE memory/iteration limits")
        elif params["test_limit_seconds"] > values["default_wall_seconds"]:
            errors.append("Functional time limit exceeds the default allocation wall limit")
        else:
            values["functional"] = FunctionalParameters(**params)

    performance = values["performance"]
    if isinstance(performance, PerformancePolicy):
        performance = asdict(performance)
    if not isinstance(performance, dict) or set(performance) - set(PerformancePolicy.__dataclass_fields__):
        errors.append("Invalid performance policy")
    else:
        perf = asdict(PerformancePolicy()) | performance
        if (type(perf["enabled"]) is not bool or type(perf["verified"]) is not bool
                or not isinstance(perf["reference"], str)):
            errors.append("Invalid performance policy flags/reference")
        for key in ("bandwidth_min_mbytes_per_sec", "bandwidth_max_mbytes_per_sec", "latency_max_usec"):
            val = perf[key]
            if val is not None and (type(val) not in (int, float) or not math.isfinite(val) or val <= 0):
                errors.append(f"Invalid performance threshold {key}")
        if perf["enabled"] and (not perf["verified"] or not isinstance(perf["reference"], str) or not perf["reference"].strip()
                                or not any(perf[k] is not None for k in ("bandwidth_min_mbytes_per_sec", "bandwidth_max_mbytes_per_sec", "latency_max_usec"))):
            errors.append("Performance evaluation requires verified thresholds and an acceptance reference")
        if not errors:
            if (perf["bandwidth_min_mbytes_per_sec"] is not None and perf["bandwidth_max_mbytes_per_sec"] is not None
                    and perf["bandwidth_min_mbytes_per_sec"] > perf["bandwidth_max_mbytes_per_sec"]):
                errors.append("Performance minimum exceeds maximum")
            values["performance"] = PerformancePolicy(**perf)
    if values["enabled"]:
        for key in ("partition", "hpcx_init", "results_root", "expected_architecture", "expected_link_layer"):
            if not values[key]:
                errors.append(f"Enabled profile requires {key}")
        if values["hca_policy"] == "configured" and not values["hca"]:
            errors.append("Configured HCA policy requires hca")
        if not allowlist and not pattern:
            errors.append("Enabled profile requires a node_allowlist or node_regex")
    return ProfileResolution(None, tuple(errors)) if errors else ProfileResolution(SiteProfile(**values))


def resolve_profile() -> ProfileResolution:
    """Production adapter: same Settings/catalog hierarchy, with no write effects."""
    try:
        from config_env import read_only_values

        resolution = parse_profile(read_only_values((SETTING,))[SETTING])
        if resolution.profile and resolution.profile.rack_inventory:
            from clusterkit_inventory import resolve_inventory
            return resolve_inventory(resolution)
        return resolution
    except (OSError, ValueError, TypeError) as exc:
        return ProfileResolution(None, (f"Cannot resolve ClusterKit configuration: {exc}",))
