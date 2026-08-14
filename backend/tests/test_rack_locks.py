# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for rack_locks.py: claims CRUD, snapshot derivation, TTL / heartbeat semantics."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import rack_locks


@pytest.fixture(autouse=True)
def isolate_claims_file(tmp_path, monkeypatch):
    """Point the module-level CLAIMS_FILE at a tmp path so each test starts clean."""
    f = tmp_path / "rack_locks.json"
    monkeypatch.setattr(rack_locks, "CLAIMS_FILE", f)
    monkeypatch.delenv("RACK_LOCK_MAX_AGE_SEC", raising=False)
    yield f


def _now_iso(offset_sec: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).isoformat()


class TestSanitizers:
    def test_sanitize_operator_strips_and_truncates(self):
        assert rack_locks.sanitize_operator("  alice  ") == "alice"
        assert rack_locks.sanitize_operator("alice\nbob") == "alice bob"
        assert rack_locks.sanitize_operator("x" * 200) == "x" * 64
        assert rack_locks.sanitize_operator(None) == ""

    def test_normalize_rack_id(self):
        assert rack_locks.normalize_rack_id("P5-R01") == "p5-r01"
        assert rack_locks.normalize_rack_id("  p17-r03 ") == "p17-r03"
        with pytest.raises(ValueError):
            rack_locks.normalize_rack_id("not-a-rack")
        with pytest.raises(ValueError):
            rack_locks.normalize_rack_id("")


class TestClaimsCRUD:
    def test_upsert_writes_and_reads(self):
        claim = rack_locks.upsert_claim("p5-r01", "alice", note="poweroff", ttl_sec=600)
        assert claim["rack"] == "p5-r01"
        assert claim["operator"] == "alice"
        assert claim["note"] == "poweroff"

        all_claims = rack_locks.list_claims()
        assert "p5-r01" in all_claims
        assert all_claims["p5-r01"]["operator"] == "alice"

    def test_upsert_requires_operator(self):
        with pytest.raises(ValueError):
            rack_locks.upsert_claim("p5-r01", "", ttl_sec=60)

    def test_upsert_refresh_by_same_operator_extends_expiry(self):
        first = rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60)
        time.sleep(0.05)
        second = rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=600)
        # Refresh preserves the original "since" but bumps expires_at.
        assert first["since"] == second["since"]
        assert second["expires_at"] > first["expires_at"]

    def test_upsert_by_other_operator_rejected(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60)
        with pytest.raises(PermissionError):
            rack_locks.upsert_claim("p5-r01", "bob", ttl_sec=60)

    def test_force_takeover_replaces_other_operator(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60, note="alice was here")
        claim = rack_locks.upsert_claim(
            "p5-r01", "bob", ttl_sec=60, note="taking over", force=True
        )
        assert claim["operator"] == "bob"
        assert claim["stolen_from"] == "alice"
        assert claim["note"] == "taking over"
        live = rack_locks.list_claims()
        assert live["p5-r01"]["operator"] == "bob"
        assert live["p5-r01"]["stolen_from"] == "alice"

    def test_force_takeover_resets_since(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60)
        original_since = rack_locks.list_claims()["p5-r01"]["since"]
        time.sleep(0.02)
        claim = rack_locks.upsert_claim("p5-r01", "bob", ttl_sec=60, force=True)
        assert claim["since"] != original_since

    def test_force_without_existing_claim_is_a_normal_claim(self):
        claim = rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60, force=True)
        assert claim["operator"] == "alice"
        assert "stolen_from" not in claim

    def test_force_refresh_by_same_operator_does_not_mark_stolen(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60)
        claim = rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=120, force=True)
        assert claim["operator"] == "alice"
        assert "stolen_from" not in claim

    def test_release_owner_only(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60)
        with pytest.raises(PermissionError):
            rack_locks.release_claim("p5-r01", "bob")
        assert rack_locks.release_claim("p5-r01", "alice") is True
        assert rack_locks.list_claims() == {}

    def test_release_nonexistent_returns_false(self):
        assert rack_locks.release_claim("p5-r01", "alice") is False

    def test_purge_drops_expired(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=60)
        # Manually expire by rewriting the file with an old expires_at.
        path = rack_locks.CLAIMS_FILE
        data = path.read_text(encoding="utf-8")
        assert "expires_at" in data
        # rewrite via the module so file format stays canonical
        with open(path, "r+", encoding="utf-8") as f:
            blob = f.read()
            import json
            obj = json.loads(blob)
            obj["p5-r01"]["expires_at"] = _now_iso(-10)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(obj))
        dropped = rack_locks.purge_expired_claims()
        assert dropped == 1
        assert rack_locks.list_claims() == {}


class TestSnapshotDerivation:
    def test_active_job_becomes_lock(self):
        active = [
            {
                "job_id": "abc",
                "rack": "p5-r01",
                "kind": "gpu_reset",
                "operator": "alice",
                "phase": "running",
                "started_at": _now_iso(-10),
            },
        ]
        snap = rack_locks.build_lock_snapshot("bob", active)
        assert snap["me"] == "bob"
        assert "p5-r01" in snap["locks"]
        entry = snap["locks"]["p5-r01"]
        assert entry["operator"] == "alice"
        assert entry["kind"] == "active_job"
        assert entry["detail"] == "gpu_reset"

    def test_terminal_phases_excluded(self):
        active = [
            {"job_id": "a", "rack": "p5-r01", "kind": "x", "operator": "alice", "phase": "done"},
            {"job_id": "b", "rack": "p5-r02", "kind": "x", "operator": "alice", "phase": "error"},
        ]
        snap = rack_locks.build_lock_snapshot("bob", active)
        assert snap["locks"] == {}

    def test_max_age_filters_stuck_jobs(self, monkeypatch):
        monkeypatch.setenv("RACK_LOCK_MAX_AGE_SEC", "120")
        active = [
            {
                "job_id": "old",
                "rack": "p5-r01",
                "kind": "stuck",
                "operator": "alice",
                "phase": "running",
                "started_at": _now_iso(-9999),
            },
            {
                "job_id": "new",
                "rack": "p5-r02",
                "kind": "fresh",
                "operator": "alice",
                "phase": "running",
                "started_at": _now_iso(-10),
            },
        ]
        snap = rack_locks.build_lock_snapshot("bob", active)
        assert "p5-r01" not in snap["locks"]
        assert "p5-r02" in snap["locks"]

    def test_claim_appears_when_no_active_job(self):
        rack_locks.upsert_claim("p5-r01", "alice", note="planned maintenance", ttl_sec=300)
        snap = rack_locks.build_lock_snapshot("bob", [])
        assert "p5-r01" in snap["locks"]
        entry = snap["locks"]["p5-r01"]
        assert entry["kind"] == "claim"
        assert entry["operator"] == "alice"
        assert entry["detail"] == "planned maintenance"

    def test_snapshot_propagates_stolen_from_after_takeover(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=300)
        rack_locks.upsert_claim("p5-r01", "bob", ttl_sec=300, force=True)
        snap = rack_locks.build_lock_snapshot("carol", [])
        entry = snap["locks"]["p5-r01"]
        assert entry["operator"] == "bob"
        assert entry.get("stolen_from") == "alice"

    def test_active_job_wins_over_claim(self):
        rack_locks.upsert_claim("p5-r01", "bob", ttl_sec=300)
        active = [
            {
                "job_id": "a",
                "rack": "p5-r01",
                "kind": "validation",
                "operator": "alice",
                "phase": "running",
                "started_at": _now_iso(-5),
            },
        ]
        snap = rack_locks.build_lock_snapshot("carol", active)
        assert snap["locks"]["p5-r01"]["operator"] == "alice"
        assert snap["locks"]["p5-r01"]["kind"] == "active_job"

    def test_is_lock_held_by_other_ignores_self(self):
        active = [
            {
                "job_id": "a",
                "rack": "p5-r01",
                "kind": "x",
                "operator": "alice",
                "phase": "running",
                "started_at": _now_iso(-5),
            },
        ]
        assert rack_locks.is_lock_held_by_other("p5-r01", "alice", active) is None
        held = rack_locks.is_lock_held_by_other("p5-r01", "bob", active)
        assert held is not None
        assert held["operator"] == "alice"

    def test_offline_operator_claim_dropped(self):
        rack_locks.upsert_claim(
            "p5-r01", "alice", client_id="client-abc-1234567", ttl_sec=300
        )
        # No client_id present in active_clients -> orphan -> dropped.
        snap = rack_locks.build_lock_snapshot("bob", [], active_clients={})
        assert "p5-r01" not in snap["locks"]
        # If we add the client with a recent heartbeat, the claim is honored.
        snap2 = rack_locks.build_lock_snapshot(
            "bob", [], active_clients={"client-abc-1234567": time.time()}
        )
        assert "p5-r01" in snap2["locks"]

    def test_claim_without_client_id_skips_offline_check(self):
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=300)  # no client_id
        snap = rack_locks.build_lock_snapshot("bob", [], active_clients={})
        assert "p5-r01" in snap["locks"]


class TestConcurrency:
    def test_concurrent_upsert_same_operator_does_not_corrupt_file(self):
        """100 refresh writes from one operator must leave a single valid JSON record."""
        def _refresh() -> None:
            for _ in range(20):
                rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=600)

        threads = [threading.Thread(target=_refresh) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        import json
        with open(rack_locks.CLAIMS_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        assert "p5-r01" in obj
        assert obj["p5-r01"]["operator"] == "alice"
