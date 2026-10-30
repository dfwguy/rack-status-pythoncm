# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Whole-rack policy, exact selection, result coverage and API lifecycle without jobs."""
import copy
import json
from pathlib import Path

import clusterkit_execution as execution
import pytest
from clusterkit_artifacts import ArtifactListingLimitError, artifacts
from clusterkit_inventory import directory
from clusterkit_planner import PlanningError, build_plan, parse_inventory, plan_request
from clusterkit_profile import MAX_PAIRWISE_NODES, parse_profile
from clusterkit_results import decode_pairs, summarize

from .test_clusterkit_api import api_env as inherited_api_env
from .test_clusterkit_execution import payload, write_before, write_stage
from .test_clusterkit_service import write_passing_summary

SCRIPTS = Path(__file__).resolve().parents[2] / "bin/scripts"


def profile(tmp_path, count=18, **changes):
    nodes = [f"node-{i}" for i in range(1, count + 1)]
    raw = {"enabled": True, "partition": "testq", "hpcx_init": str(tmp_path / "hpcx-init.sh"),
           "results_root": str(tmp_path / "results"), "expected_architecture": "aarch64",
           "expected_link_layer": "Ethernet", "hca": "fake_rail:1",
           "allowed_node_counts": [n for n in (2, 4, 8) if n <= count], "max_nodes": count,
           "whole_rack_enabled": True, "node_allowlist": nodes, "path_comparison": True,
           "topology_groups": {"R1": {"nodes": nodes, "kind": "rack", "authoritative": True,
                                      "source": "explicit administrator mapping"}}}
    raw.update(changes)
    return raw


def inventory(raw):
    nodes = raw["node_allowlist"]
    return parse_inventory("\n".join(f"{n}|IDLE|testq" for n in nodes), nodes)


def plan(tmp_path, count=18, *, raw=None, snapshot=None):
    raw = raw or profile(tmp_path, count)
    return build_plan({"scope": "rack", "group": "R1", "node_count": count},
                      parse_profile(raw), snapshot or inventory(raw), scripts_dir=str(SCRIPTS))


@pytest.mark.parametrize("count", [2, 4, 8, 18, 20, 36, 64])
def test_exact_full_rack_resources_static_recipe_and_coverage(tmp_path, count):
    result = plan(tmp_path, count)
    assert execution.validate_plan(result, SCRIPTS) == json.loads(json.dumps(result))
    assert len(result["nodes"]) == count
    assert set(result["nodes"]) == set(profile(tmp_path, count)["node_allowlist"])
    assert result["expected_pair_count"] == count * (count - 1) // 2
    for arg in (f"--nodes={count}", f"--ntasks={count}", "--ntasks-per-node=1", "--gres=none", "--time=00:05:00"):
        assert arg in result["sbatch_argv"]
    assert "--exclusive" not in result["sbatch_argv"]
    assert result["entrypoint"] == str(SCRIPTS / "clusterkit_pairwise.sbatch")
    assert result["resources"]["wall_seconds"] == 300
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("state,partition,present", [
    ("ALLOCATED", "testq", True), ("MIXED", "testq", True), ("IDLE+DRAIN", "testq", True),
    ("IDLE*", "testq", True), ("DOWN", "testq", True), ("RESERVED", "testq", True),
    ("IDLE", "otherq", True), ("IDLE", "testq", False),
])
def test_one_unusable_member_rejects_entire_rack_without_a_subset(tmp_path, state, partition, present):
    raw = profile(tmp_path)
    snap = inventory(raw)
    if present:
        snap["nodes"][-1].update(states=[state], partitions=[partition])
    else:
        snap["nodes"].pop()
    with pytest.raises(PlanningError, match="all mapped nodes idle"):
        plan(tmp_path, raw=raw, snapshot=snap)
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("changes", [
    {"whole_rack_enabled": False}, {"max_nodes": 8},
    {"node_allowlist": ["node-1", "node-2"]},
    {"topology_groups": {}},
    {"topology_groups": {"R1": {"nodes": ["node-1", "node-2"], "kind": "group", "authoritative": True}}},
])
def test_policy_and_mapping_are_checked_before_any_slurm_read(tmp_path, changes):
    raw = profile(tmp_path, **changes)

    class NoReader:
        def query(self, _):
            raise AssertionError("Rejected policy must not query Slurm")
    with pytest.raises(PlanningError):
        plan_request({"scope": "rack", "group": "R1", "node_count": 18},
                     parse_profile(raw), NoReader(), scripts_dir=str(SCRIPTS))


@pytest.mark.parametrize("count", [16, 17, 19, 20, True, "18", 0, 66])
def test_mismatched_odd_or_unbounded_full_rack_count_is_rejected(tmp_path, count):
    raw = profile(tmp_path)
    with pytest.raises(PlanningError):
        build_plan({"scope": "rack", "group": "R1", "node_count": count},
                   parse_profile(raw), inventory(raw), scripts_dir=str(SCRIPTS))


def test_no_implicit_authorization_of_large_explicit_or_group_requests(tmp_path):
    raw = profile(tmp_path)
    for scope in ("group", "explicit"):
        request = {"scope": scope, "node_count": 18}
        request.update({"group": "R1"} if scope == "group" else {"nodes": raw["node_allowlist"]})
        with pytest.raises(PlanningError, match="not enabled"):
            build_plan(request, parse_profile(raw), inventory(raw), scripts_dir=str(SCRIPTS))
    assert "whole_rack_enabled" not in parse_profile(profile(tmp_path, whole_rack_enabled=False)).profile.as_dict()


def test_even_sizes_are_configurable_but_always_bounded(tmp_path):
    for count in (10, 18, MAX_PAIRWISE_NODES):
        raw = profile(tmp_path, count, allowed_node_counts=[count])
        assert parse_profile(raw).ready
    for value in (True, "18", 3, 0, MAX_PAIRWISE_NODES + 2):
        assert not parse_profile(profile(tmp_path, allowed_node_counts=[value])).ready
    assert not parse_profile(profile(tmp_path, whole_rack_enabled="yes")).ready


def test_directory_reports_same_whole_rack_policy_as_planner(tmp_path):
    for changes, expected in (({}, True), ({"max_nodes": 8}, False),
                              ({"node_allowlist": ["node-1", "node-2"]}, False),
                              ({"whole_rack_enabled": False}, False)):
        resolution = parse_profile(profile(tmp_path, **changes))
        rack = directory(resolution)["racks"][0]
        assert rack["whole_rack_enabled"] is expected
        assert bool(rack["whole_rack_reason"]) is not expected


def test_all_eighteen_ranks_keep_independent_network_evidence(tmp_path, monkeypatch):
    result = plan(tmp_path)
    for rank, node in enumerate(result["nodes"]):
        monkeypatch.setenv("SLURM_PROCID", str(rank))
        monkeypatch.setenv("SLURM_LOCALID", "0")
        monkeypatch.setattr(execution.socket, "gethostname", lambda name=node: name)
        assert execution.rank_context(result) == (rank, node)
    monkeypatch.setenv("SLURM_PROCID", "18")
    with pytest.raises(RuntimeError):
        execution.rank_context(result)


def test_153_pairs_per_metric_and_missing_pair_detection(tmp_path):
    result = plan(tmp_path)
    raw = payload(result, "bandwidth")
    assert decode_pairs(raw, result["nodes"], "bandwidth", "123")["observed_pair_count"] == 153
    raw["Links"][0][-1] = None
    metrics = decode_pairs(raw, result["nodes"], "bandwidth", "123")
    assert metrics["observed_pair_count"] == 152 and len(metrics["missing_pairs"]) == 1


def test_full_rack_summary_preserves_primary_failure_and_unverified_performance(tmp_path):
    result = plan(tmp_path)
    root = tmp_path / "attempt-fixture"
    root.mkdir()
    write_before(result, root)
    for stage in ("baseline", "single-path"):
        write_stage(result, root, stage)
    outcome = {"job_id": "123", "preflight_exit": 0, "primary_exit": 124,
               "stages": {"baseline": 124}, "diagnostics": {"after_exit": 0}}
    summary = summarize(result, root, outcome)
    assert summary["functional_status"] == "FAIL"
    assert summary["primary_exit_status"] == 124 and summary["performance_status"] == "UNVERIFIED"
    assert len(summary["preflight"]["nodes"]) == 18


def test_rack_artifact_listing_exceeds_old_cap_and_never_returns_partial_overflow(tmp_path, monkeypatch):
    import clusterkit_artifacts
    for rank in range(18):
        for phase in range(9):
            (tmp_path / f"rank-{rank}-phase-{phase}.json").write_text("{}")
    assert len(artifacts(tmp_path)) == 162
    monkeypatch.setattr(clusterkit_artifacts, "MAX_ARTIFACT_FILES", 8)
    with pytest.raises(ArtifactListingLimitError, match="no partial list"):
        artifacts(tmp_path)


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    yield from inherited_api_env.__wrapped__(tmp_path, monkeypatch)


def configure_api(env, monkeypatch):
    raw = profile(env.root, hpcx_init=str(env.hpcx), results_root=str(env.results))
    env.nodes = raw["node_allowlist"]
    env.profile = raw
    monkeypatch.setenv("RACK_CLUSTERKIT_SITE_PROFILE", json.dumps(raw))
    return raw


def test_api_eighteen_node_preview_creates_no_attempt_or_job(api_env, monkeypatch):
    env = api_env
    configure_api(env, monkeypatch)
    before = sorted(env.root.rglob("*"))
    response = env.client.post("/api/clusterkit/preview", json={"scope": "rack", "group": "R1", "node_count": 18})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["plan"]["expected_pair_count"] == 153
    assert len(response.get_json()["plan"]["nodes"]) == 18
    assert sorted(env.root.rglob("*")) == before
    assert not env.slurm.submissions


def test_api_confirmed_full_rack_lifecycle_with_simulated_scheduler_only(api_env, monkeypatch):
    env = api_env
    configure_api(env, monkeypatch)
    response = env.client.post("/api/clusterkit/preview", json={"scope": "rack", "group": "R1", "node_count": 18})
    token = response.get_json()["confirmation_token"]
    response = env.client.post("/api/clusterkit/runs", json={"confirm": True, "confirmation_token": token})
    assert response.status_code == 202, response.get_json()
    submitted = response.get_json()
    assert len(env.slurm.submissions) == 1 and "--nodes=18" in env.slurm.submissions[0]
    root = Path(submitted["results_directory"])
    result = json.loads((root / "plan.json").read_text())
    write_passing_summary(result, root)
    env.slurm.state = "COMPLETED"
    response = env.client.get(submitted["status_url"])
    assert response.status_code == 200, response.get_json()
    summary = response.get_json()["summary"]
    assert summary["functional_status"] == "PASS" and summary["performance_status"] == "UNVERIFIED"
    for stage in summary["stages"].values():
        for metric in stage["metrics"].values():
            assert metric["observed_pair_count"] == 153
    assert env.client.get(submitted["status_url"] + "/artifacts").status_code == 200


def test_busy_after_preview_and_mapping_changes_reject_confirmation(api_env, monkeypatch):
    env = api_env
    raw = configure_api(env, monkeypatch)
    response = env.client.post("/api/clusterkit/preview", json={"scope": "rack", "group": "R1", "node_count": 18})
    token = response.get_json()["confirmation_token"]
    env.slurm.node_state = "allocated"
    response = env.client.post("/api/clusterkit/runs", json={"confirm": True, "confirmation_token": token})
    assert response.status_code == 409 and not env.slurm.submissions and not list(env.results.iterdir())
    env.slurm.node_state = "idle"
    changed = copy.deepcopy(raw)
    changed["topology_groups"]["R1"]["nodes"].pop()
    monkeypatch.setenv("RACK_CLUSTERKIT_SITE_PROFILE", json.dumps(changed))
    response = env.client.post("/api/clusterkit/runs", json={"confirm": True, "confirmation_token": token})
    assert response.status_code == 409 and not env.slurm.submissions and not list(env.results.iterdir())


def test_full_rack_batch_uses_exact_resources_and_bounded_steps_without_processes(tmp_path, monkeypatch):
    result = plan(tmp_path)
    root = Path(result["results_directory"])
    root.mkdir(parents=True)
    calls = []

    def simulated_step(argv, logfile, seconds):
        calls.append((argv, seconds))
        stage = argv[-1]
        if stage == "before":
            write_before(result, root)
        elif stage in ("baseline", "single-path"):
            write_stage(result, root, stage)
        return 0
    monkeypatch.setattr(execution, "allocation", lambda _: "123")
    monkeypatch.setattr(execution, "checked_output", lambda *args, **kwargs: "fixture pmix_v3")
    monkeypatch.setattr(execution, "run_step", simulated_step)
    monkeypatch.setenv("SLURM_EXPORT_ENV", "fixture-initial")
    assert execution.batch(result, root / "plan.json", SCRIPTS, "pairwise") == 0
    summary = json.loads((root / "summary.json").read_text())
    assert summary["functional_status"] == "PASS"
    assert summary["performance_status"] == "UNVERIFIED"
    assert [argv[-1] for argv, _ in calls] == ["before", "baseline", "single-path", "after"]
    for argv, timeout in calls:
        assert argv[0] == "srun"
        assert "--nodes=18" in argv and "--ntasks=18" in argv
        assert "--ntasks-per-node=1" in argv and "--cpu-bind=cores" in argv
        assert "--gres=none" in argv and "--exclusive" not in argv
        assert 0 < timeout < result["resources"]["wall_seconds"]
    for stage in summary["stages"].values():
        assert stage["metrics"]["bandwidth"]["observed_pair_count"] == 153
        assert stage["metrics"]["latency"]["observed_pair_count"] == 153


def test_nested_artifact_overflow_never_becomes_a_silent_partial_list(tmp_path, monkeypatch):
    import clusterkit_artifacts
    nested = tmp_path / "source" / "backend"
    nested.mkdir(parents=True)
    for index in range(4):
        (nested / f"fixture-{index}.py").write_text("# fixture\n")
    monkeypatch.setattr(clusterkit_artifacts, "MAX_ARTIFACT_FILES", 2)
    with pytest.raises(ArtifactListingLimitError, match="no partial list"):
        artifacts(tmp_path)


def test_overlapping_physical_racks_are_not_a_whole_rack_mapping(tmp_path):
    raw = profile(tmp_path)
    raw["topology_groups"]["R2"] = {"nodes": raw["node_allowlist"][-2:], "kind": "rack",
                                  "authoritative": True}
    with pytest.raises(PlanningError, match="Ambiguous physical rack membership"):
        plan(tmp_path, raw=raw)
    assert not directory(parse_profile(raw))["racks"][0]["whole_rack_enabled"]


def test_su_membership_can_overlap_a_physical_rack(tmp_path):
    raw = profile(tmp_path)
    raw["topology_groups"]["SU"] = {"nodes": raw["node_allowlist"], "kind": "su",
                                  "authoritative": True}
    assert plan(tmp_path, raw=raw)["expected_pair_count"] == 153
