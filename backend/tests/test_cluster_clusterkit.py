# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A cluster's ClusterKit choices: the record, its projection, and the launcher reading them.

NscaleTX-NC runs ClusterKit's GPU-Direct tests (gpu_gpu_bw, gpu_gpu_lat with -G) so each rail is
measured from its own GPU; another site may want the host-memory default. That choice belongs to the
cluster, so a launch from Rack Status picks it up without anyone editing Settings or the launcher.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from catalog import context, models

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"


def _cluster(**clusterkit):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", "clusterkit": clusterkit})


class TestTheRecord:
    def test_a_cluster_stored_before_the_field_has_no_choice(self):
        ck = models.Cluster.from_dict({"id": "c1", "name": "C1"}).clusterkit
        assert (ck.tests, ck.gpudirect) == ([], False)

    def test_choices_round_trip(self):
        c = _cluster(tests=["gpu_gpu_bw", "gpu_gpu_lat"], gpudirect=True)
        assert c.to_dict()["clusterkit"] == {"tests": ["gpu_gpu_bw", "gpu_gpu_lat"], "gpudirect": True, "multirack_tests": []}
        assert not c.validate()

    @pytest.mark.parametrize("bad", ["gpu_bw", "bw lat", "-G", "$(id)"])
    def test_a_name_clusterkit_does_not_know_is_refused(self, bad):
        assert any("clusterkit.tests" in p for p in _cluster(tests=[bad]).validate())

    def test_a_test_listed_twice_is_refused(self):
        assert any("twice" in p for p in _cluster(tests=["bw", "bw"]).validate())


class TestTheProjection:
    def test_the_choices_reach_the_launcher_env(self):
        env = context.high_overlay(None, _cluster(tests=["gpu_gpu_bw", "gpu_gpu_lat"], gpudirect=True))
        assert env["RACK_CLUSTERKIT_TESTS"] == "gpu_gpu_bw gpu_gpu_lat"
        assert env["RACK_CLUSTERKIT_GPUDIRECT"] == "1"

    def test_no_choice_projects_nothing_so_the_defaults_apply(self):
        env = context.high_overlay(None, _cluster())
        assert not {"RACK_CLUSTERKIT_TESTS", "RACK_CLUSTERKIT_GPUDIRECT"} & env.keys()

    def test_the_names_forward_over_ssh(self):
        from transport.factory import FORWARD_ENV_PREFIXES

        assert "RACK_CLUSTERKIT_GPUDIRECT".startswith(FORWARD_ENV_PREFIXES)

    def test_the_cluster_beats_a_global_setting(self, monkeypatch):
        import config_env

        high = {"RACK_CLUSTERKIT_TESTS": "gpu_gpu_bw"}
        monkeypatch.setattr(config_env.catalog_context, "overlays_for_active", lambda: (high, {}))
        monkeypatch.setattr(config_env, "_job_env_overrides_load", lambda: {"RACK_CLUSTERKIT_TESTS": "bw lat"})
        assert config_env._eget("RACK_CLUSTERKIT_TESTS") == "gpu_gpu_bw"


class TestTheFormsChoices:
    def test_the_vocab_serves_clusterkits_test_cases(self, tmp_path):
        from app import app
        from catalog import seed
        from catalog.store import TinyDBRepo, set_repo

        repo = TinyDBRepo(tmp_path / "catalog.json")
        set_repo(repo)
        seed.migrate(repo, env={"RACK_CLUSTER_PRODUCT_LINE": "gb200", "RACK_CLUSTER_CUSTOMER_NAME": "Lab"})
        app.config["TESTING"] = True
        try:
            with app.test_client() as c:
                vocab = c.get("/api/catalog/vocab").get_json()
        finally:
            set_repo(None)
        assert vocab["clusterkit_tests"] == list(models.CLUSTERKIT_TESTS)
        assert {"bw", "lat", "gpu_gpu_bw", "gpu_gpu_lat"} <= set(vocab["clusterkit_tests"])


class TestTheLauncher:
    """clusterkit.sbatch turns RACK_CLUSTERKIT_GPUDIRECT=1 into clusterkit.sh -G, once."""

    def _gdr(self, **env: str) -> str:
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        start = sbatch.index('_ck_exe_opt=""')
        snippet = sbatch[start:sbatch.index("_ck_failed=()", start)]
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
        r = subprocess.run(["bash", "-c", snippet + 'printf "%s" "$_ck_gdr"'],
                           env=full, capture_output=True, text=True, timeout=30, check=False)
        return r.stdout

    def test_gpudirect_adds_the_flag(self):
        assert self._gdr(RACK_CLUSTERKIT_GPUDIRECT="1") == "-G"

    def test_off_by_default(self):
        assert self._gdr() == ""

    def test_not_twice_when_extra_opts_already_has_it(self):
        assert self._gdr(RACK_CLUSTERKIT_GPUDIRECT="1", RACK_CLUSTERKIT_EXTRA_OPTS="-G") == ""

    def test_the_launch_forwards_it(self):
        from api import validation_runs

        for key in ("RACK_CLUSTERKIT_TESTS", "RACK_CLUSTERKIT_GPUDIRECT", "RACK_CLUSTERKIT_MULTIRACK_TESTS"):
            assert key in validation_runs._CLUSTERKIT_PASS_THROUGH


# --- across racks ------------------------------------------------------------------------------

U19 = [f"sys1-gso1-p-phy-gpu{i}-su19" for i in (2, 3)]
U21 = [f"sys1-gso1-p-phy-gpu{i}-su19" for i in (37, 38)]


class TestMultirackRecord:
    def test_multirack_tests_round_trip_validate_and_project(self):
        c = _cluster(multirack_tests=["bisection_bw", "alltoall"])
        assert c.clusterkit.multirack_tests == ["bisection_bw", "alltoall"]
        assert not c.validate()
        assert context.high_overlay(None, c)["RACK_CLUSTERKIT_MULTIRACK_TESTS"] == "bisection_bw alltoall"
        assert any("clusterkit.multirack_tests" in p for p in _cluster(multirack_tests=["bisect"]).validate())


class TestMultirackPrepare:
    @pytest.fixture
    def vr(self, monkeypatch, tmp_path):
        os.environ.setdefault("RACK_STATUS_DEMO", "1")
        from api import validation_runs

        monkeypatch.setattr(validation_runs, "_eget", lambda key, *a, **k: "")
        monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda _raw: tmp_path)
        monkeypatch.setattr(validation_runs, "_normalize_rack_id", lambda rack: rack)
        monkeypatch.setattr(validation_runs, "_script_exists", lambda _p: True)
        monkeypatch.setattr(validation_runs, "_nvis_workspace_dir_effective", lambda: "/home/cmods/workspace")
        monkeypatch.setattr(validation_runs, "_multirack_campaign_snapshot", lambda: {"active": False, "id": ""})
        placed = {"U19": U19, "U21": U21}
        monkeypatch.setattr(validation_runs, "_rack_node_hostnames_from_inventory", lambda rack: placed.get(rack, []))
        from app import app

        with app.app_context():  # the refusals build their responses with jsonify
            yield validation_runs

    def test_two_racks_run_across_racks_with_each_rack_a_scope(self, vr):
        prep, err = vr._prepare_run_clusterkit_singlerack({"racks": ["U19", "U21"]})
        assert err is None, err
        assert prep["racks"] == ["U19", "U21"]
        assert prep["nodes"] == U19 + U21
        assert prep["cmd"][-1] == ",".join(U19 + U21)
        env = prep["env_updates"]
        assert env["RACK_CLUSTERKIT_RACK"] == "U19+U21"
        assert env["RACK_CLUSTERKIT_SCOPES"] == f"U19:{','.join(U19)};U21:{','.join(U21)}"

    def test_a_single_rack_in_racks_stays_the_single_rack_run(self, vr, monkeypatch):
        monkeypatch.setattr(vr, "validation_target_for_rack", lambda rack, _ov: f"{rack}-ct[01-18]")
        prep, err = vr._prepare_run_clusterkit_singlerack({"racks": ["U19"]})
        assert err is None and "RACK_CLUSTERKIT_SCOPES" not in prep["env_updates"]
        assert prep["env_updates"]["RACK_CLUSTERKIT_RACK"] == "U19"

    def test_a_rack_with_no_placed_node_is_refused(self, vr):
        _prep, err = vr._prepare_run_clusterkit_singlerack({"racks": ["U19", "Z99"]})
        assert err is not None and err[1] == 409

    def test_an_active_campaign_supplies_the_nodes(self, vr, monkeypatch):
        campaign = {"active": True, "id": "wave-1", "racks": [
            {"rack": "U19", "eligible": True, "nodes": U19[:1]},
            {"rack": "U21", "eligible": True, "nodes": U21[:1]},
        ]}
        monkeypatch.setattr(vr, "_multirack_campaign_snapshot", lambda: campaign)
        prep, err = vr._prepare_run_clusterkit_singlerack({"racks": ["U19", "U21"], "campaign": "wave-1"})
        assert err is None and prep["nodes"] == [U19[0], U21[0]] and prep["campaign"] == "wave-1"

    def test_a_stale_campaign_id_is_refused(self, vr, monkeypatch):
        monkeypatch.setattr(vr, "_multirack_campaign_snapshot", lambda: {"active": True, "id": "wave-2", "racks": []})
        _prep, err = vr._prepare_run_clusterkit_singlerack({"racks": ["U19", "U21"], "campaign": "wave-1"})
        assert err is not None and err[1] == 409

    def test_the_run_is_recorded_as_multirack(self, vr, monkeypatch, tmp_path):
        from app import app

        prep = {"log_dir": tmp_path, "rk": "U19+U21", "racks": ["U19", "U21"], "target": "h1,h2", "cmd": ["true"],
                "display_cmd": "./run_clusterkit.sh h1,h2", "timeout": 120, "env_updates": {}}
        monkeypatch.setattr(vr, "_prepared", lambda *_a, **_k: (prep, None))
        recorded = {}
        monkeypatch.setattr(vr, "_record_validation_activity", lambda tool, **kw: recorded.update(tool=tool, **kw))

        class _NoThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

        monkeypatch.setattr(vr.threading, "Thread", _NoThread)
        app.config["TESTING"] = True
        with app.test_client() as c:
            r = c.post("/api/run-clusterkit-async", json={"racks": ["U19", "U21"]})
        assert r.status_code == 200, r.get_json()
        assert recorded["scope"] == "multirack" and recorded["racks"] == ["U19", "U21"] and recorded["rack"] == ""


class TestMultirackLauncher:
    def _snippet(self, tmp_path, **env) -> subprocess.CompletedProcess:
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        start = sbatch.index('_ck_exe_opt=""')
        snippet = sbatch[start:sbatch.index("_ck_failed=()", start)]
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SLURM_JOB_ID": "77", **env}
        return subprocess.run(["bash", "-c", snippet + 'printf "%s|%s" "${_ck_exe_opt% }" "$CK_GPU_TESTS"'],
                              env=full, cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False)

    def test_scopes_become_clusterkits_scope_file_and_bisection_runs(self, tmp_path):
        r = self._snippet(tmp_path, RACK_CLUSTERKIT_SCOPES="U19:a,b;U21:c,d", RACK_CLUSTERKIT_TESTS="gpu_gpu_bw")
        exe, gpu = r.stdout.split("|")
        assert exe == f"-S {tmp_path}/scopes_77.csv -d bisection_bw"
        assert gpu == "0"  # the rack-level GPU tests do not apply across racks
        assert (tmp_path / "scopes_77.csv").read_text().splitlines() == ["a,U19,1", "b,U19,1", "c,U21,2", "d,U21,2"]

    def test_the_clusters_multirack_tests_are_used(self, tmp_path):
        r = self._snippet(tmp_path, RACK_CLUSTERKIT_SCOPES="U19:a;U21:c", RACK_CLUSTERKIT_MULTIRACK_TESTS="bisection_bw alltoall")
        assert r.stdout.split("|")[0].endswith("-d bisection_bw -d alltoall")

    def test_without_scopes_the_rack_tests_run_unchanged(self, tmp_path):
        assert self._snippet(tmp_path).stdout.split("|")[0] == "-d bw -d lat"

    def test_a_launch_cancels_only_its_own_stale_job(self):
        text = (SCRIPTS / "run_clusterkit.sh").read_text()
        assert '-v n="CLUSTERKIT-${rack}"' in text and "'$2 == n" in text
        assert "$2 ~ /^CLUSTERKIT/" not in text

    def test_the_catalog_offers_it_on_the_multirack_surface(self):
        from catalog import seed

        tool = {t["id"]: t for t in seed.SEED_TOOLS}["clusterkit-multirack"]
        assert "multirack" in tool["surfaces"] and tool["execution"]["target"] == "multirack"
        assert tool["launch"]["endpoint"] == "/api/run-clusterkit-async"
