# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""TC-35 of the Phase 1 test plan: the audit trail under gunicorn, the production server.

Traces to FR-2, FR-11a, AC-3, AC-10f.

Every other case in this suite runs the app in-process through Flask's test client, which shares
this interpreter, its logging configuration and its handlers. That is exactly what makes them
unable to answer this question: gunicorn forks a worker, and logging set up in the parent is not
automatically what a worker ends up with. A stdout sink that works perfectly under Werkzeug and
silently emits nothing under gunicorn would pass the entire rest of the suite.

So this spawns the real thing -- the same `-c gunicorn.conf.py --worker-class gthread app:app`
shape the Dockerfile's ENTRYPOINT uses -- drives one audited request over a real socket, and looks
for the event in two places that fail independently: the child's own stdout, and the JSONL file.
FR-11a is the reason both matter. The documented deploy recreates the container and discards
stdout, so the file has to hold the history on its own; and AC-3 wants each sink to actually
deliver rather than one masking the other.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_ENDPOINT = "/api/rack-uptime"
CANONICAL_ACTION = "rack:uptime"

STARTUP_TIMEOUT_SEC = 45.0
#: Generous: the event has to be written by the worker and flushed to a file this process reads.
EVENT_TIMEOUT_SEC = 15.0

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "gunicorn.conf.py").is_file(),
    reason="gunicorn.conf.py absent; TC-35 verifies the production server's own config",
)


def _free_port() -> int:
    """Ask the OS for an unused port, then let go of it.

    A small race remains between releasing and gunicorn binding, which is why the readiness poll
    below distinguishes "not yet listening" from "the child exited".
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    """A gunicorn child, its captured stdout, and the audit file it was told to write."""

    def __init__(self, port: int, lines: list[str], audit_file: Path, proc, tls_cert: Path):
        self.port = port
        self.lines = lines
        self.audit_file = audit_file
        self.proc = proc
        # HTTPS-only server; verify against the certificate it generates (SAN has 127.0.0.1).
        # Read on first use: this object exists before gunicorn has loaded its config.
        self.tls_cert = tls_cert

    def post(self, path: str, body: dict) -> int:
        req = urllib.request.Request(
            f"https://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            tls = ssl.create_default_context(cafile=str(self.tls_cert))
            with urllib.request.urlopen(req, timeout=20, context=tls) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is still an answer, and the audit event is emitted either way.
            return int(exc.code)

    def audit_events(self) -> list[dict]:
        if not self.audit_file.is_file():
            return []
        return [
            json.loads(ln)
            for ln in self.audit_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def json_stdout_lines(self) -> list[dict]:
        """Only the lines that parse as JSON objects -- gunicorn's own access log does not."""
        out = []
        for raw in list(self.lines):
            stripped = raw.strip()
            if not stripped.startswith("{"):
                continue
            try:
                parsed = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    def audit_stdout_events(self) -> list[dict]:
        """Audit events found on stdout, unwrapped from the app-log envelope.

        Worth knowing operationally, and it is why this is a method rather than a dict lookup: on
        stdout the event is NESTED. With NVIS_LOG_FORMAT=json the line is the app-log envelope --
        {"ts": ..., "level": "INFO", "logger": "audit", "message": "{...the event...}"} -- so the
        event's own fields are one `json.loads` deeper. A collector that indexes `action` at the
        top level of a stdout line will find nothing, while the same field sits plainly at the top
        level in the JSONL file. Asserting the nesting here means a future change to the envelope
        shows up as a test failure rather than as a silently empty dashboard.
        """
        events = []
        for row in self.json_stdout_lines():
            if row.get("logger") != "audit":
                continue
            message = row.get("message")
            if not isinstance(message, str):
                continue
            try:
                inner = json.loads(message)
            except ValueError:
                continue
            if isinstance(inner, dict):
                events.append(inner)
        return events

    def wait_for(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False


@pytest.fixture
def gunicorn_server(tmp_path):
    """Start gunicorn on a loopback port, drain its output, and stop it afterwards."""
    port = _free_port()
    audit_file = tmp_path / "audit" / "audit.jsonl"

    env = dict(os.environ)
    env.update(
        {
            "RACK_STATUS_DEMO": "1",
            "RACK_EXEC_MODE": "local",
            # Both sinks on, so the two assertions below can fail independently (AC-3).
            "NVIS_LOG_SINKS": "stdout",
            "NVIS_LOG_FORMAT": "json",
            "NVIS_AUDIT_FILE": str(audit_file),
            "NVIS_AUDIT_ENABLED": "1",
            # A dry run keeps the handler off the wire; the audited path is unchanged.
            "RACK_UPTIME_DRY_RUN": "1",
            "NVIS_CATALOG_FILE": str(tmp_path / "catalog.json"),
            # The child is a real server, so `startup_gate.prepare()` runs in gunicorn's arbiter
            # and refuses to serve without an administrator, a policy and TLS. Pointed at this
            # test's own tmp paths, all three are bootstrapped on the spot — which is the behaviour
            # a first boot gets. Without them the child exited 1 before binding, and the failure
            # read as "gunicorn exited during startup" rather than as a missing prerequisite.
            "NVIS_IDENTITY_FILE": str(tmp_path / "identity" / "identity.json"),
            "RACK_WEB_POLICY_FILE": str(tmp_path / "store" / "policies.json"),
            "RACK_WEB_TLS_DIR": str(tmp_path / "tls"),
            "NVIS_RACKSTATUS_CONFIG": str(tmp_path / "config" / "rackstatus.toml"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    # A developer's own certificate variables must not decide what this server serves; with them
    # cleared it generates a pair under RACK_WEB_TLS_DIR, the first-boot shape.
    env.pop("RACK_WEB_TLS_CERT", None)
    env.pop("RACK_WEB_TLS_KEY", None)

    cmd = [
        sys.executable, "-m", "gunicorn",
        "-c", str(REPO_ROOT / "gunicorn.conf.py"),
        "--workers", "1",
        "--threads", "2",
        "--worker-class", "gthread",
        "--access-logfile", "-",
        "--pythonpath", str(REPO_ROOT / "backend"),
        "--bind", f"127.0.0.1:{port}",
        "app:app",
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    lines: list[str] = []

    def _drain() -> None:
        # A reader thread rather than communicate(): the child does not exit on its own, so a
        # blocking read to EOF would hang the suite instead of failing it.
        assert proc.stdout is not None
        lines.extend(proc.stdout)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    server = _Server(port, lines, audit_file, proc, tmp_path / "tls" / "server.crt")
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"gunicorn exited during startup with {proc.returncode}:\n"
                    + "".join(lines[-40:])
                )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    ready = True
                    break
            time.sleep(0.2)
        if not ready:
            pytest.fail(
                f"gunicorn did not listen on {port} within {STARTUP_TIMEOUT_SEC}s:\n"
                + "".join(lines[-40:])
            )
        yield server
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_audit_under_gunicorn(gunicorn_server):
    """TC-35: the audited event reaches BOTH the child's stdout and the JSONL file."""
    status = gunicorn_server.post(CANONICAL_ENDPOINT, {"rack": "p5-r01"})
    assert status < 500 or status == 500, f"unexpected transport failure: {status}"

    assert gunicorn_server.wait_for(
        lambda: any(
            e.get("action") == CANONICAL_ACTION for e in gunicorn_server.audit_events()
        ),
        EVENT_TIMEOUT_SEC,
    ), (
        "no audited event in the JSONL file under gunicorn. FR-11a: the file is what survives a "
        f"container recreate.\nstdout tail:\n{''.join(gunicorn_server.lines[-30:])}"
    )

    assert gunicorn_server.wait_for(
        lambda: any(
            e.get("action") == CANONICAL_ACTION for e in gunicorn_server.audit_stdout_events()
        ),
        EVENT_TIMEOUT_SEC,
    ), (
        "no JSON audit line on the child's stdout. This is the failure the in-process cases "
        "cannot see: logging configured in the parent is not automatically what a forked worker "
        f"has.\nstdout tail:\n{''.join(gunicorn_server.lines[-30:])}"
    )


def test_the_gunicorn_access_log_is_not_mistaken_for_an_audit_event(gunicorn_server):
    """Guard the guard: gunicorn logs every request too, and its lines are not JSON.

    Without this, the stdout assertion above could be satisfied by gunicorn's own access log and
    would prove nothing about the audit sink.
    """
    gunicorn_server.post(CANONICAL_ENDPOINT, {"rack": "p5-r01"})

    # Wait for the ACCESS-LOG line specifically. Waiting for "any JSON line" returned instantly,
    # because startup already emitted several -- so the assertion ran before the request's own log
    # line had been read, and failed on a race rather than on the property under test.
    assert gunicorn_server.wait_for(
        lambda: any(
            CANONICAL_ENDPOINT in ln and not ln.strip().startswith("{")
            for ln in list(gunicorn_server.lines)
        ),
        EVENT_TIMEOUT_SEC,
    ), "expected a non-JSON access-log line naming the path, so the two are distinguishable"

    gunicorn_server.wait_for(
        lambda: bool(gunicorn_server.audit_stdout_events()), EVENT_TIMEOUT_SEC
    )
    for row in gunicorn_server.audit_stdout_events():
        if row.get("action") == CANONICAL_ACTION:
            # The FR-5 field set, on the line that came out of a forked worker.
            for field in ("event_id", "ts", "principal", "result", "request_id"):
                assert field in row, f"{field} missing from the audit line under gunicorn"
            return
    pytest.fail("no audit line with the canonical action reached stdout")


def test_the_audit_file_holds_the_event_with_stdout_off(tmp_path):
    """AC-10f / FR-11a: retention must not depend on stdout being collected.

    Deliberately a separate server with `NVIS_LOG_SINKS=file`, because the deploy that motivated
    FR-11a discards stdout entirely -- so the interesting configuration is the one where stdout is
    not even a sink.
    """
    port = _free_port()
    audit_file = tmp_path / "audit" / "audit.jsonl"
    env = dict(os.environ)
    env.update(
        {
            "RACK_STATUS_DEMO": "1",
            "RACK_EXEC_MODE": "local",
            "NVIS_LOG_SINKS": "file",
            "NVIS_LOG_FILE": str(tmp_path / "app.log"),
            "NVIS_AUDIT_FILE": str(audit_file),
            "NVIS_AUDIT_ENABLED": "1",
            "RACK_UPTIME_DRY_RUN": "1",
            "NVIS_CATALOG_FILE": str(tmp_path / "catalog.json"),
            # The child is a real server, so `startup_gate.prepare()` runs in gunicorn's arbiter
            # and refuses to serve without an administrator, a policy and TLS. Pointed at this
            # test's own tmp paths, all three are bootstrapped on the spot — which is the behaviour
            # a first boot gets. Without them the child exited 1 before binding, and the failure
            # read as "gunicorn exited during startup" rather than as a missing prerequisite.
            "NVIS_IDENTITY_FILE": str(tmp_path / "identity" / "identity.json"),
            "RACK_WEB_POLICY_FILE": str(tmp_path / "store" / "policies.json"),
            "RACK_WEB_TLS_DIR": str(tmp_path / "tls"),
            "NVIS_RACKSTATUS_CONFIG": str(tmp_path / "config" / "rackstatus.toml"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("RACK_WEB_TLS_CERT", None)
    env.pop("RACK_WEB_TLS_KEY", None)
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "gunicorn", "-c", str(REPO_ROOT / "gunicorn.conf.py"),
            "--workers", "1", "--worker-class", "gthread",
            "--pythonpath", str(REPO_ROOT / "backend"),
            "--bind", f"127.0.0.1:{port}", "app:app",
        ],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        server = _Server(port, [], audit_file, proc, tmp_path / "tls" / "server.crt")
        deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"gunicorn exited with {proc.returncode}")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        server.post(CANONICAL_ENDPOINT, {"rack": "p5-r01"})
        assert server.wait_for(
            lambda: any(e.get("action") == CANONICAL_ACTION for e in server.audit_events()),
            EVENT_TIMEOUT_SEC,
        ), "the audit file must hold the event even with stdout not selected as a sink"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
