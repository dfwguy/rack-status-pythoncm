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
    assert summary["pending"] == 2
