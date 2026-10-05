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
"""Rack-status summary excludes non-GB200 (0-node) racks from validation stats."""
import threading
from pathlib import Path

import native_status
import pytest
from native_status import NativeRackTableState, rack_total_sinfo_nodes


@pytest.fixture(autouse=True)
def _no_host_results_history(monkeypatch):
    """Unit tests must not inherit completion rows from a developer/live DB."""
    monkeypatch.setattr(native_status, "_durable_completed_stage_logs_by_rack", dict)


def test_rack_total_sinfo_nodes_sums_states():
    counts = {
        ("bos01-b05", "idle"): 16,
        ("bos01-b05", "down"): 2,
        ("bos01-b06", "alloc"): 18,
    }
    assert rack_total_sinfo_nodes("bos01-b05", counts) == 18
    assert rack_total_sinfo_nodes("bos01-b06", counts) == 18
    # A rack with no sinfo entries (switch / mgmt rack) reports zero.
    assert rack_total_sinfo_nodes("bos01-a01", counts) == 0


def _build(monkeypatch, *, racks, sinfo_counts, by_rack_log, sinfo_done=True):
    # Never touch Slurm in the test.
    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: (set(), {}))
    monkeypatch.setenv("RACK_SQUEUE_JOB_LOOKUP_DISABLED", "1")
    st = NativeRackTableState("test", Path("/tmp"))
    st.racks = list(racks)
    st.sinfo_counts = dict(sinfo_counts)
    st.by_rack_log = {k: dict(v) for k, v in by_rack_log.items()}
    st.ds_done = True
    st.sinfo_done = sinfo_done
    st.log_done = True
    return st


def test_zero_node_racks_excluded_from_summary(monkeypatch):
    # Two GB200 racks (18 nodes each) + two switch racks (no sinfo nodes).
    st = _build(
        monkeypatch,
        racks=["bos01-b05", "bos01-b06", "bos01-a01", "bos01-a02"],
        sinfo_counts={("bos01-b05", "idle"): 18, ("bos01-b06", "idle"): 18},
        by_rack_log={
            "bos01-b05": {"nemo_state": "done", "nccl_ok": True, "llm_ok": True, "_path": "x-1Rack-1.txt"},
            "bos01-b06": {"nemo_state": "done", "nccl_ok": True, "llm_ok": True, "_path": "x-1Rack-2.txt"},
            # a01/a02 have no logs -> would normally count as pending.
        },
    )
    rows, summary, _footnotes, _legend = st.build_table_rows()

    # Floor/table still show all four racks.
    assert {r["rack"] for r in rows} == {"bos01-b05", "bos01-b06", "bos01-a01", "bos01-a02"}
    # Stats count only the two compute racks; the 0-node racks are excluded.
    total = summary["done"] + summary["fail"] + summary["ongoing"] + summary["pending"]
    assert total == 2
    assert summary["done"] == 2
    assert summary["pending"] == 0
    assert summary["stage1"]["done"] == 2
    assert summary["stage1"]["pending"] == 0


def test_split_stage_logs_judge_each_stage_from_its_own_log(monkeypatch):
    """A newer NeMo log must not decide the NCCL verdict, or a passing stage 1 reads as failed.

    Stage 1 and stage 2 are separate Slurm jobs since llmb-run, so ``by_rack_log`` (newest log
    of any kind) only ever holds evidence for one of them.
    """
    st = _build(
        monkeypatch,
        racks=["r01"],
        sinfo_counts={("r01", "idle"): 18},
        by_rack_log={
            # Newest log is the NeMo one; on its own it says nothing about NCCL.
            "r01": {"nemo_state": "done", "nccl_ok": False, "llm_ok": True,
                    "_path": "cmods-NEMO-1Rack-r01-18N-2.txt"},
        },
    )
    st.by_rack_stage_log = {
        "r01": {
            "nccl": {"nemo_state": "done", "nccl_ok": True, "llm_ok": False,
                     "_path": "cmods-NCCL-1Rack-r01-18N-1.txt"},
            "nemo": {"nemo_state": "done", "nccl_ok": False, "llm_ok": True,
                     "_path": "cmods-NEMO-1Rack-r01-18N-2.txt"},
        }
    }
    _rows, summary, _f, _l = st.build_table_rows()
    # NCCL passed in its own log and NeMo passed in its own, so the rack is done.
    assert summary["done"] == 1
    assert summary["fail"] == 0


def test_split_stage_failure_is_attributed_to_the_failing_stage(monkeypatch):
    st = _build(
        monkeypatch,
        racks=["r01"],
        sinfo_counts={("r01", "idle"): 18},
        by_rack_log={
            "r01": {"nemo_state": "done", "nccl_ok": True, "llm_ok": True,
                    "_path": "cmods-NEMO-1Rack-r01-18N-2.txt"},
        },
    )
    st.by_rack_stage_log = {
        "r01": {
            "nccl": {"nemo_state": "fail", "nccl_ok": False, "llm_ok": False,
                     "_path": "cmods-NCCL-1Rack-r01-18N-1.txt"},
            "nemo": {"nemo_state": "done", "nccl_ok": True, "llm_ok": True,
                     "_path": "cmods-NEMO-1Rack-r01-18N-2.txt"},
        }
    }
    _rows, summary, _f, _l = st.build_table_rows()
    assert summary["fail"] == 1, "a failed stage 1 must not be masked by a passing stage 2"


def test_durable_completion_survives_failed_probe_cache_and_cache_replacement(monkeypatch):
    """Acceptance does not disappear when the replaceable parser cache is wrong or empty."""
    monkeypatch.setattr(
        native_status,
        "_durable_completed_stage_logs_by_rack",
        lambda: {
            rack: {
                "nccl": {
                    "nemo_state": "done",
                    "nccl_ok": True,
                    "llm_ok": False,
                    "jid": 10,
                    "_path": f"/logs/nccl-{rack}-10.txt",
                    "_mtime": 10,
                },
                "nemo": {
                    "nemo_state": "done",
                    "nccl_ok": False,
                    "llm_ok": True,
                    "jid": 11,
                    "_path": f"/logs/nemo-{rack}-11.txt",
                    "_mtime": 11,
                },
            }
            for rack in ("r01", "r02")
        },
    )
    st = _build(
        monkeypatch,
        racks=["r01", "r02"],
        sinfo_counts={("r01", "idle"): 18, ("r02", "idle"): 18},
        by_rack_log={
            # r01 has a later bad parser-cache row; r02 represents a complete
            # cache replacement with no ephemeral log evidence at all.
            "r01": {
                "nemo_state": "fail",
                "nccl_ok": False,
                "llm_ok": False,
                "jid": 12,
                "_path": "cmods-NEMO-1Rack-r01-18N-12.txt",
            },
        },
    )
    rows, summary, _f, _l = st.build_table_rows()
    assert {row["rack"]: (row["stage1"], row["stage2"]) for row in rows} == {
        "r01": ("done", "done"),
        "r02": ("done", "done"),
    }
    assert summary["done"] == 2
    assert summary["stage2"]["done"] == 2


def test_logs_without_a_stage_marker_keep_the_single_log_verdict(monkeypatch):
    """Not every site names its logs '-NEMO-1Rack-'; those racks must not all read pending."""
    st = _build(
        monkeypatch,
        racks=["r01"],
        sinfo_counts={("r01", "idle"): 18},
        by_rack_log={
            "r01": {"nemo_state": "done", "nccl_ok": True, "llm_ok": True,
                    "_path": "cmods-nemo-r01-18N-7.txt"},
        },
    )
    _rows, summary, _f, _l = st.build_table_rows()
    assert summary["done"] == 1
    assert summary["pending"] == 0


def test_canonical_build_waits_for_completed_stage_log_scan(monkeypatch, tmp_path):
    """A cold native state must not publish an all-pending canonical snapshot."""
    nccl = tmp_path / "cmods-NCCL-1Rack-r01-18N-101.txt"
    nemo = tmp_path / "cmods-NEMO-1Rack-r01-18N-102.txt"
    entries = [(nccl, 1_000_000_000, 10), (nemo, 2_000_000_000, 10)]

    monkeypatch.setattr(native_status, "_cmsh_rack_list", lambda: ["r01"])
    monkeypatch.setattr(native_status, "_cmsh_ds_text", lambda: "")
    monkeypatch.setattr(native_status, "parse_cmsh_ds_to_ct_column_by_rack", lambda _text: {})
    monkeypatch.setattr(native_status, "collect_sinfo_nodes_and_shapes", lambda: ({f"r01-ct{i:02d}": "idle" for i in range(1, 19)}, {}))
    monkeypatch.setattr(native_status, "sinfo_counts_for_nodes", lambda _nodes: {("r01", "idle"): 18})
    monkeypatch.setattr(native_status, "_glob_one_rack_logs", lambda _log_dir: entries)
    monkeypatch.setattr(native_status, "_rack_from_1rack_log", lambda _name: "r01")
    monkeypatch.setattr(native_status, "_load_log_cache", lambda _path: {})
    monkeypatch.setattr(native_status, "_save_log_cache", lambda *_args: None)
    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: (set(), {}))
    monkeypatch.setattr("validation_activities.live_stage_jobs_by_rack", dict)

    probe_started = threading.Event()
    release_probe = threading.Event()

    def probe(path):
        probe_started.set()
        assert release_probe.wait(2)
        if "NCCL" in path.name:
            return {"nemo_state": "done", "nccl_ok": True, "llm_ok": False, "jid": 101}
        return {"nemo_state": "done", "nccl_ok": False, "llm_ok": True, "jid": 102}

    holder = []

    def build():
        holder.append(
            native_status.build_native_rack_status(
                tmp_path,
                False,
                probe,
                wait_for_workers=True,
                worker_timeout_sec=2,
            )
        )

    thread = threading.Thread(target=build)
    thread.start()
    assert probe_started.wait(1)
    assert thread.is_alive(), "canonical build returned before its log worker finished"
    release_probe.set()
    thread.join(2)
    assert not thread.is_alive()
    assert holder[0]["summary"]["done"] == 1
    assert holder[0]["summary"]["pending"] == 0


def test_zero_node_racks_counted_until_sinfo_ready(monkeypatch):
    # Before sinfo completes we cannot tell compute from switch racks: count all.
    st = _build(
        monkeypatch,
        racks=["bos01-b05", "bos01-a01"],
        sinfo_counts={},
        by_rack_log={},
        sinfo_done=False,
    )
    _rows, summary, _f, _l = st.build_table_rows()
    total = summary["done"] + summary["fail"] + summary["ongoing"] + summary["pending"]
    assert total == 2


def test_per_stage_squeue_does_not_force_both_stages_ongoing(monkeypatch):
    """A live Stage-1 NCCL job must not paint Stage-2 NeMo as running."""
    monkeypatch.setattr(
        native_status,
        "load_squeue_jobs_map",
        lambda: ({"100"}, {"100": "01:00"}),
    )
    monkeypatch.setenv("RACK_SQUEUE_JOB_LOOKUP_DISABLED", "1")
    st = NativeRackTableState("test", Path("/tmp"))
    st.racks = ["r01"]
    st.sinfo_counts = {("r01", "idle"): 18}
    st.by_rack_log = {
        "r01": {
            "nemo_state": "ongoing",
            "nccl_ok": True,
            "llm_ok": True,
            "jid": 100,
            "_path": "cmods-NCCL-1Rack-r01-18N-100.txt",
        },
    }
    st.by_rack_stage_log = {
        "r01": {
            "nccl": {
                "nemo_state": "ongoing",
                "nccl_ok": True,
                "llm_ok": False,
                "jid": 100,
                "_path": "cmods-NCCL-1Rack-r01-18N-100.txt",
            },
            "nemo": {
                "nemo_state": "done",
                "nccl_ok": False,
                "llm_ok": True,
                "jid": 99,
                "_path": "cmods-NEMO-1Rack-r01-18N-99.txt",
            },
        }
    }
    st.ds_done = True
    st.sinfo_done = True
    st.log_done = True
    rows, summary, _f, _l = st.build_table_rows()
    assert rows[0]["stage1"] == "ongoing"
    assert rows[0]["stage2"] == "done"
    assert summary["ongoing"] == 1
    assert summary["stage1"]["ongoing"] == 1
    assert summary["stage2"]["done"] == 1


def test_live_activity_job_overrides_stale_fail_log(monkeypatch):
    """A re-launched Stage-1 must paint ongoing even when the newest NCCL log is a prior fail."""
    monkeypatch.setattr(
        native_status,
        "load_squeue_jobs_map",
        lambda: ({"117122"}, {"117122": "00:10"}),
    )
    monkeypatch.setenv("RACK_SQUEUE_JOB_LOOKUP_DISABLED", "1")
    monkeypatch.setattr(
        "validation_activities.live_stage_jobs_by_rack",
        lambda **_kw: {"r01": {"nccl": "117122"}},
    )
    st = NativeRackTableState("test", Path("/tmp"))
    st.racks = ["r01"]
    st.sinfo_counts = {("r01", "alloc"): 18}
    st.by_rack_log = {
        "r01": {
            "nemo_state": "fail",
            "nccl_ok": False,
            "llm_ok": False,
            "jid": 114307,
            "_path": "cmods-NCCL-1Rack-r01-18N-114307.txt",
        },
    }
    st.by_rack_stage_log = {
        "r01": {
            "nccl": {
                "nemo_state": "fail",
                "nccl_ok": False,
                "llm_ok": False,
                "jid": 114307,
                "_path": "cmods-NCCL-1Rack-r01-18N-114307.txt",
            },
        }
    }
    st.ds_done = True
    st.sinfo_done = True
    st.log_done = True
    rows, summary, _f, _l = st.build_table_rows()
    assert rows[0]["stage1"] == "ongoing"
    assert rows[0]["last_job"] == "117122"
    assert rows[0]["nemo"].startswith("ongoing")
    assert summary["ongoing"] == 1
    assert summary["stage1"]["ongoing"] == 1


def test_multirack_build_rows_accepts_pure_posix_paths(monkeypatch):
    """SSH/container mode globs return PurePosixPath tuples — unpack + never .resolve().

    A regression here 500s /api/multirack-status as an HTML error page, which the SPA
    surfaces as ``Unexpected token '<!DOCTYPE' is not valid JSON`` under Results & logs.
    """
    from pathlib import PurePosixPath

    from native_status import MultirackTableState

    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: (set(), {}))
    monkeypatch.setattr(native_status, "augment_running_set_for_job_ids", lambda *a, **k: None)
    monkeypatch.setenv("RACK_SQUEUE_JOB_LOOKUP_DISABLED", "1")

    remote = PurePosixPath(
        "/home/cmsupport/nvis-rack-status/var/logs/"
        "root-NEMO-ink50-dh01-h02,ink50-dh01-h03-36N-12345.txt"
    )
    st = MultirackTableState("test-mr", Path("/tmp"))
    st.log_paths = [(remote, 1, 100)]
    st.per_path_probe = {
        str(remote): {
            "ok": True,
            "nemo_state": "done",
            "nccl_ok": True,
            "llm_ok": True,
            "jid": 12345,
            "physical_racks": ["ink50-dh01-h02", "ink50-dh01-h03"],
        }
    }
    st.sinfo_done = True
    st.log_done = True

    rows = st.build_rows(lambda bn: bn, lambda bn: ["ink50-dh01-h02", "ink50-dh01-h03"])
    assert len(rows) == 1
    assert rows[0]["nemo"] == "done"
    assert rows[0]["last_job"] == "12345"
    assert "ink50-dh01-h02" in rows[0]["rack"]
    assert rows[0]["display_rack"] == rows[0]["rack"]
    assert rows[0]["log_file"].endswith("12345.txt")
