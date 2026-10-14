# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Every shape of NCCL log Stage 1 meets, run through the verdict the dashboard actually reads.

``rack_status.sh --probe-json`` decides a log's ``nccl_ok`` for node and rack Stage 1. On Nscale NC
(2026-10-14) it passed two logs it should not have: gpu92-su34 (job 167578) whose sendrecv test
returned wrong data (178 out-of-bounds values per round), and gpu124-su25 (job 126229) whose third
all_reduce aborted mid-sweep -- because it only asked whether all_reduce produced a peak.

This corpus pins the whole rule. Each log is built in the layout the NC logs use (an LLMB
aggregated manifest, a rank table naming the host, UCX noise, per-test markers), and each case
says what the verdict must be and why. ``REFERENCE`` restates the rule in plain Python and must
agree with the shell checker on every case, so the two cannot drift apart silently.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "bin" / "scripts"
RACK_STATUS = SCRIPTS / "rack_status.sh"
INC = SCRIPTS / "nccl_parse.inc.sh"
HOST = "sys1-gso1-p-phy-gpu1-su19"
UCX_NOISE = (
    f"[1791591478.567992] [{HOST}:3016162:0]            sock.c:495  UCX  ERROR bind(fd=62 "
    "addr=2a03:83e4:410e:1200::1%0:0) failed: Cannot assign requested address\n"
)

pytestmark = pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")


# --- building logs the way NC writes them --------------------------------------------------------


def _row(size: int, busbw: float, wrong: int = 0) -> str:
    return (
        f"{size:>12} {size // 4:>13}     float     sum      -1    15.78   {busbw:6.2f}  {busbw:6.2f}"
        f"  {wrong:>6}    13.74   {busbw:6.2f}  {busbw:6.2f}  {wrong:>6}\n"
    )


def _table(host: str, ranks: int = 4) -> str:
    out = "# nThread 1 nGpus 1 minBytes 8 maxBytes 17179869184 step: 2(factor) warmup iters: 5 iters: 20\n#\n"
    out += "# Using devices\n"
    for r in range(ranks):
        out += f"#  Rank {r:>2} Group  0 Pid {3016162 + r} on {host} device {r:>2} [0008:06:00] NVIDIA GB300\n"
    out += "#\n#                                                              out-of-place                       in-place\n"
    out += "#       size         count      type   redop    root     time   algbw   busbw  #wrong     time   algbw   busbw  #wrong\n"
    out += "#        (B)    (elements)                               (us)  (GB/s)  (GB/s)             (us)  (GB/s)  (GB/s)\n"
    return out


def test_block(
    coll: str,
    *,
    peak: float = 690.0,
    wrong: int = 0,
    conclude: bool = True,
    failure: str = "",
    host: str = HOST,
    marker: bool = True,
    mpi: bool = True,
) -> str:
    """One nccl-tests test. *failure* is a failure line written where nccl-tests writes it."""
    name = f"{coll}_perf{'_mpi' if mpi else ''}"
    out = f"# Collective test starting: {name}\n" if marker else ""
    out += _table(host)
    out += _row(1048576, 20.0, wrong) + _row(17179869184, peak, wrong)
    if failure:
        out += failure + "\n"
    if not conclude:
        return out
    oob = f"{wrong} FAILED" if wrong else "0 OK"
    out += f"# Out of bounds values : {oob}\n# Avg bus bandwidth    : {peak * 0.4:.4f}\n#\n"
    out += f"# Collective test concluded: {name}\n#\n"
    return out


test_block.__test__ = False  # a builder, not a test


def llmb_log(blocks: list[str], *, job: int = 126352, rack: str = "U19", nodes: int = 1, tests: int | None = None) -> str:
    """The LLMB aggregated layout: manifest, then one ``# --- i_LOG_... ---`` section per test."""
    declared = len(blocks) if tests is None else tests
    out = (
        f"# NVIS: aggregated from llmb-run experiment microbenchmark_nccl_nccl_fp8_container-nccl_rkoike_{job}\n"
        f"# job={job} rack={rack} nodes={nodes} tests={declared}\n"
    )
    for i, b in enumerate(blocks, 1):
        out += f"# --- {i}_1_LOG_test_env1_args1_job{job}_iter1 ---\n" + UCX_NOISE + b
    return out


SUITE = ["all_reduce", "all_gather", "alltoall", "reduce_scatter", "sendrecv"]


def clean_suite(rounds: int = 1, **kw) -> list[str]:
    return [test_block(c, **kw) for _ in range(rounds) for c in SUITE]


NEMO_WITH_NCCL_DEBUG = """\
node-11:123:123 [0] NCCL INFO Channel 05/32 :  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35
 23 24 25 26 27 28 29 30 31 32 33 34 35
[NeMo I] train_step_timing in s: 6.71 consumed_samples: 216
"""


@dataclass(frozen=True)
class Case:
    id: str
    name: str
    text: str
    nccl_ok: bool
    issue: str  # substring the probe's nccl_issue must contain; "" means it must be empty


def _aborted_mid_sweep() -> str:
    blocks = clean_suite()
    blocks.insert(2, test_block("all_reduce", conclude=False))  # starts, prints rows, never concludes
    return llmb_log(blocks, job=126229, tests=len(blocks))


def _marker_less_standalone() -> str:
    return _table(HOST) + _row(1048576, 20.0) + _row(1073741824, 560.6) + (
        "# Out of bounds values : 0 OK\n# Avg bus bandwidth    : 224.24\n#\n"
        "# Collective test concluded: all_reduce_perf\n"
    )


CASES = [
    # ---- valid ----
    Case("valid-llmb-24", "cmods-NCCL-1Rack-U19-1N-126352.txt",
         llmb_log(clean_suite(rounds=4) + clean_suite()[:4]), True, ""),
    Case("valid-repeated-rounds", "cmods-NCCL-1Rack-U19-1N-126360.txt",
         llmb_log(clean_suite(rounds=2)), True, ""),
    Case("valid-no-manifest", "cmods-NCCL-1Rack-U19-1N-126361.txt",
         "".join(clean_suite()), True, ""),
    Case("valid-standalone-marker-less", "cmods-NCCL-1Rack-U19-1N-126362.txt",
         _marker_less_standalone(), True, ""),
    Case("valid-legacy-prefix-full-rack", "rkoike-NCCL-1Rack-U22-18N-118993.txt",
         llmb_log(clean_suite(), job=118993, rack="U22", nodes=18), True, ""),
    # ---- not valid: the run did not hold up, whatever bandwidth it measured ----
    Case("oob-outside-all-reduce", "cmods-NCCL-1Rack-T7-1N-167578.txt",
         llmb_log(clean_suite()[:4] + [test_block("sendrecv", wrong=178,
                  failure=f" .. {HOST} pid 2072916: Test failure common.cu:1187")], job=167578),
         False, "out-of-bounds"),
    Case("oob-in-all-reduce", "cmods-NCCL-1Rack-U19-1N-126370.txt",
         llmb_log([test_block("all_reduce", wrong=12)] + clean_suite()[1:]), False, "out-of-bounds"),
    Case("test-failure-line", "cmods-NCCL-1Rack-U19-1N-126371.txt",
         llmb_log(clean_suite()[:3] + [test_block("reduce_scatter", conclude=False,
                  failure=f" .. {HOST} pid 1: Test failure common.cu:1187")]), False, "Test failure"),
    Case("cuda-failure", "cmods-NCCL-1Rack-X19-1N-133956.txt",
         llmb_log([test_block("all_reduce", conclude=False,
                  failure=f"{HOST}: Test CUDA failure util.cu:555 'system not yet initialized'")] + clean_suite()[1:]),
         False, "Test CUDA failure"),
    Case("nccl-failure", "cmods-NCCL-1Rack-U19-1N-126372.txt",
         llmb_log(clean_suite()[:1] + [test_block("alltoall", conclude=False,
                  failure=f"{HOST}: Test NCCL failure common.cu:1012 'remote process exited or there was a network error'")]),
         False, "Test NCCL failure"),
    Case("test-timeout", "cmods-NCCL-1Rack-U19-1N-126373.txt",
         llmb_log(clean_suite()[:2] + [test_block("all_gather", conclude=False, failure="Test timeout after 600 s")]),
         False, "Test timeout"),
    Case("aborted-mid-sweep", "cmods-NCCL-1Rack-R25-1N-126229.txt", _aborted_mid_sweep(), False, "did not finish"),
    Case("manifest-declares-more", "cmods-NCCL-1Rack-U19-1N-126374.txt",
         llmb_log(clean_suite(rounds=5)[:22], tests=24), False, "2 of 24 tests did not finish"),
    Case("still-running", "cmods-NCCL-1Rack-U19-1N-126375.txt",
         llmb_log(clean_suite()[:4] + [test_block("sendrecv", conclude=False)], tests=5), False, "did not finish"),
    # ---- not valid: no NCCL result at all ----
    Case("container-never-started", "cmods-NCCL-1Rack-R2-1N-188618.txt",
         "# job=188618 rack=R2 nodes=1 tests=24\n# NVIS-FAILURE: container did not start (pyxis): "
         "No such file or directory: /opt/containers/nvidia+nemo+26.04.01-arm.sqsh\n", False, ""),
    Case("empty", "cmods-NCCL-1Rack-U19-1N-126376.txt", "", False, ""),
    Case("nemo-log-phantom-rows", "cmods-NCCL-1Rack-U19-1N-126377.txt", NEMO_WITH_NCCL_DEBUG, False, ""),
]


# --- the rule, restated -----------------------------------------------------------------------------


def REFERENCE(text: str) -> str:
    """The integrity rule in plain Python: '' for a clean (or never-armed) log, else a reason kind."""
    lines = text.splitlines()
    armed = any(
        ln.startswith(("# Collective test starting:", "# Collective test concluded:"))
        or (ln.startswith("#") and ("out-of-place" in ln or ("size" in ln and "busbw" in ln)))
        for ln in lines
    )
    if not armed:
        return ""
    oob = sum(int(m.group(1)) for ln in lines if (m := re.search(r"Out of bounds values\s*:\s*(\d+)", ln)))
    if oob:
        return "out-of-bounds"
    if any(re.search(r"Test (NCCL|CUDA) failure|Test failure|Test timeout", ln) for ln in lines):
        return "failure"
    declared = max([int(m.group(1)) for ln in lines if (m := re.match(r"# job=.*\btests=(\d+)", ln))] or [0])
    started = sum(ln.startswith("# Collective test starting:") for ln in lines)
    concluded = sum(ln.startswith("# Collective test concluded:") for ln in lines)
    return "did not finish" if max(declared, started) > concluded else ""


# --- running the real verdict -----------------------------------------------------------------------


def _probe(tmp_path: Path, case: Case) -> dict:
    p = tmp_path / case.name
    p.write_text(case.text, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(RACK_STATUS), "--no-squeue", "--no-cmsh", "--no-sinfo", "--probe-json", str(p)],
        capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, "VALIDATION_TEST": "nccl_all_reduce", "NO_COLOR": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _checker(tmp_path: Path, case: Case) -> str:
    p = tmp_path / ("chk-" + case.name)
    p.write_text(case.text, encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; nccl_integrity_issue "$1"', "_", str(p)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_the_probe_verdict_for_every_log_shape(tmp_path, case):
    out = _probe(tmp_path, case)
    assert out["nccl_ok"] is case.nccl_ok, out
    if case.issue:
        assert case.issue in out["nccl_issue"], out
    else:
        assert out["nccl_issue"] == "", out


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_the_shell_checker_and_the_reference_rule_agree(tmp_path, case):
    shell = _checker(tmp_path, case)
    ref = REFERENCE(case.text)
    if ref == "":
        assert shell == "", shell
    elif ref == "failure":
        assert shell.startswith("test failure:"), shell
    else:
        assert ref in shell, (ref, shell)


def test_every_invalid_shape_is_covered_and_every_valid_one_passes():
    """The corpus itself: at least one case per failure kind, and every valid case really passes."""
    kinds = {REFERENCE(c.text) for c in CASES if not c.nccl_ok}
    assert {"out-of-bounds", "failure", "did not finish", ""} <= kinds
    assert all(REFERENCE(c.text) == "" for c in CASES if c.nccl_ok)


def test_a_clean_wanted_peak_no_longer_hides_a_bad_log(tmp_path):
    """The two NC false passes: all_reduce has a peak in both, and both must still fail."""
    for case_id in ("oob-outside-all-reduce", "aborted-mid-sweep"):
        case = next(c for c in CASES if c.id == case_id)
        out = _probe(tmp_path, case)
        assert out["nccl_ok"] is False, (case_id, out)


def test_parser_sh_flags_agree_with_the_probe(tmp_path):
    """parser.sh (the Parser Results dialog) runs the same suite rule, so it must say the same."""
    for case in (next(c for c in CASES if c.id == i) for i in ("valid-llmb-24", "oob-outside-all-reduce")):
        p = tmp_path / case.name
        p.write_text(case.text, encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(SCRIPTS / "parser.sh"), "--json", str(p)],
            capture_output=True, text=True, timeout=120, check=False, cwd=str(tmp_path),
            env={**os.environ, "VALIDATION_TEST": "nccl_all_reduce", "NO_COLOR": "1"},
        )
        assert proc.returncode == 0, proc.stderr
        flags = json.loads(proc.stdout)["parser_flags"]
        assert flags and bool(int(flags[0]["nccl_ok"])) is case.nccl_ok, (case.id, flags)
