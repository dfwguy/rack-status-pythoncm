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
                      seed_wave: int, fill_wave: int) -> tuple[list[tuple[str, list[str]]], dict]:
    """Plan a roster as ``[(stage, wave_nodes), ...]`` that fills racks by the stage ladder.

    Stage one gives every rack its first holder, paced at ``seed_wave`` -- these are the only pulls
    that reach the app server's seeds. Each later stage grows every rack to its next rung, paced at
    ``fill_wave``: the new nodes find holders in their own rack (the scheduler ranks same-rack
    parents first), and because the ladder rises gradually no single holder feeds a whole rack.

    Racks are visited round-robin across SUs, so a wave spreads over the fabric instead of loading
    one SU's uplinks. Roster nodes missing from the topology go last, paced like seeds: they have no
    known rack-mates and will draw on the seeds or their SU. Returns the waves and a summary.
    """
    racks: dict[tuple[str, str], list[str]] = {}
    unplaced: list[str] = []
    for node in nodes:
        where = topology.get(node)
        if where is None:
            unplaced.append(node)
        else:
            racks.setdefault(where, []).append(node)
    for members in racks.values():
        members.sort(key=_natural)
    by_su: dict[str, list[tuple[str, str]]] = {}
    for key in sorted(racks, key=lambda k: (_natural(k[0]), _natural(k[1]))):
        by_su.setdefault(key[0], []).append(key)
    order: list[tuple[str, str]] = []
    queues = list(by_su.values())
    while any(queues):
        for queue in queues:
            if queue:
                order.append(queue.pop(0))

    cuts = {key: _stage_cuts(len(racks[key]), stages) for key in order}
    waves: list[tuple[str, list[str]]] = []
    per_stage: list[dict] = []
    previous = {key: 0 for key in order}
    for index, token in enumerate(stages):
        slices = [racks[key][previous[key]:cuts[key][index]] for key in order]
        slices = [part for part in slices if part]
        stage_waves = _pack(slices, seed_wave if index == 0 else fill_wave)
        waves.extend((token, wave) for wave in stage_waves)
        per_stage.append({"stage": token, "nodes": sum(len(p) for p in slices),
                          "racks": len(slices), "waves": len(stage_waves)})
        previous = {key: cuts[key][index] for key in order}
    if unplaced:
        unplaced_waves = _pack([sorted(unplaced, key=_natural)], seed_wave)
        waves.extend(("unplaced", wave) for wave in unplaced_waves)
        per_stage.append({"stage": "unplaced", "nodes": len(unplaced), "racks": 0, "waves": len(unplaced_waves)})
    summary = {"racks": len(order), "sus": len(by_su), "unplaced": len(unplaced), "stages": per_stage}
    return waves, summary


def _plan_waves(nodes: list[str], rollout: dict, env: dict, log: pathlib.Path) -> list[tuple[str, list[str]]]:
    """The roster as waves, by the request's strategy. Rack-staged falls back to sequential, loudly,
    when there is no usable topology -- the image still reaches every node, just without the plan."""
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
            waves, summary = rack_staged_waves(nodes, topology, stages, wave_size, max(1, fill))
            _append(log, f"[worker] rack-staged plan from {path}: {summary['racks']} racks in "
                         f"{summary['sus']} SUs, ladder {' → '.join(stages)}, seed waves {wave_size}, fill waves {fill}")
            for row in summary["stages"]:
                _append(log, f"[worker]   stage {row['stage']}: {row['nodes']} nodes over {row['racks']} racks "
                             f"in {row['waves']} waves")
            return waves
        _append(log, f"[worker] rack-staged: no placed nodes in {path}; falling back to sequential waves")
    return [("sequential", nodes[i:i + wave_size]) for i in range(0, len(nodes), wave_size)]


def _staged_campaign(image: str, roster: pathlib.Path, rollout: dict, scripts: str,
                     env: dict, log: pathlib.Path, label: str) -> int:
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
    for index, (stage, wave) in enumerate(_plan_waves(nodes, rollout, env, log)):
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
    # Best-effort node refresh: each peer re-pulls the origin's images.tsv. A node that is down
    # returns ssh 255; `|| true` keeps xargs from aborting the whole batch on it, and the refresh
    # never fails the sync -- the manifest is already the truth and the node sync-timer reconciles
    # stragglers.
    origin = f"http://{appserver}:12130/.nvis-p2p/catalog/images.tsv"
    refresh = (
        "xargs -P40 -I@ bash -c '"
        "ssh -o ConnectTimeout=10 -o BatchMode=yes @ "
        '"curl -s -m10 ' + origin + ' -o /opt/containers/.nvis-p2p/catalog/images.tsv" || true'
        "' < " + str(roster)
    )
    _run(["bash", "-c", refresh], log, dict(os.environ), 900)
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
        return _staged_campaign(req["images"][0], roster, inner.get("rollout", {}), scripts, env, log, req["id"])
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
    args = ap.parse_args(argv)
    if not args.ops_dir.is_dir():
        return 0
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
