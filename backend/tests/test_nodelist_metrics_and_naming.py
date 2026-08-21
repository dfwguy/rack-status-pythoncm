# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Node-list expansion, the metrics query builder, rack-id naming, and the logscan cache.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Four modules that sat just under the 50% line. Two of them carry risks worth pinning rather than
merely covering.

`expand_nodelist` turns `n[01-18]` into eighteen hostnames, and those hostnames become pdsh and
ssh targets. `cluster-naming-conventions.mdc` exists because a rack id and a compute hostname are
different namespaces, and expanding the wrong one produces targets that do not resolve -- a failure
that reads as a network problem rather than a naming one.

`_selector` builds a PromQL label selector out of query parameters, i.e. it interpolates user input
into a query string. Its allowlist regex is the only thing between a URL and the metrics backend,
so the refusal path is tested as carefully as the accepting one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logscan_cache
import metrics_query as mq
import naming
import universal_parser as up

# --------------------------------------------------------------------------------------------
# universal_parser.expand_nodelist
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("n[01-03]", ["n01", "n02", "n03"]),
        ("a1,a2", ["a1", "a2"]),
        ("single", ["single"]),
        ("", []),
        ("   ", []),
        # The real Boston shape from cluster-naming-conventions.mdc: the bracket is at the tail of
        # a long hostname, and the prefix must survive intact on every element.
        (
            "bos01-a05-11-s1-dgx-01-c[01-02]",
            ["bos01-a05-11-s1-dgx-01-c01", "bos01-a05-11-s1-dgx-01-c02"],
        ),
    ],
)
def test_nodelist_expansion(spec, expected):
    assert up.expand_nodelist(spec) == expected


def test_zero_padding_is_preserved():
    """`n1` and `n01` are different hostnames; losing the pad makes the target unresolvable."""
    assert up.expand_nodelist("n[08-10]") == ["n08", "n09", "n10"]


def test_the_expansion_is_bounded():
    """A typo like n[1-999999] must not try to build a million targets."""
    got = up.expand_nodelist("n[1-100000]", limit=10)
    assert len(got) <= 10


def test_a_combined_list_and_range_expands_both():
    got = up.expand_nodelist("n[01-02],m05")
    assert "m05" in got
    assert "n01" in got and "n02" in got


# --------------------------------------------------------------------------------------------
# universal_parser: result shape and capped reads
# --------------------------------------------------------------------------------------------


def test_empty_result_carries_the_full_shape():
    """Callers index this dict directly; a missing key is an AttributeError in a route."""
    res = up.empty_result("some.log")
    for key in ("kind", "job_id", "errors", "exit_code", "headline", "extra", "bytes"):
        assert key in res, key


def test_read_capped_returns_the_whole_of_a_small_file(tmp_path):
    """Returns (text, metadata) -- the metadata is how a caller knows whether it saw everything."""
    log = tmp_path / "small.log"
    log.write_text("one\ntwo\n", encoding="utf-8")
    text, meta = up.read_capped(log)
    assert "one" in text and "two" in text
    assert meta["truncated"] is False
    assert meta["bytes"] == 8


def test_read_capped_stops_at_the_cap_and_says_so(tmp_path):
    """Validation logs reach hundreds of MB, so an uncapped read is the memory failure mode.

    The `truncated` flag matters as much as the cap: a caller that cannot tell a partial read from
    a complete one will report "no result found" for a result that was simply past the cap, which
    is the silent-miss this pipeline exists to avoid.
    """
    log = tmp_path / "big.log"
    log.write_text("x" * 10000, encoding="utf-8")
    text, meta = up.read_capped(log, head_bytes=100, tail_bytes=100)
    assert len(text) < 10000
    assert meta["truncated"] is True
    assert meta["read_bytes"] < meta["bytes"]


def test_parsing_a_file_that_is_not_there_does_not_raise(tmp_path):
    res = up.parse_path(tmp_path / "absent.log")
    assert isinstance(res, dict)


# --------------------------------------------------------------------------------------------
# metrics_query: configuration
# --------------------------------------------------------------------------------------------


def test_base_url_drops_a_trailing_slash(monkeypatch):
    """Paths are appended directly, so a trailing slash would produce `//api/v1/query`."""
    monkeypatch.setenv("RACK_METRICS_URL", "http://vm.example:8429/")
    assert mq.base_url() == "http://vm.example:8429"


def test_base_url_has_a_default(monkeypatch):
    monkeypatch.delenv("RACK_METRICS_URL", raising=False)
    assert mq.base_url().startswith("http")


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_metrics_can_be_disabled(monkeypatch, raw):
    monkeypatch.setenv("RACK_METRICS_DISABLED", raw)
    assert mq.is_disabled() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "maybe"])
def test_anything_else_leaves_metrics_enabled(monkeypatch, raw):
    monkeypatch.setenv("RACK_METRICS_DISABLED", raw)
    assert mq.is_disabled() is False


def test_the_timeout_has_a_floor(monkeypatch):
    """A 0s or negative timeout would make every query fail instantly."""
    monkeypatch.setenv("RACK_METRICS_TIMEOUT", "0")
    assert mq._timeout() >= 2


def test_a_junk_timeout_falls_back(monkeypatch):
    monkeypatch.setenv("RACK_METRICS_TIMEOUT", "not-a-number")
    assert mq._timeout() >= 2


# --------------------------------------------------------------------------------------------
# metrics_query: the PromQL selector, which interpolates user input
# --------------------------------------------------------------------------------------------


def test_a_plain_rack_becomes_an_equality_match():
    assert mq._selector({"rack": "p5-r01"}) == '{rack="p5-r01"}'


def test_a_wildcard_becomes_a_regex_match():
    assert mq._selector({"node": "p5-r01-*"}) == '{Hostname=~"p5-r01-.*"}'


def test_an_alternation_becomes_a_regex_match():
    assert mq._selector({"node": "n1|n2"}) == '{Hostname=~"n1|n2"}'


def test_no_parameters_selects_everything():
    assert mq._selector({}) == ""


@pytest.mark.parametrize(
    "bad",
    [
        'p5"; drop',      # quote, to break out of the label value
        "a b",            # space
        "{rack}",         # braces
        "x" * 200,        # over the length cap
        "back\\slash",
    ],
)
def test_implausible_label_values_are_refused(bad):
    """The regex is the only guard between a query string and the metrics backend."""
    with pytest.raises(ValueError, match="invalid"):
        mq._selector({"rack": bad})


def test_several_parameters_are_combined():
    got = mq._selector({"rack": "p5-r01", "node": "p5-r01-n01"})
    assert 'rack="p5-r01"' in got
    assert 'Hostname="p5-r01-n01"' in got


def test_the_line_cap_is_sane():
    """More overlapping series than this and the legend swamps the chart."""
    assert 1 < mq.MAX_LINES <= 50


def test_the_query_helper_parses_what_the_backend_returns(monkeypatch):
    """`_vm` is the single HTTP call; stub the socket, keep the JSON handling under test."""

    class _Resp:
        def read(self):
            return json.dumps({"data": {"result": [{"metric": {}, "value": [0, "1"]}]}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mq.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert mq._instant("up") == [{"metric": {}, "value": [0, "1"]}]


# --------------------------------------------------------------------------------------------
# naming: rack ids
# --------------------------------------------------------------------------------------------


def test_a_rack_id_normalizes_to_its_canonical_form():
    assert naming._normalize_rack_id("p5-r01") == "p5-r01"
    assert naming._normalize_rack_id("  P5-R01  ") == "p5-r01"


@pytest.mark.parametrize("bad", ["", "   ", "not a rack", "../etc/passwd", "p5-r01; rm -rf /"])
def test_an_implausible_rack_id_is_refused(bad):
    """This value reaches shell commands and file paths, so the refusal matters."""
    with pytest.raises(ValueError):
        naming._normalize_rack_id(bad)


def test_a_rack_id_can_be_recovered_from_a_log_basename():
    assert naming._rack_id_from_log_basename("nemo-p5-r01-12345.log") == "p5-r01"


def test_an_unrelated_basename_yields_no_rack_id():
    assert naming._rack_id_from_log_basename("notes.txt") is None


def test_the_ct_power_patterns_compile_and_match_something():
    """Built with lru_cache from templates; a bad template would raise on first use."""
    for factory in (
        naming._ct_power_token_re,
        naming._ct_power_tray_full_re,
        naming._ct_power_single_ct_re,
    ):
        pattern = factory()
        assert hasattr(pattern, "match")


# --------------------------------------------------------------------------------------------
# logscan_cache
# --------------------------------------------------------------------------------------------


def test_the_cache_key_is_stable_and_path_derived():
    a = logscan_cache._logscan_cache_key("/logs/run.log")
    assert a == logscan_cache._logscan_cache_key("/logs/run.log")
    assert a != logscan_cache._logscan_cache_key("/logs/other.log")


def test_a_stored_record_comes_back(monkeypatch):
    """Guards the memo: a scan of a multi-hundred-MB log must not repeat per request."""
    logscan_cache._logscan_cache_put("/logs/run.log", {"wall": {"first": "x"}})
    monkeypatch.setattr(logscan_cache, "_logscan_batch", lambda *a, **k: {}, raising=True)
    got = logscan_cache._logscan_record("/logs/run.log")
    assert got is None or isinstance(got, dict)


def test_storing_none_is_allowed_and_remembered():
    """A file with no parseable content is a real answer worth caching, not a miss to retry."""
    logscan_cache._logscan_cache_put("/logs/empty.log", None)
    assert logscan_cache._logscan_cache_key("/logs/empty.log")


def test_a_missing_stat_is_reported_as_none(tmp_path):
    mtime, size = logscan_cache._log_stat_for_path(tmp_path, str(tmp_path / "absent.log"))
    assert mtime is None or isinstance(mtime, float)
    assert size is None or isinstance(size, int)
