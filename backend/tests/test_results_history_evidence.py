# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The substrate an external evidence producer needs from `results_history`.

Three capabilities, each of which exists because an external feed behaves differently from
the local parser: it re-presents its whole index on every poll, it lands beside our own rows
and must stay distinguishable from them, and it reports both a current verdict and the
attempts that verdict superseded.

The property worth protecting is in `TestCheckpointAtomicity`: a checkpoint must never
outlive the rows it vouches for. If it can, the next poll skips them and the evidence is
silently absent -- the exact failure this subsystem exists to prevent.
"""
from __future__ import annotations

import sqlite3

import pytest
import results_history as rh


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    rh._schema_ready.clear()
    yield tmp_path / "results.db"
    rh._schema_ready.clear()


def _run(run_id, *, rack, kind="nccl", status="pass", source="parser.sh", started=None):
    return {
        "run_id": run_id,
        "source_log_path": f"/logs/{run_id}.out",
        "workload_kind": kind,
        "rack": rack,
        "status": status,
        "source": source,
        "started_at": started or "2026-09-18T10:00:00Z",
    }


class TestSourceFilter:
    """Without this a caller cannot ask for one producer's rows without the other's."""

    def test_list_runs_filters_by_source(self, db):
        rh.ingest_runs(
            [
                _run("a", rack="r01", source="parser.sh"),
                _run("b", rack="r02", source="external_index"),
            ]
        )
        got = rh.list_runs({"source": "external_index"})
        assert [r["run_id"] for r in got] == ["b"]

    def test_a_list_of_sources_is_accepted(self, db):
        rh.ingest_runs(
            [
                _run("a", rack="r01", source="parser.sh"),
                _run("b", rack="r02", source="external_index"),
                _run("c", rack="r03", source="healthcheck"),
            ]
        )
        got = rh.list_runs({"source": ["parser.sh", "healthcheck"]})
        assert sorted(r["run_id"] for r in got) == ["a", "c"]

    def test_no_source_filter_returns_everything(self, db):
        rh.ingest_runs([_run("a", rack="r01"), _run("b", rack="r02", source="other")])
        assert rh.count_runs() == 2


class TestLatestRunsByRack:
    """The *current* overlay, as distinct from the *acceptance* record."""

    def test_newest_run_per_rack_wins_regardless_of_status(self, db):
        rh.ingest_runs(
            [
                _run("old", rack="r01", status="pass", started="2026-09-01T00:00:00Z"),
                _run("new", rack="r01", status="fail", started="2026-09-18T00:00:00Z"),
            ]
        )
        current = rh.latest_runs_by_rack(("nccl",))
        assert current["nccl"]["r01"]["run_id"] == "new"
        assert current["nccl"]["r01"]["status"] == "fail"

    def test_it_differs_from_the_acceptance_record_on_purpose(self, db):
        """A rack that passed and is now failing must read as failing on the current view
        and as passed on the acceptance view. One query cannot answer both."""
        rh.ingest_runs(
            [
                _run("pass1", rack="r01", status="pass", started="2026-09-01T00:00:00Z"),
                _run("fail1", rack="r01", status="fail", started="2026-09-18T00:00:00Z"),
            ]
        )
        assert rh.latest_runs_by_rack(("nccl",))["nccl"]["r01"]["status"] == "fail"
        accepted = rh.latest_successful_runs_by_rack(("nccl",))
        assert accepted["nccl"]["r01"]["run_id"] == "pass1"

    def test_source_narrows_the_overlay(self, db):
        rh.ingest_runs(
            [
                _run("mine", rack="r01", source="parser.sh", started="2026-09-18T00:00:00Z"),
                _run("theirs", rack="r01", source="external", started="2026-09-17T00:00:00Z"),
            ]
        )
        only = rh.latest_runs_by_rack(("nccl",), source="external")
        assert only["nccl"]["r01"]["run_id"] == "theirs"

    def test_racks_without_an_id_are_skipped(self, db):
        rh.ingest_runs([_run("a", rack=None), _run("b", rack="  ")])
        assert rh.latest_runs_by_rack(("nccl",)) == {"nccl": {}}

    def test_no_kinds_asked_for_is_an_empty_answer(self, db):
        assert rh.latest_runs_by_rack(()) == {}


class TestCheckpointedIngest:
    def test_a_first_batch_lands_and_records_its_fingerprint(self, db):
        out = rh.ingest_runs_checkpointed(
            [_run("a", rack="r01"), _run("b", rack="r02")],
            source_id="ext",
            fingerprint="fp1",
        )
        assert out == {"ingested": 2, "skipped": False, "fingerprint": "fp1"}
        assert rh.count_runs() == 2
        assert rh.ingestion_checkpoint("ext")["fingerprint"] == "fp1"

    def test_an_unchanged_fingerprint_is_skipped_entirely(self, db):
        """The point of the checkpoint: re-presenting the same index costs one SELECT, not
        a fleet's worth of writes."""
        rh.ingest_runs_checkpointed([_run("a", rack="r01")], source_id="ext", fingerprint="fp1")
        out = rh.ingest_runs_checkpointed(
            [_run("a", rack="r01"), _run("b", rack="r02")], source_id="ext", fingerprint="fp1"
        )
        assert out["skipped"] is True
        assert out["ingested"] == 0
        assert rh.count_runs() == 1, "a skipped batch must not write the second record"

    def test_a_changed_fingerprint_ingests_again(self, db):
        rh.ingest_runs_checkpointed([_run("a", rack="r01")], source_id="ext", fingerprint="fp1")
        out = rh.ingest_runs_checkpointed(
            [_run("a", rack="r01"), _run("b", rack="r02")], source_id="ext", fingerprint="fp2"
        )
        assert out["skipped"] is False
        assert rh.count_runs() == 2
        assert rh.ingestion_checkpoint("ext")["fingerprint"] == "fp2"

    def test_checkpoints_are_per_source(self, db):
        rh.ingest_runs_checkpointed([_run("a", rack="r01")], source_id="one", fingerprint="fp")
        out = rh.ingest_runs_checkpointed([_run("b", rack="r02")], source_id="two", fingerprint="fp")
        assert out["skipped"] is False, "the same fingerprint under another source is a new batch"
        assert rh.count_runs() == 2

    def test_source_metadata_is_recorded_for_operators(self, db):
        rh.ingest_runs_checkpointed(
            [_run("a", rack="r01")],
            source_id="ext",
            fingerprint="fp1",
            source_path="/evidence/index.json",
            source_mtime_ns=123456789,
            source_size=4096,
            collected_at="2026-09-18T10:00:00Z",
        )
        cp = rh.ingestion_checkpoint("ext")
        assert cp["source_path"] == "/evidence/index.json"
        assert cp["source_mtime_ns"] == 123456789
        assert cp["source_size"] == 4096
        assert cp["run_count"] == 1

    def test_an_absent_checkpoint_is_none(self, db):
        assert rh.ingestion_checkpoint("never") is None
        assert rh.ingestion_checkpoint("") is None

    def test_source_id_and_fingerprint_are_required(self, db):
        with pytest.raises(rh.ResultsHistoryError, match="source_id"):
            rh.ingest_runs_checkpointed([], source_id="", fingerprint="fp")
        with pytest.raises(rh.ResultsHistoryError, match="fingerprint"):
            rh.ingest_runs_checkpointed([], source_id="ext", fingerprint="")

    def test_an_empty_batch_still_records_the_checkpoint(self, db):
        """A producer that legitimately has nothing must not be re-read forever."""
        out = rh.ingest_runs_checkpointed([], source_id="ext", fingerprint="empty")
        assert out == {"ingested": 0, "skipped": False, "fingerprint": "empty"}
        assert rh.ingestion_checkpoint("ext")["fingerprint"] == "empty"


class TestCheckpointAtomicity:
    """A checkpoint must never vouch for rows that are not there."""

    def test_a_bad_record_aborts_the_whole_batch_and_the_checkpoint(self, db):
        """`_prepare_run` raises before anything is written, so neither the good row nor the
        claim lands. The alternative -- a checkpoint for a partial batch -- means the
        missing rows are skipped on every future poll."""
        with pytest.raises(rh.ResultsHistoryError):
            rh.ingest_runs_checkpointed(
                [_run("good", rack="r01"), {"no_source_log_path": True}],
                source_id="ext",
                fingerprint="fp1",
            )
        assert rh.count_runs() == 0
        assert rh.ingestion_checkpoint("ext") is None

    def test_a_write_failure_rolls_the_checkpoint_back(self, db, monkeypatch):
        """Same invariant, from the other side: the rows fail mid-transaction."""
        calls = {"n": 0}
        real = rh._write_run

        def explode(conn, prepared, now):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("disk full")
            return real(conn, prepared, now)

        monkeypatch.setattr(rh, "_write_run", explode)
        with pytest.raises(sqlite3.OperationalError):
            rh.ingest_runs_checkpointed(
                [_run("a", rack="r01"), _run("b", rack="r02")],
                source_id="ext",
                fingerprint="fp1",
            )
        assert rh.count_runs() == 0, "the first row must not survive its batch"
        assert rh.ingestion_checkpoint("ext") is None


class TestIngestRefactorIsBehaviourPreserving:
    """`ingest_run` was split into prepare + write so a batch could share a transaction."""

    def test_prepare_touches_no_database(self, db):
        prepared = rh._prepare_run(_run("a", rack="r01"))
        assert prepared["run_id"] == "a"
        assert rh.count_runs() == 0

    def test_single_ingest_still_upserts_without_growing_the_count(self, db):
        rh.ingest_run(_run("a", rack="r01", status="pass"))
        rh.ingest_run(_run("a", rack="r01", status="fail"))
        assert rh.count_runs() == 1
        assert rh.get_run("a")["status"] == "fail"

    def test_first_seen_at_survives_a_re_ingest(self, db):
        rh.ingest_run(_run("a", rack="r01"))
        first = rh.get_run("a")["first_seen_at"]
        rh.ingest_run(_run("a", rack="r01", status="fail"))
        assert rh.get_run("a")["first_seen_at"] == first
