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
import json
import os
import pathlib
import subprocess


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


def _staged_campaign(image: str, roster: pathlib.Path, rollout: dict, scripts: str,
                     env: dict, log: pathlib.Path, label: str) -> int:
    """Preheat the seeds, then roll the image out in waves of ``wave_size`` so each seeds the next."""
    rc = _run(["bash", f"{scripts}/preheat-catalog-image.sh", image], log, env, 1200)
    if rc != 0:
        _append(log, f"[worker] preheat failed ({rc}); aborting campaign")
        return rc
    nodes = [n.strip() for n in roster.read_text(encoding="utf-8").splitlines() if n.strip()]
    wave_size = max(1, int(rollout.get("wave_size", 50)))
    worst = 0
    for i in range(0, len(nodes), wave_size):
        wave = nodes[i:i + wave_size]
        wave_file = roster.parent / f".{label}-wave{i // wave_size:02d}.nodes"
        wave_file.write_text("\n".join(wave) + "\n", encoding="utf-8")
        _append(log, f"[worker] wave {i // wave_size} — {len(wave)} nodes")
        rc = _run(["bash", f"{scripts}/rollout-catalog-image-fleet.sh", image, str(wave_file),
                   f"{label}-w{i // wave_size}"], log, env, int(rollout.get("timeout_seconds", 1800)) + 300)
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
    origin = f"http://{appserver}:12130/.nvis-p2p/catalog/images.tsv"
    refresh = (f"xargs -P40 -I{{}} ssh -o ConnectTimeout=10 -o BatchMode=yes {{}} "
               f"'curl -s -m10 {origin} -o /opt/containers/.nvis-p2p/catalog/images.tsv' < {roster}")
    rc = _run(["bash", "-c", refresh], log, dict(os.environ), 600)
    _append(log, f"[worker] node catalog refresh exit={rc}")
    return rc


def _handle(req: dict, args: argparse.Namespace, log: pathlib.Path) -> int:
    action = req.get("action", "")
    inner = req.get("request", {})
    root = inner.get("install_root") or "/opt/nvis-p2p"
    scripts = f"{root.rstrip('/')}/current/scripts"
    env = _rollout_env(os.environ, inner.get("rollout", {}), root)
    roster = pathlib.Path(args.roster)

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ops-dir", required=True, type=pathlib.Path, help="per-cluster operations directory")
    ap.add_argument("--roster", required=True, help="eligible-node file for fleet-scoped actions")
    args = ap.parse_args()
    if not args.ops_dir.is_dir():
        return 0
    for path in sorted(args.ops_dir.glob("*.json")):
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not req.get("finished_at"):
            _claim_and_run(path, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
