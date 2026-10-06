#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Produce a live FleetStatus snapshot for a provisioned NVIS-P2P instance.

Runs where the instance lives (the app server), assembles the document ``/api/p2p/status`` serves,
and writes it atomically to ``--out`` -- on a co-located Rack Status deployment that is a file under
the app's shared state directory, so the gateway reads live coverage without reaching across the
network or re-auditing on every poll. Intended to run on a short timer (systemd/cron); the gateway
treats a snapshot older than its max-age as absent.

Coverage comes from each peer's catalog receipts (read-only, one fan-out), the campaign wave from
the newest rollout run directory, and object-store health from the local container. Nothing here
drains Slurm: the fan-out is a quick read-only ``ls`` that exits cleanly.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

_SU_RE = re.compile(r"-su(\d+)$")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _su_of(node: str) -> int:
    m = _SU_RE.search(node)
    return int(m.group(1)) if m else 0


def _read_catalog(path: pathlib.Path, container_path: pathlib.Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    images = []
    for img in raw.get("images", []):
        filename = img.get("filename", "")
        # Canonical-present = the image exists in the origin directory at its catalog byte count.
        # Only these can be distributed, so only these are "required" of the fleet.
        present = False
        if filename:
            f = container_path / filename
            try:
                present = f.is_file() and f.stat().st_size == int(img.get("bytes", 0))
            except OSError:
                present = False
        images.append({
            "order": img.get("order", 0), "name": img.get("name", ""),
            "filename": filename, "bytes": img.get("bytes", 0),
            "sha256": img.get("sha256", ""), "required_by": img.get("required_by", []),
            "canonical_present": present, "persistent_import": None,
        })
    return {
        "schema_version": raw.get("schema_version"), "catalog_name": raw.get("catalog_name"),
        "canonical_directory": raw.get("canonical_directory", ""),
        "generated_at_utc": raw.get("generated_at_utc"), "images": images,
    }


def _exact_duplicates(images: list[dict]) -> list[list[str]]:
    by_sha: dict[str, list[str]] = {}
    for img in images:
        if img["sha256"]:
            by_sha.setdefault(img["sha256"], []).append(img["filename"])
    return [sorted(f) for f in by_sha.values() if len(f) > 1]


def _audit_receipts(roster: list[str], head: str, peer_root: str, fanout: int) -> dict[str, set[str]]:
    """node -> set of catalog filenames it holds a verification receipt for.

    One read-only pdsh: each node prints ``<node>: <receipt-filename>`` per receipt. A receipt is
    written only after a sha-verified install, so its presence is the node's "verified" signal.
    """
    if not roster:
        return {}
    roster_path = pathlib.Path(tempfile.gettempdir()) / "fleet-snap.nodes"
    roster_path.write_text("\n".join(roster) + "\n", encoding="utf-8")
    remote = (
        f"PDSH_RCMD_TYPE=ssh pdsh -R ssh -f {fanout} -u 30 -w ^/tmp/fleet-snap.nodes "
        f"'ls {peer_root}/receipts/ 2>/dev/null | sed s/.sha256$//'"
    )
    held: dict[str, set[str]] = {}
    try:
        subprocess.run(["scp", "-q", str(roster_path), f"{head}:/tmp/fleet-snap.nodes"], check=False, timeout=60)
        proc = subprocess.run(["ssh", head, remote], capture_output=True, text=True, timeout=600, check=False)
        for line in proc.stdout.splitlines():
            if ":" not in line:
                continue
            node, _, name = line.partition(":")
            name = name.strip()
            if name:
                held.setdefault(node.strip(), set()).add(name)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"fleet-snapshot: receipt audit degraded: {exc}", file=sys.stderr)
    finally:
        roster_path.unlink(missing_ok=True)
    return held


def _coverage(roster: list[str], required: list[str], held: dict[str, set[str]]) -> dict:
    req = set(required)
    nodes = []
    by_su: dict[int, dict] = {}
    for node in sorted(roster):
        su = _su_of(node)
        have = held.get(node, set()) & req
        responded = node in held
        present = len(have)
        missing = len(req) - present
        if not responded:
            state = "unreachable"
        elif present == len(req):
            state = "complete"
        else:
            state = "incomplete"
        nodes.append({
            "node": node, "su": su, "state": state, "present": present, "verified": present,
            "unverified": 0, "missing": missing, "bad_size": 0, "active": 0,
            "images": {f: ("ready" if f in have else "missing") for f in required},
            "observed_at": _now_iso(), "sync_state": "complete" if state == "complete" else "pending",
        })
        bucket = by_su.setdefault(su, {"su": su, "distribution": {}, "complete": 0, "syncing": 0,
                                       "incomplete": 0, "unavailable": 0})
        bucket["distribution"][str(present)] = bucket["distribution"].get(str(present), 0) + 1
        if state == "complete":
            bucket["complete"] += 1
        elif state == "unreachable":
            bucket["unavailable"] += 1
        else:
            bucket["incomplete"] += 1
    return {
        "generated_at": _now_iso(), "required_count": len(req), "required_images": required,
        "by_su": [by_su[s] for s in sorted(by_su)], "nodes": nodes,
    }


def _latest_sync(state_dir: pathlib.Path) -> dict | None:
    runs = sorted(state_dir.glob("rollout-*"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    if not runs:
        return None
    run = runs[-1]

    def _count(name: str, header: int) -> int:
        try:
            return max(0, len((run / name).read_text(encoding="utf-8").splitlines()) - header)
        except OSError:
            return 0

    ready = _count("ready.tsv", 1)
    deferred = _count("deferred.tsv", 1)
    total = _count("nodes", 0) or (ready + deferred)
    pending = max(0, total - ready - deferred)
    done = ready + deferred
    state = "running" if pending > 0 else ("complete_with_deferrals" if deferred else "complete")
    return {
        "id": run.name, "state": state, "total": total, "complete": ready, "pending": pending,
        "deferred": deferred, "percent": round(100 * done / total, 1) if total else 0.0,
        "updated_at": datetime.datetime.fromtimestamp(run.stat().st_mtime, datetime.timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _fleet(coverage: dict) -> list[dict]:
    out = []
    for su in coverage["by_su"]:
        total = su["complete"] + su["incomplete"] + su["unavailable"] + su["syncing"]
        out.append({
            "su": su["su"], "total": total, "reachable": total - su["unavailable"],
            "eligible": total, "peers": total - su["unavailable"], "ready": su["complete"],
            "syncing": su["syncing"], "incomplete": su["incomplete"], "unavailable": su["unavailable"],
            "distribution": su["distribution"],
        })
    return out


def _operations(ops_dir: pathlib.Path) -> list[dict]:
    """The recent operations the gateway queued, as the view's ``Operation`` shape.

    The worker writes terminal state back into the same files; the private ``request`` block it uses
    to run them is dropped here. Newest first, bounded so the tile does not grow without end.
    """
    if not ops_dir.is_dir():
        return []
    ops = []
    for path in sorted(ops_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:25]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ops.append({k: v for k, v in doc.items() if k != "request"})
    return ops


def _object_store_healthy(container: str) -> bool:
    try:
        proc = subprocess.run(
            ["sudo", "docker", "inspect", "-f", "{{.State.Health.Status}}", container],
            capture_output=True, text=True, timeout=15, check=False)
        return proc.stdout.strip() == "healthy"
    except (subprocess.SubprocessError, OSError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", required=True, type=pathlib.Path)
    ap.add_argument("--roster", required=True, type=pathlib.Path, help="file of eligible node hostnames")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--head", required=True, help="ssh alias of the cluster head for the pdsh fan-out")
    ap.add_argument("--container-path", type=pathlib.Path, default=pathlib.Path("/cluster/containers"))
    ap.add_argument("--peer-root", default="/opt/containers/.nvis-p2p")
    ap.add_argument("--rollout-state-dir", type=pathlib.Path, default=pathlib.Path("/opt/nvis-p2p/current/state"))
    ap.add_argument("--object-store-container", default="nvis-p2p-object-store")
    ap.add_argument("--ops-dir", type=pathlib.Path, help="per-cluster operations directory to surface")
    ap.add_argument("--fanout", type=int, default=40)
    args = ap.parse_args()

    catalog = _read_catalog(args.catalog, args.container_path)
    # Required = catalog images actually present in the canonical directory (what the fleet can hold).
    required = [i["filename"] for i in catalog["images"] if i["canonical_present"]]
    roster = [n.strip() for n in args.roster.read_text(encoding="utf-8").splitlines() if n.strip()]

    held = _audit_receipts(roster, args.head, str(args.peer_root), args.fanout)
    coverage = _coverage(roster, required, held)
    snapshot = {
        "provisioned": True, "catalog": catalog,
        "object_store_healthy": _object_store_healthy(args.object_store_container),
        "fleet": _fleet(coverage), "coverage": coverage, "sync": _latest_sync(args.rollout_state_dir),
        "catalog_exact_duplicates": _exact_duplicates(catalog["images"]),
        "preflight_generated_at": None,
        "operations": _operations(args.ops_dir) if args.ops_dir else [],
        "generated_at": _now_iso(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    os.replace(tmp, args.out)
    print(f"fleet-snapshot: wrote {args.out} — {len(coverage['nodes'])} nodes, "
          f"{sum(1 for n in coverage['nodes'] if n['state'] == 'complete')} complete, "
          f"required={required}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
