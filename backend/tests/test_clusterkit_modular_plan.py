# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Offline stdlib tests. Never import app, start services or use a real scheduler."""
import importlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import config_env as C
from clusterkit_planner import (
    PlanningError,
    SlurmInventory,
    build_plan,
    parse_inventory,
    plan_request,
)
from clusterkit_profile import SETTING, parse_profile, resolve_profile


def profile(**changes):
    value = {"enabled": True, "partition": "testq", "hpcx_init": "/vendor/hpcx/hpcx-init.sh",
             "results_root": "/validation/results", "allowed_node_counts": [2, 4, 8], "max_nodes": 8,
             "expected_architecture": "aarch64", "expected_link_layer": "Ethernet",
             "hca": "test_rail:1", "node_regex": r"node[0-9]+", "path_comparison": True}
    value.update(changes)
    return value


def inventory(count=8, state="idle", partition="testq"):
    names = [f"node{i}" for i in range(1, count + 1)]
    return parse_inventory("\n".join(f"{n}|{state}|{partition}" for n in names), names)


class ProfileTests(unittest.TestCase):
    def test_disabled_is_default_and_incomplete_enabled_is_not_ready(self):
        self.assertFalse(parse_profile(None).ready)
        self.assertFalse(parse_profile({"enabled": True}).ready)
        self.assertGreater(len(parse_profile({"enabled": True}).errors), 4)

    def test_valid_profile_has_no_performance_acceptance(self):
        result = parse_profile(profile())
        self.assertTrue(result.ready, result.errors)
        self.assertFalse(result.profile.performance.enabled)
        self.assertEqual(result.public()["performance_status"], "UNVERIFIED")
        self.assertFalse(result.public()["compute_preflight_verified"])
        self.assertTrue(parse_profile(result.profile.as_dict()).ready)

    def test_invalid_and_unknown_configuration_fails_closed(self):
        examples = [[], "{", {"enabled": "yes"}, {"unknown": 1}, {"max_nodes": True},
                    {"max_nodes": 0}, {"allowed_node_counts": [2, 2]},
                    {"allowed_node_counts": [3]}, {"allowed_node_counts": [True]},
                    {"max_nodes": 2}, {"default_wall_seconds": 301},
                    {"partition": "q;id"}, {"hpcx_init": "relative"}, {"results_root": "/r/../escape"},
                    {"results_root": "/r,bad"}, {"hca": "test:1,other:1"}, {"hca": "test:0"},
                    {"ucx_transports": ["rc;id"]}, {"node_regex": "(a+)+"},
                    {"node_allowlist": ["node1", "NODE1"]}, {"node_allowlist": ["-n2"]},
                    {"functional": {"bandwidth_iterations": 0}},
                    {"functional": {"extra_args": "--with-stress"}},
                    {"performance": {"enabled": True}}, {"performance": {"latency_max_usec": float("nan")}},
                    {"performance": {"enabled": True, "reference": None}}]
        for example in examples:
            raw = profile(**example) if isinstance(example, dict) else example
            with self.subTest(example=example):
                self.assertFalse(parse_profile(raw).ready)

    def test_existing_profile_resolution_errors_are_reported(self):
        with mock.patch.object(C, "read_only_values", return_value={SETTING: json.dumps(profile())}):
            self.assertTrue(resolve_profile().ready)
        with mock.patch.object(C, "read_only_values", side_effect=ValueError("bad snapshot")):
            self.assertIn("bad snapshot", resolve_profile().errors[0])

    def test_preflight_only_and_larger_admin_ceiling_do_not_expand_capabilities(self):
        self.assertTrue(parse_profile(profile(max_nodes=1, allowed_node_counts=[])).ready)
        self.assertTrue(parse_profile(profile(max_nodes=64)).ready)
        self.assertFalse(parse_profile(profile(max_nodes=64, allowed_node_counts=[16])).ready)

    def test_verified_thresholds_still_do_not_accept_an_unrun_test(self):
        raw = profile(performance={"enabled": True, "verified": True, "reference": "fabric-team approval",
                                   "latency_max_usec": 10})
        result = parse_profile(raw)
        self.assertTrue(result.ready, result.errors)
        self.assertEqual(result.public()["performance_status"], "UNVERIFIED")
        plan = build_plan({"scope": "explicit", "nodes": ["node1", "node2"]}, result, inventory(2), scripts_dir="/scripts")
        self.assertEqual(plan["performance_status"], "UNVERIFIED")
        self.assertIsNone(plan["sbatch_argv"])

    def test_malformed_json_value_types_produce_readiness_instead_of_exceptions(self):
        for key in profile():
            for value in [None, False, 1, 1.5, [], {}, ""]:
                with self.subTest(key=key, value=value):
                    result = parse_profile(profile(**{key: value}))
                    self.assertIsInstance(result.public()["configuration_ready"], bool)

    def test_topology_is_explicit_and_rack_su_require_authority(self):
        groups = {"example": {"nodes": ["node1", "node2"], "kind": "group"}}
        result = parse_profile(profile(topology_groups=groups))
        self.assertTrue(result.ready, result.errors)
        self.assertTrue(parse_profile(result.profile.as_dict()).ready)
        groups["example"]["kind"] = "rack"
        self.assertFalse(parse_profile(profile(topology_groups=groups)).ready)
        groups["example"]["authoritative"] = True
        self.assertTrue(parse_profile(profile(topology_groups=groups)).ready)


class SettingsTests(unittest.TestCase):
    def test_registry_key_and_full_precedence_ladder(self):
        self.assertIn(SETTING, C._JOB_ENV_ALLOWED_KEYS)
        high, settings, env, low = ({SETTING: value} for value in ("high", "settings", "env", "low"))
        for layer, expected in [(high, "high"), (settings, "settings"), (env, "env"), (low, "low")]:
            self.assertEqual(C._resolve_setting(SETTING, "default", high, settings, env, low), expected)
            layer.clear()
        self.assertEqual(C._resolve_setting(SETTING, "default", high, settings, env, low), "default")
        self.assertEqual(C._resolve_setting(SETTING, "default", {SETTING: " "}, {SETTING: ""},
                                           {SETTING: ""}, {SETTING: "low"}), "")

    def test_readonly_settings_do_not_create_files_or_change_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);store = root / "missing" / "settings.json"
            context = types.SimpleNamespace(overlays_for_active_readonly=lambda: ({}, {SETTING: "low"}))
            before = dict(os.environ)
            with mock.patch.object(C, "_job_env_overrides_path", return_value=store), \
                    mock.patch.object(C, "_CATALOG_AVAILABLE", True), \
                    mock.patch.object(C, "catalog_context", context), \
                    mock.patch.object(C, "_job_env_flock", side_effect=AssertionError("lock write")), \
                    mock.patch.object(C, "_materialize_job_env_overrides_into_os_environ", side_effect=AssertionError("materialize")):
                self.assertEqual(C.read_only_values((SETTING,))[SETTING], "low")
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(dict(os.environ), before)

    def test_readonly_settings_precedence_blank_and_corrupt_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({SETTING: "settings"}))
            overlays = {"high": {SETTING: "high"}, "low": {SETTING: "low"}}
            context = types.SimpleNamespace(overlays_for_active_readonly=lambda: (overlays["high"], overlays["low"]))
            with mock.patch.object(C, "_job_env_overrides_path", return_value=path), \
                    mock.patch.object(C, "_CATALOG_AVAILABLE", True), mock.patch.object(C, "catalog_context", context), \
                    mock.patch.dict(os.environ, {SETTING: "env"}):
                self.assertEqual(C.read_only_values((SETTING,))[SETTING], "high")
                overlays["high"].clear()
                self.assertEqual(C.read_only_values((SETTING,))[SETTING], "settings")
                path.write_text(json.dumps({SETTING: " "}))
                self.assertEqual(C.read_only_values((SETTING,))[SETTING], "env")
                path.write_text("{bad")
                with self.assertRaises(ValueError): C.read_only_values((SETTING,))

    def test_normal_getter_retains_legacy_high_and_settings_behavior(self):
        context = types.SimpleNamespace(overlays_for_active=lambda: ({SETTING: "high"}, {SETTING: "low"}))
        with mock.patch.object(C, "_CATALOG_AVAILABLE", True), mock.patch.object(C, "catalog_context", context), \
                mock.patch.object(C, "_job_env_overrides_load", return_value={SETTING: "settings"}) as loader:
            self.assertEqual(C._eget(SETTING), "high")
            loader.assert_not_called()
            context.overlays_for_active = lambda: ({}, {SETTING: "low"})
            self.assertEqual(C._eget(SETTING), "settings")

    def test_missing_catalog_dependency_cannot_discard_an_existing_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json";path.write_text("{}")
            with mock.patch.object(C, "_CATALOG_AVAILABLE", False), \
                    mock.patch.dict(os.environ, {"NVIS_CATALOG_FILE": str(path)}), self.assertRaises(ValueError):
                C.read_only_values((SETTING,))


class PlannerTests(unittest.TestCase):
    def plan(self, request=None, raw_profile=None, snapshot=None):
        return build_plan(request or {"scope": "explicit", "nodes": ["node1", "node2"]},
                          parse_profile(raw_profile or profile()), snapshot or inventory(), scripts_dir="/reviewed/bin/scripts")

    def test_explicit_2_4_8_nodes_and_unique_pair_coverage(self):
        for count, expected_pairs in [(2, 1), (4, 6), (8, 28)]:
            names = [f"node{i}" for i in range(1, count + 1)]
            p = self.plan({"scope": "explicit", "nodes": names, "node_count": count}, snapshot=inventory(count))
            self.assertEqual(p["nodes"], names)
            self.assertEqual(p["expected_pair_count"], expected_pairs)
            self.assertEqual(p["resources"]["ntasks"], count)
            self.assertFalse(p["submission_enabled"])
            self.assertEqual(p["performance_status"], "UNVERIFIED")
            self.assertTrue(p["entrypoint_supported"])
            self.assertIn("clusterkit_pairwise.sbatch", p["entrypoint"])
            self.assertEqual(p["sbatch_argv"][-2], p["results_directory"] + "/plan.json")

    def test_one_node_preflight_has_no_inter_node_claim_or_fake_entrypoint(self):
        p = self.plan({"scope": "preflight", "nodes": ["node1"]}, snapshot=inventory(1))
        self.assertEqual(p["expected_pairs"], [])
        self.assertEqual(p["resources"]["nodes"], 1)
        self.assertEqual(p["request"]["test_mode"], "preflight")
        self.assertTrue(p["entrypoint"].endswith("clusterkit_preflight.sbatch"))
        self.assertIsNotNone(p["sbatch_argv"])

    def test_exact_reviewed_two_node_resources_export_and_shell_roundtrip(self):
        p = self.plan()
        argv = p["sbatch_argv"]
        self.assertEqual(shlex.split(p["command_display"]), argv)
        for value in ["--nodes=2", "--ntasks=2", "--ntasks-per-node=1", "--cpus-per-task=1", "--mem=1G", "--gres=none", "--no-requeue", "--time=00:05:00", "--nodelist=node1,node2"]:
            self.assertIn(value, argv)
        self.assertNotIn("--exclusive", argv)
        self.assertFalse(any(a.startswith("--export=") for a in argv))
        self.assertEqual(p["effective_profile"]["gid_policy"], "global")
        self.assertEqual(p["effective_profile"]["hca"], "test_rail:1")
        self.assertEqual(argv[-3], "/reviewed/bin/scripts/clusterkit_pairwise.sbatch")
        self.assertNotIn("--wrap", argv)

    def test_ineligible_unknown_wrong_partition_and_flagged_idle_are_rejected(self):
        for state in ["down", "drained", "draining", "idle+drain", "idle*", "idle~", "reserved", "future", "unknown"]:
            with self.subTest(state=state), self.assertRaises(PlanningError):
                self.plan(snapshot=inventory(2, state))
        with self.assertRaises(PlanningError): self.plan(snapshot=inventory(2, partition="otherq"))
        with self.assertRaises(PlanningError): self.plan(snapshot=inventory(1))

    def test_eligible_busy_explicit_nodes_are_not_called_idle(self):
        p = self.plan(snapshot=inventory(2, "allocated"))
        self.assertTrue(all(n["eligible_but_busy"] for n in p["availability"]))
        self.assertTrue(all(not n["currently_idle"] for n in p["availability"]))
        self.assertTrue(any("may wait" in w for w in p["warnings"]))

    def test_group_selection_is_exact_configured_deterministic_and_idle_only(self):
        groups = {"pool": {"nodes": [f"node{i}" for i in range(8, 0, -1)], "source": "admin list"}}
        raw = profile(topology_groups=groups)
        for count in (2, 4, 8):
            p = self.plan({"scope": "group", "group": "pool", "node_count": count}, raw_profile=raw)
            self.assertEqual(p["nodes"], [f"node{i}" for i in range(1, count + 1)])
            self.assertTrue(all(t["membership"] == "configured" for t in p["topology"]))
        snap = inventory();snap["nodes"][0]["states"] = ["ALLOCATED"]
        p = self.plan({"scope": "group", "group": "pool", "node_count": 2}, raw_profile=raw, snapshot=snap)
        self.assertEqual(p["nodes"], ["node2", "node3"])
        with self.assertRaisesRegex(PlanningError, "eligible but busy"):
            self.plan({"scope": "group", "group": "pool", "node_count": 8}, raw_profile=raw, snapshot=snap)

    def test_explicit_topology_is_unknown_without_mapping(self):
        p = self.plan()
        self.assertTrue(all(t["membership"] == "unknown" for t in p["topology"]))

    def test_limits_and_deployment_override_fields_fail_before_discovery(self):
        reader = mock.Mock();reader.query.side_effect = AssertionError("unexpected discovery")
        raw_profile = profile(max_nodes=2, allowed_node_counts=[2])
        requests = [
            {"scope": "explicit", "nodes": ["node1"]},
            {"scope": "explicit", "nodes": ["node1", "node2", "node3", "node4"]},
            {"scope": "explicit", "nodes": ["node1", "node2"], "hca": "other:1"},
            {"scope": "explicit", "nodes": ["node1", "node2"], "results_root": "/tmp"},
            {"scope": "explicit", "nodes": ["node1", "node2"], "wall_seconds": 999999},
            {"scope": "group", "group": "unknown", "node_count": 2}]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(PlanningError):
                plan_request(request, parse_profile(raw_profile), reader, scripts_dir="/scripts")
        reader.query.assert_not_called()

    def test_disabled_incomplete_or_missing_profile_never_queries_slurm(self):
        reader = mock.Mock()
        for raw in [None, {"enabled": False}, {"enabled": True}]:
            with self.subTest(raw=raw), self.assertRaises(PlanningError):
                plan_request({"scope": "explicit", "nodes": ["node1", "node2"]}, parse_profile(raw), reader, scripts_dir="/scripts")
        reader.query.assert_not_called()

    def test_administrator_paths_roundtrip_as_single_shell_safe_arguments(self):
        p = self.plan(raw_profile=profile(results_root="/review/output with spaces/$(id)", hpcx_init="/vendor/it's hpcx/hpcx-init.sh"))
        self.assertEqual(shlex.split(p["command_display"]), p["sbatch_argv"])
        self.assertIn("output with spaces/$(id)", p["results_directory"])

    def test_untrusted_labels_hostnames_counts_and_scope_mixing(self):
        bad_nodes = ["-N8", "node[1-2]", "node1,node2", "node1;id", "$(id)", "node1\nnode2", "../node", "node1 "]
        for node in bad_nodes:
            with self.subTest(node=node), self.assertRaises(PlanningError):
                self.plan({"scope": "explicit", "nodes": [node, "node2"]})
        for request in [
            {"scope": "explicit", "nodes": ["node1", "NODE1"]},
            {"scope": "explicit", "nodes": ["node1", "node2"], "label": "../escape"},
            {"scope": "explicit", "nodes": ["node1", "node2"], "label": "$(id)"},
            {"scope": "explicit", "nodes": ["node1", "node2"], "node_count": True},
            {"scope": "explicit", "nodes": ["node1", "node2"], "node_count": 4},
            {"scope": "preflight", "nodes": ["node1", "node2"]},
            {"scope": "preflight", "nodes": ["node1"], "path_comparison": True},
            {"scope": "group", "group": "pool", "nodes": ["node1"], "node_count": 2}]:
            with self.subTest(request=request), self.assertRaises(PlanningError): self.plan(request)

    def test_allowlist_intersects_regex_and_group_members(self):
        raw = profile(node_allowlist=["node2", "node3"], topology_groups={"pool": {"nodes": ["node1", "node2", "node3"]}})
        snap = parse_inventory("node2|idle|testq\nnode3|idle|testq", ["node2", "node3"])
        p = self.plan({"scope": "group", "group": "pool", "node_count": 2}, raw_profile=raw, snapshot=snap)
        self.assertEqual(p["nodes"], ["node2", "node3"])
        with self.assertRaises(PlanningError): self.plan(raw_profile=raw)

    def test_unimplemented_policies_do_not_get_a_misleading_executable_plan(self):
        for changes in [{"gid_policy": "auto"}, {"expected_link_layer": "InfiniBand"},
                        {"ucx_transports": ["tcp", "self"]}, {"default_wall_seconds": 60}]:
            with self.subTest(changes=changes):
                p = self.plan(raw_profile=profile(**changes))
                self.assertIsNone(p["sbatch_argv"])
                self.assertFalse(p["entrypoint_supported"])

    def test_plan_has_zero_filesystem_scheduler_and_environment_side_effects(self):
        resolved = parse_profile(profile());snap = inventory(2);before = dict(os.environ)
        with mock.patch("builtins.open", side_effect=AssertionError("file open")), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")), \
                mock.patch.object(Path, "open", side_effect=AssertionError("path open")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process execution")):
            p = build_plan({"scope": "explicit", "nodes": ["node1", "node2"]}, resolved, snap, scripts_dir="/reviewed/scripts")
        self.assertTrue(p["dry_run"])
        self.assertFalse(p["submitted"])
        self.assertEqual(before, dict(os.environ))
        json.dumps(p)

    def test_static_adapter_only_issues_one_bounded_sinfo_command(self):
        connection, platform = mock.Mock(), mock.Mock()
        platform.sinfo_cmd.side_effect = lambda args: "sinfo " + args
        connection.run.return_value = types.SimpleNamespace(returncode=0, stdout="node1|idle|testq*\nnode2|idle|testq")
        reader = SlurmInventory(connection, platform)
        p = plan_request({"scope": "explicit", "nodes": ["node1", "node2"]}, parse_profile(profile()), reader, scripts_dir="/scripts")
        self.assertEqual(p["nodes"], ["node1", "node2"])
        connection.run.assert_called_once()
        args, kwargs = connection.run.call_args
        self.assertEqual(shlex.split(args[0]), ["sinfo", "-N", "-h", "-o", "%N|%T|%P", "-n", "node1,node2"])
        self.assertEqual(kwargs, {"timeout": 15, "login_shell": False})
        connection.run.return_value.returncode = 1
        with self.assertRaises(PlanningError): reader.query(["node1", "node2"])

    def test_inventory_timeout_is_structured_and_never_retried(self):
        connection, platform = mock.Mock(), mock.Mock()
        platform.sinfo_cmd.return_value = "sinfo --read-only-query"
        connection.run.side_effect = subprocess.TimeoutExpired("sinfo", 15)
        with self.assertRaisesRegex(PlanningError, "timed out; no retry"):
            SlurmInventory(connection, platform).query(["node1", "node2"])
        connection.run.assert_called_once()


class CatalogSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The helper host lacks TinyDB. Load the actual catalog read code under
        # an isolated package with a persistence constructor that always aborts.
        # This tests the real JSON snapshot/overlay code, not a writable database.
        package = types.ModuleType("_ck_readonly_catalog_tests")
        package.__path__ = [str(BACKEND / "catalog")]
        tinydb, storages = types.ModuleType("tinydb"), types.ModuleType("tinydb.storages")
        class NoDatabase:
            def __init__(self, *a, **kw): raise AssertionError("Writable TinyDB was constructed")
        tinydb.Query = tinydb.TinyDB = NoDatabase
        storages.Storage = object
        storages.touch = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Catalog file touched"))
        patch = mock.patch.dict(sys.modules, {"_ck_readonly_catalog_tests": package, "tinydb": tinydb, "tinydb.storages": storages})
        patch.start()
        cls.addClassCleanup(patch.stop)
        cls.context = importlib.import_module("_ck_readonly_catalog_tests.context")
        cls.store = importlib.import_module("_ck_readonly_catalog_tests.store")
        cls.models = importlib.import_module("_ck_readonly_catalog_tests.models")

    def test_existing_cluster_profile_roundtrips_and_wins_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);catalog = root / "catalog.json"
            db = {"projects": {"1": {"id": "p", "name": "P", "clusters": [
                {"id": "c", "name": "C", "platform_id": "hw", "clusterkit_profile": profile()}]}},
                  "platforms": {"1": {"id": "hw", "name": "HW", "tool_param_defaults": {SETTING: "low"}}},
                  "meta": {"1": {"key": "active", "value": {"project": "p", "cluster": "c"}}}}
            catalog.write_text(json.dumps(db));before = catalog.read_bytes()
            with mock.patch.dict(os.environ, {"NVIS_CATALOG_FILE": str(catalog)}), \
                    mock.patch.object(self.store, "get_repo", side_effect=AssertionError("Writable catalog opened")):
                high, low = self.context.overlays_for_active_readonly()
            self.assertEqual(json.loads(high[SETTING]), profile())
            self.assertEqual(low[SETTING], "low")
            cluster = self.models.Cluster.from_dict(db["projects"]["1"]["clusters"][0])
            self.assertEqual(cluster.to_dict()["clusterkit_profile"], profile())
            self.assertEqual(before, catalog.read_bytes())
            self.assertEqual(list(root.iterdir()), [catalog])

    def test_missing_catalog_is_not_created_and_corrupt_catalog_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "catalog.json"
            with mock.patch.dict(os.environ, {"NVIS_CATALOG_FILE": str(path)}):
                self.assertEqual(self.context.overlays_for_active_readonly(), ({}, {}))
                self.assertFalse(path.parent.exists())
            path = Path(directory) / "catalog.json";path.write_text("{broken")
            with mock.patch.dict(os.environ, {"NVIS_CATALOG_FILE": str(path)}), self.assertRaises(ValueError):
                self.context.overlays_for_active_readonly()

    def test_empty_cluster_profile_disables_inheritance_and_absent_profile_inherits(self):
        for raw, expected in [(None, False), ({}, True)]:
            cluster = self.models.Cluster.from_dict({"id": "c", "name": "C", "clusterkit_profile": raw})
            high = self.context.high_overlay(cluster=cluster)
            self.assertEqual(SETTING in high, expected)
            if expected:
                self.assertFalse(parse_profile(high[SETTING]).ready)


class PrintOnlyCliTests(unittest.TestCase):
    def test_cli_uses_real_profile_and_fake_readonly_sinfo_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sinfo = root / "sinfo"
            sinfo.write_text('#!/bin/sh\nprintf "node1|idle|testq\\nnode2|idle|testq\\n"\n')
            sinfo.chmod(0o700)
            calls = root / "forbidden-calls"
            for name in ("sbatch", "srun", "salloc", "ssh"):
                file = root / name
                file.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$CLI_FORBIDDEN_CALLS"\nexit 97\n')
                file.chmod(0o700)
            env = dict(os.environ, PATH=str(root)+':'+os.environ['PATH'],
                       NVIS_CATALOG_FILE=str(root/'missing-catalog.json'),
                       RACK_CLUSTERKIT_SITE_PROFILE=json.dumps(profile()),
                       RACK_SLURM_SINFO=str(sinfo), RACK_SLURM_SKIP_MODULE_LOAD="1",
                       CLI_FORBIDDEN_CALLS=str(calls), PYTHONDONTWRITEBYTECODE="1")
            before = {p.name:p.read_bytes() for p in root.iterdir()}
            command = [sys.executable, str(BACKEND.parent/'bin/scripts/plan_clusterkit.py'), 'plan',
                       '--local-inventory', '--request', json.dumps({'scope':'explicit','nodes':['node1','node2']})]
            result = subprocess.run(command,env=env,capture_output=True,text=True,timeout=10,check=False)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue(result.stdout.startswith('PRINT-ONLY DRY-RUN'))
            plan = json.loads(result.stdout.split('\n',1)[1])
            self.assertEqual(plan['nodes'],['node1','node2'])
            self.assertFalse(plan['submitted'])
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})
            self.assertFalse(calls.exists())

    def test_cli_has_no_execute_mode(self):
        result = subprocess.run([sys.executable, str(BACKEND.parent/'bin/scripts/plan_clusterkit.py'), 'execute'],
                                capture_output=True,text=True,timeout=5,check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('invalid choice',result.stderr)


if __name__ == "__main__":
    unittest.main()
