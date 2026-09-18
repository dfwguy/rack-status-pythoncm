# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Cross-process cluster overview live-probe repository and refresh semantics."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from cluster_overview_live import (
    ClusterOverviewLiveService,
    LiveProbeRepository,
    LiveProbeSection,
)


class Clock:
    def __init__(self, value: float = 1_700_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def gated_value(started: threading.Event, release: threading.Event, value):
    started.set()
    assert release.wait(5)
    return value


def make_service(
    tmp_path: Path,
    *,
    bcm=None,
    slurm=None,
    queue=None,
    clock: Clock | None = None,
    lease_seconds: float = 30,
    ttls: dict[str, float] | None = None,
    db_name: str = "overview_live.sqlite3",
) -> ClusterOverviewLiveService:
    now = clock or Clock()
    collectors = {
        "bcm": bcm or (lambda: "BCM 11"),
        "slurm": slurm or (lambda: "slurm 25.05"),
        "queue": queue or (lambda: {"ok": True, "stdout": ""}),
    }
    return ClusterOverviewLiveService(
        tmp_path / db_name,
        collectors=collectors,
        ttls=ttls or {"bcm": 3600, "slurm": 3600, "queue": 15},
        clock=now,
        lease_seconds=lease_seconds,
    )


def _wait_section(service, cluster_id: str, section: str, *, state: str, timeout: float = 2.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = service.get_snapshot(cluster_id, allow_refresh=False)
        sec = last["sections"][section]
        if sec["state"] == state:
            return sec
        time.sleep(0.05)
    raise AssertionError(f"section {section!r} never reached {state!r}; last={last}")


def test_cold_request_returns_initializing_and_collects_in_background(tmp_path):
    started = threading.Event()
    release = threading.Event()
    try:
        service = make_service(
            tmp_path,
            bcm=lambda: gated_value(started, release, "BCM 11"),
            slurm=lambda: "slurm 25.05",
            queue=lambda: {"ok": True, "stdout": ""},
        )
        snapshot = service.get_snapshot("cluster-a")
        assert snapshot["sections"]["bcm"]["state"] == "initializing"
        assert snapshot["sections"]["bcm"]["value"] is None
        assert started.wait(1)
    finally:
        release.set()


def test_success_transition_to_fresh_with_serialized_metadata(tmp_path):
    started = threading.Event()
    release = threading.Event()
    try:
        service = make_service(
            tmp_path,
            bcm=lambda: gated_value(started, release, "BCM 11"),
        )
        cold = service.get_snapshot("cluster-a")
        assert cold["sections"]["bcm"]["state"] == "initializing"
        assert started.wait(1)
        release.set()
        section = _wait_section(service, "cluster-a", "bcm", state="fresh")
        assert section["value"] == "BCM 11"
        assert section["last_error"] is None
        assert isinstance(section["last_attempt_at"], str)
        assert isinstance(section["last_success_at"], str)
        assert section["age_seconds"] is not None
        assert section["age_seconds"] >= 0
        assert set(section) == {
            "state",
            "value",
            "last_attempt_at",
            "last_success_at",
            "age_seconds",
            "last_error",
        }
    finally:
        release.set()


def test_per_section_ttls_refresh_independently(tmp_path):
    clock = Clock()
    bcm_calls = 0
    queue_calls = 0

    def collect_bcm():
        nonlocal bcm_calls
        bcm_calls += 1
        return f"BCM {bcm_calls}"

    def collect_queue():
        nonlocal queue_calls
        queue_calls += 1
        return {"ok": True, "n": queue_calls}

    service = make_service(
        tmp_path,
        bcm=collect_bcm,
        queue=collect_queue,
        clock=clock,
        ttls={"bcm": 3600, "slurm": 3600, "queue": 15},
    )
    first = service.get_snapshot("cluster-a")
    assert first["sections"]["bcm"]["state"] == "initializing"
    bcm = _wait_section(service, "cluster-a", "bcm", state="fresh")
    queue = _wait_section(service, "cluster-a", "queue", state="fresh")
    assert bcm["value"] == "BCM 1"
    assert queue["value"] == {"ok": True, "n": 1}
    assert bcm_calls == 1
    assert queue_calls == 1

    clock.advance(20)
    second = service.get_snapshot("cluster-a")
    assert second["sections"]["bcm"]["value"] == "BCM 1"
    # queue TTL expired; bcm still within TTL
    assert bcm_calls == 1
    deadline = time.monotonic() + 2.5
    queue2 = None
    while time.monotonic() < deadline:
        queue2 = service.get_snapshot("cluster-a", allow_refresh=False)["sections"]["queue"]
        if queue2.get("value") == {"ok": True, "n": 2}:
            break
        time.sleep(0.05)
    assert queue2 is not None
    assert queue2["value"] == {"ok": True, "n": 2}
    assert queue_calls == 2
    assert bcm_calls == 1


def test_lease_contention_starts_only_one_collector(tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = 0
    second_calls = 0

    def collect():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(5)
        return "BCM 11"

    def collect_second():
        nonlocal second_calls
        second_calls += 1
        return "BCM other"

    try:
        db = tmp_path / "overview_live.sqlite3"
        first = ClusterOverviewLiveService(
            db,
            collectors={
                "bcm": collect,
                "slurm": lambda: "slurm",
                "queue": lambda: {"ok": True},
            },
            ttls={"bcm": 3600, "slurm": 3600, "queue": 15},
            lease_seconds=30,
        )
        second = ClusterOverviewLiveService(
            db,
            collectors={
                "bcm": collect_second,
                "slurm": lambda: "slurm",
                "queue": lambda: {"ok": True},
            },
            ttls={"bcm": 3600, "slurm": 3600, "queue": 15},
            lease_seconds=30,
        )
        cold = first.get_snapshot("cluster-a")
        assert cold["sections"]["bcm"]["state"] == "initializing"
        assert started.wait(2)

        contended = second.get_snapshot("cluster-a")
        assert contended["sections"]["bcm"]["state"] == "initializing"
        assert second_calls == 0
        assert calls == 1
    finally:
        release.set()

    section = _wait_section(first, "cluster-a", "bcm", state="fresh")
    assert section["value"] == "BCM 11"
    assert calls == 1
    assert second_calls == 0


def test_failure_after_success_preserves_last_good_as_stale(tmp_path):
    clock = Clock()
    outcomes = ["BCM 11", RuntimeError("temporary bcm failure")]

    def collect_bcm():
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    service = make_service(tmp_path, bcm=collect_bcm, clock=clock)
    service.get_snapshot("cluster-a")
    fresh = _wait_section(service, "cluster-a", "bcm", state="fresh")
    assert fresh["value"] == "BCM 11"

    clock.advance(4000)
    service.get_snapshot("cluster-a", force_refresh=True)
    stale = _wait_section(service, "cluster-a", "bcm", state="stale")
    assert stale["value"] == "BCM 11"
    assert "temporary bcm failure" in stale["last_error"]


def test_independent_section_failure_does_not_discard_others(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def boom_bcm():
        raise RuntimeError("bcm down")

    def slow_slurm():
        return gated_value(started, release, "slurm 25.05")

    try:
        service = make_service(
            tmp_path,
            bcm=boom_bcm,
            slurm=slow_slurm,
            queue=lambda: {"ok": True, "stdout": "q"},
        )
        service.get_snapshot("cluster-a")
        assert started.wait(1)
        failed = _wait_section(service, "cluster-a", "bcm", state="failed")
        assert failed["value"] is None
        assert "bcm down" in failed["last_error"]
        release.set()
        slurm = _wait_section(service, "cluster-a", "slurm", state="fresh")
        queue = _wait_section(service, "cluster-a", "queue", state="fresh")
        assert slurm["value"] == "slurm 25.05"
        assert queue["value"] == {"ok": True, "stdout": "q"}
    finally:
        release.set()


def test_failed_without_prior_value(tmp_path):
    service = make_service(
        tmp_path,
        bcm=lambda: (_ for _ in ()).throw(RuntimeError("no bcm")),
    )
    service.get_snapshot("cluster-a")
    failed = _wait_section(service, "cluster-a", "bcm", state="failed")
    assert failed["value"] is None
    assert failed["last_success_at"] is None
    assert "no bcm" in failed["last_error"]


def test_expired_lease_can_be_recovered(tmp_path):
    clock = Clock()
    db = tmp_path / "overview_live.sqlite3"
    blocked = threading.Event()
    release = threading.Event()

    def never_finish():
        blocked.set()
        release.wait(10)
        return "should-not-matter"

    try:
        holder = ClusterOverviewLiveService(
            db,
            collectors={
                "bcm": never_finish,
                "slurm": lambda: "slurm",
                "queue": lambda: {"ok": True},
            },
            ttls={"bcm": 3600, "slurm": 3600, "queue": 15},
            clock=clock,
            lease_seconds=10,
        )
        holder.get_snapshot("cluster-a")
        assert blocked.wait(2)
        clock.advance(11)

        recovered_calls = 0

        def recover():
            nonlocal recovered_calls
            recovered_calls += 1
            return "BCM recovered"

        recovered = ClusterOverviewLiveService(
            db,
            collectors={
                "bcm": recover,
                "slurm": lambda: "slurm",
                "queue": lambda: {"ok": True},
            },
            ttls={"bcm": 3600, "slurm": 3600, "queue": 15},
            clock=clock,
            lease_seconds=30,
        )
        recovered.get_snapshot("cluster-a")
        section = _wait_section(recovered, "cluster-a", "bcm", state="fresh")
        assert section["value"] == "BCM recovered"
        assert recovered_calls == 1
    finally:
        release.set()


def test_errors_are_sanitized_and_bounded(tmp_path):
    secret = "password=super-secret-token " + ("x" * 600)

    def boom():
        raise RuntimeError(secret)

    service = make_service(tmp_path, bcm=boom)
    service.get_snapshot("cluster-a")
    failed = _wait_section(service, "cluster-a", "bcm", state="failed")
    assert "super-secret-token" not in failed["last_error"]
    assert "[redacted]" in failed["last_error"]
    assert len(failed["last_error"]) <= 500


def test_allow_refresh_false_returns_cached_without_collecting(tmp_path):
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        return "BCM 11"

    service = make_service(tmp_path, bcm=collect)
    cold = service.get_snapshot("cluster-a", allow_refresh=False)
    assert cold["sections"]["bcm"]["state"] == "initializing"
    assert cold["sections"]["bcm"]["value"] is None
    assert calls == 0

    service.get_snapshot("cluster-a")
    _wait_section(service, "cluster-a", "bcm", state="fresh")
    before = calls
    again = service.get_snapshot("cluster-a", allow_refresh=False)
    assert again["sections"]["bcm"]["state"] == "fresh"
    assert again["sections"]["bcm"]["value"] == "BCM 11"
    assert calls == before


def test_force_refresh_bypasses_ttl(tmp_path):
    clock = Clock()
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        return f"BCM {calls}"

    service = make_service(tmp_path, bcm=collect, clock=clock)
    service.get_snapshot("cluster-a")
    _wait_section(service, "cluster-a", "bcm", state="fresh")
    assert calls == 1

    service.get_snapshot("cluster-a")
    time.sleep(0.05)
    assert calls == 1

    service.get_snapshot("cluster-a", force_refresh=True)
    deadline = time.monotonic() + 2.5
    section = None
    while time.monotonic() < deadline:
        section = service.get_snapshot("cluster-a", allow_refresh=False)["sections"]["bcm"]
        if section.get("value") == "BCM 2":
            break
        time.sleep(0.05)
    assert section is not None
    assert calls == 2
    assert section["value"] == "BCM 2"


def test_live_probe_section_metadata_shape():
    section = LiveProbeSection(
        cluster_id="c1",
        section="bcm",
        state="fresh",
        value="BCM 11",
        last_attempt_at=1_700_000_000.0,
        last_success_at=1_700_000_000.0,
        last_error=None,
        lease_owner=None,
        lease_expires_at=None,
    )
    meta = section.metadata(now=1_700_000_010.0)
    assert meta["state"] == "fresh"
    assert meta["value"] == "BCM 11"
    assert meta["age_seconds"] == 10.0
    assert meta["last_error"] is None
    assert "T" in meta["last_attempt_at"]
    assert "T" in meta["last_success_at"]


def test_repository_closes_sqlite_connections(tmp_path):
    """sqlite3 Connection context managers commit/rollback but do not close."""
    closes: list[object] = []

    class TrackingConnection:
        def __init__(self, real):
            self._real = real

        def close(self):
            closes.append(self)
            return self._real.close()

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class TrackingRepository(LiveProbeRepository):
        def _connect(self):
            return TrackingConnection(super()._connect())

    repo = TrackingRepository(tmp_path / "overview_live.sqlite3")
    assert closes, "initialize must close its sqlite connection"

    after_init = len(closes)
    assert repo.get("cluster-a", "bcm") is None
    assert len(closes) == after_init + 1

    assert repo.list_sections("cluster-a", ["bcm", "slurm"]) == {}
    assert len(closes) == after_init + 2

    leased, acquired = repo.acquire_refresh_lease("cluster-a", "bcm", "owner-1", 30)
    assert acquired
    assert leased.state == "initializing"
    assert len(closes) == after_init + 3

    fresh = repo.commit_success("cluster-a", "bcm", "owner-1", "BCM 11")
    assert fresh.state == "fresh"
    assert len(closes) == after_init + 4

    _leased2, acquired2 = repo.acquire_refresh_lease("cluster-a", "bcm", "owner-2", 30)
    assert acquired2
    failed = repo.commit_failure("cluster-a", "bcm", "owner-2", RuntimeError("boom"))
    assert failed.state == "stale"
    assert len(closes) == after_init + 6
