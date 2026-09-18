# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""TC-6 .. TC-14 and TC-38 of the Phase 1 test plan: the audit event, sink and flood guard.

Traces to AC-7, AC-9, AC-10, AC-10a, AC-10b, AC-10c, AC-10d, AC-10e, AC-10f, AC-10g, AC-14a,
AC-14b, AC-14c, AC-18, AC-31a, AC-33.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time

import audit
import logging_config as lc
import pytest
from audit import sink


@pytest.fixture(autouse=True)
def _audit_env(monkeypatch, tmp_path):
    """A cleared feature environment and an audit file this case owns."""
    for name in list(lc.SETTINGS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NVIS_LOG_SINKS", "file")
    monkeypatch.setenv("NVIS_LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("NVIS_AUDIT_FILE", str(tmp_path / "audit" / "audit.jsonl"))
    audit.reset_for_tests()
    lc.reset_for_tests()
    lc.configure(force=True)
    audit.configure(force=True)
    yield
    audit.reset_for_tests()
    lc.reset_for_tests()


def _audit_path(tmp_path):
    return tmp_path / "audit" / "audit.jsonl"


def _lines(tmp_path) -> list[dict]:
    for h in logging.getLogger(sink.AUDIT_LOGGER).handlers:
        h.flush()
    p = _audit_path(tmp_path)
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _ok(**kw):
    return {"action": "rack.power:on", "result": audit.Result.SUCCESS, **kw}


def _refused(**kw):
    return {"action": "rack.power:on", "result": audit.Result.REFUSED, **kw}


# --------------------------------------------------------------------------- TC-6


class TestTC6EventSchema:
    def test_schema(self, tmp_path):
        """AC-7: the exact key set, FR-5's fields plus policy_engine from NFR-13."""
        assert audit.emit(**_ok(resource="bos01-a05", principal="rkoike")) is True
        (event,) = _lines(tmp_path)
        assert tuple(event) == audit.EVENT_FIELDS
        for field in audit.EVENT_FIELDS:
            if field != "latency_ms":  # the only field legitimately null when untimed
                assert event[field] != "" or field in ("resource", "src_ip", "user_agent",
                                                       "request_id", "cluster")

    def test_decision_is_not_evaluated_in_phase_1(self, tmp_path):
        """AC-33: no event may claim an authorization decision no engine made."""
        audit.emit(**_ok())
        audit.emit(**_refused(principal="p1"))
        events = _lines(tmp_path)
        assert len(events) == 2, "one permitted and one refused, both written"
        assert {e["decision"] for e in events} == {audit.Decision.NOT_EVALUATED}
        assert {e["policy_engine"] for e in events} == {"none"}
        assert audit.Decision.ALLOW not in {e["decision"] for e in events}

    def test_service_comes_from_the_action_not_the_caller(self, tmp_path):
        audit.emit(**_ok(action="fabric.nvswitch:reset_sdn"))
        (event,) = _lines(tmp_path)
        assert event["service"] == "fabric.nvswitch"


# --------------------------------------------------------------------------- TC-7 / TC-32


class TestTC7EncoderOnly:
    """NFR-10: the encoder is the only write path (AC-31a), and forgery fails (AC-31)."""

    FORGERY = 'x\n{"principal":"admin","action":"rack.power:on","result":"success"}'

    def test_records_are_encoder_produced_only(self, tmp_path, monkeypatch):
        # Patch the name the sink resolves, not the shared json module -- this test reads the file
        # back with json.loads, and breaking that would fail for the wrong reason.
        monkeypatch.setattr(sink, "json", type("J", (), {
            "dumps": staticmethod(lambda *a, **k: (_ for _ in ()).throw(TypeError())),
        }))
        assert audit.emit(**_ok()) is False, "with the encoder disabled nothing may be written"
        assert _lines(tmp_path) == []

    def test_newline_in_user_agent_cannot_forge_a_record(self, tmp_path):
        """TC-32 / AC-31, the highest-severity control in the phase (threat T-49)."""
        before = len(_lines(tmp_path))
        audit.emit(**_ok(principal="rkoike", user_agent=self.FORGERY))
        events = _lines(tmp_path)

        assert len(events) == before + 1, "one line added, not two"
        assert "\n" not in json.dumps(events[-1])
        assert events[-1]["user_agent"] == self.FORGERY, "the payload survives, escaped"
        assert not [e for e in events if e["principal"] == "admin"], "no forged principal"

    def test_only_audit_events_reach_the_jsonl_file(self, tmp_path):
        """Regression: a diagnostic must not land in the trail and make it unparseable.

        The sink's own logger was ``audit.sink`` — a *child* of the ``audit`` logger — so every
        warning it emitted propagated into the JSONL file as a non-JSON line, corrupting the trail
        exactly when something had already gone wrong. Two guards now: the diagnostic logger lives
        outside the ``audit`` namespace, and the file handler accepts only marked event records.
        """
        audit.emit(**_ok(resource="a-real-event"))
        logging.getLogger("audit.some.future.module").error("not an audit event")
        logging.getLogger(sink.AUDIT_LOGGER).warning("nor is this")

        raw = _audit_path(tmp_path).read_text(encoding="utf-8")
        assert "not an audit event" not in raw
        assert "nor is this" not in raw
        events = _lines(tmp_path)  # would raise if any line were not JSON
        assert [e["resource"] for e in events] == ["a-real-event"]

    def test_no_formatted_record_construction_in_the_package(self):
        """The structural half: the write path holds exactly one serialisation call."""
        import ast
        import pathlib

        src = pathlib.Path(sink.__file__).read_text(encoding="utf-8")
        fn = next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "write"
        )
        dumps = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "dumps"
        ]
        assert len(dumps) == 1, "exactly one json.dumps in the write path"
        assert not [n for n in ast.walk(fn) if isinstance(n, ast.JoinedStr)], "no f-string record"


# --------------------------------------------------------------------------- TC-33


class TestTC33QueryStringSecret:
    """NFR-11 / AC-32: a credential in a URL is not visible to key-name redaction."""

    FAKE = "tok-not-a-real-credential-0123456789"

    def test_query_param_secret_is_redacted_in_the_trail(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RACK_PROJECT_TOKEN", self.FAKE)
        lc.reset_for_tests()
        lc.configure(force=True)
        audit.configure(force=True)

        audit.emit(**_ok(resource=f"/api/run-validation?token={self.FAKE}"))
        raw = _audit_path(tmp_path).read_text(encoding="utf-8")
        assert self.FAKE not in raw, "the raw value must not reach the audit file"
        assert "[REDACTED]" in raw


# --------------------------------------------------------------------------- TC-8 / TC-9


class TestTC8AppendOnly:
    def test_append_only(self, tmp_path):
        """AC-9: a second write leaves the first line byte-identical."""
        audit.emit(**_ok(resource="first"))
        first = _audit_path(tmp_path).read_bytes().splitlines()[0]
        audit.emit(**_ok(resource="second"))
        assert _audit_path(tmp_path).read_bytes().splitlines()[0] == first


class TestTC9RotationBounded:
    def test_caps_are_set_under_real_defaults(self):
        """AC-10a: both a size cap and a backup count, neither unlimited."""
        plan = sink.sizing()
        assert plan.max_bytes > 0
        assert plan.backups > 0
        handler = sink.installed_handler()
        assert handler.maxBytes > 0 and handler.backupCount > 0

    def test_rotation_bounds_total_bytes(self, tmp_path, monkeypatch):
        """AC-10b: 4 KB cap as a test-speed device, not a change to the shipped default."""
        monkeypatch.setenv("NVIS_AUDIT_MAX_BYTES", "4096")
        audit.configure(force=True)
        for i in range(200):
            audit.emit(**_ok(resource=f"r{i}"))

        d = _audit_path(tmp_path).parent
        files = list(d.glob("audit.jsonl*"))
        plan = sink.sizing()
        assert len(files) <= plan.backups + 1, "file count is capped by backupCount + 1"
        assert sum(f.stat().st_size for f in files) <= plan.total_bytes * 1.1


# --------------------------------------------------------------------------- TC-10


class TestTC10RetentionVariants:
    def test_long_retention_preserves_more_history(self, tmp_path, monkeypatch):
        """AC-10d: the opt-in keeps history the default budget would have evicted."""
        monkeypatch.setenv("NVIS_AUDIT_MAX_BYTES", "4096")
        monkeypatch.setenv("NVIS_AUDIT_LONG_RETENTION", "1")
        audit.configure(force=True)
        for i in range(200):
            audit.emit(**_ok(resource=f"r{i}"))

        ids = {e["resource"] for e in _lines(tmp_path)}
        rotated = list(_audit_path(tmp_path).parent.glob("audit.jsonl.*"))
        # The base count is read from the setting, not written as a literal: this asserted
        # `3 * FACTOR` and broke when the default was raised to 24, which is a test failing
        # on its own hardcoded copy of a value rather than on the property it cares about.
        assert sink.sizing().backups == int(sink.AUDIT_BACKUPS.resolve()) * sink.LONG_RETENTION_FACTOR
        assert ids or rotated, "history exists across the rotated set"

    def test_budgets_are_independent(self, tmp_path, monkeypatch):
        """AC-10e: filling the application log cannot evict audit history (FR-7d)."""
        monkeypatch.setenv("NVIS_LOG_MAX_BYTES", "2048")
        monkeypatch.setenv("NVIS_LOG_BACKUPS", "1")
        lc.reset_for_tests()
        lc.configure(force=True)
        audit.configure(force=True)

        audit.emit(**_ok(resource="must-survive"))
        for i in range(2000):
            logging.getLogger("noisy.module").info("filling the application log %d", i)
        for h in logging.getLogger().handlers:
            h.flush()

        assert _audit_path(tmp_path) != tmp_path / "app.log"
        assert any(e["resource"] == "must-survive" for e in _lines(tmp_path))

    def test_retention_without_stdout(self, tmp_path, monkeypatch):
        """AC-10f / FR-11a: the trail must not depend on stdout, which a recreate discards."""
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        lc.reset_for_tests()
        lc.configure(force=True)
        audit.configure(force=True)
        assert not any(
            type(h) is logging.StreamHandler for h in logging.getLogger().handlers
        ), "no stdout sink in this configuration"

        audit.emit(**_ok(resource="no-stdout"))
        assert any(e["resource"] == "no-stdout" for e in _lines(tmp_path))


# --------------------------------------------------------------------------- TC-38


class TestTC38RetentionSizing:
    """AC-10c / AC-10g: which of the two constraints binds, and that it is recorded."""

    def test_fresh_deployment_is_bound_by_the_ceiling(self, tmp_path):
        plan = sink.sizing(_audit_path(tmp_path))
        assert plan.binding == sink.BIND_CEILING
        assert plan.implied_days is None, "no history means no honest retention figure"

    def test_ceiling_holds_at_least_the_target_at_the_nominal_rate(self):
        """The capacity claim, computed rather than asserted from a constant."""
        plan = sink.sizing()
        events = plan.total_bytes / plan.event_bytes
        assert events / 1000 >= 30, (
            f"{plan.total_bytes} B / {plan.event_bytes} B = {events:.0f} events "
            f"= {events / 1000:.1f} days at 1000/day; FR-7b's floor is 30"
        )

    def test_the_active_file_counts_toward_the_budget(self):
        plan = sink.sizing()
        assert plan.total_bytes == plan.max_bytes * (plan.backups + 1)

    def test_target_binds_when_it_needs_less_than_the_ceiling(self, tmp_path, monkeypatch):
        """AC-10c: with a modest observed rate the time target is the tighter constraint."""
        p = _audit_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'{"x":1}\n' * 50)
        monkeypatch.setattr(sink, "_observed_events_per_day", lambda *_a: 10.0)
        monkeypatch.setenv("NVIS_AUDIT_RETENTION_DAYS", "30")

        plan = sink.sizing(p)
        assert plan.binding == sink.BIND_RETENTION
        assert plan.implied_days == 30.0

    def test_ceiling_binds_and_is_reported_when_the_rate_is_high(self, tmp_path, monkeypatch):
        """AC-10g: the ceiling is respected and named as the binding constraint."""
        p = _audit_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'{"x":1}\n' * 50)
        # Derived from the ceiling, not a literal. This injected 500,000 events/day, which filled
        # the old 20 MiB budget in well under 30 days and stopped doing so when the ceiling rose to
        # 125 MiB -- so the case broke without the behaviour changing. The rate that makes the
        # ceiling bind is a function of the ceiling, so compute it.
        one_day_of_events = sink.sizing(p).total_bytes / len(b'{"x":1}\n')
        monkeypatch.setattr(sink, "_observed_events_per_day", lambda *_a: one_day_of_events * 2)

        plan = sink.sizing(p)
        assert plan.binding == sink.BIND_CEILING
        assert plan.total_bytes <= int(sink.AUDIT_MAX_BYTES.resolve()) * (plan.backups + 1)
        assert plan.implied_days is not None and plan.implied_days < 30


# --------------------------------------------------------------------------- TC-11 / TC-12


class TestTC11FanOut:
    def test_audit_logger_fanout_exactly_once(self, tmp_path):
        """AC-10 / FR-7a: one record per event on the audit logger, no duplicates."""
        seen: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record):
                seen.append(record)

        cap = Capture()
        logging.getLogger(sink.AUDIT_LOGGER).addHandler(cap)
        try:
            audit.emit(**_ok())
            audit.emit(**_ok())
            audit.emit(**_ok())
        finally:
            logging.getLogger(sink.AUDIT_LOGGER).removeHandler(cap)
        assert len(seen) == 3

    def test_events_reach_the_configured_app_sinks_too(self, tmp_path):
        """The point of FR-7a: propagation is what makes syslog/SIEM delivery work."""
        audit.emit(**_ok(resource="fanned-out"))
        for h in logging.getLogger().handlers:
            h.flush()
        assert "fanned-out" in (tmp_path / "app.log").read_text(encoding="utf-8")


class TestTC12Permissions:
    def test_permissions_are_owner_only(self, tmp_path):
        """AC-18 / NFR-4: 0700 directory, 0600 file, regardless of the process umask."""
        audit.emit(**_ok())
        p = _audit_path(tmp_path)
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700


# --------------------------------------------------------------------------- TC-13 / TC-14


class TestTC13FloodGuard:
    def test_individuals_until_threshold_then_one_summary(self, tmp_path):
        """AC-14a as amended: T individual events plus one summary carrying N - T."""
        audit.emit(**_refused(principal="p", reason="unauthenticated"))
        for _ in range(49):
            audit.emit(**_refused(principal="p", reason="unauthenticated"))
        assert audit.flush() == 1, "the collapsed remainder flushes as exactly one summary"

        events = _lines(tmp_path)
        individual = [e for e in events if not e.get("collapsed")]
        summaries = [e for e in events if e.get("collapsed")]
        threshold = int(sink.AUDIT_FLOOD_THRESHOLD.resolve())

        assert len(individual) == threshold, f"{threshold} written individually"
        assert len(summaries) == 1
        assert summaries[0]["count"] == 50 - threshold
        assert len(events) == threshold + 1, "51 refusals do not become 51 events"

    def test_a_lone_refusal_is_written_immediately(self, tmp_path):
        """The reason the semantics were pinned: no flush needed to see one refusal."""
        audit.emit(**_refused(principal="solo", reason="cross-origin"))
        events = _lines(tmp_path)
        assert len(events) == 1
        assert events[0]["result"] == audit.Result.REFUSED
        assert not events[0].get("collapsed")

    def test_distinct_refusals_are_not_collapsed(self, tmp_path):
        """AC-14c: differing in principal, action or reason means three separate events."""
        audit.emit(**_refused(principal="a", reason="unauthenticated"))
        audit.emit(**_refused(principal="b", reason="unauthenticated"))
        audit.emit(**_refused(principal="a", reason="cross-origin"))
        audit.emit(**_refused(principal="a", action="rack.gpu:reset", reason="unauthenticated"))
        audit.flush()
        assert len(_lines(tmp_path)) == 4


class TestTC14FloodDoesNotEvictSuccesses:
    def test_flood_does_not_evict_successes(self, tmp_path, monkeypatch):
        """AC-14b: 20 successes (~10 KB) against a 4 KB x 4 = 16 KB budget all survive a flood."""
        monkeypatch.setenv("NVIS_AUDIT_MAX_BYTES", "4096")
        audit.configure(force=True)

        for i in range(20):
            audit.emit(**_ok(resource=f"success-{i:02d}"))
        for _ in range(5000):
            audit.emit(**_refused(principal="loop", reason="unauthenticated"))
        audit.flush()

        d = _audit_path(tmp_path).parent
        text = "".join(
            f.read_text(encoding="utf-8", errors="ignore") for f in sorted(d.glob("audit.jsonl*"))
        )
        survived = [i for i in range(20) if f"success-{i:02d}" in text]
        assert len(survived) == 20, (
            f"only {len(survived)}/20 successes survived; the guard is meant to stop a refusal "
            "loop evicting the history it should be recorded in"
        )


# --------------------------------------------------------------------------- TC-38


class TestRetentionBudget:
    """TC-38 (FR-7b, AC-10c, AC-10g): the budget holds a month, and says which limit bound it.

    Method is analysis, per the plan: a capacity claim asserted against the configured budget
    rather than by writing a month of events. The event size is MEASURED here rather than taken
    from ``NOMINAL_EVENT_BYTES``, because trusting the constant would make this test agree with
    the sink by construction even if the field set grew.
    """

    #: The plan's stated reference rate for the retention arithmetic.
    EVENTS_PER_DAY = 1000

    def test_default_budget_retains_a_month(self, tmp_path):
        audit.configure(force=True)
        audit.emit(**_ok(resource="/api/rack-power", user_agent="Mozilla/5.0 (X11; Linux x86_64)"))
        audit.flush()

        rows = _lines(tmp_path)
        assert rows, "need one real event to measure"
        event_bytes = len(json.dumps(rows[0], separators=(",", ":")).encode("utf-8")) + 1

        plan = sink.sizing(_audit_path(tmp_path))
        retained_events = plan.total_bytes / event_bytes
        retained_days = retained_events / self.EVENTS_PER_DAY

        assert retained_days >= 30, (
            f"budget holds {retained_days:.1f} days at {self.EVENTS_PER_DAY} events/day "
            f"({plan.total_bytes} bytes / {event_bytes} B per event); FR-7b floor is 30"
        )

    def test_the_backup_count_excludes_the_active_file(self, tmp_path):
        """AC-10g: ``backupCount=N`` keeps the active file too, so the budget is (N+1) files.

        The brief's sizing table originally said "15 MB (3 x 5 MB)". The error was conservative,
        but a retention claim that disagrees with the code is the kind of thing an auditor finds.

        **Stated as a property rather than as byte totals.** The original form asserted 3 backups
        and exactly 20 MiB, so raising the default to 24 broke it — a test failing on its own copy
        of a default instead of on the off-by-one it exists to catch.
        """
        audit.configure(force=True)
        plan = sink.sizing(_audit_path(tmp_path))
        assert plan.total_bytes == plan.max_bytes * (plan.backups + 1)

    def test_the_shipped_default_is_ninety_days_of_history(self, tmp_path):
        """The current figures, pinned separately so a change to them is a deliberate act.

        24 backups, 125 MiB, chosen on 2026-09-10 when the trail became readable: at a measured
        1.15 MB/day the previous 3 backups held about 17 days, so a review asking about last month
        found it gone.
        """
        audit.configure(force=True)
        plan = sink.sizing(_audit_path(tmp_path))
        assert plan.backups == 24
        assert plan.total_bytes == 125 * 1024 * 1024

    def test_a_fresh_file_reports_the_ceiling_as_binding(self, tmp_path):
        """With no history there is no observed rate, so implied_days is unknown, not zero.

        Reporting 0 days would read as "retains nothing"; None is the honest answer and is why
        the ceiling is the constraint that can actually be honoured on a fresh deployment.
        """
        audit.configure(force=True)
        plan = sink.sizing(_audit_path(tmp_path))
        assert plan.binding == sink.BIND_CEILING
        assert plan.implied_days is None

    def test_long_retention_lifts_the_ceiling_without_removing_it(self, tmp_path, monkeypatch):
        """FR-7c is an opt-in for more history, not for unbounded growth."""
        audit.configure(force=True)
        base = sink.sizing(_audit_path(tmp_path))
        monkeypatch.setenv("NVIS_AUDIT_LONG_RETENTION", "1")
        lc.reset_for_tests()
        lifted = sink.sizing(_audit_path(tmp_path))
        assert lifted.total_bytes > base.total_bytes
        assert lifted.backups == base.backups * sink.LONG_RETENTION_FACTOR
        assert lifted.total_bytes < base.total_bytes * 1000, "still bounded"

    def test_a_smaller_ceiling_reduces_the_budget(self, tmp_path, monkeypatch):
        """Guard against a budget that ignores its own setting."""
        monkeypatch.setenv("NVIS_AUDIT_MAX_BYTES", str(64 * 1024))
        lc.reset_for_tests()
        audit.configure(force=True)
        plan = sink.sizing(_audit_path(tmp_path))
        assert plan.max_bytes == 64 * 1024
        assert plan.total_bytes == 64 * 1024 * (plan.backups + 1)


# --------------------------------------------------------------------------- TC-34


@pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="TC-34 is a timing measurement; shared CI runners make p95 meaningless. Run locally.",
)
class TestLatencyBudget:
    """TC-34 (NFR-2a, AC-17a): the audit path adds no more than 5 ms at p95.

    Interleaved rather than run as two blocks, which the plan asks for and which matters: a
    machine that gets busy halfway through would otherwise load the whole penalty onto whichever
    arm ran second, and the result would look like an audit cost. ``Setting.resolve()`` reads the
    environment on every call and the chokepoint checks it per request, so the flag can be flipped
    between individual requests without reconfiguring anything.

    The execution stub is what makes the delta meaningful. Without it each request would spend
    milliseconds pretending to reach a head node, and the audit write would vanish into that noise.
    """

    WARMUP = 20
    SAMPLES = 200
    BUDGET_MS = 5.0

    @staticmethod
    def _p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        """Imported here, not at module scope: the rest of this file tests the sink in isolation
        and should not pay for the app's import-time bootstrap (catalog seed, threads)."""
        import app as app_module
        import host_exec

        monkeypatch.setattr(
            host_exec, "run_on_host",
            lambda cmd, *a, **k: __import__("subprocess").CompletedProcess(cmd, 0, "", ""),
            raising=True,
        )
        # A real dry run keeps the handler off the wire without changing the audited path.
        monkeypatch.setenv("RACK_UPTIME_DRY_RUN", "1")
        monkeypatch.setenv("NVIS_AUDIT_FILE", str(tmp_path / "audit" / "audit.jsonl"))
        audit.configure(force=True)
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client()

    def _timed_post(self, client) -> float:
        start = time.perf_counter()
        client.post("/api/rack-uptime", json={"rack": "p5-r01"})
        return (time.perf_counter() - start) * 1000.0

    def test_latency_budget_p95(self, client, tmp_path, monkeypatch):
        for _ in range(self.WARMUP):
            self._timed_post(client)

        on: list[float] = []
        off: list[float] = []
        for _ in range(self.SAMPLES):
            monkeypatch.setenv("NVIS_AUDIT_ENABLED", "1")
            on.append(self._timed_post(client))
            monkeypatch.setenv("NVIS_AUDIT_ENABLED", "0")
            off.append(self._timed_post(client))

        added = self._p95(on) - self._p95(off)
        assert added <= self.BUDGET_MS, (
            f"audit added {added:.2f} ms at p95 (on={self._p95(on):.2f} ms, "
            f"off={self._p95(off):.2f} ms) against a {self.BUDGET_MS} ms budget. The design's "
            "recorded fallback is a bounded queue drained by one writer thread."
        )

    def test_the_measurement_actually_measured_an_audited_path(self, client, tmp_path, monkeypatch):
        """Guard the guard: a budget met because nothing was audited proves nothing.

        If the canonical endpoint stopped being audited -- renamed, un-decorated, excluded -- the
        p95 delta would collapse to zero and TC-34 would pass for the wrong reason.
        """
        monkeypatch.setenv("NVIS_AUDIT_ENABLED", "1")
        self._timed_post(client)
        audit.flush()
        rows = _lines(tmp_path)
        assert rows, "no audit event was written, so the latency delta above measures nothing"
        # The resource is now the RESOLVED identifier (`rack:p5-r01`) rather than the URL path.
        # This asserted the path, which is exactly what changed: the gate computed the identifier
        # and then discarded it, so every rack in the fleet recorded the same resource.
        assert any(str(r.get("resource", "")).startswith("rack") for r in rows), [
            r.get("resource") for r in rows
        ]
