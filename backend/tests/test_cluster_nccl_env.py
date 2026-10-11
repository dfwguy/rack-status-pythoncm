# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A cluster's per-span NCCL environment: the record, which span a launch is, and the layering.

The case that motivated it: on NscaleTX-NC the Spectrum-X network plugin (``NCCL_NET_PLUGIN`` +
``NCCL_IB_DYNAMIC_FLOW``) helped cross-rack alltoall, and the only way to apply it was to edit the
shared launchers every other cluster runs. The record states it per cluster and per span instead,
and these tests pin the three things that make that safe: the record refuses what a job
environment cannot carry, a launch takes only its own span's list, and the operator's per-launch
choices still win.
"""
from __future__ import annotations

import os

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import pytest
from api import validation_runs
from catalog import models
from node_targets import nccl_env_scope, nccl_env_scope_for_target

SPCX = {
    "NCCL_NET_PLUGIN": "/home/cmods/workspace/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so",
    "NCCL_IB_DYNAMIC_FLOW": "1",
}


def _cluster(nccl_env=None):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", "nccl_env": nccl_env or {}})


@pytest.fixture
def active_cluster(monkeypatch):
    """Make ``cluster`` the active one for a launch, without a catalog on disk."""

    def _set(cluster):
        monkeypatch.setattr(validation_runs.catalog_context, "resolve", lambda: (None, cluster, None))

    return _set


# --- the record --------------------------------------------------------------------------------


class TestTheRecord:
    def test_a_cluster_stored_before_the_field_loads_with_empty_lists(self):
        env = models.Cluster.from_dict({"id": "c1", "name": "C1"}).nccl_env
        assert (env.node, env.rack, env.multirack) == ({}, {}, {})

    def test_lists_round_trip_and_stay_apart(self):
        c = _cluster({"multirack": SPCX, "node": {"NCCL_DEBUG": "WARN"}})
        assert c.to_dict()["nccl_env"] == {"node": {"NCCL_DEBUG": "WARN"}, "rack": {}, "multirack": SPCX}
        assert not c.validate()

    def test_values_are_kept_as_strings(self):
        assert _cluster({"rack": {"NCCL_MIN_NCHANNELS": 16}}).nccl_env.rack == {"NCCL_MIN_NCHANNELS": "16"}

    def test_a_value_may_carry_commas_and_spaces(self):
        """NCCL_IB_HCA lists are comma-separated; the job gets the env whole, not as an --export list."""
        assert not _cluster({"multirack": {"NCCL_IB_HCA": "=roce_vf_r1,roce_vf_r2", "X": "a b"}}).validate()

    @pytest.mark.parametrize(
        ("env", "fragment"),
        [
            ({"NCCL NET": "x"}, "not an environment variable name"),
            ({"1NCCL": "x"}, "not an environment variable name"),
            ({"NCCL_X": "a\nb"}, "single line"),
            ({"NCCL_X": "a\rb"}, "single line"),
            ({"NCCL_X": "x" * (models.LAUNCH_ENV_MAX_VALUE_LEN + 1)}, "longer than"),
        ],
    )
    def test_what_a_job_environment_cannot_carry_is_refused(self, env, fragment):
        problems = _cluster({"rack": env}).validate()
        assert any(p.startswith("nccl_env.rack") and fragment in p for p in problems), problems

    def test_more_variables_than_a_launch_may_carry_is_refused(self):
        env = {f"V{i}": "1" for i in range(models.LAUNCH_ENV_MAX_VARS + 1)}
        assert any("at most" in p for p in _cluster({"node": env}).validate())

    def test_an_unknown_span_has_no_list(self):
        assert _cluster({"multirack": SPCX}).nccl_env.for_scope("pod") == {}


# --- which span a launch is ----------------------------------------------------------------------


class TestTheSpan:
    @pytest.mark.parametrize(
        ("racks", "nodes", "scope"),
        [
            (2, 36, "multirack"),
            (2, 0, "multirack"),  # racks named, nodes left to the launcher
            (1, 18, "rack"),
            (1, 16, "rack"),  # a partial rack is still one NVLink domain
            (1, 0, "rack"),
            (0, 1, "node"),
            (1, 1, "node"),
            (0, 4, "rack"),  # nodes without their racks
        ],
    )
    def test_span_follows_racks_then_nodes(self, racks, nodes, scope):
        assert nccl_env_scope(racks, nodes) == scope

    def test_a_single_host_target_is_a_node(self):
        assert nccl_env_scope_for_target("sys1-gso1-p-phy-gpu5-su19") == "node"

    def test_several_hosts_of_a_rack_are_the_rack(self):
        assert nccl_env_scope_for_target("sys1-gso1-p-phy-gpu[1-12,14-17]-su19") == "rack"

    def test_a_template_the_site_resolves_later_is_the_rack(self):
        assert nccl_env_scope_for_target("U22-ct[01-18]") == "rack"
        assert nccl_env_scope_for_target("") == "rack"


# --- the layering at launch ----------------------------------------------------------------------


class TestTheLaunchEnv:
    def test_a_launch_gets_only_its_own_spans_list(self, active_cluster):
        active_cluster(_cluster({"multirack": SPCX, "rack": {"NCCL_MIN_NCHANNELS": "16"}}))
        multi = validation_runs._launch_env_from_body({}, scope="multirack")
        rack = validation_runs._launch_env_from_body({}, scope="rack")
        node = validation_runs._launch_env_from_body({}, scope="node")
        assert multi["NCCL_NET_PLUGIN"] == SPCX["NCCL_NET_PLUGIN"]
        assert "NCCL_MIN_NCHANNELS" not in multi
        assert rack["NCCL_MIN_NCHANNELS"] == "16" and "NCCL_NET_PLUGIN" not in rack
        assert node == {}

    def test_the_dialogs_variables_win_over_the_clusters(self, active_cluster):
        active_cluster(_cluster({"multirack": {**SPCX, "NCCL_DEBUG": "WARN"}}))
        env = validation_runs._launch_env_from_body(
            {"nccl_debug": "INFO", "env": {"NCCL_IB_DYNAMIC_FLOW": "0"}}, scope="multirack"
        )
        assert env["NCCL_DEBUG"] == "INFO"
        assert env["NCCL_IB_DYNAMIC_FLOW"] == "0"
        assert env["NCCL_NET_PLUGIN"] == SPCX["NCCL_NET_PLUGIN"]

    def test_every_supplied_name_is_advertised_for_forwarding_to_remote_ranks(self, active_cluster):
        """Without this, nccl_allreduce.sbatch applies the cluster's vars to the mpirun host's ranks only."""
        active_cluster(_cluster({"multirack": SPCX}))
        env = validation_runs._launch_env_from_body({"env": {"UCX_TLS": "rc"}}, scope="multirack")
        assert env["NVIS_EXTRA_ENV_KEYS"].split() == sorted([*SPCX, "UCX_TLS"])

    def test_nothing_is_advertised_when_nothing_is_supplied(self, active_cluster):
        active_cluster(_cluster())
        assert "NVIS_EXTRA_ENV_KEYS" not in validation_runs._launch_env_from_body({}, scope="rack")

    def test_no_active_cluster_launches_with_the_dialogs_env_alone(self, active_cluster):
        active_cluster(None)
        assert validation_runs._launch_env_from_body({"env": {"A": "1"}}, scope="rack") == {
            "A": "1",
            "NVIS_EXTRA_ENV_KEYS": "A",
        }

    def test_an_unreadable_catalog_does_not_refuse_the_launch(self, monkeypatch):
        def _boom():
            raise OSError("catalog.json unreadable")

        monkeypatch.setattr(validation_runs.catalog_context, "resolve", _boom)
        assert validation_runs._launch_env_from_body({"env": {"A": "1"}}, scope="rack")["A"] == "1"

    def test_a_cross_rack_nemo_launch_takes_the_multirack_list(self, active_cluster, monkeypatch, tmp_path):
        """The wiring at a real call site: the span comes from the racks the launch names."""
        active_cluster(_cluster({"multirack": SPCX, "rack": {"WRONG": "1"}}))
        monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda _raw: tmp_path)
        monkeypatch.setattr(validation_runs, "_script_exists", lambda _p: True)
        hosts = ["sys1-gso1-p-phy-gpu1-su19", "sys1-gso1-p-phy-gpu37-su19"]
        monkeypatch.setattr(validation_runs, "_placed_rack_nodes", lambda _racks: (hosts, None))
        prep, err = validation_runs._prepare_run_nemo_xrack({"racks": ["p1-r1", "p1-r2"]})
        assert err is None and prep is not None
        assert prep["env_updates"]["NCCL_NET_PLUGIN"] == SPCX["NCCL_NET_PLUGIN"]
        assert "WRONG" not in prep["env_updates"]


# --- the record through the API ------------------------------------------------------------------


class TestSavingIt:
    @pytest.fixture
    def client(self, tmp_path):
        from app import app
        from catalog import seed
        from catalog.store import TinyDBRepo, set_repo

        repo = TinyDBRepo(tmp_path / "catalog.json")
        set_repo(repo)
        seed.migrate(repo, env={"RACK_CLUSTER_PRODUCT_LINE": "gb200", "RACK_CLUSTER_CUSTOMER_NAME": "Lab"})
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c
        set_repo(None)

    def _project_with(self, client, nccl_env):
        project = client.get("/api/catalog/projects").get_json()["items"][0]
        project["clusters"][0]["nccl_env"] = nccl_env
        return project

    def test_a_valid_list_is_stored(self, client):
        project = self._project_with(client, {"multirack": SPCX})
        resp = client.put(f"/api/catalog/projects/{project['id']}", json=project)
        assert resp.status_code == 200, resp.get_json()
        stored = client.get(f"/api/catalog/projects/{project['id']}").get_json()
        item = stored.get("item", stored)
        assert item["clusters"][0]["nccl_env"]["multirack"] == SPCX

    def test_an_invalid_list_is_refused_with_the_reason(self, client):
        project = self._project_with(client, {"rack": {"BAD NAME": "1"}})
        resp = client.put(f"/api/catalog/projects/{project['id']}", json=project)
        assert resp.status_code == 400
        assert "nccl_env.rack" in str(resp.get_json())
