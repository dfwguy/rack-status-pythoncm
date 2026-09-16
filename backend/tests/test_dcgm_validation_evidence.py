# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
from __future__ import annotations

import copy

import external_validation_evidence as evidence
import results_history


def snapshot() -> dict:
    def attempt(job: str, test: str, verdict: str, node: str = "ink50-dh02-a01-11") -> dict:
        return {
            "attempt": 1,
            "job_id": job,
            "outcome": verdict,
            "slurm_state": "COMPLETED",
            "exit_code": "0:0",
            "evidence": [{
                "artifact_path": f"/cluster/bringup/results/{node}/{test}.{job}.txt",
                "detail": f"explicit {verdict.lower()}",
                "job_id": job,
                "map_method": "mtime",
                "map_note": "",
                "node": node,
                "test": test,
                "verdict": verdict,
                "workload": "dcgm",
            }],
        }
    return {
        "complete": True,
        "collected_at": "2026-09-02T19:37:57Z",
        "lake": {"validation_attempts": [
            attempt("100", "dcgmi-r1", "PASS"),
            attempt("101", "dcgmi-r1", "FAIL"),
            attempt("90", "dcgmi-r1.prev1", "PASS"),
            attempt("102", "dcgmi-r3", "INCOMPLETE"),
        ]},
    }


def test_dcgm_records_retain_schema_provenance_and_retry_history():
    records = evidence.dcgm_records(snapshot())
    assert len(records) == 4
    assert records[0]["nodes"] == ["ink50-dh02-a01-11"]
    assert records[0]["extra"]["validation_test"] == "dcgmi-r1"
    assert records[0]["extra"]["map_method"] == "mtime"
    assert records[2]["extra"]["history_only"] is True
    assert records[3]["extra"]["raw_verdict"] == "INCOMPLETE"
    assert records[3]["status"] == "unknown"


def test_dcgm_checkpoint_is_idempotent_and_newer_failure_is_authoritative(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    first = evidence.ingest_dcgm_snapshot(snapshot(), source_path=tmp_path / "index.json")
    second = evidence.ingest_dcgm_snapshot(snapshot(), source_path=tmp_path / "index.json")
    assert first["skipped"] is False
    assert first["rows_ingested"] == 4
    assert second == {"skipped": True, "run_ids": [], "rows_ingested": 4}
    assert results_history.count_runs({"source": "nscale16k_dcgm_index"}) == 4

    events = evidence.dcgm_post_run_events()
    current_r1 = [row for row in events if row["validation_test"] == "dcgmi-r1" and row["authoritative"]]
    assert [(row["job_id"], row["state"]) for row in current_r1] == [("101", "FAIL")]
    assert next(row for row in events if row["raw_test"] == "dcgmi-r1.prev1")["history_only"] is True
    assert next(row for row in events if row["validation_test"] == "dcgmi-r3")["state"] == "INCOMPLETE"


def test_changed_snapshot_is_incremental_and_persists_after_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    payload = snapshot()
    evidence.ingest_dcgm_snapshot(payload, source_path=tmp_path / "index.json")
    changed = copy.deepcopy(payload)
    changed["lake"]["validation_attempts"][1]["evidence"][0]["verdict"] = "PASS"
    changed["lake"]["validation_attempts"][1]["outcome"] = "PASS"
    result = evidence.ingest_dcgm_snapshot(changed, source_path=tmp_path / "index.json")
    assert result["skipped"] is False
    # Stable old history is retained and the changed evidence is a new identity.
    assert results_history.count_runs({"source": "nscale16k_dcgm_index"}) == 5
    assert any(row["state"] == "PASS" and row["job_id"] == "101" for row in evidence.dcgm_post_run_events())


def test_incomplete_snapshot_and_malformed_rows_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    broken = snapshot()
    broken["complete"] = False
    try:
        evidence.ingest_dcgm_snapshot(broken, source_path=tmp_path / "index.json")
        assert False, "incomplete snapshots must be rejected"
    except ValueError:
        pass
    malformed = snapshot()
    malformed["lake"]["validation_attempts"].append({"evidence": [{"workload": "dcgm", "test": "dcgmi-r4"}]})
    result = evidence.ingest_dcgm_snapshot(malformed, source_path=tmp_path / "index.json")
    assert result["rows_ingested"] == 4


def test_checkpoint_batch_is_atomic_on_invalid_record(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    good = evidence.dcgm_records(snapshot())[0]
    try:
        results_history.ingest_runs_checkpointed(
            [good, {"status": "pass"}], source_id="atomic", fingerprint="one", source_path="/index"
        )
        assert False, "bad batch must fail"
    except results_history.ResultsHistoryError:
        pass
    assert results_history.count_runs({"source": "nscale16k_dcgm_index"}) == 0
