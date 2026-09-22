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
"""The scheduler watcher.

Its whole justification is answering *when* the scheduler stopped answering, so the tests
that matter are the ones about `since` and about what happens on a cluster whose scheduler
has no readiness probe at all — a Slurm site must get an honest "not applicable" rather
than a silent skip or an invented "healthy".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scheduler_watch


@pytest.fixture(autouse=True)
def clean_history():
    scheduler_watch._samples.clear()
    yield
    scheduler_watch._samples.clear()


def _provider(kind: str, probe=None):
    attrs = {"kind": kind}
    if probe is not None:
        attrs["probe"] = staticmethod(probe)
    return type("S", (), attrs)()


class TestItFollowsTheActiveClusterNotAnAssumption:
    def test_a_backend_with_a_probe_is_asked(self, monkeypatch):
        monkeypatch.setattr(
            "schedulers.factory.build_scheduler",
            lambda *a, **k: _provider("pythoncm", lambda: {"ready": True}),
        )
        reading = scheduler_watch.sample_once()
        assert reading["scheduler"] == "pythoncm"
        assert reading["ready"] is True

    def test_a_backend_without_one_reports_unknown_not_healthy_and_not_skipped(self, monkeypatch):
        # A Slurm deployment must get an honest answer. Recording `ready=True` on no
        # evidence would make the watcher a source of false confidence, and skipping the
        # sample entirely would make a Slurm cluster indistinguishable from a dead watcher.
        monkeypatch.setattr(
            "schedulers.factory.build_scheduler", lambda *a, **k: _provider("slurm")
        )
        reading = scheduler_watch.sample_once()
        assert reading["scheduler"] == "slurm"
        assert reading["ready"] is None
        assert "no readiness probe" in reading["detail"]

    def test_it_never_hardcodes_pythoncm(self, monkeypatch):
        # The watcher this replaced probed a PythonCM broker by name. Ours asks whatever
        # the active cluster resolves to, so switching clusters switches what is watched.
        seen = []
        for kind in ("pythoncm", "slurm", "local"):
            monkeypatch.setattr(
                "schedulers.factory.build_scheduler",
                lambda *a, _k=kind, **kw: _provider(_k, lambda: {"ready": True}),
            )
            seen.append(scheduler_watch.sample_once()["scheduler"])
        assert seen == ["pythoncm", "slurm", "local"]


class TestAFailureIsDataNotAnException:
    def test_an_unreachable_scheduler_is_recorded_rather_than_raised(self, monkeypatch):
        def boom():
            raise RuntimeError("pythoncm probe failed: cert rejected")

        monkeypatch.setattr(
            "schedulers.factory.build_scheduler", lambda *a, **k: _provider("pythoncm", boom)
        )
        reading = scheduler_watch.sample_once()
        assert reading["ready"] is False
        assert "cert rejected" in reading["error"]

    def test_a_backend_that_cannot_even_be_built_is_still_a_sample(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("no connection configured")

        monkeypatch.setattr("schedulers.factory.build_scheduler", boom)
        assert scheduler_watch.sample_once()["ready"] is False


class TestSinceIsTheWholePoint:
    """A single probe says "unreachable". Only a series says "for the last forty minutes"."""

    def test_since_is_when_the_state_began_not_when_we_last_looked(self):
        for at in ("10:00:00", "10:01:00", "10:02:00"):
            scheduler_watch.record({"at": at, "scheduler": "pythoncm", "ready": False})
        assert scheduler_watch.summary()["since"] == "10:00:00"

    def test_a_recovery_resets_it(self):
        scheduler_watch.record({"at": "10:00:00", "scheduler": "p", "ready": False})
        scheduler_watch.record({"at": "10:01:00", "scheduler": "p", "ready": True})
        summary = scheduler_watch.summary()
        assert summary["current"]["ready"] is True
        assert summary["since"] == "10:01:00"

    def test_one_blink_is_distinguishable_from_a_sustained_outage(self):
        scheduler_watch.record({"at": "10:00:00", "scheduler": "p", "ready": True})
        scheduler_watch.record({"at": "10:01:00", "scheduler": "p", "ready": False})
        scheduler_watch.record({"at": "10:02:00", "scheduler": "p", "ready": True})
        summary = scheduler_watch.summary()
        assert summary["since"] == "10:02:00"
        # The blink is still counted, so a flapping scheduler is visible rather than erased
        # by the fact that the newest reading happens to be healthy.
        assert summary["unhealthy_count"] == 1

    def test_an_empty_history_is_not_an_error(self):
        assert scheduler_watch.summary()["current"] is None


class TestItIsBoundedAndSwitchable:
    def test_the_history_cannot_grow_without_limit(self):
        # In-memory and capped on purpose: this data answers "now", and persisting it would
        # be a fifth storage location for something nobody queries after the fact.
        for i in range(scheduler_watch.MAX_SAMPLES + 50):
            scheduler_watch.record({"at": str(i), "scheduler": "p", "ready": True})
        assert len(scheduler_watch.history()) == scheduler_watch.MAX_SAMPLES

    def test_demo_mode_does_not_watch(self, monkeypatch):
        # There is no cluster to reach, so it would record a wall of failures describing
        # nothing.
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")
        assert scheduler_watch.enabled() is False

    def test_it_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        monkeypatch.setenv("RACK_SCHEDULER_WATCH_DISABLED", "1")
        assert scheduler_watch.enabled() is False

    def test_the_interval_has_a_floor(self, monkeypatch):
        # The check must never become the load it is watching for.
        monkeypatch.setenv("RACK_SCHEDULER_WATCH_INTERVAL_SEC", "1")
        assert scheduler_watch.interval_sec() >= 10


class TestTheEndpoint:
    def test_it_reports_history_and_is_gated(self):
        import app as app_module
        from identity import testing

        scheduler_watch.record({"at": "10:00:00", "scheduler": "pythoncm", "ready": False})
        body = app_module.app.test_client().get("/api/scheduler-watch").get_json()
        assert body["ok"] is True
        assert body["current"]["ready"] is False
        assert len(body["history"]) == 1

        with testing.unauthenticated_client(app_module.app) as anonymous:
            assert anonymous.get("/api/scheduler-watch").status_code == 401
