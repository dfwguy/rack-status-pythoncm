# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Cross-process cluster snapshot repository and refresh semantics."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from cluster_snapshot import ClusterSnapshotService, SnapshotRepository


class Clock:
    def __init__(self, value: float = 1_700_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def payload(*, rack_count: int = 1, hosts: int = 16) -> dict:
    rows = [
        {
            "rack": f"p5-r{i + 1:02d}",
            "nemo": "done",
            "last_job": "1",
            "sinfo": "idle",
            "ct_down_unk": "",
        }
        for i in range(rack_count)
    ]
    return {
        "ok": True,
        "returncode": 0,
        "updated_at": "2026-07-22T15:00:00+00:00",
        "rows": rows,
        "physical_rf0": {f"node-{i:02d}": f"10.0.0.{i + 1}" for i in range(hosts)},
        "summary": {"done": rack_count, "fail": 0, "ongoing": 0, "pending": 0},
    }


def service(tmp_path: Path, collector, clock: Clock | None = None) -> ClusterSnapshotService:
    now = clock or Clock()
    return ClusterSnapshotService(
        SnapshotRepository(tmp_path / "snapshots.sqlite3", clock=now),
        collector=collector,
        clock=now,
        lease_seconds=30,
    )


def test_cold_overview_first_and_status_first_share_snapshot(tmp_path):
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        return payload()

    snapshots = service(tmp_path, collect)
    overview = snapshots.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=60)
    status = snapshots.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=60)

    assert overview.state == status.state == "fresh"
    assert overview.snapshot_id == status.snapshot_id
    assert len(overview.payload["rows"]) == 1
    assert len(overview.payload["physical_rf0"]) == 16
    assert calls == 1


def test_fresh_cache_hit_calls_collector_once(tmp_path):
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        return payload()

    snapshots = service(tmp_path, collect)
    snapshots.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=60)
    snapshots.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=60)
    assert calls == 1


def test_successfully_confirmed_empty_inventory_is_fresh(tmp_path):
    snapshot = service(tmp_path, lambda: payload(rack_count=0, hosts=0)).get_cluster_snapshot(
        "cluster-a", "/logs"
    )
    assert snapshot.state == "fresh"
    assert snapshot.snapshot_id
    assert snapshot.payload["rows"] == []
    assert snapshot.payload["physical_rf0"] == {}


def test_exception_on_first_refresh_is_failed_without_payload(tmp_path):
    def collect():
        raise RuntimeError("inventory unavailable")

    snapshot = service(tmp_path, collect).get_cluster_snapshot("cluster-a", "/logs")
    assert snapshot.state == "failed"
    assert snapshot.snapshot_id is None
    assert snapshot.payload is None
    assert "inventory unavailable" in snapshot.last_error


def test_timeout_on_first_refresh_is_failed_not_empty(tmp_path):
    def collect():
        raise subprocess.TimeoutExpired(["collector", "--secret", "x"], 30)

    snapshot = service(tmp_path, collect).get_cluster_snapshot("cluster-a", "/logs")
    assert snapshot.state == "failed"
    assert snapshot.payload is None
    assert snapshot.last_error == "Cluster inventory refresh timed out"
    assert "--secret" not in snapshot.last_error


def test_unsuccessful_or_nonzero_collector_result_is_failure(tmp_path):
    for result in (
        {"ok": False, "error": "cmsh failed", "rows": []},
        {"ok": True, "returncode": 2, "stderr": "failed", "rows": []},
    ):
        snapshot = service(tmp_path, lambda result=result: result).get_cluster_snapshot(
            "cluster-a", f"/logs/{result.get('returncode', 'ok')}"
        )
        assert snapshot.state == "failed"
        assert snapshot.payload is None


def test_missing_inventory_fields_are_failure_not_confirmed_empty(tmp_path):
    snapshot = service(
        tmp_path,
        lambda: {"ok": True, "returncode": 0, "updated_at": "2026-07-22T15:00:00Z"},
    ).get_cluster_snapshot("cluster-a", "/logs")
    assert snapshot.state == "failed"
    assert snapshot.payload is None
    assert snapshot.last_error == "Cluster inventory collector returned incomplete inventory"


def test_failure_after_success_preserves_payload_id_and_marks_stale(tmp_path):
    outcomes = [payload(), RuntimeError("temporary failure")]

    def collect():
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    snapshots = service(tmp_path, collect)
    good = snapshots.get_cluster_snapshot("cluster-a", "/logs")
    stale = snapshots.get_cluster_snapshot("cluster-a", "/logs", force_refresh=True)

    assert stale.state == "stale"
    assert stale.snapshot_id == good.snapshot_id
    assert stale.payload == good.payload
    assert stale.last_error == "temporary failure"


def test_lease_contention_prevents_duplicate_collection(tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return payload()

    db = tmp_path / "snapshots.sqlite3"
    first = ClusterSnapshotService(SnapshotRepository(db), collector=collect, lease_seconds=30)
    second_calls = 0

    def collect_second():
        nonlocal second_calls
        second_calls += 1
        return payload()

    second = ClusterSnapshotService(SnapshotRepository(db), collector=collect_second, lease_seconds=30)
    holder: list = []
    thread = threading.Thread(
        target=lambda: holder.append(first.get_cluster_snapshot("cluster-a", "/logs"))
    )
    thread.start()
    assert started.wait(timeout=2)

    contended = second.get_cluster_snapshot("cluster-a", "/logs")
    assert contended.state == "initializing"
    assert contended.payload is None
    assert second_calls == 0

    release.set()
    thread.join(timeout=2)
    assert holder[0].state == "fresh"
    assert calls == 1


def test_expired_lease_can_be_recovered(tmp_path):
    clock = Clock()
    repo = SnapshotRepository(tmp_path / "snapshots.sqlite3", clock=clock)
    repo.acquire_refresh_lease("cluster-a", "/logs", repo.configuration_generation(), "dead", 10)
    clock.advance(11)

    snapshot = ClusterSnapshotService(
        SnapshotRepository(tmp_path / "snapshots.sqlite3", clock=clock),
        collector=lambda: payload(),
        clock=clock,
        lease_seconds=30,
    ).get_cluster_snapshot("cluster-a", "/logs")
    assert snapshot.state == "fresh"


def test_two_service_instances_share_database(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        return payload()

    one = ClusterSnapshotService(SnapshotRepository(db), collector=collect)
    two = ClusterSnapshotService(SnapshotRepository(db), collector=collect)
    first = one.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=60)
    second = two.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=60)
    assert first.snapshot_id == second.snapshot_id
    assert calls == 1


def test_generation_invalidation_prevents_old_snapshot_reuse(tmp_path):
    snapshots = service(tmp_path, lambda: payload())
    first = snapshots.get_cluster_snapshot("cluster-a", "/logs")
    generation = snapshots.invalidate()
    second = snapshots.get_cluster_snapshot("cluster-a", "/logs")
    assert generation == first.configuration_generation + 1
    assert second.configuration_generation == generation
    assert second.snapshot_id != first.snapshot_id


def test_separate_log_roots_do_not_share_snapshots(tmp_path):
    snapshots = service(tmp_path, lambda: payload())
    first = snapshots.get_cluster_snapshot("cluster-a", "/logs/a")
    second = snapshots.get_cluster_snapshot("cluster-a", "/logs/b")
    assert first.snapshot_key != second.snapshot_key
    assert first.snapshot_id != second.snapshot_id


def test_wait_false_returns_initializing_without_blocking(tmp_path):
    started = threading.Event()
    finish = threading.Event()
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        started.set()
        assert finish.wait(5)
        return payload()

    snapshots = service(tmp_path, collect)
    first = snapshots.get_cluster_snapshot(
        "cluster-a", "/logs", max_age_seconds=60, wait=False
    )
    assert first.state == "initializing"
    assert first.payload is None
    assert started.wait(2)
    assert calls == 1

    finish.set()
    done = None
    for _ in range(50):
        done = snapshots.get_cluster_snapshot(
            "cluster-a", "/logs", max_age_seconds=60, wait=False
        )
        if done.state == "fresh" and done.payload is not None:
            break
        time.sleep(0.05)
    assert done is not None
    assert done.state == "fresh"
    assert done.payload["summary"]["done"] == 1


def test_wait_false_contention_starts_only_one_collector(tmp_path):
    started = threading.Event()
    finish = threading.Event()
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        started.set()
        assert finish.wait(5)
        return payload()

    db = tmp_path / "snapshots.sqlite3"
    first = ClusterSnapshotService(
        SnapshotRepository(db), collector=collect, lease_seconds=30
    )
    second_calls = 0

    def collect_second():
        nonlocal second_calls
        second_calls += 1
        return payload()

    second = ClusterSnapshotService(
        SnapshotRepository(db), collector=collect_second, lease_seconds=30
    )

    cold = first.get_cluster_snapshot("cluster-a", "/logs", wait=False)
    assert cold.state == "initializing"
    assert started.wait(2)

    contended = second.get_cluster_snapshot("cluster-a", "/logs", wait=False)
    assert contended.state == "initializing"
    assert contended.payload is None
    assert second_calls == 0
    assert calls == 1

    finish.set()
    done = None
    for _ in range(50):
        done = first.get_cluster_snapshot("cluster-a", "/logs", wait=False)
        if done.state == "fresh":
            break
        time.sleep(0.05)
    assert done is not None
    assert done.state == "fresh"
    assert calls == 1
    assert second_calls == 0


def test_wait_false_success_transitions_to_fresh(tmp_path):
    gate = threading.Event()

    def collect():
        assert gate.wait(5)
        return payload()

    snapshots = service(tmp_path, collect)
    first = snapshots.get_cluster_snapshot("cluster-a", "/logs", wait=False)
    assert first.state == "initializing"
    assert first.payload is None

    gate.set()
    done = None
    for _ in range(50):
        done = snapshots.get_cluster_snapshot("cluster-a", "/logs", wait=False)
        if done.state == "fresh" and done.payload is not None:
            break
        time.sleep(0.05)
    assert done is not None
    assert done.state == "fresh"
    assert done.snapshot_id is not None
    assert done.payload["summary"]["done"] == 1


def test_wait_false_failure_preserves_stale_payload(tmp_path):
    clock = Clock()
    good = service(tmp_path, lambda: payload(), clock=clock)
    fresh = good.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=1)
    assert fresh.state == "fresh"
    prior_id = fresh.snapshot_id
    prior_payload = fresh.payload

    clock.advance(10)
    started = threading.Event()
    finish = threading.Event()

    def boom():
        started.set()
        assert finish.wait(5)
        raise RuntimeError("refresh failed")

    failing = ClusterSnapshotService(
        good.repository,
        collector=boom,
        clock=clock,
        lease_seconds=30,
    )
    out = failing.get_cluster_snapshot(
        "cluster-a", "/logs", max_age_seconds=1, force_refresh=True, wait=False
    )
    assert out.payload is not None
    assert out.state in ("fresh", "stale", "initializing")
    assert started.wait(2)

    finish.set()
    done = None
    for _ in range(50):
        done = failing.get_cluster_snapshot(
            "cluster-a", "/logs", max_age_seconds=60, wait=False
        )
        if done.state == "stale" and done.last_error:
            break
        time.sleep(0.05)
    assert done is not None
    assert done.state == "stale"
    assert done.snapshot_id == prior_id
    assert done.payload == prior_payload
    assert "refresh failed" in done.last_error
