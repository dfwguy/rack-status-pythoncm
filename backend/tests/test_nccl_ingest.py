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
"""Tiered ingest: prefer structure when it exists, fall back when it does not.

Three sources answer the same question with decreasing confidence — nccl-tests' own JSON, the
Comms-Perf-Suite per-combination directory, and scraping an aggregated text log. The properties
worth pinning are less about any one parser and more about the chain: precedence, that a tier can
decline without taking the others down, that provenance is reported, and that a combination which
produced nothing is described rather than dropped.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODULE = REPO_ROOT / "bin" / "scripts" / "nvis_nccl_ingest.py"

_spec = importlib.util.spec_from_file_location("nvis_nccl_ingest", MODULE)
assert _spec and _spec.loader
ing = importlib.util.module_from_spec(_spec)
# Register before executing: @dataclass resolves annotations through sys.modules[__module__],
# which is None for a module loaded straight from a spec.
sys.modules["nvis_nccl_ingest"] = ing
_spec.loader.exec_module(ing)


HDR = (
    "#       size         count      type   redop    root     time   algbw   busbw  #wrong"
    "     time   algbw   busbw  #wrong"
)
NODELIST = "ink50-dh01-f08-[11-18,28-37]"


def _row(size: int, busbw: float) -> str:
    return (
        f"   {size:>11} {size // 4:>13}     float     sum      -1   100.00    0.00 "
        f"{busbw:>7.2f}       0   100.00    0.00 {busbw:>7.2f}       0"
    )


def _table(coll: str, peak: float, *, concluded: bool = True, avg: float = 159.7) -> str:
    out = [
        "# nccl-tests version 2.17.8 nccl-headers=22902 nccl-library=22903",
        f"# Collective test starting: {coll}_perf_mpi",
        HDR,
        _row(8, 0.0),
        _row(1 << 34, peak),
    ]
    if concluded:
        out += [
            "# Out of bounds values : 0 OK",
            f"# Avg bus bandwidth    : {avg}",
            f"# Collective test concluded: {coll}_perf_mpi",
        ]
    return "\n".join(out) + "\n"


def _yml(coll: str, idx: int, job: str = "140212", *, nvls: str = "0", g: str = "None") -> str:
    return (
        f'test_name: "{coll}"\n'
        f'log_name: "1_{idx}_LOG_{coll}_env1_args1_job{job}_iter1.txt"\n'
        'hostname: "ink50-dh01-f08-11"\n'
        'num_nodes: "18"\n'
        'num_processes: "72"\n'
        'num_process_per_node: "4"\n'
        f'slurm_id: "{job}"\n'
        f'node_list: "{NODELIST}"\n'
        f'test_binary: "{coll}_perf_mpi"\n'
        'iteration: "1"\n'
        f'cmd: "srun -N 18 env NCCL_DEBUG=WARN env NCCL_NVLS_ENABLE={nvls} '
        f'bash -c run_test ${{NCCL_TEST_PATH}}/{coll}_perf -t1 -g1 -b8 -e16G -dfloat -G{g}"\n'
    )


def _experiment(tmp_path: Path, combos, *, job: str = "140212") -> Path:
    """Build a Comms-Perf-Suite style experiment dir.

    *combos* is a list of ``(index, collective, peak_or_None)``; ``None`` writes the yml with an
    empty txt, which is the shape job 140212 actually produced for sendrecv env2/args2.
    """
    exp = tmp_path / f"microbenchmark_nccl_nccl_fp8_container-nccl_cmods_{job}"
    log = exp / f"LOG_20260810-1607_{job}_gb300_sweep_N18"
    log.mkdir(parents=True)
    (log / "test_set_metadata.yml").write_text(
        'timestamp: "1786378155"\n'
        'library: \n  name: "nccl"\n  version: "2.29.3"\n  cuda_build_version: "13.1"\n'
        'env_variables: \n  NCCL_DEBUG: "WARN"\n',
        encoding="utf-8",
    )
    for idx, coll, peak in combos:
        stem = f"1_{idx}_LOG_{coll}_env1_args1_job{job}_iter1"
        (log / f"{stem}.yml").write_text(_yml(coll, idx, job), encoding="utf-8")
        (log / f"{stem}.txt").write_text("" if peak is None else _table(coll, peak), encoding="utf-8")
    return exp


# --- the experiment tier -----------------------------------------------------------------------


def test_experiment_dir_yields_identity_and_geometry_without_guessing(tmp_path):
    exp = _experiment(tmp_path, [(1, "all_reduce", 916.0)])
    s = ing.ingest(str(exp))
    assert s is not None
    assert s.source == "experiment"
    assert s.job_id == "140212"
    assert s.num_nodes == 18 and s.num_processes == 72 and s.gpus_per_node == 4
    assert s.node_list == NODELIST
    assert s.racks == ["ink50-dh01-f08"], "the rack comes from the node list, not the filename"
    assert s.nccl_version == "2.29.3"


def test_the_log_dir_itself_is_accepted_too(tmp_path):
    exp = _experiment(tmp_path, [(1, "all_reduce", 916.0)])
    log_dir = next(exp.glob("LOG_*"))
    assert ing.ingest(str(log_dir)).source == "experiment"


def test_each_combination_keeps_its_own_parameters(tmp_path):
    """The scraper cannot tell six all_reduce configurations apart; this can."""
    exp = _experiment(tmp_path, [(1, "all_reduce", 158.0), (2, "all_reduce", 224.0)])
    s = ing.ingest(str(exp))
    assert len(s.combinations) == 2
    for c in s.combinations:
        assert c.env.get("NCCL_NVLS_ENABLE") == "0"
        assert c.args.get("-G") == "None"
        assert c.args.get("-b") == "8"


def test_peak_is_taken_at_the_largest_size(tmp_path):
    exp = _experiment(tmp_path, [(1, "all_reduce", 916.0)])
    c = ing.ingest(str(exp)).combinations[0]
    assert c.peak_busbw_gbps == 916.0
    assert c.series[0].size_bytes == 8, "the whole series survives for the chart"


def test_a_combination_that_produced_nothing_is_named_not_dropped(tmp_path):
    """Job 140212: sendrecv env2/args2 wrote a yml and an empty txt."""
    exp = _experiment(tmp_path, [(1, "all_reduce", 916.0), (2, "sendrecv", None)])
    s = ing.ingest(str(exp))
    by = {c.name.split("_LOG_")[1].split("_env")[0]: c for c in s.combinations}
    assert by["all_reduce"].status == "ok"
    assert by["sendrecv"].status == "attempted"
    assert by["sendrecv"].note
    assert s.complete is False
    assert any("sendrecv" in w for w in s.warnings)


def test_a_combination_the_runner_never_reached_is_reported_as_missing(tmp_path):
    """140212 declared 24 and wrote 23: the gap is a fact about the run, not a parse failure."""
    exp = _experiment(tmp_path, [(1, "all_reduce", 916.0), (3, "alltoall", 800.0)])
    s = ing.ingest(str(exp))
    missing = [c for c in s.combinations if c.status == "missing"]
    assert len(missing) == 1 and missing[0].name == "combination_2"


def test_combinations_sort_numerically(tmp_path):
    """1_2 must come before 1_10; string order would not."""
    exp = _experiment(tmp_path, [(i, "all_reduce", 100.0 + i) for i in (1, 2, 10)])
    s = ing.ingest(str(exp))
    order = [int(c.name.split("_")[1]) for c in s.combinations if c.status == "ok"]
    assert order == [1, 2, 10]


def test_an_all_empty_sweep_still_reports_identity(tmp_path):
    """Nothing measured is still a result: say whose job it was and that it is incomplete."""
    exp = _experiment(tmp_path, [(1, "all_reduce", None)])
    s = ing.ingest(str(exp))
    assert s.job_id == "140212" and s.num_nodes == 18
    assert s.complete is False


# --- the scrape tier ---------------------------------------------------------------------------


def test_an_aggregated_log_falls_through_to_the_scraper(tmp_path):
    log = tmp_path / "cmods-NCCL-1Rack-ink50-dh01-f08-18N-140212.txt"
    log.write_text(_table("all_reduce", 916.0) + _table("alltoall", 1805.0), encoding="utf-8")
    s = ing.ingest(str(log))
    assert s.source == "scrape"
    assert {c.collective for c in s.combinations} == {"all_reduce", "alltoall"}


def test_the_scraper_can_only_count_gaps_not_name_them(tmp_path):
    log = tmp_path / "agg.txt"
    log.write_text(_table("all_reduce", 916.0) + _table("alltoall", 800.0, concluded=False),
                   encoding="utf-8")
    s = ing.ingest(str(log))
    assert s.source == "scrape"
    assert any("cannot be identified" in w for w in s.warnings)
    # A counted-but-unnamed gap still has to make the sweep incomplete, or the warning says one
    # thing and the verdict says another.
    assert s.unaccounted == 1
    assert s.complete is False


def test_a_log_with_no_nccl_content_is_refused_by_every_tier(tmp_path):
    log = tmp_path / "nothing.txt"
    log.write_text("just some text\nwith no tables\n", encoding="utf-8")
    assert ing.ingest(str(log)) is None


# --- the chain ----------------------------------------------------------------------------------


def test_experiment_beats_scrape_for_the_same_job(tmp_path):
    """Both tiers could answer; the structured one must win."""
    exp = _experiment(tmp_path, [(1, "all_reduce", 916.0)])
    log_dir = next(exp.glob("LOG_*"))
    (log_dir / "aggregated.txt").write_text(_table("all_reduce", 111.0), encoding="utf-8")
    s = ing.ingest(str(exp))
    assert s.source == "experiment"
    assert s.combinations[0].peak_busbw_gbps == 916.0, "not the aggregated copy"


def test_json_wins_when_present(tmp_path):
    """tier 0 today is a seam; prove the ordering works before anything emits it."""
    j = tmp_path / "nccl.json"
    j.write_text(json.dumps({
        "version": 1, "nccl_version": "2.29.1",
        "command": ["/usr/local/bin/all_reduce_perf", "-b", "8"],
        "environment": {"NCCL_DEBUG": "WARN"},
        "results": [
            {"size_bytes": 8, "bus_bw_gbs": 0.1, "in_place": False, "errors": 0},
            {"size_bytes": 1 << 34, "bus_bw_gbs": 704.06, "in_place": False, "errors": 0},
            {"size_bytes": 1 << 34, "bus_bw_gbs": 999.0, "in_place": True, "errors": 0},
        ],
    }), encoding="utf-8")
    s = ing.ingest(str(j))
    assert s.source == "json"
    assert s.combinations[0].collective == "all_reduce"
    assert s.combinations[0].peak_busbw_gbps == 704.06, "out-of-place only"
    assert s.nccl_version == "2.29.1"


def test_a_malformed_json_falls_through_instead_of_failing(tmp_path):
    """Fail closed: tier 0 is unverified against a real run, so it must never be a dead end."""
    j = tmp_path / "broken.json"
    j.write_text("{not json at all", encoding="utf-8")
    assert ing.ingest(str(j)) is None, "declines, and no exception escapes"


def test_a_source_that_raises_does_not_sink_the_chain(tmp_path):
    class Exploding(ing.Source):
        name = "exploding"
        priority = -1

        def detect(self, target):
            return True

        def load(self, target):
            raise RuntimeError("boom")

    log = tmp_path / "agg.txt"
    log.write_text(_table("all_reduce", 916.0), encoding="utf-8")
    s = ing.ingest(str(log), sources=[Exploding(), *ing.DEFAULT_SOURCES])
    assert s is not None and s.source == "scrape"
    assert any("exploding" in d and "boom" in d for d in s.declined)


def test_provenance_records_who_declined(tmp_path):
    exp = _experiment(tmp_path, [(1, "all_reduce", 916.0)])
    s = ing.ingest(str(exp))
    assert s.source == "experiment"
    assert any(d.startswith("json:") for d in s.declined), "a downgrade must be visible"


def test_sources_are_tried_in_priority_order():
    order = [s.name for s in sorted(ing.DEFAULT_SOURCES, key=lambda s: s.priority)]
    assert order == ["json", "experiment", "scrape"]


def test_find_experiment_dir_bridges_a_job_id_to_its_tree(tmp_path):
    root = tmp_path / "llmb"
    wl = root / "workloads" / "microbenchmark_nccl" / "experiments"
    wl.mkdir(parents=True)
    exp = _experiment(wl, [(1, "all_reduce", 916.0)], job="140212")
    assert ing.find_experiment_dir(str(root), "140212") == str(exp)
    assert ing.find_experiment_dir(str(root), "999999") is None


# --- the flat metadata reader --------------------------------------------------------------------


def test_flat_yaml_reads_values_and_one_level_of_nesting(tmp_path):
    p = tmp_path / "m.yml"
    p.write_text('a: "1"\nlibrary: \n  name: "nccl"\n  version: "2.29.3"\nb: "2"\n', encoding="utf-8")
    d = ing.read_flat_yaml(str(p))
    assert d["a"] == "1" and d["b"] == "2"
    assert d["library"] == {"name": "nccl", "version": "2.29.3"}


def test_flat_yaml_survives_a_value_containing_colons(tmp_path):
    """The recorded command line is full of them."""
    p = tmp_path / "m.yml"
    p.write_text('cmd: "srun --container-mounts=./:/work bash -c run_test x -b8"\n', encoding="utf-8")
    assert "--container-mounts=./:/work" in ing.read_flat_yaml(str(p))["cmd"]


def test_flat_yaml_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert ing.read_flat_yaml(str(tmp_path / "nope.yml")) == {}


@pytest.mark.parametrize(
    "cmd,env_key,env_val,arg_key,arg_val",
    [
        ("env NCCL_NVLS_ENABLE=1 bash -c run_test x/all_reduce_perf -b8 -GNone",
         "NCCL_NVLS_ENABLE", "1", "-G", "None"),
        ("env NCCL_NCHANNELS_PER_NET_PEER=4 bash -c run_test x/sendrecv_perf -duint8 -G2",
         "NCCL_NCHANNELS_PER_NET_PEER", "4", "-G", "2"),
    ],
)
def test_the_recorded_command_yields_the_parameter_set(cmd, env_key, env_val, arg_key, arg_val):
    envs, args = ing.parse_cmd_env_args(cmd)
    assert envs[env_key] == env_val
    assert args[arg_key] == arg_val


def test_racks_come_from_the_node_list():
    assert ing.racks_from_nodelist("ink50-dh01-f08-[11-18,28-37]") == ["ink50-dh01-f08"]
    assert ing.racks_from_nodelist("ink50-dh01-e01-11,ink50-dh01-h22-37") == [
        "ink50-dh01-e01", "ink50-dh01-h22"]
