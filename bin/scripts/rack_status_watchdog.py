#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Probe the priority Rack Status broker and restart it if it cannot answer."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import pythoncm_broker_client


def _probe(socket_path: str, timeout: int) -> dict[str, object]:
    started = time.monotonic()
    try:
        reply = pythoncm_broker_client.request(
            socket_path, "query", ["--query", "version"], timeout=timeout
        )
        return {
            "ok": reply.get("ok") is True,
            "duration_seconds": round(time.monotonic() - started, 3),
            "connection_generation": reply.get("connection_generation"),
            "error": None if reply.get("ok") is True else reply.get("error"),
        }
    except Exception as exc:  # noqa: BLE001 - watchdog records every transport failure
        return {
            "ok": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _write_status(output_dir: Path, status: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "rack-status-watchdog-latest.json"
    tmp = output_dir / f".{latest.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, latest)
    with (output_dir / "rack-status-watchdog-history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(status, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/nvis-pythoncm/query.sock")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--output-dir", default="/cluster/OVA/codexisamoron")
    args = parser.parse_args()
    status: dict[str, object] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "service": "nvis-pythoncm-broker.service",
        "initial": _probe(args.socket, max(1, args.timeout)),
        "restarted": False,
    }
    if not status["initial"]["ok"]:  # type: ignore[index]
        restart = subprocess.run(
            ["systemctl", "restart", "nvis-pythoncm-broker.service"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        status["restarted"] = True
        status["restart_returncode"] = restart.returncode
        status["after_restart"] = _probe(args.socket, max(1, args.timeout))
    final = status.get("after_restart") or status["initial"]
    status["healthy"] = bool(final["ok"])  # type: ignore[index]
    _write_status(Path(args.output_dir), status)
    print(json.dumps(status, sort_keys=True))
    return 0 if status["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
