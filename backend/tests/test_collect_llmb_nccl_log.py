# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The aggregated Stage-1 log is named at launch, after the rack the run was launched against.

The parser finds ``<group>-NCCL-1Rack-<rack>-<N>N-<jobid>.txt`` by rack id. The collector used to
guess the rack from the first node's hostname by stripping a trailing tray number, which holds where
the hostname carries the rack and fails where it does not: on Nscale NC (2026-10-09) U22's 18-node
Stage-1 run (Slurm 118993) passed 24/24 and was written as ``…-1Rack-sys1-gso1-p-phy-gpu55-su19-…``,
so "View Parser Results" for U22 reported no matching log. The launcher now hands the collector the
rack it was launched against, and the file is prefixed with the writer's group rather than whoever
launched it -- the prefix stays, because the parser tells the stages apart by ``-nccl-1rack-``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
COLLECTOR = SCRIPTS / "collect_llmb_nccl_log.sh"
JOBID = "118993"
NC_NODES = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in range(55, 73)]

# GNU find -printf and stat -c: the collector runs on the Linux head node, as does CI.
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="collector needs GNU find/stat (Linux)")


def _group() -> str:
    return subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout.strip()


def _experiment(llmb: Path, nodes: list[str]) -> None:
    """An llmb-run experiment shaped like the real one: one test dir per collective, a rank dir per node."""
    exp = llmb / "workloads" / "microbenchmark_nccl" / "experiments" / f"microbenchmark_nccl_nccl_{JOBID}"
    log = exp / f"LOG_20261009-1009_{JOBID}_gb300_sweep_N{len(nodes)}"
    for idx, coll in enumerate(("all_reduce", "all_gather"), 1):
        test = log / f"1_{idx}_LOG_{coll}_env1_args1_job{JOBID}_iter1"
        for rank, node in enumerate(nodes):
            d = test / f"rank{rank}_{node}"
            d.mkdir(parents=True)
            (d / "stdout.txt").write_text(f"# Collective test starting: {coll}_perf_mpi\n# Avg bus bandwidth : 200\n")


def _collect(tmp_path: Path, nodes: list[str], *args: str, **env: str) -> list[str]:
    llmb, out = tmp_path / "llmb", tmp_path / "logs"
    _experiment(llmb, nodes)
    run_env = {k: v for k, v in os.environ.items() if k not in ("NVIS_RACK", "RACK_STATUS_LOG_DIR")}
    run_env.update(LLMB_INSTALL=str(llmb), RACK_JOB_LOG_DIR=str(out), **env)
    r = subprocess.run(["bash", str(COLLECTOR), JOBID, *args], capture_output=True, text=True,
                       env=run_env, check=False, timeout=60)
    assert r.returncode == 0, r.stderr
    return sorted(p.name for p in out.iterdir())


@linux_only
def test_the_launched_rack_names_the_file(tmp_path):
    assert _collect(tmp_path, NC_NODES, "U22") == [f"{_group()}-NCCL-1Rack-U22-18N-{JOBID}.txt"]


@linux_only
def test_the_argument_outranks_the_environment(tmp_path):
    assert _collect(tmp_path, NC_NODES, "U22", NVIS_RACK="U19") == [f"{_group()}-NCCL-1Rack-U22-18N-{JOBID}.txt"]


@linux_only
def test_the_dashboards_rack_names_it_when_no_argument_does(tmp_path):
    assert _collect(tmp_path, NC_NODES, NVIS_RACK="U22") == [f"{_group()}-NCCL-1Rack-U22-18N-{JOBID}.txt"]


@linux_only
def test_without_a_launched_rack_the_hostname_still_names_it(tmp_path):
    """Sites whose hostnames carry the rack keep working when nothing names the rack."""
    nodes = [f"ink50-dh01-h03-{t}" for t in range(11, 13)]
    assert _collect(tmp_path, nodes) == [f"{_group()}-NCCL-1Rack-ink50-dh01-h03-2N-{JOBID}.txt"]


@linux_only
@pytest.mark.parametrize("unsafe", ["../U22", "U22/x", "U 22", ".."])
def test_a_rack_that_is_not_a_file_name_token_is_not_put_in_a_path(tmp_path, unsafe):
    names = _collect(tmp_path, NC_NODES, unsafe, NVIS_RACK=unsafe)
    assert names == [f"{_group()}-NCCL-1Rack-sys1-gso1-p-phy-gpu55-su19-18N-{JOBID}.txt"]


def test_the_launcher_hands_the_collector_its_rack():
    """The name is fixed at launch: run_llmb_nccl.sh passes the rack when it arms the collector."""
    source = (SCRIPTS / "run_llmb_nccl.sh").read_text(encoding="utf-8")
    assert '_result_rack="${NVIS_RACK:-}"' in source
    assert '${_result_rack:+"${_result_rack}"}' in source


@linux_only
def test_the_log_names_the_hosts_it_covered(tmp_path):
    """NODELIST lets Rack Status attribute a run whose tests died before printing their rank lines."""
    _collect(tmp_path, NC_NODES[:1], "Q8")
    text = next((tmp_path / "logs").iterdir()).read_text()
    assert f"NODELIST: {NC_NODES[0]}\n" in text


def _fake(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)
    return str(path)


def _collect_unstarted(tmp_path: Path, slurm_nodes: str, stdout: str) -> subprocess.CompletedProcess[str]:
    """A job that died before llmb-run made any rank dir: only Slurm and its stdout know about it."""
    out = tmp_path / "logs"
    out.mkdir()
    (out / f"llmb-nccl_nodes_{JOBID}.out").write_text(stdout)
    run_env = {k: v for k, v in os.environ.items() if k not in ("NVIS_RACK", "RACK_STATUS_LOG_DIR")}
    run_env.update(
        LLMB_INSTALL=str(tmp_path / "llmb"),
        RACK_JOB_LOG_DIR=str(out),
        RACK_SLURM_SACCT=_fake(tmp_path, "sacct", f'[ "$5" = "{JOBID}" ] && echo "{slurm_nodes}"'),
        RACK_SLURM_SCONTROL=_fake(tmp_path, "scontrol", 'echo "$3" | tr "," "\\n"'),
    )
    return subprocess.run(["bash", str(COLLECTOR), JOBID, "U24"], capture_output=True, text=True,
                          env=run_env, check=False, timeout=60)


PYXIS_STDOUT = """[2026-10-10T08:30:15.097] error: pyxis: child 1768748 failed with error code: 1
[2026-10-10T08:30:15.097] error: pyxis: failed to create container filesystem
[2026-10-10T08:30:15.097] error: pyxis:     [ERROR] No such file or directory: /opt/containers/nvidia+nemo+26.04.01-arm.sqsh
srun: error: sys1-gso1-p-phy-gpu102-su19: task 0: Exited with exit code 1
Error: Failed to validate running environment
"""


@linux_only
def test_a_job_that_never_started_still_leaves_a_failed_result(tmp_path):
    """Nscale NC Slurm 133870: the image was not on the node, the job died in a second, and with no
    log the node stayed "pending" through three launches."""
    r = _collect_unstarted(tmp_path, "sys1-gso1-p-phy-gpu102-su19", PYXIS_STDOUT)
    assert r.returncode == 0, r.stderr
    log = tmp_path / "logs" / f"{_group()}-NCCL-1Rack-U24-1N-{JOBID}.txt"
    text = log.read_text()
    assert "NODELIST: sys1-gso1-p-phy-gpu102-su19\n" in text
    assert ("# NVIS-FAILURE: container did not start (pyxis): No such file or directory: "
            "/opt/containers/nvidia+nemo+26.04.01-arm.sqsh") in text
    assert "# Collective test starting" not in text, "a job that ran no test must not read as one that did"


@linux_only
def test_an_unstarted_job_with_no_error_still_says_it_did_not_start(tmp_path):
    r = _collect_unstarted(tmp_path, "sys1-gso1-p-phy-gpu1-su19,sys1-gso1-p-phy-gpu2-su19", "")
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "logs" / f"{_group()}-NCCL-1Rack-U24-2N-{JOBID}.txt").read_text()
    assert "NODELIST: sys1-gso1-p-phy-gpu1-su19,sys1-gso1-p-phy-gpu2-su19\n" in text
    assert "# NVIS-FAILURE: job ended before running any test" in text


@linux_only
def test_a_job_slurm_never_placed_is_not_written(tmp_path):
    """No rank dirs and no hosts: nothing to attribute, so nothing is claimed."""
    r = _collect_unstarted(tmp_path, "None assigned", PYXIS_STDOUT)
    assert r.returncode == 2
    assert "Slurm names no hosts" in r.stderr
    assert not list((tmp_path / "logs").glob("*NCCL-1Rack*"))
