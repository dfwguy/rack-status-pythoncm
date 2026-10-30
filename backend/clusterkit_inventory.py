# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Rack visibility over Rack Status's existing canonical inventory, without collectors."""
from __future__ import annotations

import re
from dataclasses import replace

from cluster_snapshot import read_existing_snapshot
from clusterkit_profile import LABEL, ProfileResolution, TopologyGroup, valid_node

MAX_RACKS = 2048
MAX_NODES = 16384


def natural_key(value):
    return tuple((1, int(p)) if p.isdecimal() else (0, p.casefold())
                 for p in re.split(r"([0-9]+)", value))


def _source_label(source):
    return "Rack Status inventory: " + source["cluster_id"]


def _normalize(snapshot, source):
    if snapshot is None or snapshot.payload is None:
        raise ValueError("No canonical rack inventory exists for the configured cluster and log root")
    payload = snapshot.payload
    raw_nodes, raw_rows = payload.get("nodes"), payload.get("rows", [])
    if (not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_NODES
            or not isinstance(raw_rows, list) or len(raw_rows) > MAX_RACKS):
        raise ValueError("Canonical rack inventory is missing or exceeds its bounds")
    names = set()
    racks = {}
    spelling = {}
    for row in raw_rows:
        name = row.get("rack") if isinstance(row, dict) else None
        if not isinstance(name, str) or not LABEL.fullmatch(name):
            raise ValueError("Canonical inventory contains an invalid rack name")
        if name.casefold() in spelling and spelling[name.casefold()] != name:
            raise ValueError("Canonical inventory contains ambiguous rack names")
        spelling[name.casefold()] = name
        racks.setdefault(name, [])
    nodes, seen = [], set()
    for raw in raw_nodes:
        if not isinstance(raw, dict) or not valid_node(raw.get("id")):
            raise ValueError("Canonical inventory contains an invalid node")
        name = raw["id"]
        if name.casefold() in seen:
            raise ValueError("Canonical inventory contains duplicate or ambiguous node placement")
        seen.add(name.casefold())
        rack = raw.get("rack")
        if rack is not None and (not isinstance(rack, str) or (rack and not LABEL.fullmatch(rack))):
            raise ValueError("Canonical inventory contains invalid rack placement")
        if rack:
            rack = spelling.get(rack.casefold(), rack)
            spelling[rack.casefold()] = rack
        node = {key: raw.get(key) for key in ("id", "rack", "tray", "status", "scheduler_status", "scheduler_state")}
        if node["status"] not in ("up", "down", "inst", "unknown"):
            node["status"] = "unknown"
        if node["scheduler_status"] not in ("idle", "running", "pending", "reserved", "draining", "drained", "down", "unknown"):
            node["scheduler_status"] = "unknown"
        node["scheduler_state"] = str(node["scheduler_state"] or "")
        if node["scheduler_status"] == "idle" and node["scheduler_state"].strip().upper() != "IDLE":
            node["scheduler_status"] = "unknown"
        node["rack"] = rack or None
        node["ip"] = ""
        if type(node["tray"]) is not int:
            node.pop("tray")
        nodes.append(node)
        if rack:
            racks.setdefault(rack, []).append(name)
            names.add(name)
    if any(len(members) > 512 for members in racks.values()):
        raise ValueError("A rack exceeds the bounded 512-node inventory limit")
    if len(racks) > MAX_RACKS:
        raise ValueError("Canonical rack count exceeds its bound")
    metadata = snapshot.metadata()
    age = metadata["age_seconds"]
    fresh = snapshot.state == "fresh" and age is not None and age <= source["max_age_seconds"]
    metadata.update(state="fresh" if fresh else "stale",
                    source="Rack Status canonical inventory",
                    checkpoint_only=source["checkpoint_only"],
                    max_age_seconds=source["max_age_seconds"])
    authoritative = payload.get("rack_mapping_mode") in ("translate", "direct")
    return {"racks": {key: sorted(values, key=natural_key) for key, values in sorted(racks.items(), key=lambda item: natural_key(item[0]))},
            "nodes": nodes, "snapshot": metadata, "authoritative": authoritative,
            "unplaced_node_count": len(nodes) - len(names),
            "node_sources": {key: str((payload.get("node_sources") or {}).get(key) or "") for key in ("orchestrator", "scheduler")},
            "hierarchy": payload.get("hierarchy"),
            "scalable_units": payload.get("scalable_units") or [],
            "naming": payload.get("naming")}


def resolve_inventory(resolution):
    """Resolve physical membership once, retaining dynamic status outside site-policy hashes."""
    source = resolution.profile.rack_inventory
    try:
        snapshot = read_existing_snapshot(source["snapshot_db"], source["cluster_id"], source["log_root"],
                                          checkpoint_only=source["checkpoint_only"])
        inventory = _normalize(snapshot, source)
        configured = {group.name: group for group in resolution.profile.topology_groups}
        if inventory["authoritative"]:
            for name, members in inventory["racks"].items():
                if not members:
                    continue
                old = configured.get(name)
                if old is not None and set(old.nodes) != set(members):
                    raise ValueError("Configured group conflicts with canonical rack membership: " + name)
                configured[name] = TopologyGroup(name, tuple(members), "rack", True, _source_label(source))
        profile = replace(resolution.profile, topology_groups=tuple(configured[name] for name in sorted(configured, key=natural_key)))
        return ProfileResolution(profile, resolution.errors, inventory)
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Visibility remains available with an explicit unavailable state; planning fails closed.
        return ProfileResolution(resolution.profile, resolution.errors,
                                 {"racks": {}, "nodes": [], "snapshot": {"state": "unavailable", "age_seconds": None},
                                  "authoritative": False, "unplaced_node_count": 0,
                                  "error": str(error)})


def require_current_inventory(resolution):
    if resolution.profile and resolution.profile.rack_inventory:
        inventory = resolution.inventory or {}
        if inventory.get("snapshot", {}).get("state") != "fresh":
            raise ValueError(inventory.get("error") or "Rack inventory is stale or unavailable; refresh the canonical inventory before previewing")
        if not inventory.get("authoritative"):
            raise ValueError("Canonical inventory does not provide authoritative rack placement")


def _state_counts(nodes):
    counts = {"idle": 0, "busy": 0, "drain": 0, "down": 0, "reserved": 0, "unknown": 0}
    for node in nodes:
        state = node["scheduler_state"].strip().upper()
        if "DRAIN" in state or "DRNG" in state:
            key = "drain"
        elif state.startswith("DOWN"):
            key = "down"
        elif state in ("RESERVED", "RESV"):
            key = "reserved"
        elif state == "IDLE":
            key = "idle"
        elif state in ("ALLOCATED", "ALLOC", "MIXED", "MIX"):
            key = "busy"
        else:
            key = "unknown"
        counts[key] += 1
    return counts


def directory(resolution):
    profile = resolution.profile
    if profile is None:
        raise ValueError("ClusterKit site profile is invalid")
    inventory = resolution.inventory
    if inventory is None:
        # Backward compatibility: manual groups remain supported without a canonical feed.
        groups = {group.name: list(group.nodes) for group in profile.topology_groups}
        inventory = {"racks": groups,
                     "nodes": [{"id": name, "rack": group.name, "tray": None, "status": "unknown",
                                "scheduler_status": "unknown", "scheduler_state": ""}
                               for group in profile.topology_groups for name in group.nodes],
                     "snapshot": {"state": "configured", "age_seconds": None,
                                  "source": "Administrator-configured groups"},
                     "authoritative": True, "unplaced_node_count": 0}
    racks = []
    by_rack = {}
    for node in inventory["nodes"]:
        by_rack.setdefault(node["rack"], []).append(node)
    for name, members in inventory["racks"].items():
        nodes = by_rack.get(name, [])
        allowed = sum(profile.allows(node) for node in members)
        authoritative = inventory["authoritative"] and bool(members)
        sizes = [n for n in (1, *profile.allowed_node_counts) if n <= profile.max_nodes and n <= allowed]
        group = next((entry for entry in profile.topology_groups if entry.name == name), None)
        whole_reason = (profile.whole_rack_reason(group) or
                        ("ClusterKit is disabled or invalid" if not resolution.ready else
                         "Rack inventory is stale or unavailable" if inventory["snapshot"]["state"] not in ("fresh", "configured") else ""))
        racks.append({"whole_rack_enabled": not whole_reason, "whole_rack_reason": whole_reason,
                      "name": name, "node_count": len(members), "nodes": members,
                      "counts": _state_counts(nodes), "allowed_node_count": allowed,
                      "allowed_node_counts": sizes if authoritative else [],
                      "authoritative": authoritative,
                      "test_enabled": bool(resolution.ready and sizes and authoritative
                                           and inventory["snapshot"]["state"] in ("fresh", "configured")),
                      "reason": ("No authoritative node membership" if not authoritative else
                                 "Testing is not enabled by the site profile" if not allowed else
                                 "Rack inventory is stale or unavailable" if inventory["snapshot"]["state"] not in ("fresh", "configured") else "")})
    return {"schema_version": 1, "racks": racks, "nodes": inventory["nodes"],
            "snapshot": inventory["snapshot"], "node_sources": inventory.get("node_sources", {"orchestrator": "", "scheduler": ""}),
            "unplaced_node_count": inventory["unplaced_node_count"], "error": inventory.get("error"),
            "naming": inventory.get("naming"), "hierarchy": inventory.get("hierarchy"),
            "scalable_units": inventory.get("scalable_units", []),
            "note": "Availability is a snapshot. Preview rechecks Slurm; no nodes are reserved."}
