# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""SQLite-backed canonical cluster inventory snapshots.

The collector runs outside SQLite transactions. A short, expiring lease prevents
Gunicorn workers from collecting the same cluster/log-root generation concurrently.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

_log = logging.getLogger(__name__)

SNAPSHOT_STATES = frozenset({"initializing", "fresh", "stale", "failed"})
_ERROR_LIMIT = 500
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|private[_ -]?key|credential)\b\s*[:=]\s*\S+"
)


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _safe_error(error: BaseException | str) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return "Cluster inventory refresh timed out"
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return (text or type(error).__name__)[:_ERROR_LIMIT]


def _snapshot_key(cluster_id: str, log_root: str, generation: int) -> str:
    material = json.dumps(
        {
            "cluster_id": cluster_id,
            "log_root": log_root,
            "configuration_generation": generation,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClusterSnapshot:
    snapshot_key: str
    cluster_id: str
    log_root: str
    configuration_generation: int
    snapshot_id: str | None
    state: str
    payload: dict | None
    last_attempt_at: float | None
    last_success_at: float | None
    last_error: str | None
    lease_owner: str | None
    lease_expires_at: float | None

    def metadata(self, now: float | None = None) -> dict[str, object]:
        current = time.time() if now is None else now
        age = None
        if self.last_success_at is not None:
            age = max(0.0, current - self.last_success_at)
        return {
            "id": self.snapshot_id,
            "state": self.state,
            "cluster_id": self.cluster_id,
            "log_root": self.log_root,
            "configuration_generation": self.configuration_generation,
            "last_attempt_at": _iso(self.last_attempt_at),
            "last_success_at": _iso(self.last_success_at),
            "age_seconds": age,
            "last_error": self.last_error,
        }


class SnapshotRepository:
    """Small SQLite repository with one row per cluster/log-root/generation."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshot_meta (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO snapshot_meta(name, value)
                VALUES ('configuration_generation', 1)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_snapshots (
                    snapshot_key TEXT PRIMARY KEY,
                    cluster_id TEXT NOT NULL,
                    log_root TEXT NOT NULL,
                    configuration_generation INTEGER NOT NULL,
                    snapshot_id TEXT,
                    state TEXT NOT NULL,
                    payload_json TEXT,
                    last_attempt_at REAL,
                    last_success_at REAL,
                    last_error TEXT,
                    lease_owner TEXT,
                    lease_expires_at REAL
                )
                """
            )

    def configuration_generation(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM snapshot_meta WHERE name = 'configuration_generation'"
            ).fetchone()
        return int(row["value"])

    def bump_configuration_generation(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE snapshot_meta
                SET value = value + 1
                WHERE name = 'configuration_generation'
                """
            )
            row = connection.execute(
                "SELECT value FROM snapshot_meta WHERE name = 'configuration_generation'"
            ).fetchone()
            connection.commit()
        return int(row["value"])

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ClusterSnapshot:
        raw_payload = row["payload_json"]
        payload = json.loads(raw_payload) if raw_payload is not None else None
        return ClusterSnapshot(
            snapshot_key=row["snapshot_key"],
            cluster_id=row["cluster_id"],
            log_root=row["log_root"],
            configuration_generation=int(row["configuration_generation"]),
            snapshot_id=row["snapshot_id"],
            state=row["state"],
            payload=payload,
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
        )

    def get(self, cluster_id: str, log_root: str, generation: int) -> ClusterSnapshot | None:
        key = _snapshot_key(cluster_id, log_root, generation)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cluster_snapshots WHERE snapshot_key = ?", (key,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def acquire_refresh_lease(
        self,
        cluster_id: str,
        log_root: str,
        generation: int,
        owner: str,
        lease_seconds: float,
    ) -> tuple[ClusterSnapshot, bool]:
        now = self.clock()
        key = _snapshot_key(cluster_id, log_root, generation)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM cluster_snapshots WHERE snapshot_key = ?", (key,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO cluster_snapshots(
                        snapshot_key, cluster_id, log_root, configuration_generation,
                        state, last_attempt_at, lease_owner, lease_expires_at
                    ) VALUES (?, ?, ?, ?, 'initializing', ?, ?, ?)
                    """,
                    (key, cluster_id, log_root, generation, now, owner, now + lease_seconds),
                )
                acquired = True
            else:
                lease_active = (
                    row["lease_owner"] is not None
                    and row["lease_expires_at"] is not None
                    and float(row["lease_expires_at"]) > now
                )
                acquired = not lease_active
                if acquired:
                    state = "initializing" if row["payload_json"] is None else row["state"]
                    connection.execute(
                        """
                        UPDATE cluster_snapshots
                        SET state = ?, last_attempt_at = ?, lease_owner = ?, lease_expires_at = ?
                        WHERE snapshot_key = ?
                        """,
                        (state, now, owner, now + lease_seconds, key),
                    )
            current = connection.execute(
                "SELECT * FROM cluster_snapshots WHERE snapshot_key = ?", (key,)
            ).fetchone()
            connection.commit()
        return self._from_row(current), acquired

    def commit_success(self, key: str, owner: str, payload: dict) -> ClusterSnapshot:
        now = self.clock()
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE cluster_snapshots
                SET snapshot_id = ?, state = 'fresh', payload_json = ?,
                    last_success_at = ?, last_error = NULL,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE snapshot_key = ? AND lease_owner = ?
                """,
                (uuid.uuid4().hex, payload_json, now, key, owner),
            )
            row = connection.execute(
                "SELECT * FROM cluster_snapshots WHERE snapshot_key = ?", (key,)
            ).fetchone()
            connection.commit()
        return self._from_row(row)

    def commit_failure(self, key: str, owner: str, error: BaseException | str) -> ClusterSnapshot:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM cluster_snapshots WHERE snapshot_key = ?", (key,)
            ).fetchone()
            if row is None:
                raise RuntimeError("snapshot disappeared during refresh")
            state = "stale" if row["payload_json"] is not None else "failed"
            connection.execute(
                """
                UPDATE cluster_snapshots
                SET state = ?, last_error = ?, lease_owner = NULL, lease_expires_at = NULL
                WHERE snapshot_key = ? AND lease_owner = ?
                """,
                (state, _safe_error(error), key, owner),
            )
            current = connection.execute(
                "SELECT * FROM cluster_snapshots WHERE snapshot_key = ?", (key,)
            ).fetchone()
            connection.commit()
        return self._from_row(current)


class ClusterSnapshotService:
    def __init__(
        self,
        repository: SnapshotRepository,
        *,
        collector: Callable[[], dict],
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 360,
    ):
        self.repository = repository
        self.collector = collector
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.owner = f"{uuid.uuid4().hex}:{id(self)}"

    def invalidate(self) -> int:
        return self.repository.bump_configuration_generation()

    @staticmethod
    def _validated_payload(result: object) -> dict:
        if not isinstance(result, dict):
            raise RuntimeError("Cluster inventory collector returned an invalid payload")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
        if result.get("ok") is False:
            raise RuntimeError(str(result.get("error") or "Cluster inventory collector failed"))
        returncode = result.get("returncode")
        if returncode is not None and int(returncode) != 0:
            raise RuntimeError(
                str(result.get("error") or result.get("stderr") or "Cluster inventory collector failed")
            )
        if (
            not isinstance(result.get("rows"), list)
            or not isinstance(result.get("physical_rf0"), dict)
            or not isinstance(result.get("summary"), dict)
        ):
            raise RuntimeError(  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
                "Cluster inventory collector returned incomplete inventory"
            )
        return result

    def _refresh_and_commit(self, snapshot_key: str) -> ClusterSnapshot:
        try:
            result = self._validated_payload(self.collector())
            return self.repository.commit_success(snapshot_key, self.owner, result)
        except Exception as error:  # noqa: BLE001 - handler logs; ruff misses that inside a class body
            _log.debug("_refresh_and_commit: ignoring error", exc_info=True)
            return self.repository.commit_failure(snapshot_key, self.owner, error)

    def _start_background_refresh(self, snapshot_key: str) -> None:
        def _run_refresh() -> None:
            try:
                self._refresh_and_commit(snapshot_key)
            except Exception:  # noqa: BLE001 - handler logs; ruff misses that inside a class body
                _log.exception(
                    "background cluster snapshot refresh failed for %s",
                    snapshot_key[:8],
                    exc_info=True,
                )

        threading.Thread(
            target=_run_refresh,
            name=f"cluster-snapshot-refresh-{snapshot_key[:8]}",
            daemon=True,
        ).start()

    def get_cluster_snapshot(
        self,
        cluster_id: str,
        log_root: str,
        *,
        max_age_seconds: float = 30,
        force_refresh: bool = False,
        wait: bool = True,
    ) -> ClusterSnapshot:
        generation = self.repository.configuration_generation()
        existing = self.repository.get(cluster_id, log_root, generation)
        if existing is not None and not force_refresh:
            reference = existing.last_attempt_at
            lease_active = (
                existing.lease_owner is not None
                and existing.lease_expires_at is not None
                and existing.lease_expires_at > self.clock()
            )
            if existing.state == "initializing" and not lease_active:
                pass
            elif reference is not None and self.clock() - reference < max_age_seconds:
                return existing

        leased, acquired = self.repository.acquire_refresh_lease(
            cluster_id,
            log_root,
            generation,
            self.owner,
            self.lease_seconds,
        )
        if not acquired:
            return leased

        if not wait:
            self._start_background_refresh(leased.snapshot_key)
            return leased

        return self._refresh_and_commit(leased.snapshot_key)
