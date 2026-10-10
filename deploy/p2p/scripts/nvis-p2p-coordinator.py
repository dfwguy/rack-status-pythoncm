#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""nvis-p2p-coordinator -- roll one image onto one rack through desired-state documents.

The P1 coordinator of docs/DESIGN_p2p-pull-agent.md. It never opens an SSH session: it writes each
node's ``<desired-dir>/<node>.json`` (served to the agents by the origin nginx) and reads progress
back from VictoriaMetrics, where the agents' ``nvis_p2p_*`` series arrive through node_exporter and
Alloy. The rack is released rung by rung (``1, 50%, 100%`` by default) so the first node seeds the
rack and the rest pull from rack-mates.

  plan    print the rung cuts and which nodes have a live agent
  run     release rungs until every node is ready or deferred; the run record lands in --state-dir
  cancel  remove the image from the rack's documents (agents stop and discard in-flight downloads)
  status  one observation of the rack, no writes

Standard library only, so it runs on the appserver's system Python.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = 1


def natural_key(name: str) -> list[tuple[int, int | str]]:
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", name.casefold()) if p]


def rack_nodes(topology: Path, rack: str) -> list[str]:
    """Members of *rack* from the cluster manager's placement (``host<TAB>su<TAB>rack``)."""
    nodes = []
    for line in topology.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == rack:
            nodes.append(parts[0].strip())
    return sorted(set(nodes), key=natural_key)


def catalog_pin(catalog: Path, filename: str) -> dict:
    """The authored pin for *filename* (``order name filename bytes sha256``)."""
    for line in catalog.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 5 and parts[2] == filename:
            return {"filename": filename, "sha256": parts[4], "bytes": int(parts[3]),
                    "tag": f"catalog-v1-{parts[0]}", "application": "NVIS-P2P-Catalog"}
    raise SystemExit(f"COORD_IMAGE_NOT_IN_CATALOG {filename} ({catalog})")


def rung_cuts(stages: list[str], total: int) -> list[int]:
    """Cumulative node counts per rung: ``['1','50%','100%']`` over 18 -> ``[1, 9, 18]``."""
    cuts: list[int] = []
    for rung in stages:
        rung = rung.strip()
        n = math.ceil(total * float(rung[:-1]) / 100) if rung.endswith("%") else int(rung)
        n = max(1, min(total, n))
        if not cuts or n > cuts[-1]:
            cuts.append(n)
    if not cuts or cuts[-1] != total:
        cuts.append(total)
    return cuts


def merge_document(existing: dict | None, node: str, origin: str, image: dict | None,
                   remove: str = "") -> dict:
    """A node's next document: keep what it already holds, add or remove one image, bump generation."""
    doc = existing if isinstance(existing, dict) and existing.get("schema_version") == SCHEMA_VERSION else {}
    images = [i for i in doc.get("images", []) if isinstance(i, dict)]
    if remove:
        images = [i for i in images if i.get("filename") != remove]
    if image is not None:
        images = [i for i in images if i.get("filename") != image["filename"]] + [image]
    return {"schema_version": SCHEMA_VERSION, "generation": int(doc.get("generation", 0)) + 1,
            "node": node, "origin": origin, "images": images}


@dataclass
class NodeRun:
    rung: int = -1           # rung that released it; -1 = not released
    generation: int = 0      # generation written when released
    released_at: float = 0.0
    state: str = "pending"   # pending | released | downloading | ready | deferred
    ready_at: float = 0.0
    reason: str = ""


@dataclass
class Run:
    run_id: str
    rack: str
    image: dict
    cuts: list[int]
    nodes: dict[str, NodeRun]
    rung: int = 0
    started: float = 0.0
    finished: float = 0.0


@dataclass
class Observation:
    """What VictoriaMetrics says about one node right now."""

    alive: bool = False
    generation: int = -1
    state: str = ""          # the image's state series, "" when the agent does not report it
    failures: int = 0


class Coordinator:
    def __init__(self, desired_dir: Path, origin: str, observe, budget: float, max_failures: int,
                 alive_window: float, now=time.time, log=print):
        self.desired_dir, self.origin, self.observe = desired_dir, origin, observe
        self.budget, self.max_failures, self.alive_window = budget, max_failures, alive_window
        self.now, self.log = now, log

    def _doc_path(self, node: str) -> Path:
        return self.desired_dir / f"{node}.json"

    def _read(self, node: str) -> dict | None:
        try:
            return json.loads(self._doc_path(node).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def _write(self, node: str, doc: dict) -> None:
        path = self._doc_path(node)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def release(self, run: Run, node: str) -> None:
        doc = merge_document(self._read(node), node, self.origin, run.image)
        self._write(node, doc)
        nr = run.nodes[node]
        nr.rung, nr.generation, nr.released_at, nr.state = run.rung, doc["generation"], self.now(), "released"
        self.log(f"COORD_RELEASE node={node} rung={run.rung} generation={doc['generation']}")

    def tick(self, run: Run) -> bool:
        """One control step. Returns True when the run is finished."""
        now = self.now()
        try:
            obs = self.observe(list(run.nodes), run.image["filename"])
        except Exception as exc:  # noqa: BLE001 -- no observation, no decision
            # Never act on a failed read: an empty observation would make every node look agentless
            # and defer the whole rack. Skip the tick; budgets keep running on the clock.
            self.log(f"COORD_OBSERVE_ERROR {type(exc).__name__}: {exc}")
            return False
        for node, nr in run.nodes.items():
            if nr.state in ("pending", "ready", "deferred"):
                continue
            o = obs.get(node, Observation())
            # A state only counts once the agent has acted on the generation we wrote; until then the
            # series (or its absence) can be left over from before the release.
            current = o.alive and o.generation >= nr.generation
            if current and o.state == "ready":
                nr.state, nr.ready_at = "ready", now
                self.log(f"COORD_READY node={node} after={now - nr.released_at:.0f}s")
            elif current and o.failures >= self.max_failures:
                nr.state, nr.reason = "deferred", f"failed_{o.failures}x"
                self.log(f"COORD_DEFERRED node={node} reason={nr.reason}")
            elif now - nr.released_at > self.budget:
                nr.state, nr.reason = "deferred", "timeout" if o.alive else "agent_down"
                self.log(f"COORD_DEFERRED node={node} reason={nr.reason}")
            elif current and o.state == "downloading":
                nr.state = "downloading"
        while run.rung < len(run.cuts):
            target = run.cuts[run.rung]
            released = [n for n, nr in run.nodes.items() if nr.state != "pending"]
            if len(released) < target:
                for node in [n for n, nr in run.nodes.items() if nr.state == "pending"][: target - len(released)]:
                    if not obs.get(node, Observation()).alive:
                        run.nodes[node].state, run.nodes[node].reason = "deferred", "no_agent"
                        self.log(f"COORD_DEFERRED node={node} reason=no_agent")
                    else:
                        self.release(run, node)
                continue  # recount: a no_agent deferral does not fill the rung with a download
            settled = all(run.nodes[n].state in ("ready", "deferred") for n in released)
            if not settled:
                return False
            run.rung += 1
            if run.rung < len(run.cuts):
                self.log(f"COORD_RUNG rung={run.rung} target={run.cuts[run.rung]}")
        if not run.finished:
            run.finished = now
            ready = sum(1 for nr in run.nodes.values() if nr.state == "ready")
            self.log(f"COORD_DONE ready={ready} deferred={len(run.nodes) - ready} "
                     f"elapsed={now - run.started:.0f}s")
        return True

    def cancel(self, nodes: list[str], filename: str) -> int:
        changed = 0
        for node in nodes:
            doc = self._read(node)
            if doc and any(i.get("filename") == filename for i in doc.get("images", [])):
                self._write(node, merge_document(doc, node, self.origin, None, remove=filename))
                changed += 1
                self.log(f"COORD_CANCEL node={node}")
        return changed


_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


def instance_selector(nodes: list[str]) -> str:
    """A PromQL regex alternation for *nodes*. Hostnames are validated, then only ``.`` is escaped:
    ``re.escape`` would also emit ``\\-``, which is not a legal escape in a PromQL string (HTTP 422)."""
    bad = [n for n in nodes if not _HOST_RE.match(n)]
    if bad:
        raise ValueError(f"not a hostname: {bad[0]!r}")
    return "|".join(n.replace(".", "\\\\.") for n in nodes)


def vm_observer(vm_url: str, alive_window: float, now=time.time):
    """Read the agents' series for *nodes* from VictoriaMetrics (Prometheus query API)."""
    def query(expr: str) -> list[dict]:
        url = f"{vm_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.load(resp)["data"]["result"]

    def observe(nodes: list[str], filename: str) -> dict[str, Observation]:
        sel = instance_selector(nodes)
        out = {n: Observation() for n in nodes}
        t = now()
        for r in query(f'nvis_p2p_agent_last_poll_timestamp_seconds{{instance=~"{sel}"}}'):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.alive = t - float(r["value"][1]) <= alive_window
        for r in query(f'nvis_p2p_agent_up{{instance=~"{sel}"}}'):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.generation = int(r["metric"].get("generation", "-1"))
        for r in query(f'nvis_p2p_image_state{{instance=~"{sel}",filename="{filename}"}} == 1'):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.state = r["metric"].get("state", "")
        for r in query(f'nvis_p2p_image_failures_total{{instance=~"{sel}",filename="{filename}"}}'):
            o = out.get(r["metric"].get("instance"))
            if o is not None:
                o.failures = int(float(r["value"][1]))
        return out

    return observe


def save(run: Run, state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{run.run_id}.json"
    doc = asdict(run)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("plan", "run", "cancel", "status"))
    ap.add_argument("--rack", required=True)
    ap.add_argument("--image", required=True, help="catalog filename")
    ap.add_argument("--topology", type=Path, default=Path("/opt/nvis-p2p/current/inventory/topology.tsv"))
    ap.add_argument("--catalog", type=Path, required=True, help="authored catalog TSV (order name filename bytes sha256)")
    ap.add_argument("--desired-dir", type=Path, default=Path("/cluster/containers/.nvis-p2p/desired"))
    ap.add_argument("--origin", required=True, help="http(s)://host:port the agents fetch from")
    ap.add_argument("--vm", default="http://127.0.0.1:8428")
    ap.add_argument("--stages", default="1,50%,100%")
    ap.add_argument("--budget", type=float, default=3600, help="seconds a released node may take before deferral")
    ap.add_argument("--max-failures", type=int, default=3)
    ap.add_argument("--alive-window", type=float, default=180, help="agent counts as alive if it polled this recently")
    ap.add_argument("--tick", type=float, default=15)
    ap.add_argument("--state-dir", type=Path, default=Path.home() / "p2p-coordinator-runs")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)

    nodes = rack_nodes(args.topology, args.rack)
    if not nodes:
        log(f"COORD_EMPTY_RACK rack={args.rack} topology={args.topology}")
        return 64
    image = catalog_pin(args.catalog, args.image)
    observe = vm_observer(args.vm, args.alive_window)
    coord = Coordinator(args.desired_dir, args.origin.rstrip("/"), observe, args.budget,
                        args.max_failures, args.alive_window, log=log)
    cuts = rung_cuts(args.stages.split(","), len(nodes))

    if args.command == "cancel":
        log(f"COORD_CANCEL_DONE rack={args.rack} documents={coord.cancel(nodes, args.image)}")
        return 0
    obs = observe(nodes, args.image)
    if args.command in ("plan", "status"):
        log(f"COORD_PLAN rack={args.rack} nodes={len(nodes)} cuts={cuts} image={args.image} "
            f"sha256={image['sha256'][:12]} live_agents={sum(o.alive for o in obs.values())}")
        for n in nodes:
            o = obs[n]
            log(f"  {n} alive={o.alive} generation={o.generation} state={o.state or '-'} failures={o.failures}")
        return 0

    run = Run(run_id=f"{args.rack}-{args.image}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}",
              rack=args.rack, image=image, cuts=cuts, nodes={n: NodeRun() for n in nodes}, started=time.time())
    log(f"COORD_START run={run.run_id} nodes={len(nodes)} cuts={cuts} budget={args.budget:.0f}s")
    while True:
        done = coord.tick(run)
        save(run, args.state_dir)
        if done:
            break
        time.sleep(args.tick)
    return 0 if all(nr.state == "ready" for nr in run.nodes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
