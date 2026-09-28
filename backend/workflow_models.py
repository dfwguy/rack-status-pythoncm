# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Validation for composable workflow step blocks.

Adds three optional, backward-compatible per-step axes plus capability typing:

* ``connection`` -- WHERE/HOW a step runs (transport profile + sudo; later exec spec).
* ``scheduler``  -- HOW a step runs across nodes (slurm/local now; run:AI/slinky/maas later).
* ``evaluation`` -- HOW a step's result is judged and what values are extracted.
* ``capability`` -- a catalog Tool id (typed params) instead of a raw command.

An absent block means "today's behavior", so existing workflows validate unchanged.
Unknown keys are tolerated (forward-compat); only known keys are type-checked.
"""
from __future__ import annotations

from typing import Any

from catalog.models import SCHEDULERS as SCHEDULER_KINDS

CONNECTION_PROFILES = ("control", "script")
CONNECTION_EXEC_MODES = ("local", "ssh")
EVAL_EXTRACT_TYPES = ("str", "int", "float")


def _err(prefix: str, problems: list[str]) -> list[str]:
    return [f"{prefix}: {p}" for p in problems]


def validate_connection(conn: Any) -> list[str]:
    if conn is None:
        return []
    if not isinstance(conn, dict):
        return ["connection must be a mapping"]
    problems: list[str] = []
    prof = conn.get("profile")
    if prof is not None and prof not in CONNECTION_PROFILES:
        problems.append(f"connection.profile {prof!r} invalid (expected one of {CONNECTION_PROFILES})")
    exec_mode = conn.get("exec")
    if exec_mode is not None and exec_mode not in CONNECTION_EXEC_MODES:
        problems.append(f"connection.exec {exec_mode!r} invalid (expected one of {CONNECTION_EXEC_MODES})")
    if "sudo" in conn and not isinstance(conn["sudo"], bool):
        problems.append("connection.sudo must be a boolean")
    return problems


def validate_scheduler(sched: Any) -> list[str]:
    if sched is None:
        return []
    if not isinstance(sched, dict):
        return ["scheduler must be a mapping"]
    problems: list[str] = []
    kind = sched.get("kind")
    if kind is not None and kind not in SCHEDULER_KINDS:
        problems.append(f"scheduler.kind {kind!r} invalid (expected one of {SCHEDULER_KINDS})")
    if not any(sched.get(k) for k in ("script", "command", "image")):
        problems.append("scheduler needs one of script/command/image to run")
    for numeric in ("ntasks_per_node",):
        v = sched.get(numeric)
        if v is not None and not (isinstance(v, int) or (isinstance(v, str) and v.strip())):
            problems.append(f"scheduler.{numeric} must be an int or a ${{param}} string")
    return problems


def validate_evaluation(ev: Any) -> list[str]:
    if ev is None:
        return []
    if not isinstance(ev, dict):
        return ["evaluation must be a mapping"]
    problems: list[str] = []
    sw = ev.get("success_when")
    if sw is not None and not isinstance(sw, dict):
        problems.append("evaluation.success_when must be a mapping")
    ex = ev.get("extract")
    if ex is not None:
        if not isinstance(ex, list):
            problems.append("evaluation.extract must be a list")
        else:
            for i, e in enumerate(ex):
                if not isinstance(e, dict) or not e.get("name") or not e.get("regex"):
                    problems.append(f"evaluation.extract[{i}] needs both 'name' and 'regex'")
                elif e.get("type") and e["type"] not in EVAL_EXTRACT_TYPES:
                    problems.append(f"evaluation.extract[{i}].type {e['type']!r} invalid (expected {EVAL_EXTRACT_TYPES})")
    asrt = ev.get("assert")
    if asrt is not None and (not isinstance(asrt, list) or not all(isinstance(a, str) for a in asrt)):
        problems.append("evaluation.assert must be a list of expression strings")
    for key in ("message", "message_pass", "message_fail", "prompt", "comment"):
        if key in ev and not isinstance(ev[key], str):
            problems.append(f"evaluation.{key} must be a string")
    return problems


HOP_KINDS = ("local", "ssh", "vpn")


def validate_hop(hop: Any, where: str) -> list[str]:
    if not isinstance(hop, dict):
        return [f"{where} must be a mapping"]
    problems: list[str] = []
    kind = hop.get("kind")
    if kind is not None and kind not in HOP_KINDS:
        problems.append(f"{where}.kind {kind!r} invalid (expected one of {HOP_KINDS})")
    if "sudo" in hop and not isinstance(hop["sudo"], bool):
        problems.append(f"{where}.sudo must be a boolean")
    if kind == "ssh" and not (hop.get("host") or where.endswith("node")):
        # a shared ssh hop needs a host; the per-node hop gets its host from `nodes`
        problems.append(f"{where} (ssh) needs a host")
    return problems


def validate_fanout(fo: Any) -> list[str]:
    if fo is None:
        return []
    if not isinstance(fo, dict):
        return ["fanout must be a mapping"]
    problems: list[str] = []
    # nodes may also come from the run (${nodelist}/rack); only flag if clearly empty
    if not (fo.get("nodes") or fo.get("rack")) and "nodes" in fo and not fo["nodes"]:
        problems.append("fanout.nodes is empty")
    hops = fo.get("hops")
    if hops is not None:
        if not isinstance(hops, list):
            problems.append("fanout.hops must be a list of hop mappings")
        else:
            for i, h in enumerate(hops):
                problems += validate_hop(h, f"fanout.hops[{i}]")
    if fo.get("node") is not None:
        problems += validate_hop(fo.get("node"), "fanout.node")
    mw = fo.get("max_workers")
    if mw is not None and not (isinstance(mw, int) or (isinstance(mw, str) and mw.strip())):
        problems.append("fanout.max_workers must be an int")
    return problems


def validate_resolve(r: Any) -> list[str]:
    if r is None:
        return []
    if not isinstance(r, dict):
        return ["resolve must be a mapping"]
    problems: list[str] = []
    if "into" in r and not (isinstance(r["into"], str) and r["into"].strip()):
        problems.append("resolve.into must be a non-empty string (output name)")
    return problems


def validate_step(step: dict) -> list[str]:
    """Return human-readable problems for a step's composition blocks (empty == valid)."""
    if not isinstance(step, dict):
        return ["step must be a mapping"]
    problems: list[str] = []
    problems += validate_connection(step.get("connection"))
    problems += validate_scheduler(step.get("scheduler"))
    problems += validate_evaluation(step.get("evaluation"))
    problems += validate_fanout(step.get("fanout"))
    problems += validate_resolve(step.get("resolve"))
    cap = step.get("capability")
    if cap is not None and not (isinstance(cap, str) and cap.strip()):
        problems.append("capability must be a non-empty string (a catalog tool id)")
    return _err(f"step '{step.get('id', '?')}'", problems)


def validate_workflow(wf: dict) -> list[str]:
    """Validate the composition blocks of every step in a workflow (empty == valid)."""
    problems: list[str] = []
    steps = wf.get("steps") if isinstance(wf, dict) else None
    if isinstance(steps, list):
        for step in steps:
            problems += validate_step(step)
    return problems
