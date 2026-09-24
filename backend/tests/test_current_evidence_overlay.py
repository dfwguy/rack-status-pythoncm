# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""A current failure must override a monotonic pass on the fleet view.

This was a real defect, not a hypothetical. `_durable_completed_stage_logs_by_rack` read
only `latest_successful_runs_by_rack`, which is by design the newest *pass* per rack — so
once a rack had ever passed, no later evidence could change its colour. A rack that broke
after acceptance stayed green, and green is the one answer nobody re-checks.

Acceptance history is still monotonic and still right: it answers "was this rack ever
accepted". The overlay answers "is it working now", and the fleet view needs the second.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import external_evidence
import native_status

RACK = "ink50-dh01-h18"


def _accepted(mtime: float = 1000.0):
    return {"nccl": {RACK: {
        "job_id": "100", "log_mtime": mtime, "source_log_path": "/logs/pass.log",
        "ended_at": "2026-09-01T00:00:00Z",
    }}}


def _current(status: str, mtime: float = 2000.0):
    return {"nccl": {RACK: {
        "status": status, "log_mtime": mtime, "source_log_path": "/logs/now.log",
    }}}


@pytest.fixture
def overlay(monkeypatch):
    """Drive both halves independently."""
    state = {"accepted": {}, "current": {}}
    monkeypatch.setattr(
        native_status.results_history, "latest_successful_runs_by_rack",
        lambda *_a, **_k: state["accepted"],
    )
    monkeypatch.setattr(
        native_status.results_history, "latest_runs_by_rack",
        lambda *_a, **_k: state["current"],
    )
    return state


class TestACurrentFailureWins:
    def test_a_newer_failure_turns_an_accepted_rack_red(self, overlay):
        overlay["accepted"] = _accepted()
        overlay["current"] = _current("fail")
        probe = native_status._durable_completed_stage_logs_by_rack()[RACK]["nccl"]
        assert probe["nemo_state"] == "fail"
        assert probe["nccl_ok"] is False
        assert probe["_superseded_by_current"] is True

    def test_without_the_overlay_the_pass_would_stand(self, overlay):
        """The control, and the shape of the original bug: no current feed, so green holds."""
        overlay["accepted"] = _accepted()
        overlay["current"] = {}
        probe = native_status._durable_completed_stage_logs_by_rack()[RACK]["nccl"]
        assert probe["nemo_state"] == "done"
        assert probe["_durable_completion"] is True

    def test_a_current_pass_leaves_acceptance_alone(self, overlay):
        overlay["accepted"] = _accepted()
        overlay["current"] = _current("pass")
        probe = native_status._durable_completed_stage_logs_by_rack()[RACK]["nccl"]
        assert probe["nemo_state"] == "done"
        assert "_superseded_by_current" not in probe


class TestItWillNotGoBackwards:
    def test_a_stale_failure_does_not_retract_a_newer_pass(self, overlay):
        # The same defect in the other direction. A collector can be re-run against older
        # logs, so the overlay is checked for being newer rather than assumed to be.
        overlay["accepted"] = _accepted(mtime=5000.0)
        overlay["current"] = _current("fail", mtime=1000.0)
        probe = native_status._durable_completed_stage_logs_by_rack()[RACK]["nccl"]
        assert probe["nemo_state"] == "done"

    def test_an_equally_timed_failure_does_override(self, overlay):
        # Ties go to the current feed: it is the one that describes now, and a collector
        # writing both from one observation gives them the same mtime.
        overlay["accepted"] = _accepted(mtime=3000.0)
        overlay["current"] = _current("fail", mtime=3000.0)
        assert native_status._durable_completed_stage_logs_by_rack()[RACK]["nccl"][
            "nemo_state"
        ] == "fail"

    def test_a_failure_for_a_rack_never_accepted_adds_nothing(self, overlay):
        # There is no green to correct, and inventing a red row here would report a rack as
        # failed when it has simply never been run.
        overlay["accepted"] = {}
        overlay["current"] = _current("fail")
        assert native_status._durable_completed_stage_logs_by_rack() == {}

    def test_an_unreadable_mtime_does_not_override(self, overlay):
        overlay["accepted"] = _accepted()
        overlay["current"] = {"nccl": {RACK: {"status": "fail", "log_mtime": "not-a-number"}}}
        assert native_status._durable_completed_stage_logs_by_rack()[RACK]["nccl"][
            "nemo_state"
        ] == "done"


class TestItSurvivesAMissingFeed:
    def test_a_history_failure_leaves_the_view_renderable(self, monkeypatch):
        # The fleet dashboard must draw even when the optional overlay cannot be read.
        monkeypatch.setattr(
            native_status.results_history, "latest_successful_runs_by_rack",
            lambda *_a, **_k: _accepted(),
        )

        def boom(*_a, **_k):
            raise RuntimeError("results db locked")

        monkeypatch.setattr(native_status.results_history, "latest_runs_by_rack", boom)
        probe = native_status._durable_completed_stage_logs_by_rack()[RACK]["nccl"]
        assert probe["nemo_state"] == "done"

    def test_the_overlay_asks_for_the_current_source_only(self, monkeypatch):
        # Asking without the filter would pull acceptance rows back in and compare a feed
        # against itself.
        seen = {}
        monkeypatch.setattr(
            native_status.results_history, "latest_successful_runs_by_rack",
            lambda *_a, **_k: {},
        )
        monkeypatch.setattr(
            native_status.results_history, "latest_runs_by_rack",
            lambda *a, **kw: seen.update(kw) or {},
        )
        native_status._durable_completed_stage_logs_by_rack()
        assert seen["source"] == external_evidence.SOURCE_CURRENT
