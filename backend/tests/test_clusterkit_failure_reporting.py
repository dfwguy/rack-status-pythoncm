# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Regressions for Q1: retain the first failure; never demand artifacts of a skipped stage."""
import tempfile
import unittest
from pathlib import Path

if __package__:
    from .test_clusterkit_execution import make_plan, write_before, write_stage
else:
    from test_clusterkit_execution import make_plan, write_before, write_stage
from clusterkit_results import process_failure, summarize
from clusterkit_summary import validate_summary


class FailureReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = make_plan(Path(self.tmp.name), 8, compare=True)
        self.root = Path(self.plan["results_directory"])
        self.root.mkdir()
        write_before(self.plan, self.root)

    def outcome(self, **changes):
        return {"job_id": "123", "preflight_exit": 0, "primary_exit": 143,
                "stages": {"baseline": 143}, "diagnostics": {"after_exit": 0}, **changes}

    def test_primary_ucx_timeout_is_explained_without_skipped_rank_file_cascade(self):
        (self.root / "baseline.log").write_text(
            "[node3:10:0] ud_ep.c:296 Fatal: UD endpoint 0x1: unhandled timeout error\n"
            "[node6:11:0] ud_ep.c:296 Fatal: UD endpoint 0x2: unhandled timeout error\n"
            "PMPI_Comm_split_type\n")
        summary = summarize(self.plan, self.root, self.outcome())
        self.assertEqual(summary["functional_status"], "FAIL")
        self.assertEqual(summary["performance_status"], "UNVERIFIED")
        self.assertEqual(summary["primary_exit_status"], 143)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("UCX communication timed out during MPI setup", summary["errors"][0])
        self.assertEqual(summary["stages"]["baseline"]["failure"]["affected_nodes"], ["node3", "node6"])
        skipped = summary["stages"]["single-path"]
        self.assertEqual(skipped["status"], "SKIPPED")
        self.assertIsNone(skipped["exit_status"])
        self.assertEqual(skipped["reason"], "Not run because baseline failed")
        self.assertEqual(skipped["metrics"], {})
        self.assertEqual(skipped["placements"], [])
        self.assertFalse(any("single-path rank" in e or "Missing bandwidth" in e for e in summary["errors"]))
        self.assertTrue(summary["stages"]["baseline"]["evidence_errors"])
        validate_summary(summary, self.plan, "123")

    def test_unrecognized_failure_is_not_misdiagnosed_as_a_network_problem(self):
        summary = summarize(self.plan, self.root, self.outcome())
        failure = summary["stages"]["baseline"]["failure"]
        self.assertEqual(failure["category"], "PROCESS_FAILED")
        self.assertEqual(failure["evidence"], [])
        self.assertIn("exit 143", failure["message"])

    def test_missing_stage_after_success_is_an_error_instead_of_an_implicit_skip(self):
        write_stage(self.plan, self.root, "baseline")
        summary = summarize(self.plan, self.root, self.outcome(primary_exit=0, stages={"baseline": 0}))
        self.assertEqual(summary["functional_status"], "FAIL")
        self.assertEqual(summary["stages"]["single-path"]["status"], "FAIL")
        self.assertIn("single-path: Planned stage has no execution record", summary["errors"])

    def test_preflight_failure_skips_both_stages_without_inventing_missing_artifacts(self):
        summary = summarize(self.plan, self.root, self.outcome(preflight_exit=9, primary_exit=9, stages={}))
        self.assertEqual(summary["functional_status"], "FAIL")
        self.assertEqual(summary["preflight"]["status"], "FAIL")
        self.assertTrue(all(s["status"] == "SKIPPED" for s in summary["stages"].values()))
        self.assertFalse(any("rank-placement" in e or "Missing bandwidth" in e for e in summary["errors"]))

    def test_success_with_missing_metrics_still_fails_evidence_validation(self):
        summary = summarize(self.plan, self.root, self.outcome(primary_exit=0, stages={"baseline": 0, "single-path": 0}))
        self.assertEqual(summary["functional_status"], "FAIL")
        self.assertTrue(any("Missing bandwidth JSON" in e for e in summary["errors"]))

    def test_successful_legacy_summaries_remain_valid_but_skipped_cannot_pass(self):
        for stage in ("baseline", "single-path"):
            write_stage(self.plan, self.root, stage)
        summary = summarize(self.plan, self.root, self.outcome(primary_exit=0, stages={"baseline": 0, "single-path": 0}))
        self.assertEqual(summary["functional_status"], "PASS")
        validate_summary(summary, self.plan, "123")
        summary["stages"]["single-path"]["status"] = "SKIPPED"
        with self.assertRaisesRegex(ValueError, "failed stage"):
            validate_summary(summary, self.plan, "123")

    def test_failure_extraction_is_bounded_and_only_names_planned_hosts(self):
        line = "[node0:10] ud_ep.c:296 Fatal: UD endpoint unhandled timeout error "
        (self.root / "baseline.log").write_text(
            "[outsider:10] ud_ep.c:296 Fatal: UD endpoint unhandled timeout error\n"
            + (line + "x" * 1000 + "\n") * 200)
        failure = process_failure(self.root, "baseline", self.plan["nodes"], 143)
        self.assertEqual(failure["affected_nodes"], ["node0"])
        self.assertEqual(len(failure["evidence"]), 4)
        self.assertTrue(all(len(v["excerpt"]) <= 512 for v in failure["evidence"]))

    def test_failure_extraction_does_not_follow_symlinks(self):
        outside = self.root.parent / "outside.log"
        outside.write_text("[node0] ud_ep.c Fatal: unhandled timeout error")
        (self.root / "baseline.log").symlink_to(outside)
        self.assertEqual(process_failure(self.root, "baseline", self.plan["nodes"], 1)["category"], "PROCESS_FAILED")
