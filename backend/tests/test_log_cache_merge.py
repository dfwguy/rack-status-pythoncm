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
"""The log-analysis cache must not lose a concurrent worker's freshly probed entries.

Both cache writers used to do ``disk = load(); ...probe...; save(disk)``. Probing is a minute
of remote commands per cycle, so two Gunicorn workers overlap routinely: whichever saved second
wrote the snapshot it had loaded before the other's entries existed, erasing them. Those logs
were then re-probed on the very next cycle -- the cache silently stopped paying for itself
exactly when the dashboard was busiest.

``_save_log_cache`` now re-reads and merges under an exclusive sidecar lock, with the probe
still outside the lock so concurrent probing stays parallel.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import native_status


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "cache" / "native_log_analysis.json"


def _entry(tag: str) -> dict:
    return {"mtime_ns": 1, "size": 1, "probe": {"ok": 1, "jid": tag}}


def test_load_returns_empty_when_absent(cache_path):
    assert native_status._load_log_cache(cache_path) == {}


def test_save_then_load_round_trips(cache_path):
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("1")}, {"/l/a.txt"})
    assert native_status._load_log_cache(cache_path)["/l/a.txt"]["probe"]["jid"] == "1"


def test_a_concurrent_workers_entry_survives_our_save(cache_path):
    """The regression: worker B saves the snapshot it loaded before A's entry existed."""
    both = {"/l/a.txt", "/l/b.txt"}
    # Worker A finishes first and publishes its probe.
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("A")}, both)
    # Worker B saves a snapshot that predates A's entry. A's entry must survive.
    native_status._save_log_cache(cache_path, {"/l/b.txt": _entry("B")}, both)

    got = native_status._load_log_cache(cache_path)
    assert set(got) == both, "a concurrent worker's entry was lost"
    assert got["/l/a.txt"]["probe"]["jid"] == "A"
    assert got["/l/b.txt"]["probe"]["jid"] == "B"


def test_our_fresh_probe_wins_over_the_stored_one(cache_path):
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("old")}, {"/l/a.txt"})
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("new")}, {"/l/a.txt"})
    assert native_status._load_log_cache(cache_path)["/l/a.txt"]["probe"]["jid"] == "new"


def test_entries_for_deleted_logs_are_pruned(cache_path):
    native_status._save_log_cache(
        cache_path, {"/l/a.txt": _entry("1"), "/l/gone.txt": _entry("2")}, {"/l/a.txt", "/l/gone.txt"}
    )
    # Next cycle only sees a.txt, so the cache must not keep growing.
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("1")}, {"/l/a.txt"})
    assert set(native_status._load_log_cache(cache_path)) == {"/l/a.txt"}


def test_empty_alive_set_does_not_wipe_the_cache(cache_path):
    """A failed or empty scan must not be read as "every log was deleted"."""
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("1")}, {"/l/a.txt"})
    native_status._save_log_cache(cache_path, {}, set())
    assert set(native_status._load_log_cache(cache_path)) == {"/l/a.txt"}


def test_concurrent_saves_keep_every_entry(cache_path):
    """Eight threads racing the read-merge-write must not drop any entry."""
    keys = {f"/l/{i}.txt" for i in range(8)}
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        native_status._save_log_cache(cache_path, {f"/l/{i}.txt": _entry(str(i))}, keys)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    got = native_status._load_log_cache(cache_path)
    assert set(got) == keys, f"lost {sorted(keys - set(got))} under concurrency"


def test_a_stale_version_is_discarded(cache_path):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"version": native_status._NATIVE_LOG_CACHE_VERSION - 1, "entries": {"/l/a.txt": _entry("1")}}),
        encoding="utf-8",
    )
    assert native_status._load_log_cache(cache_path) == {}


def test_corrupt_json_is_discarded_not_raised(cache_path):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not json", encoding="utf-8")
    assert native_status._load_log_cache(cache_path) == {}
    # And a save over the corrupt file still succeeds.
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("1")}, {"/l/a.txt"})
    assert set(native_status._load_log_cache(cache_path)) == {"/l/a.txt"}


def test_both_caches_share_one_version_counter():
    """Same probe path produces both files, so one logic bump must invalidate both."""
    body = (native_status._PKG_DIR / "native_status.py").read_text(encoding="utf-8")
    assert body.count("_NATIVE_LOG_CACHE_VERSION = ") == 1
    assert "_MULTIRACK_LOG_CACHE_VERSION" not in body


def test_the_lock_is_a_sidecar_not_the_published_file(cache_path):
    """os.replace swaps the inode, so locking the cache file itself would guard nothing."""
    native_status._save_log_cache(cache_path, {"/l/a.txt": _entry("1")}, {"/l/a.txt"})
    assert (cache_path.parent / (cache_path.name + ".lock")).exists()
    assert not (cache_path.parent / (cache_path.name + ".tmp")).exists()


def test_completed_native_probe_is_archived_once_per_fingerprint(monkeypatch, tmp_path):
    ingested = []
    monkeypatch.setattr(native_status.results_history, "make_run_id", lambda *_a, **_k: "run-1")
    monkeypatch.setattr(native_status.results_history, "get_run", lambda _rid: None)
    monkeypatch.setattr(native_status.results_history, "ingest_run", lambda row: ingested.append(row) or "run-1")
    native_status._native_history_seen.clear()

    log = tmp_path / "cmods-NCCL-1Rack-bos01-b01-16N-42.txt"
    probe = {"jid": "42", "nccl_ok": True, "llm_ok": False}
    native_status._archive_completed_native_probe(
        log, mtime_ns=2_000_000_000, size=99, rack="bos01-b01", kind="nccl", probe=probe
    )
    native_status._archive_completed_native_probe(
        log, mtime_ns=2_000_000_000, size=99, rack="bos01-b01", kind="nccl", probe=probe
    )

    assert len(ingested) == 1
    assert ingested[0] == {
        "run_id": "run-1",
        "source_log_path": str(log),
        "log_basename": log.name,
        "log_mtime": 2.0,
        "log_size": 99,
        "workload_kind": "nccl",
        "rack": "bos01-b01",
        "job_id": "42",
        "status": "pass",
        "source": "native_status_probe",
        "extra": {"fingerprint_validated": True},
    }


@pytest.mark.parametrize(
    ("kind", "probe"),
    [
        ("nccl", {"nccl_ok": False}),
        ("nemo", {"llm_ok": False}),
        (None, {"nccl_ok": True, "llm_ok": True}),
    ],
)
def test_incomplete_native_probe_is_not_archived(monkeypatch, tmp_path, kind, probe):
    monkeypatch.setattr(
        native_status.results_history,
        "ingest_run",
        lambda _row: pytest.fail("incomplete probes must not enter completion history"),
    )
    native_status._archive_completed_native_probe(
        tmp_path / "one-rack.txt",
        mtime_ns=1,
        size=1,
        rack="bos01-b01",
        kind=kind,
        probe=probe,
    )


def test_worker_archives_a_fingerprint_validated_cache_hit(monkeypatch, tmp_path):
    log = tmp_path / "cmods-NEMO-1Rack-r01-16N-43.txt"
    cached = {"jid": "43", "nccl_ok": False, "llm_ok": True}
    monkeypatch.setattr(
        native_status,
        "_load_log_cache",
        lambda _path: {str(log): {"mtime_ns": 7, "size": 11, "probe": cached}},
    )
    monkeypatch.setattr(native_status, "_glob_one_rack_logs", lambda _path: [(log, 7, 11)])
    monkeypatch.setattr(native_status, "_rack_from_1rack_log", lambda _name: "r01")
    archived = []
    monkeypatch.setattr(
        native_status,
        "_archive_completed_native_probe",
        lambda path, **kwargs: archived.append((path, kwargs)),
    )
    state = native_status.NativeRackTableState("test", Path(tmp_path))

    state._worker_logs(lambda _path: pytest.fail("a matching cache hit must not be re-probed"))

    assert archived == [
        (
            log,
            {
                "mtime_ns": 7,
                "size": 11,
                "rack": "r01",
                "kind": "nemo",
                "probe": cached,
            },
        )
    ]
