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
        assert c.to_dict()["clusterkit"] == {
            "tests": ["gpu_gpu_bw", "gpu_gpu_lat"], "gpudirect": True, "multirack_tests": [],
            "env": {"rack": {}, "multirack": {}},
        }
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


@pytest.fixture
def vr(monkeypatch, tmp_path):
    """The ClusterKit prepares with the cluster around them stubbed: two placed racks, no campaign."""
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
    monkeypatch.setattr(validation_runs, "validation_target_for_rack", lambda rack, _ov: f"{rack}-ct[01-18]")
    from app import app

    with app.app_context():  # the refusals build their responses with jsonify
        yield validation_runs


class TestMultirackPrepare:
    def test_two_racks_run_across_racks_with_each_rack_a_scope(self, vr):
        prep, err = vr._prepare_run_clusterkit_singlerack({"racks": ["U19", "U21"]})
        assert err is None, err
        assert prep["racks"] == ["U19", "U21"]
        assert prep["nodes"] == U19 + U21
        assert prep["cmd"][-1] == ",".join(U19 + U21)
        env = prep["env_updates"]
        assert env["RACK_CLUSTERKIT_RACK"] == "U19+U21"
        assert env["RACK_CLUSTERKIT_SCOPES"] == f"U19:{','.join(U19)};U21:{','.join(U21)}"

    def test_a_single_rack_in_racks_stays_the_single_rack_run(self, vr):
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


# --- the cluster's environment, per launch level --------------------------------------------------


class TestTheClustersEnvironment:
    """Variables a site gives every ClusterKit job, per level, without touching Settings or the scripts.

    NscaleTX-NC's GB300 platform defaults to CUDA 12, but the site's workspace holds only the CUDA 13
    HPC-X (2.25.1): job 190457 on U2 died at start looking for hpcx-v2.22.1-...-cuda12. CUDA_MAJ=13 in
    the cluster's ClusterKit environment is the site saying what it installed.
    """

    @staticmethod
    def _with_cluster(vr, monkeypatch, **env):
        cluster = _cluster(env=env)
        monkeypatch.setattr(vr.catalog_context, "resolve", lambda: (None, cluster, None))
        # The platform's default, as the low overlay serves it on NC.
        monkeypatch.setattr(vr, "_eget", lambda key, *a, **k: "12" if key == "CUDA_MAJ" else "")

    def test_a_cluster_stored_before_the_field_has_empty_lists(self):
        ck = models.Cluster.from_dict({"id": "c1", "name": "C1", "clusterkit": {"tests": ["bw"]}}).clusterkit
        assert ck.env == {"rack": {}, "multirack": {}}

    def test_the_lists_round_trip_and_keep_numbers_as_values(self):
        c = _cluster(env={"rack": {"CUDA_MAJ": 13}, "multirack": {"UCX_TLS": "rc,cuda"}})
        assert c.to_dict()["clusterkit"]["env"] == {"rack": {"CUDA_MAJ": "13"}, "multirack": {"UCX_TLS": "rc,cuda"}}
        assert not c.validate()

    @pytest.mark.parametrize(("env", "fragment"), [
        ({"rack": {"9BAD": "1"}}, "clusterkit.env.rack: '9BAD' is not an environment variable name"),
        ({"multirack": {"A": "x\ny"}}, "clusterkit.env.multirack.A must be a single line"),
        ({"rack": {f"V{i}": "1" for i in range(models.LAUNCH_ENV_MAX_VARS + 1)}}, "at most"),
    ])
    def test_the_launch_dialogs_rules_apply(self, env, fragment):
        # The same rules as the launch dialog's extra variables: both end in the same job env.
        assert any(fragment in p for p in _cluster(env=env).validate())

    def test_a_rack_launch_takes_the_rack_list_over_the_platform_default(self, vr, monkeypatch):
        self._with_cluster(vr, monkeypatch, rack={"CUDA_MAJ": "13"}, multirack={"ONLY_ACROSS": "1"})
        prep, err = vr._prepare_run_clusterkit_singlerack({"rack": "U19"})
        assert err is None, err
        env = prep["env_updates"]
        assert env["CUDA_MAJ"] == "13"
        assert "ONLY_ACROSS" not in env  # the lists do not inherit from one another
        assert env["NVIS_EXTRA_ENV_KEYS"] == "CUDA_MAJ"

    def test_a_multirack_launch_takes_the_multirack_list(self, vr, monkeypatch):
        self._with_cluster(vr, monkeypatch, rack={"CUDA_MAJ": "13"}, multirack={"CUDA_MAJ": "13", "UCX_TLS": "rc"})
        prep, err = vr._prepare_run_clusterkit_singlerack({"racks": ["U19", "U21"]})
        assert err is None, err
        env = prep["env_updates"]
        assert (env["CUDA_MAJ"], env["UCX_TLS"]) == ("13", "rc")
        assert env["NVIS_EXTRA_ENV_KEYS"] == "CUDA_MAJ UCX_TLS"
        # The scopes are the launch's structure, never something a list can replace.
        assert env["RACK_CLUSTERKIT_SCOPES"].startswith("U19:")

    def test_without_a_list_the_platform_default_and_no_forwarding(self, vr, monkeypatch):
        self._with_cluster(vr, monkeypatch)
        prep, _err = vr._prepare_run_clusterkit_singlerack({"rack": "U19"})
        assert prep["env_updates"]["CUDA_MAJ"] == "12"
        assert "NVIS_EXTRA_ENV_KEYS" not in prep["env_updates"]

    def test_an_unreadable_catalog_launches_without_the_list(self, vr, monkeypatch):
        def broken():
            raise RuntimeError("catalog unavailable")

        monkeypatch.setattr(vr.catalog_context, "resolve", broken)
        prep, err = vr._prepare_run_clusterkit_singlerack({"rack": "U19"})
        assert err is None and "NVIS_EXTRA_ENV_KEYS" not in prep["env_updates"]

    @staticmethod
    def _forwarded(keys: str) -> str:
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        start = sbatch.index('_ck_extra_x=""')
        snippet = sbatch[start:sbatch.index('for idx in "${!_CK_ROWS[@]}"', start)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NVIS_EXTRA_ENV_KEYS": keys}
        r = subprocess.run(["bash", "-c", snippet + 'printf "|%s" "$_ck_extra_x"'],
                           env=env, capture_output=True, text=True, timeout=30, check=False)
        return r.stdout.split("|", 1)[1]

    def test_the_launcher_forwards_each_listed_name_to_every_rank(self):
        assert self._forwarded("CUDA_MAJ UCX_TLS") == " -x CUDA_MAJ -x UCX_TLS"

    def test_the_launcher_forwards_nothing_it_cannot_name(self):
        # The names reach mpirun's command line; one that is not a variable name never does.
        assert self._forwarded("GOOD $(id) 1BAD") == " -x GOOD"
        assert self._forwarded("") == ""

    def test_both_fabric_branches_carry_the_forwarding(self):
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        mpi_opts = [ln for ln in sbatch.splitlines() if ln.strip().startswith("mpi_opt=")]
        assert len(mpi_opts) == 2 and all(ln.rstrip().endswith('${_ck_extra_x}"') for ln in mpi_opts)


# --- pairs ------------------------------------------------------------------------------------------


class TestPairs:
    """ClusterKit pairs nodes and refuses an odd count, failing every rail with no data.

    Job 192679 on U2: 9 of 18 nodes drained, the launcher sent the other 9, and ClusterKit answered
    "pairwise tests must be run with an even number of nodes" on all four rails.
    """

    @staticmethod
    def _submit(tmp_path, hosts: str) -> subprocess.CompletedProcess:
        fake = tmp_path / "bin"
        fake.mkdir()
        (fake / "sbatch").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$SBATCH_ARGS"\necho "Submitted batch job 1"\n')
        (fake / "squeue").write_text("#!/bin/bash\nexit 0\n")
        for f in ("sbatch", "squeue"):
            (fake / f).chmod(0o755)
        env = {
            "PATH": f"{fake}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "NVIS_SLURM_HOSTLIST": hosts,
            "NVIS_WORKSPACE_DIR": str(tmp_path),
            "RACK_JOB_LOG_DIR": str(tmp_path / "logs"),
            "RACK_CLUSTERKIT_RACK": "U2",
            "SBATCH_ARGS": str(tmp_path / "sbatch.args"),
            "USER": "t",
        }
        return subprocess.run(["bash", str(SCRIPTS / "run_clusterkit.sh"), "U2"], env=env, cwd=tmp_path,
                              capture_output=True, text=True, timeout=60, check=False)

    def _sbatch_args(self, tmp_path) -> list[str]:
        return (tmp_path / "sbatch.args").read_text().splitlines()

    def test_an_odd_count_leaves_the_last_node_out_by_name(self, tmp_path):
        r = self._submit(tmp_path, "n1,n2,n3,n4,n5")
        assert r.returncode == 0, r.stderr
        args = self._sbatch_args(tmp_path)
        assert args[args.index("-N") + 1] == "4"
        assert args[args.index("-w") + 1] == "n1,n2,n3,n4"
        assert "4 of 5 schedulable nodes tested, n5 left out" in r.stderr

    def test_an_even_count_is_submitted_whole(self, tmp_path):
        r = self._submit(tmp_path, "n1,n2,n3,n4")
        assert r.returncode == 0, r.stderr
        args = self._sbatch_args(tmp_path)
        assert args[args.index("-w") + 1] == "n1,n2,n3,n4" and "left out" not in r.stderr

    def test_the_scope_file_holds_only_hosts_the_job_has(self, tmp_path):
        # A multi-rack run whose scopes name a node the launcher dropped (drained, or the odd one out).
        hostfile = tmp_path / "machinefile"
        hostfile.write_text("a\nb\nc\nd\n")
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        start = sbatch.index('_ck_exe_opt=""')
        snippet = sbatch[start:sbatch.index("_ck_failed=()", start)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SLURM_JOB_ID": "9", "HOSTFILE": str(hostfile),
               "RACK_CLUSTERKIT_SCOPES": "U19:a,b,x;U21:c,d"}
        subprocess.run(["bash", "-c", snippet], env=env, cwd=tmp_path, capture_output=True, text=True,
                       timeout=30, check=False)
        assert (tmp_path / "scopes_9.csv").read_text().splitlines() == ["a,U19,1", "b,U19,1", "c,U21,2", "d,U21,2"]
