# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Time-window derivation, the comments store, the analytics cache, and remote cwd mapping.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Four modules that were among the least covered in the backend -- log_time_window at 12.8%,
comments_store at 18.0%, analytics_cache at 26.2%, stream_exec at 39.3% -- and all four are pure
enough to test directly, which is why they are together here rather than reached through HTTP.

`log_time_window` is the one that matters most. It answers "what time range did this run actually
occupy?", and the answer drives the Grafana window an operator reads results in. Its own docstring
records a real incident: reading the first 64 MB of a 973 MB log made the run's "end" whatever
happened 7% of the way in, so the window closed hours early and the panels looked empty. A wrong
window here is indistinguishable from a run that produced nothing.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics_cache  # noqa: E402
import comments_store  # noqa: E402
import log_time_window as ltw  # noqa: E402
import stream_exec  # noqa: E402


# --------------------------------------------------------------------------------------------
# log_time_window: zone resolution
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["UTC", "utc", "GMT", "z", "Z", "uct"])
def test_utc_spellings_all_resolve_to_utc(name):
    """The browser sends whatever the Charts TZ menu holds; these are all the same zone."""
    assert ltw._zoneinfo_for_log_parsing(name) == ZoneInfo("UTC")


def test_a_real_zone_is_honoured():
    assert ltw._zoneinfo_for_log_parsing("Europe/Lisbon") == ZoneInfo("Europe/Lisbon")


def test_an_unknown_zone_falls_back_rather_than_raising(monkeypatch):
    """A bad `display_timezone` from a URL must not take out the request."""
    monkeypatch.delenv("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ", raising=False)
    assert ltw._zoneinfo_for_log_parsing("Not/AZone") == ZoneInfo("UTC")


def test_the_env_default_applies_when_no_override(monkeypatch):
    monkeypatch.setenv("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ", "Asia/Tokyo")
    assert ltw._zoneinfo_for_log_parsing(None) == ZoneInfo("Asia/Tokyo")
    assert ltw._log_ts_zone() == ZoneInfo("Asia/Tokyo")


# --------------------------------------------------------------------------------------------
# log_time_window: timestamp parsing
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["2026-08-21T10:00:00", "2026-08-21 10:00:00", "2026-08-21T10:00:00.500"],
)
def test_naive_stamps_are_read_in_the_given_zone(raw):
    """NEMO logs carry no offset, so the zone is supplied rather than inferred."""
    got = ltw._parse_ts_unix(raw, ZoneInfo("UTC"))
    expected = datetime(2026, 8, 21, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    assert got == pytest.approx(expected, abs=1)


def test_the_zone_actually_changes_the_answer():
    """Guard against a zone argument that is accepted and ignored."""
    utc = ltw._parse_ts_unix("2026-08-21T10:00:00", ZoneInfo("UTC"))
    tokyo = ltw._parse_ts_unix("2026-08-21T10:00:00", ZoneInfo("Asia/Tokyo"))
    assert utc - tokyo == pytest.approx(9 * 3600)


def test_an_aware_stamp_keeps_its_own_offset():
    aware = ltw._parse_ts_unix("2026-08-21T10:00:00+02:00", ZoneInfo("UTC"))
    assert aware == datetime(2026, 8, 21, 8, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("raw", ["", "not-a-date", "2026-13-45T99:99:99", "21/08/2026"])
def test_unparseable_stamps_return_none(raw):
    assert ltw._parse_ts_unix(raw, ZoneInfo("UTC")) is None


# --------------------------------------------------------------------------------------------
# log_time_window: reading wall times out of a log
# --------------------------------------------------------------------------------------------


@pytest.fixture
def no_logscan(monkeypatch):
    """Force the file-reading path: with a scanner record present it returns early."""
    monkeypatch.setattr(ltw, "_logscan_record", lambda path: None, raising=True)


def test_wall_times_come_from_training_lines_only(tmp_path, no_logscan, monkeypatch):
    """Only lines that mark training progress count; other timestamped noise must not widen it."""
    log = tmp_path / "nemo.log"
    log.write_text(
        "2026-08-21T09:00:00 starting up, nothing to see\n"
        "2026-08-21T10:00:00 train_step_timing: 1.2\n"
        "2026-08-21T10:05:00 consumed_samples: 512\n"
        "2026-08-21T23:00:00 shutting down\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ltw, "_host_read_text", lambda p, max_bytes=None: log.read_text())
    times = ltw._llm_wall_times_from_log(log, "UTC")
    assert len(times) == 2
    span = max(times) - min(times)
    assert span == pytest.approx(300, abs=1), "should span 10:00 -> 10:05, not the whole file"


def test_the_scanner_record_wins_when_present(tmp_path, monkeypatch):
    """The whole-file first/last is preferred precisely because the fallback read is capped."""
    log = tmp_path / "nemo.log"
    log.write_text("irrelevant\n", encoding="utf-8")
    monkeypatch.setattr(
        ltw, "_logscan_record",
        lambda path: {"wall": {"first": "2026-08-21T00:00:00", "last": "2026-08-21T12:00:00"}},
    )
    times = ltw._llm_wall_times_from_log(log, "UTC")
    assert max(times) - min(times) == pytest.approx(12 * 3600, abs=1)


def test_an_unreadable_log_yields_no_times(tmp_path, no_logscan, monkeypatch):
    monkeypatch.setattr(ltw, "_host_read_text", lambda p, max_bytes=None: None)
    assert ltw._llm_wall_times_from_log(tmp_path / "gone.log", "UTC") == []


# --------------------------------------------------------------------------------------------
# log_time_window: the fallback, and the composed window
# --------------------------------------------------------------------------------------------


def test_fallback_without_step_data_is_a_two_hour_window(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("x", encoding="utf-8")
    lo, hi, note = ltw._fallback_window_from_steps_and_mtime(log, None)
    assert note == "mtime_2h"
    assert hi - lo == pytest.approx(7200, abs=1)


def test_fallback_estimates_the_span_from_step_timings(tmp_path):
    """With iterations and step times the window can be derived instead of guessed."""
    log = tmp_path / "run.log"
    log.write_text("x", encoding="utf-8")
    row = {"step_series": [{"iteration": i, "step_time": 2.0} for i in range(1, 101)]}
    lo, hi, note = ltw._fallback_window_from_steps_and_mtime(log, row)
    assert note == "mtime_step_estimate"
    # 100 iterations * 2s * 1.15 headroom
    assert hi - lo == pytest.approx(230, abs=5)


def test_a_missing_file_has_no_fallback_window(tmp_path):
    assert ltw._fallback_window_from_steps_and_mtime(tmp_path / "nope.log", None) is None


def test_the_window_prefers_log_timestamps_and_pads_them(tmp_path, no_logscan, monkeypatch):
    log = tmp_path / "nemo.log"
    log.write_text("2026-08-21T10:00:00 train_step_timing: 1\n", encoding="utf-8")
    monkeypatch.setattr(ltw, "_host_read_text", lambda p, max_bytes=None: log.read_text())
    monkeypatch.setenv("RACK_WEB_GRAFANA_TIME_PAD_SEC", "60")
    lo, hi, note = ltw._compute_llm_time_window_unix(log, None, "UTC")
    assert note == "log_timestamps"
    assert hi - lo == pytest.approx(120, abs=1), "one stamp, padded both sides"


def test_the_window_falls_back_when_the_log_has_no_training_lines(
    tmp_path, no_logscan, monkeypatch
):
    log = tmp_path / "nemo.log"
    log.write_text("nothing that looks like training\n", encoding="utf-8")
    monkeypatch.setattr(ltw, "_host_read_text", lambda p, max_bytes=None: log.read_text())
    _lo, _hi, note = ltw._compute_llm_time_window_unix(log, None, "UTC")
    assert note == "mtime_2h"


# --------------------------------------------------------------------------------------------
# comments_store
# --------------------------------------------------------------------------------------------


@pytest.fixture
def comments_file(tmp_path, monkeypatch):
    path = tmp_path / "rack_comments.json"
    monkeypatch.setenv("RACK_COMMENTS_FILE", str(path))
    return path


@pytest.mark.parametrize(
    "raw",
    [
        "nemo-run.log",
        "nccl_allreduce.out",
        "MULTIRACK-NEMO-2026.txt",
    ],
)
def test_multirack_basenames_are_accepted(raw):
    assert comments_store._normalize_multirack_comment_basename(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "../nemo.log",        # traversal
        "dir/nemo.log",       # a path, not a basename
        "dir\\nemo.log",
        "plain.log",          # neither nemo nor nccl
        "",
        "   ",
        None,
        123,
        "nemo" + "x" * 600,   # over the length cap
    ],
)
def test_implausible_multirack_basenames_are_refused(raw):
    assert comments_store._normalize_multirack_comment_basename(raw) is None


def test_storage_key_is_lowercased_and_prefixed():
    assert comments_store._multirack_comment_storage_key("NEMO-Run.log") == "mr:nemo-run.log"
    assert comments_store._multirack_comment_storage_key("bad.log") is None


def test_stored_comments_split_into_racks_and_multirack_logs():
    racks, multirack = comments_store._split_stored_comments(
        {
            "p5-r01": "rack note",
            "mr:nemo-run.log": "log note",
            "not a rack id": "dropped",
            "p5-r02": "   ",       # blank, dropped
            "p5-r03": 17,          # not a string, dropped
        }
    )
    assert racks == {"p5-r01": "rack note"}
    assert multirack == {"mr:nemo-run.log": "log note"}


def test_a_non_dict_document_splits_to_nothing():
    assert comments_store._split_stored_comments(["not", "a", "dict"]) == ({}, {})


def test_setting_then_reading_a_rack_comment(comments_file):
    comments_store.set_comment_for_rack("p5-r01", "drained for cabling")
    assert comments_store.read_comments()["p5-r01"] == "drained for cabling"
    assert json.loads(comments_file.read_text())  # actually persisted


def test_clearing_a_rack_comment_removes_the_key(comments_file):
    comments_store.set_comment_for_rack("p5-r01", "temporary")
    comments_store.set_comment_for_rack("p5-r01", None)
    assert "p5-r01" not in comments_store.read_comments()


def test_a_rack_comment_does_not_disturb_a_multirack_one(comments_file):
    """The two live in one file under different key shapes; each write preserves the other."""
    comments_store.set_comment_for_multirack_log("nemo-run.log", "campaign note")
    comments_store.set_comment_for_rack("p5-r01", "rack note")
    after = comments_store.read_comments()
    assert after.get("p5-r01") == "rack note"
    assert after.get("mr:nemo-run.log") == "campaign note"


def test_reading_with_no_file_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_COMMENTS_FILE", str(tmp_path / "absent" / "x.json"))
    assert comments_store.read_comments() == {}


def test_an_over_long_comment_is_truncated_not_rejected(comments_file, monkeypatch):
    monkeypatch.setenv("RACK_COMMENT_MAX_LEN", "10")
    comments_store.set_comment_for_rack("p5-r01", "x" * 500)
    assert len(comments_store.read_comments()["p5-r01"]) <= 10


# --------------------------------------------------------------------------------------------
# analytics_cache
# --------------------------------------------------------------------------------------------


def test_the_row_fingerprint_ignores_ordering():
    """Same fleet, different row order, same key -- or the cache never hits."""
    a = [{"rack": "p5-r01", "nemo": "ok"}, {"rack": "p5-r02", "nemo": "fail"}]
    b = list(reversed(a))
    assert analytics_cache._analytics_rows_fingerprint(a) == \
        analytics_cache._analytics_rows_fingerprint(b)


def test_the_row_fingerprint_changes_with_content():
    a = [{"rack": "p5-r01", "nemo": "ok"}]
    b = [{"rack": "p5-r01", "nemo": "fail"}]
    assert analytics_cache._analytics_rows_fingerprint(a) != \
        analytics_cache._analytics_rows_fingerprint(b)


@pytest.mark.parametrize("junk", [None, "rows", 42, {"not": "a list"}])
def test_a_non_list_fingerprints_as_empty(junk):
    empty = analytics_cache._analytics_rows_fingerprint([])
    assert analytics_cache._analytics_rows_fingerprint(junk) == empty


def test_cache_put_then_get_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(
        analytics_cache, "_ANALYTICS_AGG_CACHE_PATH", tmp_path / "agg.json", raising=True
    )
    payload = {
        "analyzed_at": "2026-08-21T00:00:00Z",
        "rows_fingerprint": "abc",
        "single_rack": {"p5-r01": 1},
        "multirack": {},
    }
    analytics_cache._analytics_aggregate_cache_put("/logs/dir", payload)
    got = analytics_cache._analytics_aggregate_cache_get("/logs/dir")
    assert got is not None
    assert got["rows_fingerprint"] == "abc"


def test_a_miss_and_a_corrupt_file_both_return_none(tmp_path, monkeypatch):
    path = tmp_path / "agg.json"
    monkeypatch.setattr(analytics_cache, "_ANALYTICS_AGG_CACHE_PATH", path, raising=True)
    assert analytics_cache._analytics_aggregate_cache_get("/never/written") is None
    path.write_text("{ not json", encoding="utf-8")
    assert analytics_cache._analytics_aggregate_cache_get("/logs/dir") is None


# --------------------------------------------------------------------------------------------
# stream_exec: mapping a local cwd onto the cluster
# --------------------------------------------------------------------------------------------


def test_repo_root_maps_onto_the_remote_base(monkeypatch):
    monkeypatch.setattr(stream_exec, "_REMOTE_BASE", "/home/cmsupport/nvis", raising=True)
    assert stream_exec._translate_cwd_for_remote(str(stream_exec.REPO_ROOT)) == \
        "/home/cmsupport/nvis"


def test_a_path_under_the_repo_keeps_its_tail(monkeypatch):
    monkeypatch.setattr(stream_exec, "_REMOTE_BASE", "/home/cmsupport/nvis", raising=True)
    got = stream_exec._translate_cwd_for_remote(str(stream_exec.REPO_ROOT / "bin" / "scripts"))
    assert got == "/home/cmsupport/nvis/bin/scripts"


def test_a_path_outside_the_repo_is_left_alone(monkeypatch):
    """Callers that already pre-translated, or point at a remote log dir, must pass through."""
    monkeypatch.setattr(stream_exec, "_REMOTE_BASE", "/home/cmsupport/nvis", raising=True)
    assert stream_exec._translate_cwd_for_remote("/raid/logs") == "/raid/logs"


def test_without_a_remote_base_repo_root_becomes_the_remote_home(monkeypatch):
    monkeypatch.setattr(stream_exec, "_REMOTE_BASE", "", raising=True)
    assert stream_exec._translate_cwd_for_remote(str(stream_exec.REPO_ROOT)) is None
    assert stream_exec._translate_cwd_for_remote("/raid/logs") == "/raid/logs"


class _FakeStdout:
    """A pipe that hands over its content once, then signals EOF like a real one."""

    def __init__(self, payload: bytes):
        self._chunks = [payload] if payload else []
        self.closed = False

    def read(self, _n: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    def __init__(self, payload=b"hello\n", returncode=0, never_exits=False):
        self.stdout = _FakeStdout(payload)
        self.returncode = returncode
        self._never_exits = never_exits
        self.killed = False

    def poll(self):
        return None if self._never_exits and not self.killed else self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.fixture
def local_stream(monkeypatch):
    """Run the generator locally, with nothing spawned: the transport is stubbed out."""
    monkeypatch.setattr(stream_exec.host_exec, "exec_kind", lambda: "local", raising=True)
    monkeypatch.setattr(stream_exec.host_exec, "which", lambda _name: None, raising=True)
    monkeypatch.setattr(
        stream_exec.host_exec, "wrap_for_host",
        lambda cmd, **k: list(cmd), raising=True,
    )


def _drain(gen) -> str:
    return b"".join(gen).decode("utf-8")


def test_the_stream_opens_with_the_command_and_cwd(local_stream, monkeypatch):
    """The header is what tells an operator watching the pane WHAT is running."""
    monkeypatch.setattr(stream_exec.subprocess, "Popen", lambda *a, **k: _FakeProc())
    out = _drain(
        stream_exec._merged_stdout_stream_generator(
            ["echo", "hello"], "/tmp", "echo hello", 30,
            linebuffer_env_var="RACK_TEST_LINEBUFFER",
        )
    )
    assert out.startswith("# echo hello\n# cwd: /tmp\n")


def test_the_stream_carries_output_then_the_exit_code(local_stream, monkeypatch):
    monkeypatch.setattr(
        stream_exec.subprocess, "Popen", lambda *a, **k: _FakeProc(b"line one\nline two\n", 0)
    )
    out = _drain(
        stream_exec._merged_stdout_stream_generator(
            ["run"], "/tmp", "run", 30, linebuffer_env_var="RACK_TEST_LINEBUFFER",
        )
    )
    assert "line one" in out and "line two" in out
    assert out.rstrip().endswith("--- exit 0 ---")


def test_a_failing_command_reports_its_code_rather_than_raising(local_stream, monkeypatch):
    """The pane must show the failure; a raised exception would just truncate the stream."""
    monkeypatch.setattr(stream_exec.subprocess, "Popen", lambda *a, **k: _FakeProc(b"boom\n", 3))
    out = _drain(
        stream_exec._merged_stdout_stream_generator(
            ["run"], "/tmp", "run", 30, linebuffer_env_var="RACK_TEST_LINEBUFFER",
        )
    )
    assert "--- exit 3 ---" in out


def test_a_wedged_command_is_killed_and_said_so(local_stream, monkeypatch):
    """timeout_sec=0 takes the deadline branch immediately, without waiting for one."""
    proc = _FakeProc(b"", 0, never_exits=True)
    monkeypatch.setattr(stream_exec.subprocess, "Popen", lambda *a, **k: proc)
    out = _drain(
        stream_exec._merged_stdout_stream_generator(
            ["sleep", "forever"], "/tmp", "sleep forever", 0,
            linebuffer_env_var="RACK_TEST_LINEBUFFER",
        )
    )
    assert "*** timed out ***" in out
    assert proc.killed, "a timed-out command must actually be killed, not just reported"


def test_line_buffering_prefixes_stdbuf_when_available(local_stream, monkeypatch):
    """Without this the UI gets block-buffered clumps instead of a live log."""
    seen = {}
    monkeypatch.setattr(stream_exec.host_exec, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("RACK_TEST_LINEBUFFER", "1")

    def _capture(cmd, **k):
        seen["cmd"] = list(cmd)
        return _FakeProc(b"")

    monkeypatch.setattr(stream_exec.subprocess, "Popen", _capture)
    monkeypatch.setattr(stream_exec.host_exec, "wrap_for_host", lambda cmd, **k: list(cmd))
    _drain(
        stream_exec._merged_stdout_stream_generator(
            ["run"], "/tmp", "run", 30, linebuffer_env_var="RACK_TEST_LINEBUFFER",
        )
    )
    assert seen["cmd"][:3] == ["/usr/bin/stdbuf", "-oL", "-eL"]


def test_the_stream_headers_forbid_caching_and_proxy_buffering():
    """A streamed job log that gets cached or buffered shows a finished run as still running.

    `no-store` rather than `no-cache`, which is the stronger of the two: no-cache permits storing
    the response and revalidating, no-store forbids keeping it at all. `X-Accel-Buffering: no` is
    the other half -- nginx will otherwise hold a streamed body until it has enough to forward,
    which stalls the live log an operator is watching even with caching off.
    """
    headers = stream_exec._STREAM_NO_CACHE_HEADERS
    assert headers.get("Cache-Control") == "no-store"
    assert headers.get("X-Accel-Buffering") == "no"


def test_time_is_available_to_the_stream_module():
    """Regression for the hoisted stdlib import: `time` must be bound at module level."""
    assert isinstance(time.time(), float)
    assert hasattr(stream_exec, "REPO_ROOT")
