#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Produce a detailed, secret-free live Rack Status health audit."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import pythoncm_broker_client

_CONTAINER_PROBE = r'''
import app, json, requests, time
c = app.app.test_client()
with c.session_transaction() as session:
    session["rack_web_auth"] = "1"
cookie = c.get_cookie(app.app.config.get("SESSION_COOKIE_NAME", "session"))
s = requests.Session()
s.cookies.set(cookie.key, cookie.value)
out = {}
for path in ("/compute/racks", "/api/status?refresh=1", "/api/cluster-overview/live?refresh=1"):
    started = time.monotonic()
    response = s.get("http://127.0.0.1:5056" + path, timeout=330)
    item = {"http": response.status_code, "seconds": round(time.monotonic() - started, 3),
            "bytes": len(response.content), "content_type": response.headers.get("content-type", "")}
    if path.startswith("/api/status") and response.status_code == 200:
        body = response.json()
        rows = body.get("rows") or []
        item.update({"row_count": len(rows), "summary": body.get("summary"),
                     "snapshot": body.get("snapshot"), "native_workers": body.get("native_workers"),
                     "validated_racks": [row.get("rack") for row in rows
                                           if row.get("stage1") == "done" and row.get("stage2") == "done"]})
    elif path.startswith("/api/cluster-overview/live") and response.status_code == 200:
        body = response.json()
        item.update({"ok": body.get("ok"), "reachable": body.get("reachable"),
                     "sections": body.get("sections")})
    out[path] = item
print(json.dumps(out, sort_keys=True))
'''


def _run(command: list[str], timeout: int = 30, stdin: str | None = None) -> dict[str, object]:
    started = time.monotonic()
    try:
        proc = subprocess.run(command, input=stdin, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, check=False)
        return {"returncode": proc.returncode, "stdout": proc.stdout.strip()[-12000:],
                "stderr": proc.stderr.strip()[-4000:], "seconds": round(time.monotonic() - started, 3)}
    except Exception as exc:  # noqa: BLE001 - audit records collection failures
        return {"returncode": None, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 3)}


def _systemd(unit: str) -> dict[str, object]:
    result = _run(["systemctl", "show", unit, "--property=ActiveState,SubState,NRestarts,ExecMainStartTimestamp"], 15)
    values: dict[str, object] = {"unit": unit, "probe": result}
    if result.get("returncode") == 0:
        values.update(line.split("=", 1) for line in str(result.get("stdout") or "").splitlines() if "=" in line)
        values.pop("probe", None)
    return values


def _broker(socket_path: str) -> dict[str, object]:
    started = time.monotonic()
    try:
        reply = pythoncm_broker_client.request(socket_path, "query", ["--query", "version"], timeout=10)
        return {"ok": reply.get("ok") is True, "duration_seconds": round(time.monotonic()-started, 3),
                "generation": reply.get("connection_generation"), "error": reply.get("error")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "duration_seconds": round(time.monotonic()-started, 3),
                "error": f"{type(exc).__name__}: {exc}"}


def _write(output_dir: Path, audit: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "rack-status-latest.json"
    tmp = output_dir / f".{latest.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, latest)
    with (output_dir / "rack-status-history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(audit, sort_keys=True) + "\n")
    api = audit.get("application") if isinstance(audit.get("application"), dict) else {}
    status = api.get("/api/status?refresh=1") if isinstance(api, dict) else {}
    overview = api.get("/api/cluster-overview/live?refresh=1") if isinstance(api, dict) else {}
    markdown = (
        "# Rack Status latest hourly audit\n\n"
        f"- Checked (UTC): `{audit.get('checked_at')}`\n"
        f"- Overall: `{'HEALTHY' if audit.get('healthy') else 'UNHEALTHY'}`\n"
        f"- Query broker: `{json.dumps(audit.get('query_broker'), sort_keys=True)}`\n"
        f"- Rack rows: `{status.get('row_count') if isinstance(status, dict) else None}`\n"
        f"- Validation summary: `{json.dumps(status.get('summary') if isinstance(status, dict) else None, sort_keys=True)}`\n"
        f"- Snapshot: `{json.dumps(status.get('snapshot') if isinstance(status, dict) else None, sort_keys=True)}`\n"
        f"- Live overview: `{json.dumps(overview.get('sections') if isinstance(overview, dict) else None, sort_keys=True)}`\n"
        f"- Recent error counts: `{json.dumps(audit.get('recent_error_counts'), sort_keys=True)}`\n"
    )
    (output_dir / "RACK_STATUS_LATEST.md").write_text(markdown, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="/cluster/OVA/codexisamoron")
    parser.add_argument("--expected-racks", type=int, default=224)
    args = parser.parse_args()
    logs = _run(["docker", "logs", "--since", "1h", "rack-status-rack-status-1"], 30)
    container = _run(["docker", "inspect", "rack-status-rack-status-1", "--format",
                      "{{json .State}}"], 15)
    app_probe = _run(["docker", "exec", "-i", "rack-status-rack-status-1", "sh", "-lc",
                      "cd /app/backend && python3 -"], 360, _CONTAINER_PROBE)
    application: dict[str, object] = {"probe_error": app_probe}
    if app_probe.get("returncode") == 0:
        try:
            application = json.loads(str(app_probe.get("stdout") or "{}").splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            application = {"probe_error": f"{type(exc).__name__}: {exc}", "raw": app_probe}
    broker = _broker("/run/nvis-pythoncm/query.sock")
    status = application.get("/api/status?refresh=1") if isinstance(application, dict) else {}
    overview = application.get("/api/cluster-overview/live?refresh=1") if isinstance(application, dict) else {}
    sections = overview.get("sections") if isinstance(overview, dict) else {}
    section_health = all(
        isinstance(value, dict) and value.get("state") == "fresh" and not value.get("last_error")
        for value in sections.values()
    ) if isinstance(sections, dict) and sections else False
    snapshot = status.get("snapshot") if isinstance(status, dict) else {}
    workers = status.get("native_workers") if isinstance(status, dict) else {}
    healthy = bool(
        broker.get("ok") and status.get("http") == 200 and overview.get("http") == 200
        and status.get("row_count") == args.expected_racks
        and isinstance(snapshot, dict) and snapshot.get("state") == "fresh"
        and not (workers.get("recent_errors") if isinstance(workers, dict) else ["missing"])
        and section_health
    )
    log_text = str(logs.get("stdout") or "") + "\n" + str(logs.get("stderr") or "")
    audit: dict[str, object] = {
        "schema": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "healthy": healthy,
        "query_broker": broker,
        "services": [_systemd(unit) for unit in (
            "nvis-pythoncm-broker.service", "nvis-pythoncm-exec-broker.service",
            "rack-status-watchdog.timer", "rack-status-hourly-audit.timer",
            "rack-validation-evidence-refresh.timer")],
        "socket_queues": _run(["ss", "-xlp"], 15), "container": container,
        "application": application,
        "recent_error_counts": {
            "broker_resource_unavailable": log_text.count("Resource temporarily unavailable"),
            "broker_disconnect_timeout": log_text.count("disconnect exceeded"),
            "application_broker_failures": log_text.count("PythonCM broker") + log_text.count("persistent PythonCM"),
        },
    }
    _write(Path(args.output_dir), audit)
    print(json.dumps({"healthy": healthy, "checked_at": audit["checked_at"]}))
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
