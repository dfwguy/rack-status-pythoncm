# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Display-name changes preserve stable run identity, history and submission safety."""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import test_clusterkit_service as fixtures
else:
    import test_clusterkit_service as fixtures

import clusterkit_context as context
import clusterkit_service as service
from clusterkit_attempts import load_intent, receipt


class Registry(fixtures.FakeRegistry):
    def activity_history(self):
        return list(self.rows.values())


class RenameTests(unittest.TestCase):
    resolution = fixtures.ClusterKitServiceTests.resolution
    request = fixtures.ClusterKitServiceTests.request

    def setUp(self):
        fixtures.ClusterKitServiceTests.setUp(self)
        self.patch = mock.patch("subprocess.Popen", side_effect=AssertionError("No external execution"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.old = context.capture(str(fixtures.SCRIPTS))
        self.old["selection"] = {"project": "test-project", "cluster": "test-cluster"}
        self.old["cluster_name"] = "Original display name"
        self.new = {**self.old, "cluster_name": "New display name"}

    def preview(self, ctx):
        resolution = self.resolution()
        reader = fixtures.FakeReader(self.nodes)
        result = service.preview(self.request(), resolution, reader,
            scripts_dir=str(fixtures.SCRIPTS), context=ctx, secret=self.secret, now=1000)
        return result, resolution, reader

    def submit(self, scheduler=None):
        preview, resolution, reader = self.preview(self.old)
        scheduler = scheduler or fixtures.FakeScheduler()
        registry = Registry()
        result = service.submit_confirmed(preview["confirmation_token"], resolution, reader,
            scheduler, registry, scripts_dir=str(fixtures.SCRIPTS), context=self.old,
            secret=self.secret, now=1001)
        return result, resolution, scheduler, registry

    def test_only_the_display_name_may_change(self):
        context.require_same(self.old, self.new)
        self.assertEqual(self.old["cluster_name"], "Original display name")
        for key in set(self.old) - {"cluster_name"}:
            altered = copy.deepcopy(self.new)
            altered[key] = None
            with self.subTest(field=key), self.assertRaises(ValueError):
                context.require_same(self.old, altered)
        for bad in (None, "", [], 1):
            self.assertFalse(context.same_context(self.old, bad))
        self.assertFalse(context.same_context(self.old, {**self.new, "cluster_name": []}))

    def test_renaming_does_not_hide_history_or_prevent_status_and_recovery(self):
        result, resolution, scheduler, registry = self.submit()
        root = Path(result["results_directory"])
        original = (root / "submission-intent.json").read_bytes()
        status = service.reconcile(result["activity_id"], registry, scheduler, context=self.new)
        self.assertEqual(status["scheduler_job_id"], "12345")
        page = service.history(resolution, registry, context=self.new)
        self.assertEqual([r["activity_id"] for r in page["runs"]], [result["activity_id"]])
        recovered = service.recover_activity(result["activity_id"], resolution, Registry(), context=self.new)
        self.assertEqual(recovered["activity_id"], result["activity_id"])
        self.assertEqual((root / "submission-intent.json").read_bytes(), original)

    def test_another_cluster_still_cannot_read_the_history(self):
        result, resolution, scheduler, registry = self.submit()
        other = copy.deepcopy(self.new)
        other["selection"]["cluster"] = "different-cluster"
        self.assertEqual(service.history(resolution, registry, context=other)["runs"], [])
        with self.assertRaises(ValueError):
            service.reconcile(result["activity_id"], registry, scheduler, context=other)

    def test_a_signed_preview_remains_identity_bound_after_a_display_rename(self):
        preview, resolution, reader = self.preview(self.old)
        result = service.confirm_plan(preview["confirmation_token"], resolution, reader,
            scripts_dir=str(fixtures.SCRIPTS), context=self.new, secret=self.secret, now=1001)
        self.assertEqual(result["nodes"], self.nodes[:2])

    def test_unknown_overlapping_submission_still_blocks_after_a_rename(self):
        class Lost(fixtures.FakeScheduler):
            def submit(self, spec):
                self.specs.append(spec)
                raise TimeoutError("Simulated lost acknowledgement")

        result, resolution, _scheduler, registry = self.submit(Lost())
        self.assertEqual(result["submission_state"], "SUBMISSION_UNKNOWN")
        preview, resolution, reader = self.preview(self.new)
        with self.assertRaisesRegex(ValueError, "SUBMISSION_UNKNOWN"):
            service.submit_confirmed(preview["confirmation_token"], resolution, reader,
                fixtures.FakeScheduler(), registry, scripts_dir=str(fixtures.SCRIPTS),
                context=self.new, secret=self.secret, now=1001)

    def test_receipts_still_match_the_original_intent_exactly(self):
        result, _resolution, _scheduler, _registry = self.submit()
        root = Path(result["results_directory"])
        intent, _ = load_intent(root)
        path = root / "submission-receipt.json"
        value = json.loads(path.read_text())
        value["context"]["cluster_name"] = "Tampered receipt label"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "Invalid submission receipt"):
            receipt(root, intent)

    def test_legacy_contexts_without_catalog_identity_remain_name_bound(self):
        old = {**self.old, "selection": {"project": "", "cluster": ""}}
        new = {**old, "cluster_name": "different-cluster"}
        self.assertFalse(context.same_context(old, new))
        context.require_same(old, copy.deepcopy(old))
