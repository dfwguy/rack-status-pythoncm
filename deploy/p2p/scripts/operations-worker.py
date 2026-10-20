#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Claim and run queued NVIS-P2P fleet operations.

The gateway (``backend/p2p_operations.py``) writes an operation request into a shared directory; it
cannot run the operation itself. This worker runs where the instance lives -- on a short timer or
cron -- claims each unfinished request with an exclusive lock, runs the matching catalog script with
the campaign pacing the gateway recorded, appends a log, and writes the terminal state back so the
fleet snapshot can report it.

Supported: ``preflight`` (read-only), ``persist`` (preheat an image onto the seeds), ``distribute``
(a staged catalog campaign over the roster), ``verify`` (receipt audit). ``repair`` re-distributes
to the previous run's deferred nodes. Each is the same first-class script an operator would run by
hand; the worker only supplies the pacing and the staging.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import math
import os
import pathlib
import re
import subprocess
import tempfile


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(log: pathlib.Path, text: str) -> None:
    with log.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _run(cmd: list[str], log: pathlib.Path, env: dict, timeout: int) -> int:
    _append(log, f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        _append(log, f"[worker] {type(exc).__name__}: {exc}")
        return 127
    if proc.stdout:
        _append(log, proc.stdout.rstrip())
    if proc.stderr:
        _append(log, "--- stderr ---\n" + proc.stderr.rstrip())
    return proc.returncode


def _rollout_env(base: dict, rollout: dict, root: str) -> dict:
    env = dict(base)
    env["NVIS_P2P_ROOT"] = f"{root.rstrip('/')}/current"
    env["NVIS_P2P_ROLLOUT_PARALLELISM"] = str(rollout.get("parallelism", 48))
    env["NVIS_P2P_ROLLOUT_POLL_SECONDS"] = str(rollout.get("poll_seconds", 60))
    env["NVIS_P2P_ROLLOUT_MAX_LAUNCHES"] = str(rollout.get("max_launches", 3))
    env["NVIS_P2P_ROLLOUT_MAX_UNREACHABLE"] = str(rollout.get("max_unreachable", 3))
    env["NVIS_P2P_ROLLOUT_TIMEOUT_SECONDS"] = str(rollout.get("timeout_seconds", 1800))
    env["NVIS_P2P_ROLLOUT_STALL_SECONDS"] = str(rollout.get("stall_seconds", 600))
    env["NVIS_P2P_ROLLOUT_VERIFY_MBPS"] = str(rollout.get("verify_mb_per_second", 100))
    env["NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS"] = str(rollout.get("download_timeout_seconds", 3600))
    return env


def _natural(text: str) -> list:
    """Sort key that orders ``U7`` before ``U19`` and ``su9`` before ``su19``."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _load_topology(path: pathlib.Path) -> dict[str, tuple[str, str]]:
    """``node -> (su, rack)`` from the topology inventory ``lib-topology.sh`` reads.

    One row per compute node, tab-separated ``<hostname> <su> <rack>``, captured from the cluster
    manager's placement by ``build-topology-inventory.sh``. It is the same file every peer's
    scheduler labels (``idc`` = SU, ``location`` = rack) were rendered from, so a plan built on it
    groups nodes exactly the way the scheduler prefers parents. Rows missing a field are skipped.
    """
    topology: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) >= 3 and all(parts[:3]):
            topology[parts[0]] = (parts[1], parts[2])
    return topology


class FleetState:
    """What the fleet snapshot last saw, for one image: who holds it, who answered, who has a record.

    ``holders`` already carry the image (a valid receipt); ``unreachable`` did not answer the last
    receipt audit; ``present`` counts each node's verified catalog images -- a peer that has finished
    many downloads is the safest choice to seed a rack. ``age`` is seconds since the snapshot.
    """

    def __init__(self, holders: set[str], unreachable: set[str], present: dict[str, int], age: float):
        self.holders = holders
        self.unreachable = unreachable
        self.present = present
        self.age = age


#: A snapshot older than this is not trusted to say who holds what (the cron refreshes every 15 min).
_SNAPSHOT_MAX_AGE = 3600


def _load_fleet_state(path: pathlib.Path, image: str, log: pathlib.Path) -> FleetState | None:
    """The snapshot's view of ``image`` across the fleet, or None when it is missing, unreadable or
    stale -- the plan then falls back to placement alone, and the log says why."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.datetime.strptime(doc["coverage"]["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        rows = doc["coverage"]["nodes"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _append(log, f"[worker] rack-staged: no usable fleet snapshot at {path} ({type(exc).__name__}); "
                     "planning by placement only")
        return None
    age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
    if age > _SNAPSHOT_MAX_AGE:
        _append(log, f"[worker] rack-staged: fleet snapshot is {int(age)}s old; planning by placement only")
        return None
    holders, unreachable, present = set(), set(), {}
    for row in rows:
        node = row.get("node")
        if not node:
            continue
        if row.get("state") == "unreachable":
            unreachable.add(node)
        if (row.get("images") or {}).get(image) == "ready":
            holders.add(node)
        present[node] = int(row.get("verified") or row.get("present") or 0)
    return FleetState(holders, unreachable, present, age)


def _rung(token: str, size: int) -> int:
    """How many of a ``size``-node rack a ladder rung asks for: a count, or a share rounded up."""
    if token.endswith("%"):
        return math.ceil(size * int(token[:-1]) / 100)
    return int(token)


def _stage_cuts(size: int, stages: list[str]) -> list[int]:
    """Cumulative per-stage targets for one rack: never falling, never past the rack, ending full."""
    cuts, held = [], 0
    for token in stages:
        held = max(held, min(size, _rung(token, size)))
        cuts.append(held)
    cuts[-1] = size
    return cuts


def _pack(slices: list[list[str]], cap: int) -> list[list[str]]:
    """Pack per-rack slices into waves of at most ``cap`` nodes, keeping a rack's slice together.

    A slice larger than a whole wave is split; otherwise a slice never straddles two waves, so a
    rack advances one rung in one wave and its new holders are all ready before the next stage.
    """
    waves: list[list[str]] = []
    current: list[str] = []
    for part in slices:
        for start in range(0, len(part), cap):
            chunk = part[start:start + cap]
            if current and len(current) + len(chunk) > cap:
                waves.append(current)
                current = []
            current.extend(chunk)
    if current:
        waves.append(current)
    return waves


def rack_staged_waves(nodes: list[str], topology: dict[str, tuple[str, str]], stages: list[str],
                      seed_wave: int, fill_wave: int,
                      fleet: FleetState | None = None) -> tuple[list[tuple[str, list[str]]], dict]:
    """Plan a roster as ``[(stage, wave_nodes), ...]`` that fills racks by the stage ladder.

    Stage one gives every rack its first holder, paced at ``seed_wave`` -- these are the only pulls
    that reach the app server's seeds. Each later stage grows every rack to its next rung, paced at
    ``fill_wave``: the new nodes find holders in their own rack (the scheduler ranks same-rack
    parents first), and because the ladder rises gradually no single holder feeds a whole rack.

    With a ``fleet`` snapshot the plan also knows what is already there. A rack's rungs are measured
    against the whole rack (from the topology), and nodes that already hold the image count toward
    them -- so a rack that is half full skips straight to its last stage, and no seed wave is spent
    on a rack that has a copy. Within a rack the most proven peers (most verified images) go first,
    so a rack is seeded by a node that has finished downloads before; nodes the snapshot could not
    reach, and roster nodes that already hold the image, go to the rack's last stage.

    Racks are visited round-robin across SUs, so a wave spreads over the fabric instead of loading
    one SU's uplinks. Roster nodes missing from the topology go last, paced like seeds: they have no
    known rack-mates and will draw on the seeds or their SU. Returns the waves and a summary.
    """
    holders = fleet.holders if fleet else set()
    unreachable = fleet.unreachable if fleet else set()
    present = fleet.present if fleet else {}
    racks: dict[tuple[str, str], list[str]] = {}
    unplaced: list[str] = []
    for node in nodes:
        where = topology.get(node)
        if where is None:
            unplaced.append(node)
        else:
            racks.setdefault(where, []).append(node)
    rack_size: dict[tuple[str, str], int] = {}
    rack_held: dict[tuple[str, str], int] = {}
    if fleet is not None:
        for node, where in topology.items():
            if where in racks:
                rack_size[where] = rack_size.get(where, 0) + 1
                rack_held[where] = rack_held.get(where, 0) + (node in holders)
    # Fill order inside a rack: proven reachable peers first, then the unreachable, then roster nodes
    # that already hold the image (they short-circuit on their receipt wherever they land).
    late: dict[tuple[str, str], int] = {}
    for key, members in racks.items():
        members.sort(key=lambda n: (n in holders, n in unreachable, -present.get(n, 0), _natural(n)))
        late[key] = sum(1 for n in members if n in holders or n in unreachable)
    by_su: dict[str, list[tuple[str, str]]] = {}
    for key in sorted(racks, key=lambda k: (_natural(k[0]), _natural(k[1]))):
        by_su.setdefault(key[0], []).append(key)
    order: list[tuple[str, str]] = []
    queues = list(by_su.values())
    while any(queues):
        for queue in queues:
            if queue:
                order.append(queue.pop(0))

    # How far along each rung a rack already is, and how many roster nodes each stage takes from it.
    # Without a snapshot the rack is just its roster nodes and starts empty (the plain ladder).
    taken = {key: 0 for key in order}
    waves: list[tuple[str, list[str]]] = []
    per_stage: list[dict] = []
    last = len(stages) - 1
    for index, token in enumerate(stages):
        slices = []
        for key in order:
            members = racks[key]
            if index == last:
                part = members[taken[key]:]
            else:
                size = rack_size.get(key, len(members))
                target = _stage_cuts(size, stages)[index]
                eligible = len(members) - late[key]          # only proven, reachable, not-yet-holding
                want = max(0, target - rack_held.get(key, 0) - taken[key])
                part = members[taken[key]:min(eligible, taken[key] + want)]
            taken[key] += len(part)
            if part:
                slices.append(part)
        stage_waves = _pack(slices, seed_wave if index == 0 else fill_wave)
        waves.extend((token, wave) for wave in stage_waves)
        per_stage.append({"stage": token, "nodes": sum(len(p) for p in slices),
                          "racks": len(slices), "waves": len(stage_waves)})
    if unplaced:
        unplaced_waves = _pack([sorted(unplaced, key=_natural)], seed_wave)
        waves.extend(("unplaced", wave) for wave in unplaced_waves)
        per_stage.append({"stage": "unplaced", "nodes": len(unplaced), "racks": 0, "waves": len(unplaced_waves)})
    summary = {"racks": len(order), "sus": len(by_su), "unplaced": len(unplaced), "stages": per_stage,
               "racks_already_holding": sum(1 for key in order if rack_held.get(key, 0) > 0),
               "roster_holders": sum(1 for n in nodes if n in holders),
               "roster_unreachable": sum(1 for n in nodes if n in unreachable)}
    return waves, summary


def _plan_waves(nodes: list[str], rollout: dict, env: dict, log: pathlib.Path,
                image: str = "", snapshot: pathlib.Path | None = None) -> list[tuple[str, list[str]]]:
    """The roster as waves, by the request's strategy. Rack-staged falls back to sequential, loudly,
    when there is no usable topology -- the image still reaches every node, just without the plan.
    A fresh fleet snapshot, when there is one, lets the plan skip racks that already hold ``image``
    and seed each rack from a proven peer."""
    wave_size = max(1, int(rollout.get("wave_size", 50)))
    if rollout.get("strategy") == "rack-staged":
        path = pathlib.Path(env.get("NVIS_TOPOLOGY_FILE") or f"{env['NVIS_P2P_ROOT']}/inventory/topology.tsv")
        try:
            topology = _load_topology(path)
        except OSError as exc:
            topology = {}
            _append(log, f"[worker] rack-staged: topology inventory unreadable ({exc})")
        if topology:
            fill = int(rollout.get("fill_wave_size") or 0) or wave_size
            stages = list(rollout.get("stages") or ["1", "50%", "100%"])
            fleet = _load_fleet_state(snapshot, image, log) if snapshot and image else None
            waves, summary = rack_staged_waves(nodes, topology, stages, wave_size, max(1, fill), fleet)
            _append(log, f"[worker] rack-staged plan from {path}: {summary['racks']} racks in "
                         f"{summary['sus']} SUs, ladder {' → '.join(stages)}, seed waves {wave_size}, fill waves {fill}")
            if fleet is not None:
                _append(log, f"[worker]   fleet snapshot ({int(fleet.age)}s old): "
                             f"{summary['racks_already_holding']} racks already hold the image, "
                             f"{summary['roster_holders']} roster nodes hold it, "
                             f"{summary['roster_unreachable']} roster nodes were unreachable (placed last)")
            for row in summary["stages"]:
                _append(log, f"[worker]   stage {row['stage']}: {row['nodes']} nodes over {row['racks']} racks "
                             f"in {row['waves']} waves")
            return waves
        _append(log, f"[worker] rack-staged: no placed nodes in {path}; falling back to sequential waves")
    return [("sequential", nodes[i:i + wave_size]) for i in range(0, len(nodes), wave_size)]


def _staged_campaign(image: str, roster: pathlib.Path, rollout: dict, scripts: str,
                     env: dict, log: pathlib.Path, label: str, snapshot: pathlib.Path | None = None) -> int:
    """Preheat the seeds, then roll the image out wave by wave so each wave seeds the next.

    The waves are the roster in order (``sequential``) or a placement plan (``rack-staged``, see
    :func:`rack_staged_waves`); either way they run one after another through the same rollout
    script, so a wave's holders are complete before the next wave asks for the image.
    """
    rc = _run(["bash", f"{scripts}/preheat-catalog-image.sh", image], log, env, 1200)
    if rc != 0:
        _append(log, f"[worker] preheat failed ({rc}); aborting campaign")
        return rc
    nodes = [n.strip() for n in roster.read_text(encoding="utf-8").splitlines() if n.strip()]
    worst = 0
    for index, (stage, wave) in enumerate(_plan_waves(nodes, rollout, env, log, image, snapshot)):
        wave_file = roster.parent / f".{label}-wave{index:02d}.nodes"
        wave_file.write_text("\n".join(wave) + "\n", encoding="utf-8")
        tag = "" if stage == "sequential" else f" (stage {stage})"
        _append(log, f"[worker] wave {index} — {len(wave)} nodes{tag}")
        rc = _run(["bash", f"{scripts}/rollout-catalog-image-fleet.sh", image, str(wave_file),
                   f"{label}-w{index}"], log, env, int(rollout.get("timeout_seconds", 1800)) + 300)
        wave_file.unlink(missing_ok=True)
        worst = worst or rc
    return worst


def _sync_catalog(inner: dict, roster: pathlib.Path, log: pathlib.Path) -> int:
    """Project the authored catalog onto the cluster: write the manifest, then refresh the nodes.

    The operator's images are written to the origin's ``images.json``/``images.tsv`` (served from the
    canonical directory), then every peer re-pulls the ``images.tsv`` so a later distribute verifies
    against the new pins rather than a stale local copy.
    """
    catalog_path = pathlib.Path(inner.get("catalog_path", ""))
    images = inner.get("catalog", [])
    appserver = (inner.get("appserver_ip") or "").strip()
    if not catalog_path.name or not images:
        _append(log, "[worker] sync: missing catalog_path or authored images")
        return 2
    doc = {"schema_version": 1, "catalog_name": "authored",
           "canonical_directory": inner.get("container_path", ""), "images": images}
    try:
        catalog_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        rows = ["order\tname\tfilename\tbytes\tsha256"]
        rows += [f"{i.get('order', 0)}\t{i.get('name', '')}\t{i.get('filename', '')}\t"
                 f"{i.get('bytes', 0)}\t{i.get('sha256', '')}" for i in images]
        catalog_path.with_name("images.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    except OSError as exc:
        _append(log, f"[worker] sync: could not write the manifest: {exc}")
        return 1
    _append(log, f"[worker] wrote {catalog_path} + images.tsv ({len(images)} images)")
    if not appserver or not roster.exists():
        _append(log, "[worker] sync: no appserver ip or roster; manifest written, node refresh skipped")
        return 0
    # Best-effort node refresh: each peer re-pulls the origin's images.tsv, in one clush fan-out
    # from this appserver -- bounded fan-out and per-node timeouts, where an ssh-per-node loop let a
    # dead node hang its slot. A node that is down just misses this round (clush reports it on
    # stderr); the refresh never fails the sync -- the manifest is already the truth and the node
    # sync-timer reconciles stragglers.
    origin = f"http://{appserver}:12130/.nvis-p2p/catalog/images.tsv"
    refresh = ["clush", "--nostdin", "--hostfile", str(roster), "-f", "40", "-t", "10", "-u", "60",
               f"curl -s -m10 {origin} -o /opt/containers/.nvis-p2p/catalog/images.tsv"]
    _run(refresh, log, dict(os.environ), 900)
    _append(log, "[worker] node catalog refresh dispatched (best-effort)")
    return 0


def _manifest_pins(catalog_path: str) -> dict[str, str]:
    """``filename -> sha256`` from the cluster's manifest, or ``{}`` if it cannot be read."""
    try:
        doc = json.loads(pathlib.Path(catalog_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {i.get("filename", ""): i.get("sha256", "") for i in doc.get("images", []) if isinstance(i, dict)}


def _ensure_pinned(inner: dict, images: list[str], roster: pathlib.Path, log: pathlib.Path) -> int:
    """Make the cluster's manifest pin each image as the authored catalog does, before any transfer.

    Seeds and nodes verify a download against the MANIFEST's pin, so an image staged and pinned in
    Rack Status but not yet synced -- or re-staged under the same name with a new hash -- fails on
    every node, and only after preheat and a whole wave. When the request carries the authored
    catalog and the manifest disagrees for any image, sync first and re-check; refuse if it still
    disagrees. No authored catalog means the manifest is the only source, and there is nothing to
    compare.
    """
    authored = {i.get("filename", ""): i.get("sha256", "") for i in inner.get("catalog") or []}
    if not authored:
        return 0
    unpinned = [i for i in images if not authored.get(i)]
    if unpinned:
        _append(log, f"[worker] not pinned in the authored catalog: {', '.join(unpinned)}; refusing")
        return 2
    stale = [i for i in images if _manifest_pins(inner.get("catalog_path", "")).get(i) != authored[i]]
    if not stale:
        return 0
    _append(log, f"[worker] cluster manifest pins differ from the catalog for {', '.join(stale)}; syncing first")
    rc = _sync_catalog(inner, roster, log)
    if rc != 0:
        return rc
    manifest = _manifest_pins(inner.get("catalog_path", ""))
    still = [i for i in images if manifest.get(i) != authored[i]]
    if still:
        _append(log, f"[worker] manifest still disagrees after sync for {', '.join(still)}; refusing")
        return 3
    return 0


def _handle(req: dict, args: argparse.Namespace, log: pathlib.Path) -> int:
    action = req.get("action", "")
    inner = req.get("request", {})
    root = inner.get("install_root") or "/opt/nvis-p2p"
    scripts = f"{root.rstrip('/')}/current/scripts"
    env = _rollout_env(os.environ, inner.get("rollout", {}), root)
    # A named roster travels with the request as roster_nodes; write it out and target it instead of
    # the cluster's default inventory. Blank -> the default roster the cron passes.
    roster_nodes = inner.get("roster_nodes")
    if roster_nodes:
        roster = pathlib.Path(tempfile.gettempdir()) / f"op-{req.get('id', 'roster')}.nodes"
        roster.write_text("\n".join(roster_nodes) + "\n", encoding="utf-8")
        _append(log, f"[worker] targeting roster '{req.get('roster')}' ({len(roster_nodes)} nodes)")
    else:
        roster = pathlib.Path(args.roster)

    if action in ("persist", "distribute", "repair"):
        rc = _ensure_pinned(inner, req.get("images") or [], roster, log)
        if rc != 0:
            return rc
    if action == "preflight":
        return _run(["bash", f"{scripts}/preflight-catalog-fleet.sh", str(roster)], log, env, 600)
    if action == "persist":
        return _run(["bash", f"{scripts}/preheat-catalog-image.sh", req["images"][0]], log, env, 1200)
    if action == "verify":
        return _run(["bash", f"{scripts}/audit-fleet.sh", str(roster)], log, env, 900)
    if action == "sync":
        return _sync_catalog(inner, roster, log)
    if action in ("distribute", "repair"):
        return _staged_campaign(req["images"][0], roster, inner.get("rollout", {}), scripts, env, log, req["id"],
                                getattr(args, "snapshot", None))
    _append(log, f"[worker] no handler for action {action!r}")
    return 2


def _claim_and_run(path: pathlib.Path, args: argparse.Namespace) -> None:
    lock = path.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return  # another run owns it
    os.close(fd)
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
        if req.get("finished_at"):
            return
        log = path.with_suffix(".log")
        _append(log, f"=== {req.get('action')} {req.get('id')} started {_now()} ===")
        rc = _handle(req, args, log)
        req["state"] = "succeeded" if rc == 0 else "failed"
        req["return_code"] = rc
        req["finished_at"] = _now()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(req, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        _append(log, f"=== {req.get('id')} {req['state']} (rc={rc}) {_now()} ===")
    finally:
        lock.unlink(missing_ok=True)


def default_snapshot(ops_dir: pathlib.Path) -> pathlib.Path:
    """The fleet snapshot that sits beside an operations directory.

    Both live under the app's state directory, keyed by cluster: ``<state>/p2p-operations/<cluster>``
    holds the queue and ``<state>/p2p-snapshots/<cluster>.json`` the snapshot cron's output.
    """
    return ops_dir.parent.parent / "p2p-snapshots" / f"{ops_dir.name}.json"


def _pending(ops_dir: pathlib.Path) -> list[pathlib.Path]:
    """Unfinished requests in the order they were enqueued.

    Not filename order: ids start with the action, so a distribute enqueued after a sync would sort
    -- and run -- first ("d" < "s"). Enqueue time first, id as the tie-break.
    """
    pending: list[tuple[str, str, pathlib.Path]] = []
    for path in ops_dir.glob("*.json"):
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not req.get("finished_at"):
            pending.append((str(req.get("started_at") or ""), path.name, path))
    return [path for _, _, path in sorted(pending)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ops-dir", required=True, type=pathlib.Path, help="per-cluster operations directory")
    ap.add_argument("--roster", required=True, help="eligible-node file for fleet-scoped actions")
    ap.add_argument("--snapshot", type=pathlib.Path, default=None,
                    help="fleet snapshot the rack-staged plan reads (default: the cluster's "
                         "<state>/p2p-snapshots/<cluster>.json beside <state>/p2p-operations/<cluster>)")
    args = ap.parse_args(argv)
    if not args.ops_dir.is_dir():
        return 0
    if args.snapshot is None:
        args.snapshot = default_snapshot(args.ops_dir)
    # One worker at a time per cluster. The per-request lock stops two workers running the SAME
    # request; it does not stop a cron tick starting the next request while a long rollout is still
    # going, which put two fleet-wide transfers on the seeds at once. A tick that finds the worker
    # busy leaves the queue to it.
    with open(args.ops_dir / ".worker.lock", "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        for path in _pending(args.ops_dir):
            _claim_and_run(path, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
