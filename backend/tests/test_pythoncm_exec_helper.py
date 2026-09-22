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
"""The head-side helper's pure logic, which is where a silent wrong answer would come from.

`bin/scripts/nvis_pythoncm_exec.py` runs on a BCM head and imports `pythoncm`, so the
CMDaemon calls cannot be exercised here. Everything around them can, and the parts worth
pinning are the ones that would be *wrong rather than broken*: expanding a node list,
deciding a job's state from what is on disk, and the wrapper that isolates per-node output.

The module is loaded by path because `bin/scripts/` is deployed to the head, not importable
as a package from the backend.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "nvis_pythoncm_exec.py"


def _load():
    spec = importlib.util.spec_from_file_location("nvis_pythoncm_exec", _HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load()


class TestExpandingANodeList:
    @pytest.mark.parametrize("nodelist,expected", [
        ("n01", ["n01"]),
        ("n01,n02", ["n01", "n02"]),
        ("n[01-03]", ["n01", "n02", "n03"]),
        ("n[1-3]", ["n1", "n2", "n3"]),
        ("n[01-02],n07", ["n01", "n02", "n07"]),
        ("rack[01-02]-dgx", ["rack01-dgx", "rack02-dgx"]),
        ("n[01,03-04]", ["n01", "n03", "n04"]),
        ("", []),
    ])
    def test_slurm_bracket_notation(self, nodelist, expected):
        # Written out rather than shelled to `scontrol show hostnames`, because that binary
        # is precisely what a cluster running this provider does not have.
        assert helper.expand_nodelist(nodelist) == expected

    def test_zero_padding_comes_from_the_literal_the_site_wrote(self):
        # n01 and n1 are different hostnames; inferring the width would invent one of them.
        assert helper.expand_nodelist("n[8-10]") == ["n8", "n9", "n10"]
        assert helper.expand_nodelist("n[008-010]") == ["n008", "n009", "n010"]

    def test_a_repeated_node_is_collapsed(self):
        # Running the command twice on one node is never what the caller meant.
        assert helper.expand_nodelist("n01,n[01-02]") == ["n01", "n02"]


class _Manifests:
    """Shared task-directory fixtures for the state tests."""

    @staticmethod
    def _manifest(tmp_path, nodes):
        manifest = {
            "task_id": "t1", "pythoncm_id": "p1",
            "jobs_dir": str(tmp_path), "nodes": nodes,
        }
        (tmp_path / "t1").mkdir(parents=True, exist_ok=True)
        return manifest

    @staticmethod
    def _finish(tmp_path, node, rc):
        (tmp_path / "t1" / f"{node}.status").write_text(str(rc), encoding="utf-8")


class _Gone:
    """A cluster that has already forgotten the command, which is the normal case."""

    class parallel:
        @staticmethod
        def active_command_status(ids=None):
            return []


class _Running:
    """A cluster still reporting the command."""

    class parallel:
        @staticmethod
        def active_command_status(ids=None):
            return [type("S", (), {"unique_key": "p1", "status": "RUNNING"})()]


class TestDerivingStateFromWhatIsOnDisk(_Manifests):
    """CMDaemon drops a finished command from its active status, so the files decide."""

    def test_every_node_exiting_zero_is_completed(self, tmp_path):
        m = self._manifest(tmp_path, ["n01", "n02"])
        self._finish(tmp_path, "n01", 0)
        self._finish(tmp_path, "n02", 0)
        assert helper._derive_state(lambda: _Gone, m) == ("COMPLETED", True)

    def test_one_node_failing_fails_the_job(self, tmp_path):
        m = self._manifest(tmp_path, ["n01", "n02"])
        self._finish(tmp_path, "n01", 0)
        self._finish(tmp_path, "n02", 3)
        assert helper._derive_state(lambda: _Gone, m) == ("FAILED", True)

    def test_a_node_that_never_wrote_a_status_is_a_failure_not_still_running(self, tmp_path):
        # Gone from CMDaemon but missing a status file means the command died. Calling that
        # RUNNING is what would make a caller poll uselessly until its whole budget expired.
        m = self._manifest(tmp_path, ["n01", "n02"])
        self._finish(tmp_path, "n01", 0)
        assert helper._derive_state(lambda: _Gone, m) == ("FAILED", True)

    def test_a_recorded_cancel_wins_over_the_files(self, tmp_path):
        m = self._manifest(tmp_path, ["n01"])
        m["cancelled_at"] = "2026-09-20T00:00:00+00:00"
        assert helper._derive_state(lambda: _Gone, m) == ("CANCELLED", True)

    def test_the_worst_node_sets_the_aggregate_return_code(self, tmp_path):
        m = self._manifest(tmp_path, ["n01", "n02"])
        self._finish(tmp_path, "n01", 0)
        self._finish(tmp_path, "n02", 7)
        assert helper._result(lambda: _Gone, m)["returncode"] == 7


class TestAnsweringWithoutConnecting(_Manifests):
    """Why this exists: a fresh `pythoncm` client costs ~10 CPU-seconds on a big controller.

    Paying that on every five-second poll would spend more of the head node's CPU watching
    an hour-long job than running it. Most polls can be answered from the files the work
    itself wrote, and these pin which ones can and cannot.
    """

    def test_a_finished_job_is_answered_from_disk_with_no_connection(self, tmp_path):
        def explode():
            raise AssertionError("the files already prove the answer; do not connect")

        m = self._manifest(tmp_path, ["n01"])
        self._finish(tmp_path, "n01", 0)
        assert helper._derive_state(explode, m) == ("COMPLETED", True)

    def test_a_terminal_answer_from_disk_is_authoritative_not_a_guess(self, tmp_path):
        # The status file is written last and by rename, so its existence means that node
        # finished. That is proof, not an inference, which is why the cheap path may end a
        # wait on its own.
        m = self._manifest(tmp_path, ["n01", "n02"])
        self._finish(tmp_path, "n01", 0)
        self._finish(tmp_path, "n02", 4)
        state, authoritative = helper._derive_state(None, m)
        assert (state, authoritative) == ("FAILED", True)

    def test_an_unfinished_job_offline_is_a_flagged_guess(self, tmp_path):
        # Offline, a job still running and a job whose ranks died are indistinguishable.
        # Reporting RUNNING keeps the caller waiting; the flag is what stops it concluding.
        m = self._manifest(tmp_path, ["n01", "n02"])
        self._finish(tmp_path, "n01", 0)
        assert helper._derive_state(None, m) == ("RUNNING", False)

    def test_the_same_state_online_is_resolved_to_the_truth(self, tmp_path):
        # The verification poll: CMDaemon has forgotten it, so the guess above was wrong and
        # the job is actually dead.
        m = self._manifest(tmp_path, ["n01", "n02"])
        self._finish(tmp_path, "n01", 0)
        assert helper._derive_state(lambda: _Gone, m) == ("FAILED", True)
        assert helper._derive_state(lambda: _Running, m) == ("RUNNING", True)

    def test_a_head_mode_run_cannot_be_concluded_offline(self, tmp_path):
        # No nodes means no status files, so disk has nothing to prove either way.
        m = self._manifest(tmp_path, [])
        assert helper._derive_state(None, m) == ("RUNNING", False)
        assert helper._derive_state(lambda: _Gone, m) == ("COMPLETED", True)

    def test_the_cli_builds_no_client_for_an_offline_status(self, tmp_path, monkeypatch):
        # The end-to-end guarantee: `--offline` must not reach `_cluster` at all, since
        # constructing it is the entire cost being avoided.
        def explode(_args):
            raise AssertionError("--offline must not construct a pythoncm client")

        monkeypatch.setattr(helper, "_cluster", explode)
        m = self._manifest(tmp_path, ["n01"])
        self._finish(tmp_path, "n01", 0)
        helper._write_manifest(str(tmp_path), "t1", m)
        args = helper.build_parser().parse_args(
            ["status", "--jobs-dir", str(tmp_path), "--task-id", "t1", "--offline"]
        )
        assert helper.execute(args)["state"] == "COMPLETED"

    def test_a_cancel_is_never_offline(self, tmp_path, monkeypatch):
        # A cancel that did not reach CMDaemon has not cancelled anything, and the leftover
        # ranks are what drain the next job's rack.
        reached = []
        monkeypatch.setattr(helper, "_cluster", lambda _a: reached.append(1) or _Gone)
        m = self._manifest(tmp_path, ["n01"])
        helper._write_manifest(str(tmp_path), "t1", m)
        args = helper.build_parser().parse_args(
            ["cancel", "--jobs-dir", str(tmp_path), "--task-id", "t1", "--offline"]
        )
        helper.execute(args)
        assert reached == [1]


class TestThePerNodeWrapper:
    def test_each_node_leaves_its_own_stdout_stderr_and_status(self, tmp_path):
        # ProgramRunner returns one aggregate stream for the whole fan-out, which cannot say
        # which of eighteen nodes failed. This is the mechanism that recovers that.
        wrapped = helper._wrapped_command("echo hi; echo oops >&2; exit 5", str(tmp_path))
        proc = subprocess.run(wrapped, shell=True, capture_output=True, text=True, check=False)
        host = subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, check=False
        ).stdout.strip()
        assert proc.returncode == 5
        assert (tmp_path / f"{host}.stdout").read_text().strip() == "hi"
        assert (tmp_path / f"{host}.stderr").read_text().strip() == "oops"
        assert (tmp_path / f"{host}.status").read_text().strip() == "5"

    def test_a_command_with_quotes_is_not_mangled_by_the_wrapper(self, tmp_path):
        wrapped = helper._wrapped_command("""printf '%s\\n' "a 'b' c" """, str(tmp_path))
        proc = subprocess.run(wrapped, shell=True, capture_output=True, text=True, check=False)
        assert proc.returncode == 0

    def test_a_command_that_calls_exit_still_records_its_status(self, tmp_path):
        # A brace group would run `exit` in the wrapper's own shell and record nothing, so
        # every script ending in `exit 0` would look like a node that never finished.
        wrapped = helper._wrapped_command("echo done; exit 0", str(tmp_path))
        subprocess.run(wrapped, shell=True, capture_output=True, text=True, check=False)
        assert [p.name for p in tmp_path.glob("*.status")], "no status file was written"

    def test_the_status_file_appears_only_once_the_node_has_finished(self, tmp_path):
        # Written by rename, so a reader never sees a half-written status and concludes the
        # node exited zero.
        assert ".status.tmp" in helper._wrapped_command("true", str(tmp_path))
        assert "mv " in helper._wrapped_command("true", str(tmp_path))


class TestTheContractWithTheCaller:
    def test_a_failure_is_still_one_json_object_and_exit_two(self):
        # The caller parses the last JSON line and nothing else, so an unhandled traceback
        # on stdout would read as a corrupt answer rather than a failure.
        proc = subprocess.run(
            [sys.executable, str(_HELPER), "status", "--task-id", "nope",
             "--jobs-dir", "/nonexistent"],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 2
        body = json.loads(proc.stdout.strip().splitlines()[-1])
        assert body["ok"] is False and body["error"]

    def test_the_actions_it_accepts_are_the_ones_the_provider_sends(self):
        # The two halves are deployed separately -- the provider ships in the image, the
        # helper is rsynced to the head -- so nothing but a test keeps them in step.
        assert set(helper.build_parser()._actions[1].choices) == {
            "submit", "status", "cancel", "result", "probe",
        }

    @pytest.mark.parametrize("mode,expected", [
        ("fanout", ["n01", "n02", "n03"]),   # every node
        ("leader", ["n01"]),                  # an MPI launcher fans out itself
        ("head", []),                         # empty means CMDaemon's active head
    ])
    def test_every_launch_mode_the_provider_allows_is_placed_somewhere(self, mode, expected,
                                                                       tmp_path):
        placed = {}

        class Cluster:
            class parallel:
                @staticmethod
                def execute(nodes, **_kw):
                    placed["nodes"] = nodes
                    return type("S", (), {"unique_key": "p1"})()

        from schedulers.pythoncm import LAUNCH_MODES
        assert mode in LAUNCH_MODES

        helper._submit(Cluster, {
            "command": "true", "nodelist": "n[01-03]", "launch_mode": mode,
            "jobs_dir": str(tmp_path), "timeout": 60,
        })
        assert placed["nodes"] == expected

    def test_a_credential_file_is_read_and_stripped(self, tmp_path):
        secret = tmp_path / "bcm.pw"
        secret.write_text("  s3cret\n", encoding="utf-8")
        assert helper._secret(str(secret), "UNUSED") == "s3cret"

    def test_a_missing_credential_file_says_which_one(self, tmp_path):
        with pytest.raises(RuntimeError, match="cannot read credential file"):
            helper._secret(str(tmp_path / "absent"), "UNUSED")
