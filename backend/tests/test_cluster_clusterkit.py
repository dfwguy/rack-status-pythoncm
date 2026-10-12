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
        assert c.to_dict()["clusterkit"] == {"tests": ["gpu_gpu_bw", "gpu_gpu_lat"], "gpudirect": True}
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
        import inspect

        from api import validation_runs

        assert "RACK_CLUSTERKIT_GPUDIRECT" in inspect.getsource(validation_runs._prepare_run_clusterkit_singlerack)
