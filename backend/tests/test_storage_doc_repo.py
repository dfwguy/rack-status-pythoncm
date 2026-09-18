# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The SQLite document backend, and the archive that now sits on it.

Two things are worth pinning here. The backends must be **interchangeable**, because the
point of `Repo` is that a caller does not know which one it has -- so the protocol tests run
against both. And the archive's monotonic guard must survive having moved from a hand-written
SQL statement into the backend: a stale snapshot dragging a finished activity back to running
is a silent corruption of the acceptance record, which is the one thing that store exists to
prevent. See `storage-engines.mdc`.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from catalog.store import TABLE_PLATFORMS, SqliteDocRepo, TinyDBRepo


@pytest.fixture(params=["sqlite", "tinydb"])
def repo(request, tmp_path):
    """Both backends, same tests. A divergence here is a bug in one of them."""
    if request.param == "sqlite":
        return SqliteDocRepo(tmp_path / "docs.sqlite3")
    return TinyDBRepo(tmp_path / "catalog.json")


class TestRepoProtocolParity:
    """Every `Repo` method, against every backend."""

    def test_upsert_then_get_round_trips(self, repo):
        repo.upsert(TABLE_PLATFORMS, {"id": "p1", "name": "One", "nested": {"a": [1, 2]}})
        assert repo.get(TABLE_PLATFORMS, "p1") == {
            "id": "p1",
            "name": "One",
            "nested": {"a": [1, 2]},
        }

    def test_upsert_replaces_rather_than_merges(self, repo):
        repo.upsert(TABLE_PLATFORMS, {"id": "p1", "name": "One", "gone": True})
        repo.upsert(TABLE_PLATFORMS, {"id": "p1", "name": "Two"})
        assert repo.get(TABLE_PLATFORMS, "p1") == {"id": "p1", "name": "Two"}

    def test_a_document_without_an_id_is_refused(self, repo):
        with pytest.raises(ValueError, match="non-empty 'id'"):
            repo.upsert(TABLE_PLATFORMS, {"name": "no id"})

    def test_get_of_an_absent_or_blank_id_is_none(self, repo):
        assert repo.get(TABLE_PLATFORMS, "nope") is None
        assert repo.get(TABLE_PLATFORMS, "") is None

    def test_list_returns_every_document(self, repo):
        for i in range(3):
            repo.upsert(TABLE_PLATFORMS, {"id": f"p{i}"})
        assert sorted(d["id"] for d in repo.list(TABLE_PLATFORMS)) == ["p0", "p1", "p2"]

    def test_list_of_an_untouched_table_is_empty_not_an_error(self, repo):
        assert repo.list("tools") == []

    def test_bulk_upsert_counts_written_and_skips_idless(self, repo):
        n = repo.bulk_upsert(
            TABLE_PLATFORMS, [{"id": "a"}, {"id": ""}, {"no": "id"}, {"id": "b"}]
        )
        assert n == 2
        assert sorted(d["id"] for d in repo.list(TABLE_PLATFORMS)) == ["a", "b"]

    def test_bulk_upsert_of_nothing_is_zero(self, repo):
        assert repo.bulk_upsert(TABLE_PLATFORMS, []) == 0

    def test_delete_reports_whether_it_removed_anything(self, repo):
        repo.upsert(TABLE_PLATFORMS, {"id": "p1"})
        assert repo.delete(TABLE_PLATFORMS, "p1") is True
        assert repo.delete(TABLE_PLATFORMS, "p1") is False
        assert repo.delete(TABLE_PLATFORMS, "") is False

    def test_query_filters_with_the_predicate(self, repo):
        repo.bulk_upsert(TABLE_PLATFORMS, [{"id": "a", "k": 1}, {"id": "b", "k": 2}])
        assert [d["id"] for d in repo.query(TABLE_PLATFORMS, lambda d: d["k"] == 2)] == ["b"]

    def test_meta_round_trips_and_defaults(self, repo):
        assert repo.get_meta("active", "fallback") == "fallback"
        repo.set_meta("active", {"project": "a", "cluster": "b"})
        assert repo.get_meta("active") == {"project": "a", "cluster": "b"}


class TestSqliteSpecific:
    @pytest.fixture
    def repo(self, tmp_path):
        return SqliteDocRepo(tmp_path / "docs.sqlite3")

    def test_a_table_name_that_is_not_an_identifier_is_refused(self, repo):
        """A table name cannot be a bound parameter, so the legal set is closed rather than
        escaped. Every caller passes a module constant; this keeps that true."""
        with pytest.raises(ValueError, match="illegal document table name"):
            repo.list("platforms; DROP TABLE platforms")

    def test_a_clock_field_that_is_not_an_identifier_is_refused(self, repo):
        with pytest.raises(ValueError, match="illegal clock field name"):
            repo.upsert_if_newer("activities", {"id": "a"}, clock="x') OR 1=1 --")

    def test_one_corrupt_payload_does_not_hide_the_rest(self, repo):
        """An append-preserving store whose every read fails on one bad row has lost the
        data it was keeping."""
        repo.bulk_upsert(TABLE_PLATFORMS, [{"id": "good1"}, {"id": "good2"}])
        conn = sqlite3.connect(str(repo.path))
        conn.execute(f"INSERT INTO {TABLE_PLATFORMS} (id, payload) VALUES ('bad', 'nope')")
        conn.commit()
        conn.close()
        assert sorted(d["id"] for d in repo.list(TABLE_PLATFORMS)) == ["good1", "good2"]
        assert repo.get(TABLE_PLATFORMS, "bad") is None

    def test_count_does_not_deserialise(self, repo):
        repo.bulk_upsert(TABLE_PLATFORMS, [{"id": f"p{i}"} for i in range(5)])
        assert repo.count(TABLE_PLATFORMS) == 5

    def test_the_store_survives_being_reopened(self, tmp_path):
        path = tmp_path / "docs.sqlite3"
        SqliteDocRepo(path).upsert(TABLE_PLATFORMS, {"id": "p1", "name": "One"})
        assert SqliteDocRepo(path).get(TABLE_PLATFORMS, "p1")["name"] == "One"


class TestConditionalUpsert:
    """`upsert_if_newer` is the guard the archive used to enforce in SQL."""

    @pytest.fixture
    def repo(self, tmp_path):
        return SqliteDocRepo(tmp_path / "a.sqlite3")

    def _put(self, repo, stamp, phase):
        return repo.upsert_if_newer(
            "activities", {"id": "a1", "updated_at": stamp, "phase": phase}, clock="updated_at"
        )

    def test_a_first_write_always_lands(self, repo):
        assert self._put(repo, "2026-09-18T10:00:00Z", "running") is True

    def test_a_newer_snapshot_wins(self, repo):
        self._put(repo, "2026-09-18T10:00:00Z", "running")
        assert self._put(repo, "2026-09-18T11:00:00Z", "done") is True
        assert repo.get("activities", "a1")["phase"] == "done"

    def test_a_stale_snapshot_cannot_overwrite_a_newer_one(self, repo):
        """The regression this exists to prevent: a background reconcile holding a row it
        read minutes ago must not drag a finished activity back to running."""
        self._put(repo, "2026-09-18T11:00:00Z", "done")
        assert self._put(repo, "2026-09-18T09:00:00Z", "running") is False
        assert repo.get("activities", "a1")["phase"] == "done"

    def test_an_equal_timestamp_still_refreshes(self, repo):
        """Matches the `>=` of the SQL this replaced: a same-second update is a real update,
        and ISO seconds are coarse enough for two to collide."""
        self._put(repo, "2026-09-18T11:00:00Z", "done")
        assert self._put(repo, "2026-09-18T11:00:00Z", "adopted") is True
        assert repo.get("activities", "a1")["phase"] == "adopted"

    def test_a_document_with_no_clock_never_displaces_a_stamped_one(self, repo):
        self._put(repo, "2026-09-18T11:00:00Z", "done")
        written = repo.upsert_if_newer(
            "activities", {"id": "a1", "phase": "clobber"}, clock="updated_at"
        )
        assert written is False
        assert repo.get("activities", "a1")["phase"] == "done"

    def test_an_idless_document_is_refused(self, repo):
        with pytest.raises(ValueError, match="non-empty 'id'"):
            repo.upsert_if_newer("activities", {"updated_at": "x"}, clock="updated_at")


class TestLegacyArchiveImport:
    """The pre-document relational table must be carried across, and never destroyed.

    This is the acceptance record -- the thing that answers "did this rack pass". A schema
    change that loses it is precisely the failure the archive was built to prevent.
    """

    LEGACY_DDL = """
        CREATE TABLE validation_activities (
            activity_id TEXT PRIMARY KEY,
            tool        TEXT NOT NULL,
            rack        TEXT,
            scope       TEXT,
            log_dir     TEXT,
            created_at  TEXT,
            updated_at  TEXT,
            payload     TEXT NOT NULL
        )
    """

    def _legacy(self, path, rows):
        conn = sqlite3.connect(str(path))
        conn.execute(self.LEGACY_DDL)
        conn.executemany(
            "INSERT INTO validation_activities "
            "(activity_id, tool, rack, scope, log_dir, created_at, updated_at, payload) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

    def _row(self, aid, *, tool="nccl", rack="r01", stamp="2026-09-01T00:00:00Z", phase="done"):
        payload = json.dumps(
            {
                "activity_id": aid,
                "tool": tool,
                "rack": rack,
                "phase": phase,
                "updated_at": stamp,
            }
        )
        return (aid, tool, rack, "singlerack", "/logs", stamp, stamp, payload)

    @pytest.fixture
    def module(self, tmp_path, monkeypatch):
        import validation_activities as va

        monkeypatch.setattr(va, "ACTIVITIES_FILE", tmp_path / "live.json")
        monkeypatch.setattr(va, "ACTIVITY_ARCHIVE_DB", tmp_path / "archive.sqlite3")
        va._archive_repos.clear()
        va._archive_imported.clear()
        yield va
        va._archive_repos.clear()
        va._archive_imported.clear()

    def test_legacy_rows_are_imported_on_first_use(self, module, tmp_path):
        self._legacy(tmp_path / "archive.sqlite3", [self._row("a1"), self._row("a2")])
        repo = module._archive()
        assert sorted(d["id"] for d in repo.list(module.ARCHIVE_TABLE)) == ["a1", "a2"]
        assert repo.get(module.ARCHIVE_TABLE, "a1")["activity_id"] == "a1"

    def test_the_legacy_table_is_left_intact(self, module, tmp_path):
        """Read, never written or dropped: a rollback must still find the data."""
        path = tmp_path / "archive.sqlite3"
        self._legacy(path, [self._row("a1")])
        module._archive()
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM validation_activities").fetchone()[0] == 1
        finally:
            conn.close()

    def test_import_does_not_run_twice_and_does_not_resurrect_a_deletion(self, module, tmp_path):
        """An import that re-ran on every open would undo an operator's dismissal."""
        self._legacy(tmp_path / "archive.sqlite3", [self._row("a1")])
        repo = module._archive()
        assert repo.delete(module.ARCHIVE_TABLE, "a1") is True
        module._archive_repos.clear()  # force a reopen, as a restart would
        module._archive_imported.clear()
        assert module._archive().list(module.ARCHIVE_TABLE) == []

    def test_an_archive_with_no_legacy_table_is_fine(self, module):
        assert module._archive().list(module.ARCHIVE_TABLE) == []

    def test_a_corrupt_legacy_payload_is_skipped_not_fatal(self, module, tmp_path):
        good = self._row("a1")
        bad = ("a2", "nccl", "r02", "singlerack", "/logs", "x", "x", "not json")
        self._legacy(tmp_path / "archive.sqlite3", [good, bad])
        assert [d["id"] for d in module._archive().list(module.ARCHIVE_TABLE)] == ["a1"]


class TestArchiveUsesTheGuard:
    """End to end through `validation_activities`, not just the backend."""

    @pytest.fixture
    def module(self, tmp_path, monkeypatch):
        import validation_activities as va

        monkeypatch.setattr(va, "ACTIVITIES_FILE", tmp_path / "live.json")
        monkeypatch.setattr(va, "ACTIVITY_ARCHIVE_DB", tmp_path / "archive.sqlite3")
        va._archive_repos.clear()
        va._archive_imported.clear()
        yield va
        va._archive_repos.clear()
        va._archive_imported.clear()

    def test_a_stale_snapshot_cannot_reopen_a_finished_activity(self, module):
        done = {
            "activity_id": "a1",
            "tool": "nccl",
            "rack": "r01",
            "phase": "done",
            "updated_at": "2026-09-18T12:00:00Z",
        }
        stale = {**done, "phase": "running", "updated_at": "2026-09-18T08:00:00Z"}
        assert module._archive_upsert_rows([done]) is True
        assert module._archive_upsert_rows([stale]) is True  # accepted, but must not apply
        stored = module._archive().get(module.ARCHIVE_TABLE, "a1")
        assert stored["phase"] == "done"

    def test_repointing_the_path_repoints_the_archive(self, module, tmp_path):
        """The aliasing trap: a repo bound at import would keep writing to the real file
        while a test asserted against its temp one."""
        module._archive_upsert_rows([{"activity_id": "a1", "updated_at": "2026-01-01T00:00:00Z"}])
        assert module._archive().count(module.ARCHIVE_TABLE) == 1
        module.ACTIVITY_ARCHIVE_DB = tmp_path / "second.sqlite3"
        assert module._archive().count(module.ARCHIVE_TABLE) == 0

    def test_rows_without_an_activity_id_are_dropped_quietly(self, module):
        assert module._archive_upsert_rows([{"tool": "nccl"}, "not a dict"]) is True
        assert module._archive().count(module.ARCHIVE_TABLE) == 0
