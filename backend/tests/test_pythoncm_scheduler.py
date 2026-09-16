# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""PythonCM scheduler and its out-of-process helper protocol."""
from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import pythoncm_broker_client
from schedulers import JobHandle, JobSpec, PythonCMScheduler, build_scheduler

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "nvis_pythoncm_exec.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("nvis_pythoncm_exec_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


class FakeConnection:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.statuses = ["RUNNING", "COMPLETED"]
        self.writes: list[tuple[str, bytes]] = []

    def run(self, cmd, timeout=120, profile="control"):
        self.calls.append(cmd)
        action = cmd[2]
        if action == "submit":
            body = {
                "ok": True,
                "task_id": "private-task-id",
                "pythoncm_id": "bd90bf76-82b9-4c43-a792-598648c192ed",
            }
        elif action == "status":
            body = {"ok": True, "state": self.statuses.pop(0)}
        elif action == "result":
            body = {
                "ok": True,
                "state": "COMPLETED",
                "returncode": 0,
                "stdout_b64": base64.b64encode(b"n1: pass\n").decode(),
                "stderr_b64": "",
                "output": "/logs/pythoncm-%j.out",
            }
        elif action == "cancel":
            body = {"ok": True, "state": "CANCELLED"}
        elif action == "probe":
            body = {"ok": True, "ready": True}
        else:  # pragma: no cover - test fake guard
            raise AssertionError(action)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body) + "\n", "")

    def write_bytes(self, path, data):
        self.writes.append((path, data))


def _payload_from_submit(call: list[str]) -> dict:
    encoded = call[call.index("--payload") + 1]
    return json.loads(base64.urlsafe_b64decode(encoded).decode())


def test_submit_uses_exact_nodes_and_returns_programrunner_uuid():
    conn = FakeConnection()
    scheduler = PythonCMScheduler(
        conn,
        env={"RACK_REMOTE_BASE_PATH": "/srv/rack-status"},
    )
    handle = scheduler.submit(JobSpec(
        name="rack-check",
        nodelist="node[01-02]",
        nodes=2,
        ntasks_per_node=4,
        command="hostname",
        env={"TEST_FLAG": "yes"},
    ))

    assert handle.job_id == "bd90bf76-82b9-4c43-a792-598648c192ed"
    assert handle.kind == "pythoncm"
    payload = _payload_from_submit(conn.calls[0])
    assert payload["nodelist"] == "node[01-02]"
    assert payload["nodes"] == 2
    assert payload["ntasks_per_node"] == 4
    assert payload["command"] == "hostname"
    assert payload["env"] == {"TEST_FLAG": "yes"}
    assert payload["jobs_dir"] == "/srv/rack-status/var/pythoncm-jobs"


def test_multi_node_submit_requires_exact_nodelist():
    scheduler = PythonCMScheduler(FakeConnection(), env={})
    with pytest.raises(ValueError, match="exact nodelist"):
        scheduler.submit(JobSpec(command="hostname", nodes=2))


def test_wait_collects_logs_and_writes_requested_output():
    conn = FakeConnection()
    scheduler = PythonCMScheduler(conn, env={})
    handle = scheduler.submit(JobSpec(command="hostname"))

    result = scheduler.wait(handle, timeout=2, poll_interval=0.01)

    assert result.state == "COMPLETED"
    assert result.returncode == 0
    assert result.stdout == "n1: pass\n"
    assert conn.writes == [
        (f"/logs/pythoncm-{handle.job_id}.out", b"n1: pass\n")
    ]


def test_cancel_uses_persistent_programrunner_handle():
    conn = FakeConnection()
    scheduler = PythonCMScheduler(conn, env={})
    handle = JobHandle("bd90bf76-82b9-4c43-a792-598648c192ed", "pythoncm")
    scheduler.cancel(handle)
    assert conn.calls[0][2] == "cancel"
    assert conn.calls[0][-1] == handle.job_id


def test_factory_selects_pythoncm():
    assert build_scheduler("pythoncm", connection=FakeConnection(), env={}).kind == "pythoncm"


def test_credentials_use_file_paths_not_raw_secret_argv():
    scheduler = PythonCMScheduler(FakeConnection(), env={
        "NVIS_BCM_USERNAME": "operator",
        "NVIS_BCM_PASSWORD": "must-not-appear",
        "NVIS_BCM_PASSWORD_FILE": "/run/secrets/bcm-password",
        "NVIS_BCM_COOKIE": "also-must-not-appear",
        "NVIS_BCM_COOKIE_FILE": "/run/secrets/bcm-cookie",
    })
    argv = scheduler._helper_argv("probe")
    assert "operator" in argv
    assert "/run/secrets/bcm-password" in argv
    assert "/run/secrets/bcm-cookie" in argv
    assert "must-not-appear" not in argv
    assert "also-must-not-appear" not in argv


def test_probe_is_non_mutating_and_uses_the_execution_helper():
    conn = FakeConnection()
    scheduler = PythonCMScheduler(conn, env={})

    result = scheduler.probe(timeout=7)

    assert result == {"ok": True, "ready": True}
    assert [call[2] for call in conn.calls] == ["probe"]


def test_submit_error_has_cookie_readiness_hint():
    class DeniedConnection(FakeConnection):
        def run(self, cmd, timeout=120, profile="control"):
            body = {
                "ok": False,
                "error": "PythonCM execution needs NVIS_BCM_COOKIE",
                "hint": "configure a BCM login/cookie with cmproc permission",
            }
            return subprocess.CompletedProcess(cmd, 2, json.dumps(body), "")

    scheduler = PythonCMScheduler(DeniedConnection(), env={})
    with pytest.raises(RuntimeError, match="cmproc permission"):
        scheduler.submit(JobSpec(command="hostname"))


def test_scheduler_routes_submit_through_persistent_broker(monkeypatch):
    conn = FakeConnection()
    seen = {}

    def broker_request(path, service, argv, *, timeout):
        seen.update(path=path, service=service, argv=argv, timeout=timeout)
        return {
            "ok": True,
            "task_id": "private-task-id",
            "pythoncm_id": "bd90bf76-82b9-4c43-a792-598648c192ed",
            "transport": "persistent-pythoncm",
        }

    monkeypatch.setattr(pythoncm_broker_client, "request", broker_request)
    scheduler = PythonCMScheduler(
        conn, env={"NVIS_PYTHONCM_SOCKET": "/run/nvis-pythoncm/query.sock"}
    )
    handle = scheduler.submit(JobSpec(command="hostname"))

    assert handle.job_id == "bd90bf76-82b9-4c43-a792-598648c192ed"
    assert conn.calls == []
    assert seen["service"] == "exec"
    assert seen["argv"][0] == "submit"


def test_scheduler_prefers_dedicated_exec_broker(monkeypatch):
    seen = {}

    def broker_request(path, service, argv, *, timeout):
        seen.update(path=path, service=service)
        return {
            "ok": True,
            "task_id": "private-task-id",
            "pythoncm_id": "bd90bf76-82b9-4c43-a792-598648c192ed",
        }

    monkeypatch.setattr(pythoncm_broker_client, "request", broker_request)
    scheduler = PythonCMScheduler(FakeConnection(), env={
        "NVIS_PYTHONCM_SOCKET": "/run/nvis-pythoncm/query.sock",
        "NVIS_PYTHONCM_EXEC_SOCKET": "/run/nvis-pythoncm/exec.sock",
    })
    scheduler.submit(JobSpec(command="hostname"))

    assert seen == {"path": "/run/nvis-pythoncm/exec.sock", "service": "exec"}


def test_accepted_broker_submit_failure_is_never_replayed(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(
        pythoncm_broker_client,
        "request",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad response")),
    )
    scheduler = PythonCMScheduler(
        conn, env={"NVIS_PYTHONCM_SOCKET": "/run/nvis-pythoncm/query.sock"}
    )
    with pytest.raises(RuntimeError, match="persistent PythonCM submit broker failed"):
        scheduler.submit(JobSpec(command="hostname"))
    assert conn.calls == []


def test_helper_expands_bracketed_exact_nodelists():
    assert helper._expand_nodelist("node[01-03,07],other9") == [
        "node01", "node02", "node03", "node07", "other9",
    ]


def test_helper_resolves_every_node_or_reports_missing():
    class Node:
        def __init__(self, name):
            self.hostname = name

    class Cluster:
        def get_by_name(self, name):
            return None if name == "node02" else Node(name)

    nodes, missing = helper._resolve_nodes(Cluster(), "node[01-03]")
    assert [node.hostname for node in nodes] == ["node01", "node03"]
    assert missing == ["node02"]


def test_helper_active_status_uses_uuid_objects(monkeypatch):
    from uuid import UUID

    node_id = "d14a4f0a-aa34-459d-9456-67fb99421868"

    class Status:
        def get_state(self, node):
            assert isinstance(node, UUID)
            return 0, "Running"

    class Parallel:
        def active_command_status(self, ids):
            assert isinstance(ids[0], UUID)
            return [Status()]

    class Cluster:
        parallel = Parallel()

        def disconnect(self):
            pass

    monkeypatch.setattr(helper, "_cluster", Cluster)
    assert helper._active_status({
        "pythoncm_id": "bd90bf76-82b9-4c43-a792-598648c192ed",
        "node_uuids": [node_id],
    }) == "RUNNING"


def test_helper_wrapper_captures_output_and_exit_code(tmp_path):
    import subprocess

    command = helper._wrapped_command(
        "printf 'pass\\n'; printf 'warn\\n' >&2; exit 7", tmp_path,
    )
    proc = subprocess.run(
        ["bash", "-lc", command], capture_output=True, text=True, check=False,
    )
    node = subprocess.check_output(["hostname", "-s"], text=True).strip()
    assert proc.returncode == 7
    assert proc.stdout == "" and proc.stderr == ""
    assert (tmp_path / f"{node}.stdout").read_text() == "pass\n"
    assert (tmp_path / f"{node}.stderr").read_text() == "warn\n"
    assert (tmp_path / f"{node}.status").read_text() == "7\n"


def test_helper_requires_cookie_credentials_for_cmproc(monkeypatch, capsys):
    monkeypatch.setattr(helper, "_cluster", lambda: (_ for _ in ()).throw(
        PermissionError("PythonCM execution needs a cookie")
    ))
    rc = helper.main(["probe"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert "cookie" in payload["error"]
    assert "cmproc permission" in payload["hint"]


def test_helper_reuses_caller_owned_cluster():
    class Parallel:
        @staticmethod
        def active_command_status():
            return []

    class Cluster:
        parallel = Parallel()

        def disconnect(self):
            raise AssertionError("broker-owned cluster must stay connected")

    args = helper.build_parser().parse_args(["probe"])
    assert helper.execute(args, cluster=Cluster()) == {"ok": True, "ready": True}


def test_helper_reads_secret_file(tmp_path):
    secret = tmp_path / "cookie"
    secret.write_text("cookie-value\n")
    helper._CONNECTION_OPTIONS = {"cookie_file": str(secret)}
    assert helper._secret("cookie_file", "UNUSED_TEST_SECRET") == "cookie-value"
