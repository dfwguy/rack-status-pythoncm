# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Offline tests for the product-facing ClusterKit backend lifecycle."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SCRIPTS = REPO / "bin" / "scripts"
sys.path.insert(0, str(BACKEND))

import clusterkit_service as service
from clusterkit_planner import parse_inventory
from clusterkit_profile import parse_profile
from schedulers import JobHandle


class FakeReader:
    def __init__(self, nodes, *, state="idle"):
        self.nodes = list(nodes)
        self.state = state
        self.queries = []

    def query(self, candidates):
        self.queries.append(tuple(candidates))
        return parse_inventory(
            "\n".join(f"{node}|{self.state}|testq" for node in candidates),
            candidates,
        )


class FakeScheduler:
    def __init__(self):
        self.specs = []
        self.state = "PENDING"
        self.cancelled = []

    def submit(self, spec):
        self.specs.append(spec)
        return JobHandle("12345", "slurm", "12345")

    def status(self, handle):
        self.last_status = handle
        return self.state

    def find_by_tag(self, *args):
        self.recovery_query = args
        return getattr(self, "recovered_handle", None)

    def cancel(self, handle):
        self.cancelled.append(handle.job_id)


class FakeRegistry:
    def __init__(self):
        self.rows = {}

    def record_activity(self, tool, **fields):
        activity_id = fields["activity_id"]
        self.rows[activity_id] = {
            "activity_id": activity_id,
            "tool": tool,
            "phase": "starting",
            "work_phase": "pending",
            **copy.deepcopy(fields),
        }
        return activity_id

    def update_activity(self, activity_id, **fields):
        self.rows[activity_id].update(fields)

    def add_or_update_task(self, activity_id, **fields):
        tasks = self.rows[activity_id]["tasks"]
        task = next(
            (item for item in tasks if item.get("task_id") == fields.get("task_id")),
            tasks[0],
        )
        task.update({key: value for key, value in fields.items() if value is not None})
        return str(task["task_id"])

    def get_activity(self, activity_id):
        row = self.rows.get(activity_id)
        return copy.deepcopy(row) if row else None


def write_passing_summary(plan, root, job_id="12345"):
    if __package__:
        from .test_clusterkit_execution import node_record, payload
    else:
        from test_clusterkit_execution import node_record, payload
    from clusterkit_results import summarize
    for rank in range(plan['node_count']):
        record = node_record(plan,rank)
        record['job_id'] = job_id
        (root/f'node-{rank}-before.json').write_text(json.dumps(record))
    stages = [] if plan['scope']=='preflight' else ['baseline'] + (['single-path'] if plan['effective_profile']['path_comparison'] else [])
    for stage in stages:
        directory = root/stage/'vendor-date'
        directory.mkdir(parents=True)
        for test in ('bandwidth','latency'):
            raw = payload(plan,test)
            raw['JOBID'] = job_id
            (directory/(test+'.json')).write_text(json.dumps(raw))
        for rank in range(plan['node_count']):
            record = node_record(plan,rank)
            record.update(job_id=job_id,stage=stage)
            (root/f'rank-placement-{stage}-{rank}.json').write_text(json.dumps(record))
    outcome = {'job_id':job_id,'preflight_exit':0,'primary_exit':0,
               'stages':{stage:0 for stage in stages},'errors':[],'diagnostics':{}}
    summary = summarize(plan,root,outcome)
    (root/'summary.json').write_text(json.dumps(summary))
    return summary


class ClusterKitServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.results = self.root / "results"
        self.results.mkdir()
        self.hpcx = self.root / "hpcx-init.sh"
        self.hpcx.write_text("# reviewed fake init\n")
        self.nodes = [f"node{i}" for i in range(1, 9)]
        self.secret = "s" * 64

    def resolution(self, **changes):
        raw = {
            "enabled": True,
            "partition": "testq",
            "hpcx_init": str(self.hpcx),
            "results_root": str(self.results),
            "allowed_node_counts": [2, 4, 8],
            "max_nodes": 8,
            "default_wall_seconds": 300,
            "max_wall_seconds": 300,
            "expected_architecture": "aarch64",
            "hca_policy": "configured",
            "hca": "fake_rail:1",
            "expected_link_layer": "Ethernet",
            "node_allowlist": self.nodes,
            "topology_groups": {
                "u5": {"nodes": self.nodes, "kind": "su", "authoritative": True,
                       "source": "test mapping"}
            },
        }
        raw.update(changes)
        result = parse_profile(raw)
        self.assertTrue(result.ready, result.errors)
        return result

    def request(self, count=2):
        return {"scope": "explicit", "nodes": self.nodes[:count], "node_count": count,
                "label": f"pair-{count}"}

    def test_readiness_and_inventory_expose_configured_choices(self):
        resolution = self.resolution()
        ready = service.readiness(resolution)
        self.assertTrue(ready["configuration_ready"])
        self.assertEqual(ready["allowed_node_counts"], [2, 4, 8])
        self.assertEqual(ready["groups"][0]["name"], "u5")
        snapshot = service.inventory(resolution, FakeReader(self.nodes))
        self.assertEqual(len(snapshot["nodes"]), 8)

    def test_preview_is_read_only_signed_and_revalidated(self):
        resolution = self.resolution()
        reader = FakeReader(self.nodes)
        result = service.preview(self.request(), resolution, reader, scripts_dir=str(SCRIPTS),
                                 secret=self.secret, now=1000, ttl_seconds=60)
        self.assertTrue(result["plan"]["dry_run"])
        self.assertFalse(result["plan"]["submitted"])
        confirmed = service.confirm_plan(result["confirmation_token"], resolution, reader,
                                         scripts_dir=str(SCRIPTS), secret=self.secret, now=1050)
        self.assertEqual(confirmed["attempt_id"], result["plan"]["attempt_id"])
        self.assertEqual(confirmed["nodes"], self.nodes[:2])
        self.assertEqual(list(self.results.iterdir()), [])

    def test_confirmation_rejects_tampering_expiry_and_policy_change(self):
        resolution = self.resolution()
        reader = FakeReader(self.nodes)
        result = service.preview(self.request(), resolution, reader, scripts_dir=str(SCRIPTS),
                                 secret=self.secret, now=1000, ttl_seconds=60)
        token = result["confirmation_token"]
        with self.assertRaises(service.ConfirmationError):
            service.verify_preview(token + "x", self.secret, now=1001)
        with self.assertRaises(service.ConfirmationError):
            service.verify_preview(token, self.secret, now=1061)
        changed = self.resolution(path_comparison=True)
        with self.assertRaisesRegex(service.ConfirmationError, "policy changed"):
            service.confirm_plan(token, changed, reader, scripts_dir=str(SCRIPTS),
                                 secret=self.secret, now=1001)

    def test_submit_is_exactly_once_and_uses_static_script_arguments(self):
        resolution = self.resolution()
        reader = FakeReader(self.nodes)
        scheduler = FakeScheduler()
        registry = FakeRegistry()
        result = service.preview(self.request(4), resolution, reader, scripts_dir=str(SCRIPTS),
                                 secret=self.secret, now=1000, ttl_seconds=60)
        submitted = service.submit_confirmed(
            result["confirmation_token"], resolution, reader, scheduler, registry,
            scripts_dir=str(SCRIPTS), secret=self.secret, operator="tester", now=1001,
        )
        self.assertEqual(submitted["scheduler_job_id"], "12345")
        self.assertEqual(len(scheduler.specs), 1)
        spec = scheduler.specs[0]
        self.assertEqual(spec.nodes, 4)
        self.assertEqual(spec.ntasks_per_node, 1)
        self.assertTrue(spec.script.endswith("clusterkit_pairwise.sbatch"))
        self.assertEqual(spec.script_args[0], str(Path(submitted["results_directory"]) / "plan.json"))
        self.assertIn("--gres=none", spec.extra_args)
        self.assertNotIn("--exclusive", spec.extra_args)
        with self.assertRaises(service.DuplicateSubmissionError):
            service.submit_confirmed(
                result["confirmation_token"], resolution, reader, scheduler, registry,
                scripts_dir=str(SCRIPTS), secret=self.secret, now=1002,
            )
        self.assertEqual(len(scheduler.specs), 1)

    def test_reconcile_survives_restart_and_requires_normalized_success(self):
        resolution = self.resolution()
        reader = FakeReader(self.nodes)
        scheduler = FakeScheduler()
        registry = FakeRegistry()
        result = service.preview(self.request(), resolution, reader, scripts_dir=str(SCRIPTS),
                                 secret=self.secret, now=1000, ttl_seconds=60)
        submitted = service.submit_confirmed(
            result["confirmation_token"], resolution, reader, scheduler, registry,
            scripts_dir=str(SCRIPTS), secret=self.secret, now=1001,
        )
        root = Path(submitted["results_directory"])
        plan = json.loads((root / "plan.json").read_text())
        write_passing_summary(plan, root)
        scheduler.state = "COMPLETED"
        reconciled = service.reconcile(submitted["activity_id"], registry, scheduler)
        self.assertEqual(reconciled["summary"]["functional_status"], "PASS")
        self.assertEqual(reconciled["activity"]["work_phase"], "done")
        self.assertEqual(reconciled["activity"]["returncode"], 0)

    def test_completed_job_waits_for_summary_then_fails_closed_after_grace(self):
        resolution = self.resolution()
        reader = FakeReader(self.nodes)
        scheduler = FakeScheduler()
        registry = FakeRegistry()
        result = service.preview(self.request(), resolution, reader, scripts_dir=str(SCRIPTS),
                                 secret=self.secret, now=1000, ttl_seconds=60)
        submitted = service.submit_confirmed(
            result["confirmation_token"], resolution, reader, scheduler, registry,
            scripts_dir=str(SCRIPTS), secret=self.secret, now=1001,
        )
        scheduler.state = "COMPLETED"
        collecting = service.reconcile(submitted["activity_id"], registry, scheduler)
        self.assertEqual(collecting["activity"]["work_phase"], "collecting")
        registry.rows[submitted["activity_id"]]["tasks"][0]["collecting_since"] = (
            "2000-01-01T00:00:00+00:00"
        )
        failed = service.reconcile(submitted["activity_id"], registry, scheduler)
        self.assertEqual(failed["activity"]["work_phase"], "failed")
        self.assertIn("without a passing", failed["activity"]["error"])

    def test_artifact_access_is_bounded_and_flat(self):
        root = self.results / "attempt"
        root.mkdir()
        (root / "summary.json").write_text('{"ok": true}\n')
        nested = root / "baseline" / "vendor-date"
        nested.mkdir(parents=True)
        (nested / "bandwidth.json").write_text('{"Testname": "bandwidth"}\n')
        activity = {"log_dir": str(root)}
        names = [row["name"] for row in service.artifacts(activity)]
        self.assertIn("summary.json", names)
        self.assertIn("baseline/vendor-date/bandwidth.json", names)
        data, content_type = service.artifact(activity, "summary.json")
        self.assertEqual(json.loads(data), {"ok": True})
        self.assertEqual(content_type, "application/json")
        nested_data, _ = service.artifact(activity, "baseline/vendor-date/bandwidth.json")
        self.assertEqual(json.loads(nested_data)["Testname"], "bandwidth")
        with self.assertRaises(ValueError):
            service.artifact(activity, "../summary.json")
if __name__ == "__main__":
    unittest.main()
