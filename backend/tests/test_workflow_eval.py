# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Unit tests for the workflow value-based evaluator."""
import workflow_eval


class TestDefault:
    def test_no_block_uses_exit_code(self):
        ok, outs, detail = workflow_eval.evaluate(None, returncode=0, stdout="")
        assert ok and outs == {} and detail == []
        ok, _, _ = workflow_eval.evaluate(None, returncode=1, stdout="")
        assert not ok


class TestSuccessWhen:
    def test_exit_code_and_contains(self):
        ev = {"success_when": {"exit_code": 0, "stdout_contains": "OK"}}
        assert workflow_eval.evaluate(ev, returncode=0, stdout="all OK here")[0]
        assert not workflow_eval.evaluate(ev, returncode=0, stdout="nope")[0]
        assert not workflow_eval.evaluate(ev, returncode=1, stdout="all OK here")[0]

    def test_not_contains_and_regex(self):
        ev = {"success_when": {"stdout_not_contains": "error", "stdout_regex": r"busbw"}}
        assert workflow_eval.evaluate(ev, returncode=0, stdout="busbw 557")[0]
        assert not workflow_eval.evaluate(ev, returncode=0, stdout="busbw error")[0]

    def test_success_when_without_exit_code_ignores_rc(self):
        ev = {"success_when": {"stdout_contains": "done"}}
        # rc != 0 but success_when has no exit_code -> only content checked
        assert workflow_eval.evaluate(ev, returncode=5, stdout="done")[0]


class TestExtractAndAssert:
    def test_extract_typed_and_assert_pass(self):
        ev = {
            "extract": [{"name": "bw", "regex": r"busbw\s+([0-9.]+)", "type": "float"}],
            "assert": ["bw >= 300"],
        }
        ok, outs, _ = workflow_eval.evaluate(ev, returncode=0, stdout="busbw 557.1")
        assert ok and outs["bw"] == 557.1

    def test_assert_fail(self):
        ev = {
            "extract": [{"name": "bw", "regex": r"busbw\s+([0-9.]+)", "type": "float"}],
            "assert": ["bw >= 600"],
        }
        ok, outs, _ = workflow_eval.evaluate(ev, returncode=0, stdout="busbw 557.1")
        assert not ok and outs["bw"] == 557.1

    def test_assert_missing_value_fails(self):
        ev = {"assert": ["nope >= 1"]}
        assert not workflow_eval.evaluate(ev, returncode=0, stdout="")[0]

    def test_extract_no_match_records_detail(self):
        ev = {"extract": [{"name": "bw", "regex": r"XXX([0-9]+)"}]}
        _ok, outs, detail = workflow_eval.evaluate(ev, returncode=0, stdout="none")
        assert "bw" not in outs and any("no match" in d for d in detail)

    def test_resolve_messages_pass_fail_selection(self):
        ev = {"message": "generic", "message_pass": "all good", "message_fail": "broken"}
        assert workflow_eval.resolve_messages(ev, True, {}) == ("all good", "")
        assert workflow_eval.resolve_messages(ev, False, {}) == ("broken", "")

    def test_resolve_messages_generic_fallback(self):
        ev = {"message": "context", "prompt": "fix it"}
        msg, prompt = workflow_eval.resolve_messages(ev, False, {})
        assert msg == "context" and prompt == "fix it"

    def test_resolve_messages_substitutes_outputs(self):
        ev = {"message_fail": "busbw ${bw} below 300"}
        msg, _ = workflow_eval.resolve_messages(ev, False, {"bw": 120.0})
        assert msg == "busbw 120.0 below 300"

    def test_resolve_messages_comment_alias_for_prompt(self):
        _msg, prompt = workflow_eval.resolve_messages({"comment": "hey agent"}, True, {})
        assert prompt == "hey agent"

    def test_operators(self):
        ev = {"extract": [{"name": "n", "regex": r"n=([0-9]+)", "type": "int"}]}
        for expr, expected in [("n == 4", True), ("n != 5", True), ("n < 5", True),
                               ("n > 3", True), ("n <= 4", True), ("n >= 5", False)]:
            e = dict(ev, **{"assert": [expr]})
            assert workflow_eval.evaluate(e, returncode=0, stdout="n=4")[0] is expected
