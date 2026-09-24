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
"""Ingesting a site collector's evidence snapshot.

Two properties carry this module and the tests are mostly about them. **Acceptance is
monotonic** — a rack that passed stays passed, because an acceptance record is a historical
claim a later failure does not retract. **Current state is not** — a rack broken now must
read broken, or a pass from last week paints it green forever, and green is the one answer
nobody re-checks.

The third is that **nothing here knows a site's floor plan**: the implementation this was
reconstructed from computed rack ids from `su`/`rack_number` through a hard-coded table of
one customer's halls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import external_evidence


def _snapshot(**overrides):
    base = {
        "complete": True,
        "collected_at": "2026-09-24T12:00:00Z",
        "rack_nccl": {"latest_by_rack": [
            {"rack": "ink50-dh01-h18", "outcome": "PASS", "main_log": "/logs/nccl-h18.log",
             "job_id": "12345", "mtime_ns": 1_700_000_000_000_000_000, "node_count": 18,
             "parquet_exact": True, "source_sha256": "abc123"},
        ]},
        "rack_nemo": {"latest_by_rack": []},
    }
    base.update(overrides)
    return base


class TestTheTwoFeedsAreSeparate:
    def test_a_pass_lands_in_both_acceptance_and_current(self):
        snap = _snapshot()
        assert [r["source"] for r in external_evidence.accepted_records(snap)] == [
            external_evidence.SOURCE_SETTLED
        ]
        assert [r["source"] for r in external_evidence.current_records(snap)] == [
            external_evidence.SOURCE_CURRENT
        ]

    def test_a_failure_reaches_current_but_never_acceptance(self):
        # Acceptance is never inferred: importing failures there would let a later retry
        # retract a pass that genuinely happened.
        snap = _snapshot(rack_nccl={"latest_by_rack": [
            {"rack": "r01", "outcome": "FAIL", "main_log": "/logs/x.log"},
        ]})
        assert external_evidence.accepted_records(snap) == []
        current = external_evidence.current_records(snap)
        assert [r["status"] for r in current] == ["fail"]

    def test_the_current_row_id_is_stable_across_refreshes(self):
        # Keyed on rack and stage without the log path, so each refresh updates one row per
        # rack instead of appending a near-duplicate and turning history into noise.
        first = external_evidence.current_records(_snapshot())[0]
        moved = _snapshot(rack_nccl={"latest_by_rack": [
            {"rack": "ink50-dh01-h18", "outcome": "FAIL", "main_log": "/logs/DIFFERENT.log"},
        ]})
        second = external_evidence.current_records(moved)[0]
        assert first["run_id"] == second["run_id"]
        assert first["status"] != second["status"]

    def test_an_acceptance_id_is_keyed_to_its_artifact(self):
        # The opposite rule: acceptance accumulates, so a different log is a different record.
        first = external_evidence.accepted_records(_snapshot())[0]
        other = _snapshot(rack_nccl={"latest_by_rack": [
            {"rack": "ink50-dh01-h18", "outcome": "PASS", "main_log": "/logs/second-run.log"},
        ]})
        assert first["run_id"] != external_evidence.accepted_records(other)[0]["run_id"]


class TestAPartialSnapshotIsRefusedWhole:
    def test_a_snapshot_not_marked_complete_is_rejected(self):
        # A rack absent from a half-written snapshot is indistinguishable from a rack with no
        # evidence, so the gap would read as "not validated" rather than "we stopped early".
        with pytest.raises(external_evidence.SnapshotRejected, match="not marked complete"):
            external_evidence.accepted_records(_snapshot(complete=False))

    def test_a_missing_section_list_is_rejected(self):
        with pytest.raises(external_evidence.SnapshotRejected, match="latest_by_rack"):
            external_evidence.accepted_records(_snapshot(rack_nccl={"wrong": []}))

    def test_an_absent_section_is_fine(self):
        # A collector that does not run NeMo simply omits it.
        snap = _snapshot()
        del snap["rack_nemo"]
        assert len(external_evidence.accepted_records(snap)) == 1

    def test_a_pass_with_no_artifact_is_rejected(self):
        # A pass with nothing behind it is not evidence.
        with pytest.raises(external_evidence.SnapshotRejected, match="no main_log"):
            external_evidence.accepted_records(_snapshot(rack_nccl={"latest_by_rack": [
                {"rack": "r01", "outcome": "PASS", "main_log": ""},
            ]}))


class TestRackIdentityComesFromTheCatalog:
    def test_an_explicit_rack_is_used_as_given(self):
        assert external_evidence.accepted_records(_snapshot())[0]["rack"] == "ink50-dh01-h18"

    def test_coordinates_resolve_through_the_clusters_su_map(self, monkeypatch):
        # Replaces the hard-coded hall/row table: `su` names an entry in the cluster's own SU
        # map and `rack_number` indexes into it, 1-based the way a floor is labelled.
        monkeypatch.setattr(
            external_evidence, "_su_map",
            lambda: {"su12": ["ink50-dh01-h18", "ink50-dh01-h19"]},
        )
        snap = _snapshot(rack_nccl={"latest_by_rack": [
            {"su": "12", "rack_number": 2, "outcome": "PASS", "main_log": "/l.log"},
        ]})
        assert external_evidence.accepted_records(snap)[0]["rack"] == "ink50-dh01-h19"

    def test_an_unresolvable_coordinate_is_refused_not_guessed(self, monkeypatch):
        monkeypatch.setattr(external_evidence, "_su_map", dict)
        snap = _snapshot(rack_nccl={"latest_by_rack": [
            {"su": "99", "rack_number": 1, "outcome": "PASS", "main_log": "/l.log"},
        ]})
        with pytest.raises(external_evidence.SnapshotRejected, match="no resolvable rack"):
            external_evidence.accepted_records(snap)

    def test_an_out_of_range_rack_number_is_refused(self, monkeypatch):
        monkeypatch.setattr(external_evidence, "_su_map", lambda: {"su12": ["r01"]})
        snap = _snapshot(rack_nccl={"latest_by_rack": [
            {"su": "12", "rack_number": 7, "outcome": "PASS", "main_log": "/l.log"},
        ]})
        with pytest.raises(external_evidence.SnapshotRejected, match="no resolvable rack"):
            external_evidence.accepted_records(snap)

    def test_an_unplaceable_current_row_is_skipped_rather_than_refused(self, monkeypatch):
        # Asymmetric on purpose: a current row that cannot be placed simply overlays nothing,
        # while an unplaceable *acceptance* row would silently lose a pass.
        monkeypatch.setattr(external_evidence, "_su_map", dict)
        snap = _snapshot(rack_nccl={"latest_by_rack": [
            {"su": "99", "rack_number": 1, "outcome": "FAIL", "main_log": "/l.log"},
        ]})
        assert external_evidence.current_records(snap) == []


class TestCollectorProvenanceIsCarriedNotInterpreted:
    def test_the_index_fields_survive_into_extra(self):
        # How somebody later answers "why did we believe this". Not interpreted here, because
        # interpreting it would form the second opinion this module exists to avoid.
        extra = external_evidence.accepted_records(_snapshot())[0]["extra"]
        assert extra["parquet_exact"] is True
        assert extra["source_sha256"] == "abc123"
        assert extra["node_count"] == 18

    def test_a_nanosecond_mtime_becomes_seconds(self):
        assert external_evidence.accepted_records(_snapshot())[0]["log_mtime"] == pytest.approx(
            1_700_000_000.0
        )

    def test_a_missing_mtime_is_zero_not_an_error(self):
        snap = _snapshot(rack_nccl={"latest_by_rack": [
            {"rack": "r01", "outcome": "PASS", "main_log": "/l.log"},
        ]})
        assert external_evidence.accepted_records(snap)[0]["log_mtime"] == 0.0


class TestDcgmRows:
    @staticmethod
    def _with_dcgm(*evidence):
        return _snapshot(lake={"validation_attempts": [
            {"job_id": "999", "attempt": 1, "evidence": list(evidence)},
        ]})

    def test_a_current_row_is_authoritative(self):
        rows = external_evidence.dcgm_records(self._with_dcgm(
            {"workload": "dcgm", "test": "dcgmi-r3", "node": "n01",
             "artifact_path": "/a/r3.json", "verdict": "PASS"},
        ))
        assert rows[0]["extra"]["authoritative"] is True
        assert rows[0]["extra"]["history_only"] is False
        assert rows[0]["status"] == "pass"

    def test_a_prev_row_is_history_only(self):
        # Kept for an export, but marked so it cannot inflate a node's failure count -- the
        # same rule `post_run_report`'s authoritative filter applies.
        rows = external_evidence.dcgm_records(self._with_dcgm(
            {"workload": "dcgm", "test": "dcgmi-r3.prev1", "node": "n01",
             "artifact_path": "/a/old.json", "verdict": "FAIL"},
        ))
        assert rows[0]["extra"]["history_only"] is True
        assert rows[0]["extra"]["authoritative"] is False

    def test_an_unknown_test_name_is_skipped(self):
        # A row nobody can interpret is noise in a view used to decide whether to drain.
        assert external_evidence.dcgm_records(self._with_dcgm(
            {"workload": "dcgm", "test": "something-else", "node": "n01",
             "artifact_path": "/a.json", "verdict": "PASS"},
        )) == []

    def test_a_row_without_a_node_or_artifact_is_skipped(self):
        assert external_evidence.dcgm_records(self._with_dcgm(
            {"workload": "dcgm", "test": "dcgmi-r1", "node": "", "artifact_path": "/a.json"},
            {"workload": "dcgm", "test": "dcgmi-r1", "node": "n01", "artifact_path": ""},
        )) == []

    def test_a_non_dcgm_workload_is_ignored(self):
        assert external_evidence.dcgm_records(self._with_dcgm(
            {"workload": "nccl", "test": "dcgmi-r1", "node": "n01", "artifact_path": "/a.json"},
        )) == []

    def test_no_lake_section_is_not_an_error(self):
        assert external_evidence.dcgm_records(_snapshot()) == []


class TestIngest:
    def test_a_snapshot_lands_under_one_checkpoint(self, tmp_path, monkeypatch):
        # All three feeds in one checkpoint: they describe one observation, and importing
        # acceptance then failing on diagnostics would claim a consistency we do not have.
        seen = {}
        monkeypatch.setattr(
            external_evidence.results_history, "ingest_runs_checkpointed",
            lambda records, **kw: seen.update(records=list(records), **kw) or {
                "ingested": len(list(records)), "skipped": 0, "fingerprint": kw["fingerprint"],
            },
        )
        path = tmp_path / "v.json"
        path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        external_evidence.ingest_snapshot(path)
        assert seen["source_id"] == "external-evidence"
        assert {r["source"] for r in seen["records"]} == {
            external_evidence.SOURCE_SETTLED, external_evidence.SOURCE_CURRENT
        }
        assert seen["collected_at"] == "2026-09-24T12:00:00Z"

    def test_the_fingerprint_follows_content_not_mtime(self, tmp_path):
        # A collector that rewrites its output every cycle moves the mtime whether or not
        # anything changed, and re-ingesting identical evidence is wasted fleet-database work.
        path = tmp_path / "v.json"
        path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        snap = _snapshot()
        first = external_evidence._fingerprint(path, snap)
        path.write_text(json.dumps(snap), encoding="utf-8")  # rewritten, identical content
        assert external_evidence._fingerprint(path, snap) == first

    def test_a_missing_file_is_a_refusal_not_a_traceback(self, tmp_path):
        with pytest.raises(external_evidence.SnapshotRejected, match="cannot read"):
            external_evidence.ingest_snapshot(tmp_path / "absent.json")

    def test_malformed_json_is_a_refusal(self, tmp_path):
        path = tmp_path / "v.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(external_evidence.SnapshotRejected, match="not valid JSON"):
            external_evidence.ingest_snapshot(path)

    def test_a_non_object_root_is_a_refusal(self, tmp_path):
        path = tmp_path / "v.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(external_evidence.SnapshotRejected, match="must be an object"):
            external_evidence.ingest_snapshot(path)
