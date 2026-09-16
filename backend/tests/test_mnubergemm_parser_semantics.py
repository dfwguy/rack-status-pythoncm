from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import results_history  # noqa: E402
import universal_parser as up  # noqa: E402


HEADER = "HOSTLIST: ink50-dh02-e02-11,ink50-dh02-e02-12\n"
ZERO = "MNUB [I] G: 0 ink50-dh02-e02-11 L: 0 GFlops:0\n"
POSITIVE = "MNUB [I] G: 0 ink50-dh02-e02-11 L: 0 GFlops:7.5e+05\n"
QUAL_OK = "".join(
    f"MNUB [I] G: {rank} ink50-dh02-e02-11 L: {rank % 4} returning QUAL_OK\n"
    for rank in range(8)
)
TEARDOWN = "MNUB [I] G: 0 ink50-dh02-e02-11 L: 0 normal mpi teardown\n"


def _shell_row(tmp_path: Path, text: str) -> dict[str, object]:
    log = tmp_path / "mnubergemm-26.03-v1.17-g001-99999.out"
    log.write_text(text)
    proc = subprocess.run(
        [
            "bash",
            str(REPO / "bin/scripts/parser.sh"),
            "--json",
            "--no-squeue",
            "--no-cmsh",
            "--no-sinfo",
            str(log),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1", "NVIS_PARSER_INVENTORY": "0"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)["mnubergemm_stress"]
    assert len(rows) == 1
    return rows[0]


def _shell_verdict(tmp_path: Path, text: str) -> str:
    return str(_shell_row(tmp_path, text)["verdict"])


def _metric(parsed: dict[str, object], name: str) -> float | None:
    return next(
        (metric["value"] for metric in parsed["metrics"] if metric["name"] == name), None
    )


def test_warmup_zero_then_positive_completed_run_passes(tmp_path: Path) -> None:
    text = HEADER + ZERO + POSITIVE + QUAL_OK + TEARDOWN
    assert _shell_verdict(tmp_path, text) == "DONE"
    parsed = up.parse("mnubergemm-99999.out", text, kind="mnubergemm")
    assert parsed["status"] == "pass"
    assert parsed["extra"]["mnubergemm_positive_work"] is True


def test_all_zero_completed_run_fails(tmp_path: Path) -> None:
    text = HEADER + ZERO + QUAL_OK + TEARDOWN
    assert _shell_verdict(tmp_path, text) == "FAIL"
    parsed = up.parse("mnubergemm-99999.out", text, kind="mnubergemm")
    assert parsed["status"] == "fail"
    assert parsed["errors"][0]["kind"] == "mnubergemm_no_positive_work"


def test_incomplete_positive_run_abstains_or_reports_running(tmp_path: Path) -> None:
    text = HEADER + ZERO + POSITIVE
    assert _shell_verdict(tmp_path, text) == "UNK"
    parsed = up.parse(
        "mnubergemm-99999.out", text, kind="mnubergemm", running_hint=True
    )
    assert parsed["status"] == "running"


def test_hard_fault_fails_even_after_positive_work(tmp_path: Path) -> None:
    text = HEADER + POSITIVE + "MNUB [E] G: 0 CUDART error\n"
    assert _shell_verdict(tmp_path, text) == "FAIL"
    parsed = up.parse("mnubergemm-99999.out", text, kind="mnubergemm")
    assert parsed["status"] == "fail"


def test_ranks_are_topology_not_gflops_sample_count(tmp_path: Path) -> None:
    nodes = [f"ink50-dh02-i21-{slot:02d}" for slot in (*range(11, 19), *range(28, 38))]
    header = "HOSTLIST: " + ",".join(nodes) + "\n"
    samples = "".join(
        f"MNUB [I] G: 0 {nodes[0]} L: 0 GFlops:{7.5e5 + sample}\n"
        for sample in range(3)
    )
    qualified = "".join(
        f"MNUB [I] G: {rank} {nodes[rank // 4]} L: {rank % 4} returning QUAL_OK\n"
        for rank in range(72)
    )
    text = header + samples + qualified + TEARDOWN

    shell = _shell_row(tmp_path, text)
    assert shell["ranks"] == "72"
    assert shell["rank_samples"] == "3"
    assert shell["qualified_ranks"] == "72"
    assert shell["verdict"] == "DONE"

    parsed = up.parse("mnubergemm-55912.out", text, kind="mnubergemm")
    assert _metric(parsed, "mnubergemm_ranks") == 72
    assert _metric(parsed, "mnubergemm_rank_samples") == 3
    assert _metric(parsed, "mnubergemm_qualified_ranks") == 72
    assert parsed["extra"]["mnubergemm_rank_samples"] == 3
    assert parsed["extra"]["mnubergemm_qualified_ranks"] == 72
    assert parsed["status"] == "pass"

    records = results_history.records_from_parser_json(
        {"mnubergemm_stress": [shell]}, source_log_path="/logs/mnubergemm-55912.out"
    )
    history_metrics = {metric["metric_name"]: metric["value"] for metric in records[0]["metrics"]}
    assert history_metrics["ranks"] == 72
    assert history_metrics["rank_samples"] == 3
    assert history_metrics["qualified_ranks"] == 72


def test_terminal_run_fails_when_not_every_expected_rank_qualifies(tmp_path: Path) -> None:
    partial_qualification = "".join(
        f"MNUB [I] G: {rank} ink50-dh02-e02-11 L: {rank % 4} returning QUAL_OK\n"
        for rank in range(7)
    )
    text = HEADER + POSITIVE + partial_qualification + TEARDOWN

    shell = _shell_row(tmp_path, text)
    assert shell["ranks"] == "8"
    assert shell["qualified_ranks"] == "7"
    assert shell["verdict"] == "FAIL"
    parsed = up.parse("mnubergemm-99999.out", text, kind="mnubergemm")
    assert parsed["status"] == "fail"
    assert parsed["errors"][0]["kind"] == "mnubergemm_qualification_incomplete"


def test_terminal_run_without_expected_topology_fails_closed(tmp_path: Path) -> None:
    text = POSITIVE + QUAL_OK + TEARDOWN

    shell = _shell_row(tmp_path, text)
    assert shell["nodes"] == "-"
    assert shell["ranks"] == "-"
    assert shell["qualified_ranks"] == "8"
    assert shell["verdict"] == "FAIL"
    parsed = up.parse("mnubergemm-99999.out", text, kind="mnubergemm")
    assert parsed["status"] == "fail"
    assert _metric(parsed, "mnubergemm_ranks") is None


@pytest.mark.parametrize("positive, expected", [(True, "timeout"), (False, "fail")])
def test_expected_walltime_requires_positive_work(positive: bool, expected: str) -> None:
    text = HEADER + ZERO + (POSITIVE if positive else "") + (
        "slurmstepd: error: *** JOB 99999 CANCELLED DUE TO TIME LIMIT ***\n"
    )
    parsed = up.parse("mnubergemm-99999.out", text, kind="mnubergemm")
    assert parsed["status"] == expected
