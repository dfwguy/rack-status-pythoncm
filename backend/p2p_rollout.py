# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""P2P pull rollouts -- one image onto racks, driven by documents and metrics, as a Rack Status run.

docs/DESIGN_p2p-pull-agent.md, phase P2. A rollout is a workflow-engine run (citizen 4) of the
built-in ``nvis-p2p-rollout`` workflow, whose single native step is the coordinator below. It never
opens an SSH session: it writes each node's desired-state document into the directory the origin
serves to the node agents (``P2pSettings.desired_dir``), and reads progress back from the agents'
``nvis_p2p_*`` series in VictoriaMetrics.

The run record carries everything:

* ``params["plan"]`` -- fixed when the run starts: the image pin, the racks and their members in tray
  order (the server's placement, never a hostname guess), the origin, the paths.
* ``params["pacing"]`` -- steerable while it runs: the rung ladder, the per-node budget, how many
  racks are driven at once. A change applies from the next tick.
* ``params["commands"]`` -- operator commands (retry nodes), applied once each, in order.
* ``run["rollout"]`` -- the coordinator's own state, saved every tick, so a restart resumes from the
  last tick (the step is registered ``resumable``).

Every write is idempotent -- a document is rewritten whole, never patched -- which is what makes a
resumed tick safe to repeat.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import metrics_query
import workflow_engine
from json_db import atomic_write_json

WORKFLOW_ID = "nvis-p2p-rollout"
NATIVE_STEP = "p2p_rollout"
SCHEMA_VERSION = 1
TICK_SECONDS = 15.0
#: An agent counts as alive if it polled this recently (its poll is 30 s, metrics lag ~30 s).
ALIVE_WINDOW_SECONDS = 180.0
#: Consecutive failed attempts an agent reports before the node is deferred.
MAX_FAILURES = 3
DEFAULT_STAGES = ("1", "50%", "100%")

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_NODE_STATES = ("pending", "released", "downloading", "ready", "deferred")


class RolloutError(ValueError):
    """A rollout request that cannot be honoured; the API maps it to a 400."""


# -- pure helpers ---------------------------------------------------------------------------------


def natural_key(name: str) -> list[tuple[int, int | str]]:
    """Sort key that orders digit runs numerically (``Q2`` before ``Q10``)."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", str(name).casefold()) if p]


def rung_cuts(stages: Iterable[str], total: int) -> list[int]:
    """Cumulative node counts per rung: ``['1','50%','100%']`` over 18 -> ``[1, 9, 18]``."""
    cuts: list[int] = []
    for rung in stages:
        rung = str(rung).strip()
        if not rung:
            continue
        n = math.ceil(total * float(rung[:-1]) / 100) if rung.endswith("%") else int(rung)
        n = max(1, min(total, n))
        if not cuts or n > cuts[-1]:
            cuts.append(n)
    if not cuts or cuts[-1] != total:
        cuts.append(total)
    return cuts


def merge_document(existing: dict | None, node: str, origin: str, image: dict | None,
                   remove: str = "") -> dict:
    """A node's next document: keep what it already holds, add or remove one image, bump generation.

    The generation bump is what an agent reads as "start over on this one" -- it clears its failure
    count and backoff -- so a retry is simply a rewrite.
    """
    doc = existing if isinstance(existing, dict) and existing.get("schema_version") == SCHEMA_VERSION else {}
    images = [i for i in doc.get("images", []) if isinstance(i, dict)]
    if remove:
        images = [i for i in images if i.get("filename") != remove]
    if image is not None:
        images = [i for i in images if i.get("filename") != image["filename"]] + [dict(image)]
    return {"schema_version": SCHEMA_VERSION, "generation": int(doc.get("generation", 0)) + 1,
            "node": node, "origin": origin, "images": images}


def instance_selector(nodes: Iterable[str]) -> str:
    """A PromQL regex alternation for *nodes*. Hostnames are validated, then only ``.`` is escaped:
    ``re.escape`` would also emit ``\\-``, which is not a legal escape in a PromQL string (HTTP 422)."""
    nodes = list(nodes)
    bad = [n for n in nodes if not _HOST_RE.match(n)]
    if bad:
        raise ValueError(f"not a hostname: {bad[0]!r}")
    return "|".join(n.replace(".", "\\\\.") for n in nodes)


def normalize_pacing(raw: dict | None, defaults: dict) -> dict:
    """Validate pacing (stages, budget, racks in flight) over *defaults*. Raises RolloutError."""
    raw = raw or {}
    out = dict(defaults)
    if raw.get("stages"):
        stages = [str(s).replace(" ", "") for s in raw["stages"]] if isinstance(raw["stages"], list) else []
        problems = _validate_stages(stages)
        if problems:
            raise RolloutError("; ".join(problems))
        out["stages"] = stages
    for key, low, high in (("budget_seconds", 60, 86_400), ("max_active_racks", 1, 64)):
        if key in raw and raw[key] not in (None, ""):
            try:
                value = int(raw[key])
            except (TypeError, ValueError):
                raise RolloutError(f"{key} must be an integer") from None
            if not low <= value <= high:
                raise RolloutError(f"{key} must be between {low} and {high}, got {value}")
            out[key] = value
    return out


def _validate_stages(stages: list[str]) -> list[str]:
    from catalog.models import validate_rollout_stages  # the cluster settings' own rule
    return validate_rollout_stages(stages, where="stages")


# -- the coordinator --------------------------------------------------------------------------------


@dataclass
class Observation:
    """What the metrics say about one node right now."""

    alive: bool = False
    generation: int = -1
    state: str = ""
    failures: int = 0


Observer = Callable[[list[str], str], dict[str, Observation]]


def vm_observer(base_url: str, now: Callable[[], float] = time.time) -> Observer:
    """Read the agents' series for *nodes* from VictoriaMetrics through ``metrics_query``."""
    def observe(nodes: list[str], filename: str) -> dict[str, Observation]:
        out = {n: Observation() for n in nodes}
        if not nodes:
            return out
        sel = instance_selector(nodes)
        t = now()
        for r in metrics_query.instant(f'nvis_p2p_agent_last_poll_timestamp_seconds{{instance=~"{sel}"}}', base_url):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.alive = t - float(r["value"][1]) <= ALIVE_WINDOW_SECONDS
        for r in metrics_query.instant(f'nvis_p2p_agent_up{{instance=~"{sel}"}}', base_url):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.generation = int(r["metric"].get("generation", "-1"))
        for r in metrics_query.instant(
                f'nvis_p2p_image_state{{instance=~"{sel}",filename="{filename}"}} == 1', base_url):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.state = r["metric"].get("state", "")
        for r in metrics_query.instant(
                f'nvis_p2p_image_failures_total{{instance=~"{sel}",filename="{filename}"}}', base_url):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.failures = int(float(r["value"][1]))
        return out

    return observe


class DocumentStore:
    """The desired-state documents, one file per node in the directory the origin serves."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def read(self, node: str) -> dict | None:
        try:
            return json.loads((self.dir / f"{node}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def write(self, node: str, doc: dict) -> None:
        # World-readable: the origin serves the file to the agent. Atomic, so an agent never reads
        # half a document; a unique temp name, so two writers cannot collide on one.
        atomic_write_json(self.dir / f"{node}.json", doc, mode=0o644)


def new_state(plan: dict, now: float) -> dict:
    """The coordinator's initial state for *plan*."""
    racks = {rack: {"rung": 0, "state": "waiting"} for rack in plan["racks"]}
    nodes = {}
    for rack, members in plan["racks"].items():
        for node in members:
            nodes[node] = {"rack": rack, "rung": -1, "generation": 0, "released_at": 0.0, "state": "pending",
                           "ready_at": 0.0, "reason": "", "attempts": 0}
    return {"racks": racks, "nodes": nodes, "commands_done": 0, "started": now, "finished": 0.0,
            "last_tick": 0.0, "last_error": "", "log": []}


class Coordinator:
    """One tick of a rollout over the run's state. Pure apart from the store, observer and clock."""

    def __init__(self, store: DocumentStore, observe: Observer, now: Callable[[], float] = time.time):
        self.store, self.observe, self.now = store, observe, now

    def _log(self, state: dict, msg: str) -> None:
        state["log"] = (state.get("log", []) + [f"{time.strftime('%H:%M:%S', time.gmtime(self.now()))} {msg}"])[-200:]

    def _release(self, plan: dict, state: dict, node: str) -> None:
        doc = merge_document(self.store.read(node), node, plan["origin"], plan["image"])
        self.store.write(node, doc)
        nr = state["nodes"][node]
        rack = state["racks"][nr["rack"]]
        nr.update(rung=rack["rung"], generation=doc["generation"], released_at=self.now(), state="released",
                  reason="", attempts=nr.get("attempts", 0) + 1)
        self._log(state, f"release {node} rack={nr['rack']} rung={rack['rung']} generation={doc['generation']}")

    def apply_commands(self, plan: dict, state: dict, commands: list[dict]) -> None:
        """Run operator commands not yet applied (each once, in order)."""
        for cmd in commands:
            seq = int(cmd.get("seq", 0))
            if seq <= state.get("commands_done", 0):
                continue
            if cmd.get("kind") == "retry":
                wanted = cmd.get("nodes") or []
                targets = [n for n, nr in state["nodes"].items()
                           if (n in wanted or (not wanted and nr["state"] == "deferred"))
                           and nr["state"] in ("deferred", "released", "downloading")]
                for node in targets:
                    # Back to pending, not straight to a document: the next tick releases it through
                    # the same liveness check as a first release, so a node that still has no agent
                    # is deferred again at once instead of waiting out a whole node budget.
                    nr = state["nodes"][node]
                    nr.update(state="pending", reason="")
                    rack = state["racks"][nr["rack"]]
                    if rack["state"] == "done":
                        rack["state"] = "waiting"
                self._log(state, f"retry seq={seq} nodes={len(targets)}")
            state["commands_done"] = seq

    def tick(self, plan: dict, pacing: dict, state: dict) -> bool:
        """Advance the rollout one step. Returns True when every rack is done."""
        now = self.now()
        racks = state["racks"]
        # Bring racks into flight up to the pacing limit, in plan order.
        active = [r for r, rs in racks.items() if rs["state"] == "active"]
        for rack, rs in racks.items():
            if len(active) >= pacing["max_active_racks"]:
                break
            if rs["state"] == "waiting":
                rs["state"] = "active"
                active.append(rack)
                self._log(state, f"rack {rack} active")
        watched = [n for n, nr in state["nodes"].items() if racks[nr["rack"]]["state"] == "active"]
        try:
            obs = self.observe(watched, plan["image"]["filename"])
        except Exception as exc:  # noqa: BLE001 -- no observation, no decision
            # Never decide on a failed read: an empty observation would make every node look agentless
            # and defer whole racks. Skip the tick; budgets keep running on the clock.
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            self._log(state, f"observe failed, tick skipped: {state['last_error']}")
            return False
        state["last_error"] = ""
        for node in watched:
            nr = state["nodes"][node]
            if nr["state"] in ("pending", "ready", "deferred"):
                continue
            o = obs.get(node, Observation())
            # A state counts only once the agent has acted on the generation we wrote; until then the
            # series (or its absence) can be left over from before the release.
            current = o.alive and o.generation >= nr["generation"]
            if current and o.state == "ready":
                nr.update(state="ready", ready_at=now)
                self._log(state, f"ready {node} after {now - nr['released_at']:.0f}s")
            elif current and o.failures >= MAX_FAILURES:
                nr.update(state="deferred", reason=f"failed_{o.failures}x")
                self._log(state, f"deferred {node} {nr['reason']}")
            elif now - nr["released_at"] > pacing["budget_seconds"]:
                nr.update(state="deferred", reason="timeout" if o.alive else "agent_down")
                self._log(state, f"deferred {node} {nr['reason']}")
            elif current and o.state == "downloading":
                nr["state"] = "downloading"
        for rack in active:
            rs = racks[rack]
            members = plan["racks"][rack]
            cuts = rung_cuts(pacing["stages"], len(members))
            rs["rung"] = min(rs["rung"], len(cuts) - 1)
            while rs["state"] == "active":
                released = [n for n in members if state["nodes"][n]["state"] != "pending"]
                target = cuts[rs["rung"]]
                if len(released) < target:
                    for node in [n for n in members if state["nodes"][n]["state"] == "pending"][: target - len(released)]:
                        if not obs.get(node, Observation()).alive:
                            state["nodes"][node].update(state="deferred", reason="no_agent")
                            self._log(state, f"deferred {node} no_agent")
                        else:
                            self._release(plan, state, node)
                    continue  # recount: a no_agent deferral does not fill the rung with a download
                if not all(state["nodes"][n]["state"] in ("ready", "deferred") for n in released):
                    break
                if rs["rung"] + 1 < len(cuts):
                    rs["rung"] += 1
                    self._log(state, f"rack {rack} rung {rs['rung']} target={cuts[rs['rung']]}")
                else:
                    rs["state"] = "done"
                    self._log(state, f"rack {rack} done")
        state["last_tick"] = now
        return all(rs["state"] == "done" for rs in racks.values())

    def cancel(self, plan: dict, state: dict) -> int:
        """Withdraw the image from every node still working on it; installed nodes keep it."""
        withdrawn = 0
        for node, nr in state["nodes"].items():
            if nr["state"] in ("released", "downloading"):
                doc = self.store.read(node)
                if doc and any(i.get("filename") == plan["image"]["filename"] for i in doc.get("images", [])):
                    self.store.write(node, merge_document(doc, node, plan["origin"], None,
                                                          remove=plan["image"]["filename"]))
                    withdrawn += 1
                nr.update(state="deferred", reason="cancelled")
        self._log(state, f"cancelled; withdrawn from {withdrawn} node(s)")
        return withdrawn


def summarize(state: dict | None) -> dict:
    """Counts per node state, for a list row or a progress bar."""
    counts = {s: 0 for s in _NODE_STATES}
    for nr in (state or {}).get("nodes", {}).values():
        counts[nr.get("state", "pending")] = counts.get(nr.get("state", "pending"), 0) + 1
    total = sum(counts.values())
    return {"total": total, **counts}


# -- the native step ------------------------------------------------------------------------------


def _sleep_unless_cancelled(run: dict, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and run.get("state") != "cancelled":
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def run_rollout_step(run: dict, step: dict, *, coordinator: Coordinator | None = None,
                     tick_seconds: float | None = None) -> tuple[bool, str]:
    """The native step: tick until every rack is done, the operator cancels, or the plan is invalid."""
    params = run.get("params", {})
    plan = params.get("plan") or {}
    if not plan.get("racks") or not plan.get("image") or not plan.get("desired_dir"):
        return False, "rollout plan is incomplete (racks, image and desired_dir are required)"
    coord = coordinator or Coordinator(DocumentStore(plan["desired_dir"]), vm_observer(plan.get("metrics_url") or ""))
    state = run.get("rollout")
    if not state:
        state = run["rollout"] = new_state(plan, coord.now())
        workflow_engine.persist_run(run)
    interval = TICK_SECONDS if tick_seconds is None else tick_seconds
    while True:
        if run.get("state") == "cancelled":
            coord.cancel(plan, state)
            state["finished"] = coord.now()
            workflow_engine.persist_run(run)
            return False, _report(plan, state, "cancelled")
        coord.apply_commands(plan, state, list(params.get("commands") or []))
        done = coord.tick(plan, params.get("pacing") or {}, state)
        run["progress"] = summarize(state)
        workflow_engine.persist_run(run)
        if done:
            state["finished"] = coord.now()
            workflow_engine.persist_run(run)
            # A rollout that reached every node it could is complete even when some were deferred:
            # 16 of 18 nodes installed is not a failed rollout, and the deferrals are in its counts
            # and report, each with its reason, for a retry.
            return True, _report(plan, state, "done")
        _sleep_unless_cancelled(run, interval)


def _report(plan: dict, state: dict, how: str) -> str:
    c = summarize(state)
    head = (f"rollout {how}: image={plan['image']['filename']} ready={c['ready']}/{c['total']} "
            f"deferred={c['deferred']} elapsed={state.get('finished', 0) - state.get('started', 0):.0f}s")
    lines = [head]
    lines += [f"  {n}: {nr['reason']}" for n, nr in sorted(state["nodes"].items()) if nr["state"] == "deferred"]
    lines += ["", "log:"] + state.get("log", [])
    return "\n".join(lines)


# -- starting one ---------------------------------------------------------------------------------


def plan_rollout(*, cluster_id: str, settings: Any, catalog_images: list[dict], inventory: list[dict] | None,
                 filename: str, racks: list[str]) -> dict:
    """Resolve a rollout request into a fixed plan, or raise RolloutError saying what is missing.

    Rack membership is the server's placement (the cluster snapshot's ``nodes[]``, already placed by
    the rack-mapping mode and naming templates), in tray order -- never a parse of hostnames.
    """
    blockers = settings.rollout_blockers()
    if blockers:
        raise RolloutError("pull rollouts are not configured: " + "; ".join(blockers))
    pin = next((i for i in catalog_images if i.get("filename") == filename), None)
    if pin is None:
        raise RolloutError(f"{filename!r} is not in the cluster's catalog")
    if not re.fullmatch(r"[0-9a-f]{64}", str(pin.get("sha256") or "")):
        raise RolloutError(f"{filename!r} has no sha256 pin in the catalog")
    if not isinstance(inventory, list):
        raise RolloutError("the cluster's node inventory is not available yet; try again shortly")
    wanted = [r.strip() for r in racks if r and r.strip()]
    if not wanted:
        raise RolloutError("choose at least one rack")
    by_rack: dict[str, list[dict]] = {}
    for n in inventory:
        if n.get("rack"):
            by_rack.setdefault(str(n["rack"]), []).append(n)
    members: dict[str, list[str]] = {}
    for rack in wanted:
        nodes = by_rack.get(rack)
        if not nodes:
            raise RolloutError(f"rack {rack!r} has no placed nodes in the inventory")
        nodes = sorted(nodes, key=lambda n: (n.get("tray") or 10**6, str(n["id"])))
        members[rack] = [str(n["id"]) for n in nodes]
    return {
        "cluster_id": cluster_id,
        "image": {"filename": filename, "sha256": pin["sha256"], "bytes": int(pin["bytes"]),
                  "tag": f"catalog-v1-{pin.get('order', 0)}", "application": "NVIS-P2P-Catalog"},
        "racks": members,
        "origin": settings.resolved_origin_url(),
        "desired_dir": settings.desired_dir,
        "metrics_url": settings.metrics_url,
    }


def default_pacing(settings: Any) -> dict:
    return {"stages": list(settings.rollout_stages or DEFAULT_STAGES),
            "budget_seconds": settings.rollout_node_budget_seconds,
            "max_active_racks": settings.rollout_max_active_racks}


def start(plan: dict, pacing: dict, operator: str) -> dict:
    """Start (or join) the rollout run for *plan*. The key makes a double start join the live run."""
    key = f"{plan['cluster_id']}|{plan['image']['filename']}|{','.join(sorted(plan['racks']))}"
    params = {"cluster_id": plan["cluster_id"], "plan": plan, "pacing": pacing, "commands": [],
              "operator": operator}
    return workflow_engine.start_workflow_run(WORKFLOW_ID, params, idempotency_key=key)


def request_retry(run_id: str, nodes: list[str]) -> dict:
    """Queue a retry of *nodes* (empty = every deferred node) on a live rollout."""
    run = workflow_engine.get_run_state(run_id)
    if not run or run.get("workflow_id") != WORKFLOW_ID:
        raise RolloutError(f"rollout {run_id!r} not found")
    bad = [n for n in nodes if not _HOST_RE.match(str(n))]
    if bad:
        raise RolloutError(f"not a hostname: {bad[0]!r}")
    commands = list(run.get("params", {}).get("commands") or [])
    seq = max([int(c.get("seq", 0)) for c in commands] + [0]) + 1
    commands.append({"seq": seq, "kind": "retry", "nodes": [str(n) for n in nodes], "at": time.time()})
    try:
        return workflow_engine.update_run_params(run_id, {"commands": commands})
    except ValueError as exc:
        raise RolloutError(str(exc)) from None


def set_pacing(run_id: str, pacing: dict) -> dict:
    run = workflow_engine.get_run_state(run_id)
    if not run or run.get("workflow_id") != WORKFLOW_ID:
        raise RolloutError(f"rollout {run_id!r} not found")
    merged = normalize_pacing(pacing, run.get("params", {}).get("pacing") or {})
    try:
        return workflow_engine.update_run_params(run_id, {"pacing": merged})
    except ValueError as exc:
        raise RolloutError(str(exc)) from None


def describe(run: dict, *, detail: bool = False) -> dict:
    """The API's view of a rollout run."""
    params = run.get("params", {})
    plan = params.get("plan") or {}
    state = run.get("rollout") or {}
    out = {
        "run_id": run["run_id"], "state": run.get("state"), "started_at": run.get("started_at"),
        "updated_at": run.get("updated_at"), "operator": params.get("operator", ""),
        "cluster_id": params.get("cluster_id", ""), "image": (plan.get("image") or {}).get("filename", ""),
        "racks": list((plan.get("racks") or {}).keys()), "pacing": params.get("pacing") or {},
        "counts": summarize(state), "last_error": state.get("last_error", ""),
    }
    if detail:
        nodes = state.get("nodes", {})
        out["rack_detail"] = [
            {"rack": rack, "state": (state.get("racks", {}).get(rack) or {}).get("state", "waiting"),
             "rung": (state.get("racks", {}).get(rack) or {}).get("rung", 0),
             "cuts": rung_cuts((params.get("pacing") or {}).get("stages") or DEFAULT_STAGES, len(members)),
             "nodes": [{"node": n, **{k: nodes.get(n, {}).get(k) for k in
                                      ("state", "reason", "rung", "released_at", "ready_at", "attempts")}}
                       for n in members]}
            for rack, members in (plan.get("racks") or {}).items()
        ]
        out["log"] = state.get("log", [])[-100:]
        out["image_pin"] = plan.get("image") or {}
    return out


def _register() -> None:
    workflow_engine.register_native_step(NATIVE_STEP, run_rollout_step, resumable=True)
    workflow_engine.register_builtin_workflow({
        "id": WORKFLOW_ID,
        "name": "NVIS-P2P pull rollout",
        "description": "Roll one catalog image onto racks through desired-state documents, rung by rung.",
        "version": 1,
        "steps": [{"id": "rollout", "name": "Roll out", "type": "automated", "native": NATIVE_STEP}],
    })


_register()
