# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Read-only ClusterKit planning. Deliberately has no submit/execute operation."""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import shlex
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from clusterkit_job import job_spec
from clusterkit_profile import (
    LABEL,
    MAX_PAIRWISE_NODES,
    ProfileResolution,
    valid_node,
    valid_pairwise_count,
    valid_path,
)
from schedulers.slurm import SlurmScheduler


class PlanningError(ValueError):
    pass


@dataclass(frozen=True)
class RunRequest:
    scope: str
    node_count: int
    nodes: tuple[str, ...] = ()
    group: str = ""
    label: str = "clusterkit"
    test_mode: str = "cpu-pairwise"
    path_comparison: bool | None = None


def parse_request(raw):
    if not isinstance(raw, dict) or set(raw) - set(RunRequest.__dataclass_fields__):
        raise PlanningError("Request contains unknown fields; deployment overrides are not run inputs")
    scope = raw.get("scope")
    if scope not in ("preflight", "explicit", "group", "rack"):
        raise PlanningError("scope must be preflight, explicit, group or rack")
    nodes = raw.get("nodes", [])
    if not isinstance(nodes, list) or len(nodes) > MAX_PAIRWISE_NODES or any(not valid_node(n) for n in nodes):
        raise PlanningError("nodes must be a bounded list of literal hostnames")
    if len({n.casefold() for n in nodes}) != len(nodes):
        raise PlanningError("Duplicate nodes are forbidden")
    count = raw.get("node_count", len(nodes) if scope not in ("group", "rack") else None)
    if type(count) is not int or count < 1:
        raise PlanningError("node_count must be a positive integer")
    if scope != "preflight" and not valid_pairwise_count(count):
        raise PlanningError(f"Pairwise node_count must be even and between 2 and {MAX_PAIRWISE_NODES}")
    group, label = raw.get("group", ""), raw.get("label", "clusterkit")
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        raise PlanningError("label must contain 1–64 letters/digits/underscore/dash and start alphanumeric")
    if not isinstance(group, str) or (group and not LABEL.fullmatch(group)):
        raise PlanningError("Invalid group name")
    if scope in ("group", "rack") or (scope == "preflight" and group):
        if not group or nodes:
            raise PlanningError("Group selection requires a configured group and no explicit nodes")
    elif group or not nodes or len(nodes) != count:
        raise PlanningError("Explicit/preflight selection requires exactly node_count nodes and no group")
    if scope == "preflight" and count != 1:
        raise PlanningError("Preflight requires exactly one node")
    mode = raw.get("test_mode", "preflight" if scope == "preflight" else "cpu-pairwise")
    if mode != ("preflight" if scope == "preflight" else "cpu-pairwise"):
        raise PlanningError("Unsupported test mode for this scope")
    compare = raw.get("path_comparison")
    if compare is not None and type(compare) is not bool:
        raise PlanningError("path_comparison must be a boolean")
    if scope == "preflight" and compare:
        raise PlanningError("Preflight does not perform path comparisons")
    return RunRequest(scope, count, tuple(nodes), group, label, mode, compare)


def candidates_for(resolution, request):
    """Enforce profile/request limits before querying any scheduler."""
    if not isinstance(resolution, ProfileResolution) or not resolution.ready:
        raise PlanningError("ClusterKit profile is disabled or invalid")
    profile = resolution.profile
    if request.node_count > profile.max_nodes:
        raise PlanningError("Requested node count exceeds the administrator maximum")
    if request.scope not in ("preflight", "rack") and request.node_count not in profile.allowed_node_counts:
        raise PlanningError("Requested node count is not enabled by the administrator")
    if request.scope in ("group", "rack") or (request.scope == "preflight" and request.group):
        group = next((g for g in profile.topology_groups if g.name == request.group), None)
        if group is None:
            raise PlanningError("Unknown configured topology group")
        if request.scope == "rack":
            reason = profile.whole_rack_reason(group)
            if reason:
                raise PlanningError(reason)
            if request.node_count != len(group.nodes):
                raise PlanningError("Whole-rack node_count must match every mapped rack member; no subset is permitted")
            candidates = tuple(group.nodes)
        else:
            candidates = tuple(n for n in group.nodes if profile.allows(n))
    else:
        candidates = request.nodes
    if not candidates or any(not profile.allows(n) for n in candidates):
        raise PlanningError("Selection contains a node outside the site allowlist/regex")
    return candidates


class SlurmInventory:
    """Use the existing transport/provider seam, with a fixed read-only operation.

    The caller supplies its control connection and active platform. No scheduler
    submit/status/cancel service is constructed. No shell fragment comes from a
    run request: literal nodes are validated, then joined with shlex.join.
    """

    def __init__(self, connection, platform):
        self.connection, self.platform = connection, platform

    def query(self, candidates):
        if not candidates or len(candidates) > 512 or any(not valid_node(n) for n in candidates):
            raise PlanningError("Invalid bounded inventory query")
        argv = ["-N", "-h", "-o", "%N|%T|%P", "-n", ",".join(candidates)]
        command = self.platform.sinfo_cmd(shlex.join(argv))
        try:
            response = self.connection.run(command, timeout=15, login_shell=False)
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
            raise PlanningError("Read-only Slurm inventory is unavailable or timed out; no retry") from exc
        if response.returncode:
            raise PlanningError("Read-only Slurm inventory query failed")
        text = response.stdout or ""
        if len(text) > 2 * 1024 * 1024:
            raise PlanningError("Inventory response exceeds the snapshot limit")
        return parse_inventory(text, candidates)


def parse_inventory(text, candidates):
    """Keep full state/flags; idle+drain and idle* must never become plain idle."""
    rows = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split("|")
        if len(fields) != 3 or not valid_node(fields[0]):
            raise PlanningError("Malformed Slurm node inventory")
        name, state, partition = fields
        if name not in candidates:
            raise PlanningError("Slurm returned an unexpected node")
        state, partition = state.strip().upper(), partition.strip().removesuffix("*")
        row = rows.setdefault(name, {"node": name, "states": [], "partitions": []})
        if state not in row["states"]:
            row["states"].append(state)
        if partition not in row["partitions"]:
            row["partitions"].append(partition)
    return {"source": "slurm", "captured_at": datetime.now(timezone.utc).isoformat(),
            "nodes": list(rows.values())}


def _compatibility_blockers(profile, request, compare):
    """Fail closed for recipes outside the reviewed CPU/RoCE execution path."""
    reasons = []
    if (profile.expected_link_layer != "Ethernet" or profile.gid_policy != "global"
            or profile.ucx_transports != ("rc", "sm", "self")):
        reasons.append("Reviewed execution requires Ethernet, global RoCE-v2 GIDs and rc,sm,self transports")
    required = 60 if request.scope == "preflight" else (
        90 + (2 if compare else 1) * (2 * profile.functional.test_limit_seconds + 30))
    if profile.default_wall_seconds < required:
        reasons.append(f"Allocation wall time must allow the bounded recipe ({required} seconds)")
    if profile.performance.enabled:
        reasons.append("Enabled performance evaluation requires the Phase 5 result service")
    return reasons


def build_plan(raw_request, resolution, inventory, *, scripts_dir, attempt_id=None):
    """Pure plan: no filesystem reads/writes, environment mutation or execution."""
    request = parse_request(raw_request)
    candidates = candidates_for(resolution, request)
    profile = resolution.profile
    if not valid_path(scripts_dir):
        raise PlanningError("Reviewed scripts directory must be an absolute safe path")
    if (not isinstance(inventory, dict) or inventory.get("source") != "slurm"
            or not isinstance(inventory.get("nodes"), list) or not inventory.get("captured_at")):
        raise PlanningError("A timestamped read-only Slurm inventory is required")
    by_name = {}
    for row in inventory["nodes"]:
        if not isinstance(row, dict) or not valid_node(row.get("node")):
            raise PlanningError("Invalid inventory node")
        if row["node"] not in candidates:
            continue  # A caller may reuse a larger read-only inventory snapshot.
        if row["node"] in by_name:
            raise PlanningError("Invalid or duplicate inventory node")
        by_name[row["node"]] = row
    status = {}
    for node in candidates:
        row = by_name.get(node, {})
        states, partitions = row.get("states", []), row.get("partitions", [])
        if (not isinstance(states, list) or not isinstance(partitions, list)
                or any(not isinstance(v, str) for v in states + partitions)):
            raise PlanningError("Invalid inventory states/partitions")
        reason = ""
        if not row:
            reason = "unknown to Slurm"
        elif profile.partition not in partitions:
            reason = "wrong partition"
        elif not states or any(state not in ("IDLE", "ALLOCATED", "MIXED") for state in states):
            reason = "unavailable state or flag"
        idle = not reason and states == ["IDLE"]
        status[node] = {"node": node, "states": states, "partitions": partitions,
                        "eligible": not reason, "currently_idle": bool(idle),
                        "eligible_but_busy": not reason and not idle, "reason": reason}
    if request.scope == "rack":
        unavailable = [n for n in candidates if not status[n]["currently_idle"]]
        if unavailable:
            raise PlanningError("Whole-rack testing requires all mapped nodes idle in the configured partition; "
                                + unavailable[0] + " is busy, unavailable or unknown; no subset was selected")
        selected = sorted(candidates, key=_natural_key)
    elif request.scope == "group" or (request.scope == "preflight" and request.group):
        eligible = sorted((n for n in candidates if status[n]["currently_idle"]), key=_natural_key)
        if len(eligible) < request.node_count:
            busy = sum(status[n]["eligible_but_busy"] for n in candidates)
            raise PlanningError(f"Insufficient idle group members: {len(eligible)} idle, {busy} eligible but busy; no nodes added")
        selected = eligible[:request.node_count]
    else:
        selected = list(request.nodes)
        bad = [n for n in selected if not status[n]["eligible"]]
        if bad:
            raise PlanningError("Ineligible node: " + bad[0] + " (" + status[bad[0]]["reason"] + ")")
    assert len(selected) == request.node_count
    compare = False if request.scope == "preflight" else (
        profile.path_comparison if request.path_comparison is None else request.path_comparison)
    if compare and not profile.path_comparison:
        raise PlanningError("Path comparison is not enabled by the administrator")
    attempt_id = uuid.uuid4().hex if attempt_id is None else attempt_id
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
        raise PlanningError("Invalid attempt identifier")
    outdir = str(PurePosixPath(profile.results_root) / (request.label + "-" + attempt_id))
    seconds = profile.default_wall_seconds
    wall = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    resources = ["--parsable", f"--partition={profile.partition}",
                 f"--nodes={request.node_count}", f"--ntasks={request.node_count}",
                 "--ntasks-per-node=1", "--cpus-per-task=1", "--mem=1G", "--gres=none",
                 "--no-requeue", f"--time={wall}", "--nodelist=" + ",".join(selected)]
    blockers = _compatibility_blockers(profile, request, compare)
    sbatch, entrypoint = None, None
    if not blockers:
        filename = "clusterkit_preflight.sbatch" if request.scope == "preflight" else "clusterkit_pairwise.sbatch"
        entrypoint = str(PurePosixPath(scripts_dir) / filename)
    pairs = [list(pair) for pair in itertools.combinations(selected, 2)] if request.scope != "preflight" else []
    topology = [{"node": node, "membership": "configured" if any(node in g.nodes for g in profile.topology_groups) else "unknown",
                 "groups": [{"name": g.name, "kind": g.kind, "authoritative": g.authoritative, "source": g.source}
                            for g in profile.topology_groups if node in g.nodes]}
                for node in selected]
    warnings = ["Inventory is a snapshot; idle nodes are not reserved and may become busy before scheduling.",
                "Functional success is separate from performance acceptance; measurements remain UNVERIFIED.",
                "A new attempt directory must be created exclusively at submission; this plan creates nothing."]
    if any(status[n]["eligible_but_busy"] for n in selected):
        warnings.append("Explicit selection includes eligible but busy nodes; scheduling may wait.")
    if any(t["membership"] == "unknown" for t in topology):
        warnings.append("Physical rack/SU membership is unknown; hostnames were not interpreted as topology.")
    plan = {"schema_version": 2, "dry_run": True, "submitted": False, "attempt_id": attempt_id,
            "scope": request.scope, "node_count": request.node_count, "nodes": selected,
            "request": asdict(request), "effective_profile": profile.as_dict(),
            "inventory": {"source": "slurm", "captured_at": inventory["captured_at"]},
            "inventory_snapshot": inventory, "scripts_directory": scripts_dir,
            "availability": [status[n] for n in selected], "topology": topology,
            "resources": {"nodes": request.node_count, "ntasks": request.node_count,
                          "ntasks_per_node": 1, "cpus_per_task": 1, "memory_per_node": "1G",
                          "gpu_resources": 0, "exclusive": False, "wall_seconds": seconds},
            "results_directory": outdir, "expected_pairs": pairs, "expected_pair_count": len(pairs),
            "coverage_mode": "preflight-only" if request.scope == "preflight" else "all-unordered-pairs",
            "performance_status": "UNVERIFIED", "functional_status": "NOT_RUN",
            "entrypoint": entrypoint, "entrypoint_supported": not blockers,
            "sbatch_resource_argv": resources, "sbatch_argv": sbatch,
            "command_display": shlex.join(sbatch) if sbatch else None,
            "execution_ready": not blockers, "submission_enabled": not blockers,
            "execution_blockers": blockers,
            "submission_requirements": [
                "A current signed preview confirmation is required",
                "Policy and Slurm inventory are revalidated before the attempt is claimed",
            ],
            "warnings": warnings}
    if entrypoint:
        plan["sbatch_argv"] = ["sbatch", *SlurmScheduler.render_argv(job_spec(plan))]
        plan["command_display"] = shlex.join(plan["sbatch_argv"])
    plan["plan_hash"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan


def _natural_key(value):
    return tuple((1, int(piece)) if piece.isdecimal() else (0, piece)
                 for piece in re.split(r"([0-9]+)", value))


def plan_request(raw_request, resolution, reader, *, scripts_dir):
    """Validate before discovery; reader is the existing transport-backed adapter."""
    request = parse_request(raw_request)
    candidates = candidates_for(resolution, request)
    if resolution.profile.rack_inventory:
        from clusterkit_inventory import require_current_inventory
        require_current_inventory(resolution)
    return build_plan(raw_request, resolution, reader.query(candidates), scripts_dir=scripts_dir)
