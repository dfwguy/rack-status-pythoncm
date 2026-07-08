"""Unit tests for the Scheduler abstraction (Slurm/Local backends + stubs + factory)."""
import subprocess
from unittest import mock

import pytest

from schedulers import (
    JobSpec,
    LocalScheduler,
    RunAIScheduler,
    SlurmScheduler,
    build_scheduler,
)


class FakeConn:
    """Records commands and returns canned CompletedProcess by substring match."""

    def __init__(self, responses=None):
        self.calls: list[str] = []
        self.responses = responses or {}

    def run(self, cmd, timeout=120, profile="control", **kw):
        self.calls.append(cmd)
        for key, (rc, out, err) in self.responses.items():
            if key in cmd:
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 0, "", "")


class TestSlurmScheduler:
    def test_submit_builds_argv_and_parses_jobid(self):
        conn = FakeConn({"sbatch": (0, "12345\n", "")})
        sched = SlurmScheduler(conn)
        spec = JobSpec(name="nccl", nodes=3, nodelist="n1,n2,n3", ntasks_per_node=4,
                       output="/logs/nccl_%j.out", script="nccl_allreduce.sbatch")
        handle = sched.submit(spec)
        assert handle.job_id == "12345"
        cmd = conn.calls[0]
        for frag in ["sbatch", "--parsable", "--job-name=nccl", "-N 3",
                     "--nodelist=n1,n2,n3", "--ntasks-per-node=4",
                     "--output=/logs/nccl_%j.out", "nccl_allreduce.sbatch"]:
            assert frag in cmd, f"missing {frag!r} in: {cmd}"

    def test_submit_command_uses_wrap(self):
        conn = FakeConn({"sbatch": (0, "9\n", "")})
        SlurmScheduler(conn).submit(JobSpec(command="srun hostname", nodes=2))
        assert "--wrap" in conn.calls[0] and "srun hostname" in conn.calls[0]

    def test_submit_requires_script_or_command(self):
        with pytest.raises(ValueError):
            SlurmScheduler(FakeConn()).submit(JobSpec(nodes=1))

    def test_submit_failure_raises(self):
        conn = FakeConn({"sbatch": (1, "", "Invalid partition")})
        with pytest.raises(RuntimeError, match="sbatch failed"):
            SlurmScheduler(conn).submit(JobSpec(script="x.sbatch"))

    def test_status_normalizes_cancelled(self):
        conn = FakeConn({"sacct": (0, "CANCELLED+ \n", "")})
        from schedulers.base import JobHandle
        assert SlurmScheduler(conn).status(JobHandle("1", "slurm")) == "CANCELLED"

    def test_status_pending_when_sacct_empty(self):
        conn = FakeConn({"sacct": (0, "", "")})
        from schedulers.base import JobHandle
        assert SlurmScheduler(conn).status(JobHandle("1", "slurm")) == "PENDING"


class TestLocalScheduler:
    def test_run_success(self):
        conn = FakeConn({"echo": (0, "hi\n", "")})
        res = LocalScheduler(conn).run(JobSpec(command="echo hi"))
        assert res.state == "COMPLETED" and res.returncode == 0 and "hi" in res.stdout

    def test_run_failure(self):
        conn = FakeConn({"false": (3, "", "boom")})
        res = LocalScheduler(conn).run(JobSpec(command="false"))
        assert res.state == "FAILED" and res.returncode == 3


class TestStubs:
    def test_runai_raises(self):
        with pytest.raises(NotImplementedError, match="run:AI"):
            RunAIScheduler().submit(JobSpec(image="img"))


class TestFactory:
    def test_selection(self):
        conn = FakeConn()
        assert build_scheduler("slurm", connection=conn).kind == "slurm"
        assert build_scheduler("local", connection=conn).kind == "local"
        assert build_scheduler("runai", connection=conn).kind == "runai"

    def test_default_from_env(self):
        conn = FakeConn()
        with mock.patch.dict("os.environ", {"RACK_SCHEDULER": "local"}, clear=False):
            assert build_scheduler(connection=conn).kind == "local"
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("RACK_SCHEDULER", None)
            assert build_scheduler(connection=conn).kind == "slurm"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown scheduler"):
            build_scheduler("pbs", connection=FakeConn())
