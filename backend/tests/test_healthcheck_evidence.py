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
"""Per-node health-check markers.

Read at request time and never ingested, because a marker is *current state*: the checker
overwrites it every cycle, nothing joins it to anything, and persisting it would add rows
that are stale on arrival plus a fourth writer to the fleet database.

The root comes from the cluster record. The implementation this was ported from hard-coded
`/cluster/OVA/healthchecks/logs`, which exists on exactly one appliance.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import healthcheck_evidence


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A marker tree, with the cluster pointed at it."""
    monkeypatch.setattr(healthcheck_evidence, "log_root", lambda: str(tmp_path))
    healthcheck_evidence.clear_cache()
    yield tmp_path
    healthcheck_evidence.clear_cache()


def _node(root: Path, host: str, status: str, epoch: str = "1700000000", log: str = ""):
    directory = root / host
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".last-check").write_text(f"{epoch} {status}\n", encoding="utf-8")
    if log:
        (directory / "health.log").write_text(log, encoding="utf-8")
    return directory


class TestReadingMarkers:
    def test_a_healthy_node_becomes_a_pass_event(self, root):
        _node(root, "ink50-dh01-h18-01", "ok")
        events = healthcheck_evidence.post_run_events()
        assert len(events) == 1
        assert events[0]["status"] == "PASS"
        assert events[0]["node"] == "ink50-dh01-h18-01"
        assert events[0]["source"] == healthcheck_evidence.SOURCE

    def test_auto_action_trigger_clean_and_failed_vocabulary(self, root):
        # node_health.sh writes exactly ``clean`` (no reason) or ``failed``; both must map,
        # or every healthy node reads UNKNOWN in the health view.
        _node(root, "gpu1-su19", "clean")
        _node(root, "gpu2-su19", "failed")
        by_node = {e["node"]: e["status"] for e in healthcheck_evidence.post_run_events()}
        assert by_node == {"gpu1-su19": "PASS", "gpu2-su19": "FAIL"}

    def test_event_conforms_to_the_post_run_run_shape(self, root):
        # The post-run table renders a health marker through the same row path as a Slurm run,
        # so every field that path reads must be present and typed -- a missing `state` crashed
        # the view (undefined.toLowerCase). `state` carries the verdict so the colour maps.
        _node(root, "gpu1-su19", "clean", epoch="1700000000")
        ev = healthcheck_evidence.post_run_events()[0]
        for field in ("node", "rack", "job_id", "tool", "state", "started_at", "reason",
                      "attributed_failure", "failed_job", "source"):
            assert field in ev, f"missing run field: {field}"
        assert ev["state"] == "PASS"
        assert ev["tool"] == "node-healthcheck"
        assert ev["attributed_failure"] is False
        assert ev["started_at"].startswith("2023-")  # ISO-8601 from the epoch, not a bare int

    def test_a_non_numeric_timestamp_sanitises_to_empty_not_a_bad_date(self, root):
        # A half-written marker's timestamp must not reach the client as a non-date string.
        directory = root / "gpu9-su19"
        directory.mkdir()
        (directory / ".last-check").write_text("not-a-number clean\n", encoding="utf-8")
        ev = healthcheck_evidence.post_run_events()[0]
        assert ev["started_at"] == ""
        assert ev["state"] == "PASS"

    def test_a_failure_carries_the_log_tail_as_its_reason(self, root):
        # A red row with no reason sends the operator to the filesystem anyway.
        _node(root, "n01", "failed", log="GPU4 ECC uncorrectable\nfan 3 stalled\n")
        event = healthcheck_evidence.post_run_events()[0]
        assert event["status"] == "FAIL"
        assert "ECC uncorrectable" in event["detail"]

    def test_a_pass_carries_no_tail(self, root):
        # Nothing to explain, and reading the log would be work per node per request.
        _node(root, "n01", "ok", log="all good\n")
        assert healthcheck_evidence.post_run_events()[0]["detail"] == ""

    def test_an_unknown_status_is_unknown_not_a_guess(self, root):
        # A health view that invents a verdict is worse than one admitting it cannot read.
        _node(root, "n01", "weird-new-state")
        event = healthcheck_evidence.post_run_events()[0]
        assert event["status"] == "UNKNOWN"
        assert event["raw_verdict"] == "weird-new-state"

    def test_every_event_is_authoritative(self, root):
        # One marker per node, overwritten in place, so there is no superseded row to keep
        # out of a failure count.
        _node(root, "n01", "ok")
        event = healthcheck_evidence.post_run_events()[0]
        assert event["authoritative"] is True
        assert event["history_only"] is False


class TestWhatItSkips:
    def test_a_half_written_marker_is_skipped(self, root):
        # Parsing one field optimistically would fabricate a verdict.
        directory = root / "n01"
        directory.mkdir()
        (directory / ".last-check").write_text("1700000000\n", encoding="utf-8")
        assert healthcheck_evidence.post_run_events() == []

    def test_a_directory_with_no_marker_is_skipped(self, root):
        (root / "n01").mkdir()
        assert healthcheck_evidence.post_run_events() == []

    def test_a_stray_file_at_the_root_is_ignored(self, root):
        (root / "README").write_text("notes", encoding="utf-8")
        _node(root, "n01", "ok")
        assert len(healthcheck_evidence.post_run_events()) == 1


class TestFilters:
    def test_it_narrows_to_one_node(self, root):
        _node(root, "n01", "ok")
        _node(root, "n02", "failed")
        events = healthcheck_evidence.post_run_events(node="n02")
        assert [e["node"] for e in events] == ["n02"]

    def test_it_narrows_to_a_rack_by_hostname_prefix(self, root):
        _node(root, "ink50-dh01-h18-01", "ok")
        _node(root, "ink50-dh01-h19-01", "ok")
        events = healthcheck_evidence.post_run_events(rack="ink50-dh01-h18")
        assert [e["node"] for e in events] == ["ink50-dh01-h18-01"]

    def test_filters_are_case_insensitive(self, root):
        # DNS is case-insensitive, and a marker directory's spelling is whatever the checker
        # used.
        _node(root, "INK50-DH01-H18-01", "ok")
        assert len(healthcheck_evidence.post_run_events(node="ink50-dh01-h18-01")) == 1


class TestWhenThereIsNoFeed:
    def test_no_configured_root_yields_nothing(self, monkeypatch):
        # The common case: most clusters have no such checker, and the post-run report must
        # render either way.
        monkeypatch.setattr(healthcheck_evidence, "log_root", lambda: "")
        assert healthcheck_evidence.post_run_events() == []

    def test_a_configured_but_absent_root_yields_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(healthcheck_evidence, "log_root", lambda: str(tmp_path / "gone"))
        assert healthcheck_evidence.post_run_events() == []

    def test_the_root_comes_from_the_cluster_record(self, monkeypatch):
        from catalog import models

        cluster = models.Cluster.from_dict({
            "id": "c", "name": "C", "evidence": {"healthcheck_root": "/srv/hc"},
        })
        monkeypatch.setattr(
            "catalog.context.resolve", lambda *a, **k: (None, cluster, None)
        )
        assert healthcheck_evidence.log_root() == "/srv/hc"

    def test_an_unreadable_catalog_is_no_feed_rather_than_an_error(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("catalog unavailable")

        monkeypatch.setattr("catalog.context.resolve", boom)
        assert healthcheck_evidence.log_root() == ""


class TestCaching:
    def test_a_second_read_is_served_from_cache(self, root):
        _node(root, "n01", "ok")
        first = healthcheck_evidence.post_run_events()[0]
        second = healthcheck_evidence.post_run_events()[0]
        # Same object: the cache is keyed on the marker's own mtime and size, so an
        # unchanged marker costs one parse however often the report is refreshed.
        assert first is second

    def test_a_rewritten_marker_invalidates_it(self, root):
        _node(root, "n01", "ok", epoch="1700000000")
        assert healthcheck_evidence.post_run_events()[0]["status"] == "PASS"
        _node(root, "n01", "failed", epoch="1700009999")
        assert healthcheck_evidence.post_run_events()[0]["status"] == "FAIL"
