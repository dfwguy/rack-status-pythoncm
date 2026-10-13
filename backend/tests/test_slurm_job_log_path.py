# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Where a finished job's log is: ask Slurm what it wrote, before guessing by filename.

Scheduler -> Recent finished jobs -> open log found nothing for ClusterKit job 192682 (Nscale NC,
2026-10-13): the job had aged out of scontrol, and the fallback globbed the top of the log directory
for ``*-<jid>.txt`` while ClusterKit writes into its ``clusterkit/`` subdirectory. Slurm accounting
still had the job's ``--output`` -- ``/home/cmods/nvis/logs/clusterkit/rkoike-CK-U2-%j.txt`` -- which
answers for every launcher without a filename convention to guess.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import slurm_query as sq

CK = "/home/cmods/nvis/logs/clusterkit/rkoike-CK-U2-192682.txt"


def _proc(stdout: str, rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, "")


@pytest.fixture
def slurm(monkeypatch):
    """scontrol has forgotten the job; accounting answers with *sacct*; *files* exist on the host."""
    state = {"sacct": "", "files": set(), "globbed": []}

    def run_slurm(cmd, timeout=0):
        if "scontrol" in cmd:
            return _proc("", 1)  # aged out
        if "sacct" in cmd:
            return _proc(state["sacct"])
        raise AssertionError(cmd)

    def run_on_host(cmd, timeout=0):
        state["globbed"].append(cmd)
        return _proc("")

    monkeypatch.setattr(sq, "_get_platform", lambda: SimpleNamespace(
        sacct_cmd=lambda a: f"sacct {a}", scontrol_cmd=lambda a: f"scontrol {a}"))
    monkeypatch.setattr(sq.host_exec, "run_slurm_on_host", run_slurm)
    monkeypatch.setattr(sq.host_exec, "run_on_host", run_on_host)
    monkeypatch.setattr(sq.host_exec, "is_file", lambda p: str(p) in state["files"])
    return state


def test_an_aged_out_job_is_found_where_accounting_says_it_wrote(slurm):
    slurm["sacct"] = "/home/cmods/nvis/logs/clusterkit/rkoike-CK-U2-%j.txt|CLUSTERKIT-U2|rkoike|gpu6,gpu10|/home/cmods/nvis/logs\n"
    slurm["files"] = {CK}
    assert sq._slurm_job_log_path("192682", Path("/home/cmods/nvis/logs")) == (CK, "sacct")
    assert slurm["globbed"] == []  # no guessing once Slurm has answered


@pytest.mark.parametrize(("pattern", "expected"), [
    ("/l/%x-%j.out", "/l/NEMO-36N-7.out"),
    ("/l/%u/%5j.txt", "/l/rkoike/00007.txt"),
    ("/l/slurm-%J.out", "/l/slurm-7.out"),
    ("/l/100%%-%j", "/l/100%-7"),
    ("/l/%N-%j.out", "/l/gpu1-7.out"),
])
def test_slurms_placeholders_are_filled_in(pattern, expected):
    assert sq._expand_slurm_filename_pattern(pattern, job_id="7", name="NEMO-36N", user="rkoike", node="gpu1") == expected


def test_a_placeholder_this_record_cannot_fill_is_not_guessed():
    # An array task, or %N for a multi-node job: say nothing rather than name a wrong file.
    assert sq._expand_slurm_filename_pattern("/l/%A_%a.out", job_id="7", name="x", user="u", node="n") == ""
    assert sq._expand_slurm_filename_pattern("/l/%N.out", job_id="7", name="x", user="u", node="") == ""


def test_a_relative_output_is_under_the_jobs_workdir(slurm):
    slurm["sacct"] = "nccl-mpi_U20+U23_%j.out|nccl_allreduce|rkoike|gpu[19-36]|/home/cmods/nvis/logs\n"
    slurm["files"] = {"/home/cmods/nvis/logs/nccl-mpi_U20+U23_194808.out"}
    assert sq._slurm_job_log_path("194808", Path("/x")) == ("/home/cmods/nvis/logs/nccl-mpi_U20+U23_194808.out", "sacct")


def test_a_recorded_file_that_is_gone_falls_back_to_looking(slurm):
    slurm["sacct"] = f"{CK.replace('192682', '%j')}|CLUSTERKIT-U2|rkoike|gpu6|/w\n"
    assert sq._slurm_job_log_path("192682", Path("/home/cmods/nvis/logs")) == ("", "not_found")
    assert slurm["globbed"], "the log-directory glob should still run"


def test_no_accounting_falls_back_to_looking(slurm):
    assert sq._slurm_job_log_path("192682", Path("/home/cmods/nvis/logs"))[1] == "not_found"
    assert slurm["globbed"]


@pytest.mark.parametrize("pattern", ["/dev/null", "(null)", "", "/l/../etc/passwd-%j"])
def test_no_output_or_an_unsafe_one_is_no_answer(slurm, pattern):
    slurm["sacct"] = f"{pattern}|x|u|n|/w\n"
    slurm["files"] = {"/dev/null", "/l/../etc/passwd-5"}
    assert sq._sacct_stdout_path("5") == ""
