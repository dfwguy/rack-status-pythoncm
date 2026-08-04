# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Rack-status summary excludes non-GB200 (0-node) racks from validation stats."""
from pathlib import Path

import native_status
from native_status import NativeRackTableState, rack_total_sinfo_nodes


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
