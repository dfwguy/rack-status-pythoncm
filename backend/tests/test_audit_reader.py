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
"""`audit/reader.py` — TC-1…TC-10 and TC-18…TC-23 of the audit-reader test plan.

Three of these catch failures that would otherwise be **silent**, which is why they exist rather
than a happy-path read test:

* **TC-9** — a page that skips events. An offset over an append-only trail shifts between requests,
  so page 2 both repeats and drops rows, and nothing in the response looks wrong.
* **TC-2** — history reordered by clock skew. Sorting by `ts` rather than insertion order would let
  an NTP step move an event, and would make the cursor skip past it.
* **TC-21** — a read that writes. `identity-auth-traps.mdc` §2: pruning on a read path had TinyDB
  rewrite the file even when nothing matched, defeating a cache keyed on its mtime, and every
  functional test passed throughout.

Plan: `.plc/briefs/auth-audit-reader-test-plan.md`.
"""

from __future__ import annotations

import json
import sys
import tracemalloc
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import functools
import operator

from audit import reader


def _event(i: int, *, ts: str | None = None, **over) -> str:
    event = {
        "event_id": f"id-{i:05d}",
        "ts": ts or f"2026-09-10T00:00:{i % 60:02d}.{i % 1000:03d}+00:00",
        "principal": "user:alice" if i % 2 else "user:bob",
        "auth_method": "token",
        "service": "rack.power",
        "action": "rack.power:off",
        "variant": "sync",
        "resource": f"rack.power:r{i}",
        "decision": "allow",
        "policy_engine": "rbac-v1",
        "result": "success",
        "src_ip": "127.0.0.1",
        "src_ip_trust": "peer",
        "user_agent": "Mozilla/5.0",
        "request_id": "r" * 32,
        "cluster": "nscale-tx",
        "latency_ms": 1.5,
    }
    event.update(over)
    return json.dumps(event)


@pytest.fixture
def trail(tmp_path):
    """Build a trail. `write(n, rotated=k)` lays *n* events across *k+1* files, oldest in `.k`.

    Returns the active path, which is what `reader.read(path=...)` anchors on — the rotated
    siblings are discovered from it, exactly as they are in production.
    """
    active = tmp_path / "audit.jsonl"

    def write(total: int, rotated: int = 0, *, events: list[str] | None = None) -> Path:
        lines = events if events is not None else [_event(i) for i in range(total)]
        buckets = rotated + 1
        per = max(1, len(lines) // buckets)
        # Oldest events go in the highest-numbered backup: `.1` is older than the active file.
        chunks = [lines[i:i + per] for i in range(0, len(lines), per)] or [[]]
        chunks = chunks[:buckets - 1] + [functools.reduce(operator.iadd, chunks[buckets - 1:], [])] if buckets > 1 else chunks
        for position, chunk in enumerate(chunks):
            target = active if position == len(chunks) - 1 else active.with_name(
                f"audit.jsonl.{len(chunks) - 1 - position}"
            )
            target.write_text("\n".join(chunk) + ("\n" if chunk else ""), encoding="utf-8")
        return active

    return write


class TestTC1NewestFirstAcrossRotation:
    def test_the_first_page_is_the_newest_events(self, trail):
        active = trail(30, rotated=2)
        page = reader.read(limit=10, path=active)
        assert [e["event_id"] for e in page.events] == [f"id-{i:05d}" for i in range(29, 19, -1)]

    def test_it_crosses_a_file_boundary_without_a_gap(self, trail):
        """The whole trail read in pages must reproduce the whole trail, exactly once each."""
        active = trail(30, rotated=2)
        seen, cursor = [], ""
        for _ in range(10):
            page = reader.read(limit=7, cursor=cursor, path=active)
            if not page.events:
                break
            seen += [e["event_id"] for e in page.events]
            cursor = page.cursor
        assert seen == [f"id-{i:05d}" for i in range(29, -1, -1)]
        assert len(set(seen)) == len(seen), "an event was returned twice"

    def test_it_reports_how_many_files_it_saw(self, trail):
        assert reader.read(limit=1, path=trail(30, rotated=2)).files == 3


class TestTC2OrderIsInsertionNotTimestamp:
    """The sequence is the physical order of the lines. `ts` is wall-clock and must not sort it."""

    def test_a_backdated_event_keeps_its_position(self, trail):
        """One event written with an earlier `ts` than its predecessor — an NTP step mid-run.

        Sorting by `ts` would move it, and a cursor anchored either side of it would then skip
        whatever the sort displaced. It must appear where it was written.
        """
        events = [_event(0), _event(1), _event(2, ts="2026-01-01T00:00:00.000+00:00"), _event(3)]
        active = trail(0, events=events)
        assert [e["event_id"] for e in reader.read(limit=10, path=active).events] == [
            "id-00003", "id-00002", "id-00001", "id-00000",
        ]

    def test_paging_over_skewed_data_still_loses_nothing(self, trail):
        events = [_event(0), _event(1, ts="2020-01-01T00:00:00.000+00:00"), _event(2), _event(3)]
        active = trail(0, events=events)
        first = reader.read(limit=2, path=active)
        second = reader.read(limit=2, cursor=first.cursor, path=active)
        assert [e["event_id"] for e in first.events + second.events] == [
            "id-00003", "id-00002", "id-00001", "id-00000",
        ]


class TestTC3AndTC4Filters:
    @pytest.mark.parametrize(("field_name", "value", "expect"), [
        ("principal", "user:alice", lambda e: e["principal"] == "user:alice"),
        ("action", "rack.power:off", lambda e: e["action"] == "rack.power:off"),
        ("service", "rack.power", lambda e: e["service"] == "rack.power"),
        ("decision", "allow", lambda e: e["decision"] == "allow"),
        ("result", "success", lambda e: e["result"] == "success"),
    ])
    def test_each_filter_narrows(self, trail, field_name, value, expect):
        active = trail(20)
        page = reader.read(reader.Filter(**{field_name: value}), limit=100, path=active)
        assert page.events, f"{field_name}={value} matched nothing; the case asserts nothing"
        assert all(expect(e) for e in page.events)

    def test_an_unmatched_filter_returns_nothing_rather_than_everything(self, trail):
        """The direction that matters: a filter bug that fails *open* returns the whole trail, and
        an operator reading it would conclude the wrong principal acted."""
        page = reader.read(reader.Filter(principal="user:nobody"), limit=100, path=trail(20))
        assert page.events == []

    def test_two_filters_compose_as_and(self, trail):
        """Asserted with a pair that would return more under OR — otherwise AND and OR are
        indistinguishable."""
        active = trail(20)
        alice = reader.read(reader.Filter(principal="user:alice"), limit=100, path=active)
        both = reader.read(
            reader.Filter(principal="user:alice", resource="r3"), limit=100, path=active
        )
        assert 0 < len(both.events) < len(alice.events)
        assert all(e["principal"] == "user:alice" and "r3" in e["resource"] for e in both.events)

    def test_resource_matches_a_substring_so_a_fan_out_is_findable(self, trail):
        """A fan-out records `rack.power:r1 (+3 more)`. An exact match would never find it, and
        that event is precisely the one a multi-rack investigation is looking for."""
        active = trail(0, events=[_event(1, resource="rack.power:r1 (+3 more)")])
        assert reader.read(reader.Filter(resource="r1"), limit=10, path=active).events


class TestTC7AndTC8TheCap:
    def test_an_unmatched_filter_stops_at_the_cap(self, trail, monkeypatch):
        monkeypatch.setattr(reader, "MAX_SCAN_EVENTS", 40)
        page = reader.read(reader.Filter(principal="user:nobody"), limit=10, path=trail(200))
        assert page.truncated is True
        assert page.scanned_events == 40
        assert page.events == []

    def test_a_truncated_answer_is_distinguishable_from_an_empty_one(self, trail, monkeypatch):
        """The point of FR-5. Both return no events; only one means "we stopped looking"."""
        monkeypatch.setattr(reader, "MAX_SCAN_EVENTS", 40)
        stopped = reader.read(reader.Filter(principal="user:nobody"), limit=10, path=trail(200))
        complete = reader.read(reader.Filter(principal="user:nobody"), limit=10, path=trail(5))
        assert stopped.events == complete.events == []
        assert stopped.truncated is True
        assert complete.truncated is False

    def test_a_since_bound_terminates_the_scan_rather_than_filtering(self, trail):
        """This is what keeps a bounded question cheap over unbounded history, and it is the
        mitigation for raising retention six-fold: without early termination a time-filtered query
        would scan to the cap every time."""
        events = [_event(i, ts=f"2026-09-{(i // 10) + 1:02d}T00:00:00.000+00:00") for i in range(60)]
        active = trail(0, events=events)
        page = reader.read(reader.Filter(since="2026-09-06T00:00:00.000+00:00"), limit=100,
                           path=active)
        assert page.events, "nothing matched; the case asserts nothing"
        assert page.scanned_events < 30, (
            f"scanned {page.scanned_events} of 60 — the since bound excluded rows instead of "
            f"stopping the walk, so far-back queries will always hit the cap"
        )


class TestTC9PaginationUnderConcurrentAppend:
    def test_appending_between_pages_neither_repeats_nor_skips(self, trail):
        """**The defect an offset would cause.** New events land at the newest end, which is where
        page 1 started, so an offset-based page 2 would return rows already seen and silently drop
        the same number from the far end."""
        active = trail(20)
        first = reader.read(limit=5, path=active)

        with open(active, "a", encoding="utf-8") as handle:
            handle.writelines(_event(i) + "\n" for i in range(100, 110))

        second = reader.read(limit=5, cursor=first.cursor, path=active)
        assert not ({e["event_id"] for e in first.events} & {e["event_id"] for e in second.events})
        assert [e["event_id"] for e in second.events] == [f"id-{i:05d}" for i in range(14, 9, -1)]


class TestTC10AnExpiredCursor:
    def test_a_rotated_away_anchor_reports_itself_and_returns_the_newest_page(self, trail):
        """A real state, not an error. Returning an empty page would read as "no more events",
        which is the one conclusion that must not be reachable by accident."""
        active = trail(20)
        page = reader.read(limit=5, cursor="id-99999", path=active)
        assert page.cursor_expired is True
        assert [e["event_id"] for e in page.events] == [f"id-{i:05d}" for i in range(19, 14, -1)]

    def test_a_valid_cursor_does_not_claim_expiry(self, trail):
        active = trail(20)
        first = reader.read(limit=5, path=active)
        assert reader.read(limit=5, cursor=first.cursor, path=active).cursor_expired is False


class TestTC19AndTC20MalformedLines:
    def test_a_torn_final_line_is_skipped_and_counted(self, trail):
        """A crash mid-write leaves a partial JSON object. The trail is the thing you read *after*
        something went wrong, so this is expected input."""
        active = trail(10)
        with open(active, "a", encoding="utf-8") as handle:
            handle.write('{"event_id": "torn", "ts": "2026-')
        page = reader.read(limit=20, path=active)
        assert page.skipped_lines == 1
        assert len(page.events) == 10

    @pytest.mark.parametrize("garbage", ["not json at all", "[1,2,3]", '"a string"', "42", "null"])
    def test_a_non_object_line_is_skipped(self, trail, garbage):
        """A JSON *array* is valid JSON and not an event. Parsing without a type check would put a
        list where the caller expects a mapping and fail much later, in the renderer."""
        active = trail(0, events=[_event(1), garbage, _event(2)])
        page = reader.read(limit=10, path=active)
        assert page.skipped_lines == 1
        assert [e["event_id"] for e in page.events] == ["id-00002", "id-00001"]

    def test_a_blank_line_is_not_counted_as_malformed(self, trail):
        """Trailing newlines are normal file structure, not corruption. Counting them would make
        `skipped_lines` alarming on a healthy trail and train operators to ignore it."""
        active = trail(0, events=[_event(1), "", "   ", _event(2)])
        assert reader.read(limit=10, path=active).skipped_lines == 0


class TestTC21TheReadWritesNothing:
    def test_mtime_size_and_inode_are_unchanged(self, trail):
        """NFR-4. `atime` is deliberately excluded: a read legitimately updates it, so asserting
        on it would fail for the wrong reason."""
        active = trail(50, rotated=2)
        before = reader.stat_snapshot(active)
        assert before, "nothing to observe"

        reader.read(limit=20, path=active)
        reader.read(reader.Filter(principal="user:alice"), limit=20, path=active)

        assert reader.stat_snapshot(active) == before

    def test_it_creates_no_files(self, trail, tmp_path):
        active = trail(20)
        before = {p.name for p in tmp_path.iterdir()}
        reader.read(limit=10, path=active)
        assert {p.name for p in tmp_path.iterdir()} == before


class TestTC22AnAbsentTrail:
    def test_no_file_is_an_empty_answer_not_an_error(self, tmp_path):
        """Every first boot is in this state. A reader that raised would make a fresh, healthy
        install look broken."""
        page = reader.read(limit=10, path=tmp_path / "audit.jsonl")
        assert page.events == []
        assert page.files == 0
        assert page.truncated is False
        assert page.oldest_held == ""


class TestTC18MemoryIsBounded:
    def test_peak_allocation_does_not_grow_with_the_trail(self, trail, tmp_path):
        """NFR-2, asserted as a *property* rather than one small number.

        `tracemalloc` measures Python allocations only, so a reader that let the OS page in the
        whole file could show a small heap and still be unbounded. Comparing two trail sizes is
        the mitigation: what matters is that peak does not scale with the file.
        """
        small = trail(500)
        tracemalloc.start()
        reader.read(limit=50, path=small)
        small_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        big = tmp_path / "audit.jsonl"
        big.write_text("\n".join(_event(i) for i in range(20_000)) + "\n", encoding="utf-8")
        tracemalloc.start()
        reader.read(limit=50, path=big)
        big_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        assert big_peak < 4 * 1024 * 1024, f"peak {big_peak / 1048576:.1f} MiB exceeds the 4 MiB bound"
        assert big_peak < small_peak * 3, (
            f"peak grew with the trail ({small_peak} -> {big_peak}) on a 40x larger file, so the "
            f"read is not bounded — it is merely small at this size"
        )


class TestTheScanCapContentsArePinned:
    def test_the_named_figures(self):
        """FR-5's cap is a security-relevant bound: raising it silently would let one request walk
        the whole 125 MiB trail, and lowering it would truncate answers that used to be complete."""
        assert reader.MAX_SCAN_EVENTS == 20_000
        assert reader.MAX_SCAN_BYTES == 8 * 1024 * 1024
        assert reader.DEFAULT_PAGE == 50
        assert reader.MAX_PAGE == 500

    def test_a_caller_cannot_exceed_the_page_ceiling(self, trail):
        assert len(reader.read(limit=10_000, path=trail(600)).events) <= reader.MAX_PAGE
