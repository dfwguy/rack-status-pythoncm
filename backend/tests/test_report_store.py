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
"""Saved reports must outlive the tab that asked for them.

A report costs minutes of fleet-wide SSH. Before this store the finished document lived only in
the job table and was freed on collection, so leaving Analytics mid-sweep discarded the whole
thing. These tests pin the properties that make walking away safe: the bytes survive, listing
never advertises a download that cannot succeed, and an id from a URL cannot escape the directory.
"""
from __future__ import annotations

import json

import pytest

import report_store


@pytest.fixture(autouse=True)
def isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIS_REPORTS_DIR", str(tmp_path / "reports"))
    yield tmp_path / "reports"


class TestSaveAndLoad:
    def test_a_saved_report_comes_back_byte_identical(self):
        # A PDF must survive exactly; a single mangled byte makes the file unopenable.
        blob = b"%PDF-1.4\n\x00\x01\x02 binary \xff\xfe tail"
        entry = report_store.save(kind="rack-status", fmt="pdf", filename="cluster", data=blob)
        loaded = report_store.load(entry["id"])
        assert loaded is not None
        assert loaded[0] == blob

    def test_text_is_accepted_and_stored_as_utf8(self):
        entry = report_store.save(kind="rack-status", fmt="csv", filename="c", data="a,b\n1,2\n")
        assert report_store.load(entry["id"])[0] == b"a,b\n1,2\n"

    def test_metadata_records_what_the_list_needs_to_show(self):
        entry = report_store.save(
            kind="nscale", fmt="json", filename="report", data="{}",
            meta={"cluster": "NscaleTX-GB300", "nodes": 2013},
        )
        assert entry["kind"] == "nscale"
        assert entry["format"] == "json"
        assert entry["size"] == 2
        assert entry["cluster"] == "NscaleTX-GB300"
        assert entry["nodes"] == 2013
        assert entry["created"] > 0

    def test_the_filename_gets_the_right_extension(self):
        entry = report_store.save(kind="rack-status", fmt="csv", filename="cluster-2026", data="x")
        assert entry["filename"] == "cluster-2026.csv"

    def test_an_unsupported_format_is_refused_rather_than_written(self):
        with pytest.raises(ValueError):
            report_store.save(kind="x", fmt="html", filename="n", data="<html>")
        assert report_store.list_entries() == []

    def test_two_reports_never_collide(self):
        a = report_store.save(kind="rack-status", fmt="csv", filename="same", data="a")
        b = report_store.save(kind="rack-status", fmt="csv", filename="same", data="b")
        assert a["id"] != b["id"]
        assert report_store.load(a["id"])[0] == b"a"
        assert report_store.load(b["id"])[0] == b"b"


class TestJsonPayloadIsNotClobberedByItsOwnMetadata:
    """The sidecar must never share a path with a payload.

    A JSON report's payload is ``<id>.json``, so naming the sidecar ``<id>.json`` makes them the
    same file and the metadata overwrites the document. That silently corrupts the Nscale customer
    deliverable specifically, because it is the one report that is JSON-only -- it would download
    as its own metadata and fail the schema it exists to satisfy.
    """

    def test_a_json_report_downloads_as_the_document_not_the_metadata(self):
        document = json.dumps({"report_metadata": {"report_id": "NSC-1"}, "test_results": []})
        entry = report_store.save(
            kind="nscale", fmt="json", filename="nscale", data=document,
            meta={"cluster": "NscaleTX-GB300"},
        )
        loaded = report_store.load(entry["id"])
        assert loaded is not None
        assert json.loads(loaded[0].decode()) == json.loads(document)
        # The giveaway for the collision: the payload would parse as the sidecar instead.
        assert "report_metadata" in loaded[0].decode()
        assert "cluster" not in json.loads(loaded[0].decode())

    def test_the_payload_and_sidecar_are_distinct_files(self, isolated_dir):
        entry = report_store.save(kind="nscale", fmt="json", filename="n", data='{"real":1}')
        payload = isolated_dir / f"{entry['id']}.json"
        sidecar = isolated_dir / f"{entry['id']}.meta.json"
        assert payload.is_file() and sidecar.is_file()
        assert payload.read_bytes() != sidecar.read_bytes()

    def test_a_json_report_survives_a_later_save(self):
        """Writing another report must not disturb an existing JSON payload."""
        first = report_store.save(kind="nscale", fmt="json", filename="a", data='{"keep":true}')
        report_store.save(kind="rack-status", fmt="json", filename="b", data='{"other":true}')
        assert json.loads(report_store.load(first["id"])[0]) == {"keep": True}


class TestFilenameSafety:
    @pytest.mark.parametrize("raw", [
        "../../etc/passwd",
        'quote"; rm -rf /',
        "with spaces and /slashes/",
        "",
    ])
    def test_a_hostile_or_empty_name_becomes_a_safe_one(self, raw):
        # The stem comes from report metadata, which includes an operator-typed cluster name.
        entry = report_store.save(kind="k", fmt="csv", filename=raw, data="x")
        name = entry["filename"]
        assert "/" not in name and ".." not in name and '"' not in name and " " not in name
        assert name.endswith(".csv")

    def test_the_payload_never_lands_outside_the_reports_directory(self, isolated_dir):
        report_store.save(kind="k", fmt="csv", filename="../../escape", data="x")
        # Every file created must be a direct child of the reports dir.
        assert {p.parent for p in isolated_dir.iterdir()} == {isolated_dir}


class TestListing:
    def test_newest_report_is_listed_first(self):
        ids = [report_store.save(kind="k", fmt="csv", filename=f"r{i}", data="x")["id"]
               for i in range(3)]
        assert [e["id"] for e in report_store.list_entries()] == list(reversed(ids))

    def test_an_empty_store_lists_nothing_rather_than_failing(self):
        assert report_store.list_entries() == []

    def test_a_sidecar_without_its_payload_is_not_offered(self, isolated_dir):
        entry = report_store.save(kind="k", fmt="csv", filename="r", data="x")
        # How an interrupted save looks. Listing it would offer a download that cannot succeed.
        (isolated_dir / f"{entry['id']}.csv").unlink()
        assert report_store.list_entries() == []

    def test_a_corrupt_sidecar_is_skipped_without_taking_the_list_down(self, isolated_dir):
        good = report_store.save(kind="k", fmt="csv", filename="good", data="x")
        (isolated_dir / "deadbeef.meta.json").write_text("{not json", encoding="utf-8")
        assert [e["id"] for e in report_store.list_entries()] == [good["id"]]

    def test_listing_does_not_read_the_payloads(self, isolated_dir):
        # Enumerating must stay cheap: a 2000-node JSON report is several MB, and the list is
        # rendered every time the Reports page opens.
        big = report_store.save(kind="k", fmt="json", filename="big", data="y" * 200_000)
        # The size is read from the sidecar, so listing never opens the payload at all.
        entries = report_store.list_entries()
        assert entries[0]["size"] == 200_000
        assert (isolated_dir / f"{big['id']}.json").stat().st_size == 200_000


class TestLoadRejectsBadIds:
    @pytest.mark.parametrize("bad", [
        "../../../etc/passwd",
        "../store/catalog",
        "not-a-hex-id",
        "",
        "a" * 31,
        "g" * 32,
    ])
    def test_an_id_that_is_not_one_of_ours_returns_nothing(self, bad):
        assert report_store.load(bad) is None
        assert report_store.delete(bad) is False

    def test_an_unknown_but_well_formed_id_is_simply_missing(self):
        assert report_store.load("0" * 32) is None


class TestDelete:
    def test_deleting_removes_the_row_and_the_bytes(self, isolated_dir):
        entry = report_store.save(kind="k", fmt="csv", filename="r", data="x")
        assert report_store.delete(entry["id"]) is True
        assert report_store.list_entries() == []
        assert report_store.load(entry["id"]) is None
        assert list(isolated_dir.glob("*")) == []

    def test_deleting_twice_is_not_an_error(self):
        entry = report_store.save(kind="k", fmt="csv", filename="r", data="x")
        assert report_store.delete(entry["id"]) is True
        assert report_store.delete(entry["id"]) is False

    def test_deleting_one_leaves_the_others(self):
        a = report_store.save(kind="k", fmt="csv", filename="a", data="1")
        b = report_store.save(kind="k", fmt="csv", filename="b", data="2")
        report_store.delete(a["id"])
        assert [e["id"] for e in report_store.list_entries()] == [b["id"]]


class TestRetention:
    def test_the_directory_does_not_grow_without_bound(self, monkeypatch):
        monkeypatch.setattr(report_store, "MAX_REPORTS", 3)
        ids = [report_store.save(kind="k", fmt="csv", filename=f"r{i}", data="x")["id"]
               for i in range(6)]
        kept = [e["id"] for e in report_store.list_entries()]
        assert len(kept) == 3
        # The newest survive; the oldest are the ones dropped.
        assert kept == list(reversed(ids[-3:]))

    def test_pruning_removes_payloads_too_not_just_sidecars(self, monkeypatch, isolated_dir):
        monkeypatch.setattr(report_store, "MAX_REPORTS", 2)
        for i in range(5):
            report_store.save(kind="k", fmt="csv", filename=f"r{i}", data="x")
        # Two sidecars plus two payloads and nothing left behind.
        assert len(list(isolated_dir.glob("*.csv"))) == 2
        assert len(list(isolated_dir.glob("*.meta.json"))) == 2


class TestDurability:
    def test_a_saved_report_is_readable_by_a_fresh_process(self, isolated_dir):
        """No in-memory index: a restart must not lose the reports on disk."""
        entry = report_store.save(kind="k", fmt="csv", filename="r", data="persisted")
        # Whatever a reload would do, the state lives entirely in these two files.
        sidecar = json.loads((isolated_dir / f"{entry['id']}.meta.json").read_text())
        assert sidecar["id"] == entry["id"]
        assert (isolated_dir / f"{entry['id']}.csv").read_bytes() == b"persisted"

    def test_no_temporary_files_are_left_behind(self, isolated_dir):
        report_store.save(kind="k", fmt="pdf", filename="r", data=b"%PDF-")
        assert list(isolated_dir.glob("*.tmp")) == []
