# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The PythonCM provider, and the wire it speaks to the helper on the head.

`pythoncm` cannot be imported here -- it only exists on a BCM head -- so the subject is the
half we own: the argv built, the payload encoded, the JSON decoded, and the refusals. A fake
connection stands in for the head and answers the way the helper is specified to.

The helper's own pure functions are covered in `test_pythoncm_exec_helper.py`.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedulers.base import JobSpec
from schedulers.factory import build_scheduler
from schedulers.pythoncm import PythonCMScheduler

PYTHONCM_ID = "3f2b6d1e-0000-4c4a-9f01-aaaabbbbcccc"


class FakeHead:
    """Stands in for the head node, answering as the helper is specified to."""

    def __init__(self, *, states=None, submit_error=None, banner=""):
        self.calls: list[list[str]] = []
        self.states = list(states or ["COMPLETED"])
        self.submit_error = submit_error
        self.banner = banner

    def run(self, cmd, timeout=120, profile=None, **_kw):
        self.calls.append(list(cmd))
        action = cmd[2]
        if action == "probe":
            body = {"ok": True, "ready": True}
        elif action == "submit":
            body = self.submit_error or {
                "ok": True, "task_id": "task-1", "pythoncm_id": PYTHONCM_ID, "nodes": ["n01"],
            }
        elif action == "status":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            body = {"ok": True, "task_id": "task-1", "state": state}
        elif action == "cancel":
            body = {"ok": True, "task_id": "task-1", "state": "CANCELLED"}
        else:
            body = {
                "ok": True, "task_id": "task-1", "state": "COMPLETED", "returncode": 0,
                "stdout_b64": base64.b64encode(b"done").decode(),
                "stderr_b64": "", "output": "",
                "nodes": [{"node": "n01", "returncode": 0, "finished": True}],
            }
        stdout = self.banner + json.dumps(body)
        code = 0 if body.get("ok") else 2
        return subprocess.CompletedProcess(cmd, code, stdout + "\n", "")

    def actions(self) -> list[str]:
        return [c[2] for c in self.calls]

    def payload(self) -> dict:
        for call in self.calls:
            if call[2] == "submit":
                return json.loads(base64.urlsafe_b64decode(call[call.index("--payload") + 1]))
        raise AssertionError("no submit call was made")


@pytest.fixture
def head(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_PYTHONCM_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    return FakeHead()


def _sched(conn) -> PythonCMScheduler:
    return PythonCMScheduler(conn, platform=None, profile="control")


class TestTheHandleIsTheOneAnOperatorCanLookUp:
    def test_submit_returns_the_programrunner_id_not_our_private_task_id(self, head):
        handle = _sched(head).submit(JobSpec(name="j", nodelist="n01", command="true"))
        # The private task id is carried alongside, but the handle an operator sees is the
        # one BCM's own tooling knows about.
        assert handle.job_id == PYTHONCM_ID
        assert json.loads(handle.submit_output)["task_id"] == "task-1"

    def test_status_and_cancel_pass_that_same_id_back(self, head):
        sched = _sched(head)
        handle = sched.submit(JobSpec(name="j", nodelist="n01", command="true"))
        sched.status(handle)
        sched.cancel(handle)
        for call in head.calls[1:]:
            assert call[call.index("--task-id") + 1] == PYTHONCM_ID


class TestWhatCrossesTheWire:
    def test_the_payload_carries_launch_mode_and_execution_user(self, head):
        _sched(head).submit(JobSpec(
            name="j", nodelist="n[01-03]", command="hostname",
            launch_mode="leader", execution_user="cmods",
        ))
        body = head.payload()
        assert body["launch_mode"] == "leader"
        assert body["execution_user"] == "cmods"
        assert body["nodelist"] == "n[01-03]"

    def test_a_command_with_shell_metacharacters_survives_the_trip(self, head):
        nasty = "echo 'a b' && printf \"%s\\n\" $HOME | grep -v x"
        _sched(head).submit(JobSpec(name="j", nodelist="n01", command=nasty))
        # Base64 is why: the command goes through a login shell on the head untouched.
        assert head.payload()["command"] == nasty

    def test_a_login_banner_before_the_json_does_not_corrupt_the_answer(self, monkeypatch):
        # Heads print MOTDs and module-load chatter that is not ours to suppress, so the
        # parser takes the LAST JSON line rather than the first.
        noisy = FakeHead(banner="Welcome to bcm-head-01\nLoading module slurm\n{not json}\n")
        monkeypatch.setenv("RACK_PYTHONCM_JOBS_DIR", "/tmp/j")
        assert _sched(noisy).submit(JobSpec(name="j", nodelist="n01", command="true")).job_id


class TestCredentialsNeverReachArgv:
    def test_only_paths_are_passed_secrets_are_read_by_the_helper(self, head, monkeypatch):
        monkeypatch.setenv("NVIS_BCM_PASSWORD_FILE", "/etc/nvis/bcm.pw")
        monkeypatch.setenv("NVIS_BCM_PASSWORD", "hunter2-do-not-leak")
        monkeypatch.setenv("NVIS_BCM_COOKIE", "cookie-do-not-leak")
        monkeypatch.setenv("NVIS_BCM_USERNAME", "cmods")
        _sched(head).probe()
        argv = " ".join(head.calls[0])
        # argv is world-readable in `ps` on a shared head node, so the value must not be
        # there -- only the path to it, and the non-secret username.
        assert "/etc/nvis/bcm.pw" in argv
        assert "cmods" in argv
        assert "hunter2-do-not-leak" not in argv
        assert "cookie-do-not-leak" not in argv


class TestItRefusesWhatItCannotHonour:
    def test_multi_node_without_a_nodelist_is_refused_not_guessed(self, head):
        # There is no queue to allocate from, so "give me 3 nodes" has no meaning.
        with pytest.raises(ValueError, match="nodelist"):
            _sched(head).submit(JobSpec(name="j", nodes=3, command="true"))
        assert head.calls == []

    def test_an_unknown_launch_mode_is_refused_before_the_head_is_touched(self, head):
        with pytest.raises(ValueError, match="launch_mode"):
            _sched(head).submit(JobSpec(name="j", nodelist="n01", command="x", launch_mode="all"))
        assert head.calls == []

    def test_neither_script_nor_command_is_refused(self, head):
        with pytest.raises(ValueError, match="script or command"):
            _sched(head).submit(JobSpec(name="j", nodelist="n01"))

    def test_a_helper_error_surfaces_its_hint(self, monkeypatch):
        failing = FakeHead(submit_error={
            "ok": False, "error": "PermissionError: denied",
            "hint": "check the cert/key pair or cookie file",
        })
        monkeypatch.setenv("RACK_PYTHONCM_JOBS_DIR", "/tmp/j")
        with pytest.raises(RuntimeError, match="cert/key pair"):
            _sched(failing).submit(JobSpec(name="j", nodelist="n01", command="true"))


class TestTimeoutEndsTheWorkNotJustTheWait:
    def test_wait_cancels_when_the_budget_expires(self, head, monkeypatch):
        # Leftover ProgramRunner processes are what the next job's BCM prolog flags as rogue,
        # draining the rack. Giving up on waiting must therefore stop the work.
        stuck = FakeHead(states=["RUNNING"])
        sched = _sched(stuck)
        handle = sched.submit(JobSpec(name="j", nodelist="n01", command="sleep 999"))
        clock = iter([0.0, 0.0] + [10_000.0] * 50)
        monkeypatch.setattr("time.monotonic", lambda: next(clock))
        result = sched.wait(handle, timeout=1)
        assert result.state == "TIMEOUT"
        assert "cancel" in stuck.actions()

    def test_run_also_cancels_a_provider_that_reports_timeout(self, head):
        # The base class carries this, so every provider gets it, not just this one.
        class TimingOut(PythonCMScheduler):
            def wait(self, handle, **_kw):
                from schedulers.base import JobResult
                return JobResult(returncode=1, state="TIMEOUT", job_id=handle.job_id)

        TimingOut(head).run(JobSpec(name="j", nodelist="n01", command="true"))
        assert "cancel" in head.actions()


class TestWaitingIsCheap:
    """A fresh `pythoncm` client costs ~10 CPU-seconds, so polls must not each build one.

    This is the reason we declined to port the fork's broker daemon
    (`docs/explanation/why-no-pythoncm-broker.md`): the cost only compounds on the polling
    path, and the polling path is the one that can answer from durable files instead.
    """

    def test_most_polls_are_offline(self, head):
        # Driven by state rather than a fake clock: `time.monotonic` is shared, and
        # `script_tree`'s cache reads it too, so a scripted clock measures the wrong thing.
        running_for = 25
        stuck = FakeHead(states=["RUNNING"] * running_for + ["COMPLETED"])
        sched = _sched(stuck)
        sched.wait(
            sched.submit(JobSpec(name="j", nodelist="n01", command="sleep 60")), timeout=3600
        )

        polls = [c for c in stuck.calls if c[2] == "status"]
        offline = [c for c in polls if "--offline" in c]
        assert len(polls) == running_for + 1
        # One authoritative check per twelve polls, not one per poll.
        assert len(offline) == len(polls) - len(polls) // 12

    def test_the_helper_path_is_resolved_once_not_per_poll(self, head, monkeypatch):
        # `script_tree` caches its layer probes for 30s, so re-resolving inside a long wait
        # would put remote directory probes back in the loop -- and could switch which
        # project's copy of the helper a running job is polled with.
        resolutions = []
        import paths

        real = paths._script
        monkeypatch.setattr(
            paths, "_script", lambda *a, **k: resolutions.append(a[0]) or real(*a, **k)
        )
        sched = _sched(FakeHead(states=["RUNNING"] * 5 + ["COMPLETED"]))
        sched.wait(sched.submit(JobSpec(name="j", nodelist="n01", command="x")), timeout=3600)
        assert resolutions.count("nvis_pythoncm_exec.py") == 1

    def test_a_terminal_state_from_an_offline_poll_still_ends_the_wait(self, head):
        # The cheap path must be able to finish on its own, or it saves nothing: a job that
        # completes between verification polls would otherwise wait for the next one.
        done = FakeHead(states=["COMPLETED"])
        sched = _sched(done)
        result = sched.wait(
            sched.submit(JobSpec(name="j", nodelist="n01", command="true")), timeout=60
        )
        assert result.state == "COMPLETED"
        assert "--offline" in next(c for c in done.calls if c[2] == "status")

    def test_an_explicit_status_call_is_always_authoritative(self, head):
        # `status()` is the public one-shot question, asked by callers that are about to act
        # on the answer. It must never return the offline guess.
        sched = _sched(head)
        handle = sched.submit(JobSpec(name="j", nodelist="n01", command="true"))
        sched.status(handle)
        assert "--offline" not in next(c for c in head.calls if c[2] == "status")


class TestItIsAFirstClassProvider:
    def test_the_factory_builds_it_by_name(self, head):
        assert build_scheduler("pythoncm", connection=head).kind == "pythoncm"

    def test_slurm_is_still_the_default(self, head, monkeypatch):
        monkeypatch.delenv("RACK_SCHEDULER", raising=False)
        assert build_scheduler(connection=head).kind == "slurm"

    def test_the_helper_is_resolved_through_the_script_tree(self, head):
        # Not off RACK_REMOTE_BASE_PATH: two resolvers that disagree is how the API and a
        # workflow step came to run different copies of the same script.
        _sched(head).probe()
        assert head.calls[0][1].endswith("nvis_pythoncm_exec.py")

    def test_probe_touches_nothing(self, head):
        _sched(head).probe()
        assert head.actions() == ["probe"]

    def test_a_result_carries_per_node_outcomes(self, head):
        sched = _sched(head)
        result = sched.run(JobSpec(name="j", nodelist="n01", command="true"))
        # One aggregate exit code cannot say which of eighteen nodes failed.
        assert result.node_results == [{"node": "n01", "returncode": 0, "finished": True}]
        assert result.stdout == "done"
