# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""SQLite-backed progressive Cluster Overview live probes.

Per-section values and leases are shared across Gunicorn workers. Collectors run
outside SQLite transactions; BEGIN IMMEDIATE leases prevent duplicate refreshes.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

LIVE_PROBE_STATES = frozenset({"initializing", "fresh", "stale", "failed"})
_ERROR_LIMIT = 500
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|private[_ -]?key|credential)\b\s*[:=]\s*\S+"
)


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _safe_error(error: BaseException | str) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return (text or type(error).__name__)[:_ERROR_LIMIT]


def safe_error(error: BaseException | str) -> str:
    """Public bounded sanitizer for live-probe / overview error strings."""
    return _safe_error(error)


@dataclass(frozen=True)
class LiveProbeSection:
    cluster_id: str
    section: str
    state: str
    value: object | None
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
            "state": self.state,
            "value": self.value,
            "last_attempt_at": _iso(self.last_attempt_at),
            "last_success_at": _iso(self.last_success_at),
            "age_seconds": age,
            "last_error": self.last_error,
        }


class LiveProbeRepository:
    """One row per (cluster_id, section) with cross-process refresh leases."""

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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection, connection:
            yield connection

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS overview_live_probes (
                    cluster_id TEXT NOT NULL,
                    section TEXT NOT NULL,
                    state TEXT NOT NULL,
                    value_json TEXT,
                    last_attempt_at REAL,
                    last_success_at REAL,
                    last_error TEXT,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    PRIMARY KEY (cluster_id, section)
                )
                """
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> LiveProbeSection:
        raw_value = row["value_json"]
        value = json.loads(raw_value) if raw_value is not None else None
        return LiveProbeSection(
            cluster_id=row["cluster_id"],
            section=row["section"],
            state=row["state"],
            value=value,
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
        )

    def get(self, cluster_id: str, section: str) -> LiveProbeSection | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM overview_live_probes
                WHERE cluster_id = ? AND section = ?
                """,
                (cluster_id, section),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_sections(self, cluster_id: str, sections: list[str]) -> dict[str, LiveProbeSection]:
        result: dict[str, LiveProbeSection] = {}
        with self._connection() as connection:
            for section in sections:
                row = connection.execute(
                    """
                    SELECT * FROM overview_live_probes
                    WHERE cluster_id = ? AND section = ?
                    """,
                    (cluster_id, section),
                ).fetchone()
                if row is not None:
                    result[section] = self._from_row(row)
        return result

    def acquire_refresh_lease(
        self,
        cluster_id: str,
        section: str,
        owner: str,
        lease_seconds: float,
    ) -> tuple[LiveProbeSection, bool]:
        now = self.clock()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM overview_live_probes
                WHERE cluster_id = ? AND section = ?
                """,
                (cluster_id, section),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO overview_live_probes(
                        cluster_id, section, state, last_attempt_at,
                        lease_owner, lease_expires_at
                    ) VALUES (?, ?, 'initializing', ?, ?, ?)
                    """,
                    (cluster_id, section, now, owner, now + lease_seconds),
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
                    state = "initializing" if row["value_json"] is None else row["state"]
                    connection.execute(
                        """
                        UPDATE overview_live_probes
                        SET state = ?, last_attempt_at = ?,
                            lease_owner = ?, lease_expires_at = ?
                        WHERE cluster_id = ? AND section = ?
                        """,
                        (state, now, owner, now + lease_seconds, cluster_id, section),
                    )
            current = connection.execute(
                """
                SELECT * FROM overview_live_probes
                WHERE cluster_id = ? AND section = ?
                """,
                (cluster_id, section),
            ).fetchone()
            connection.commit()
        return self._from_row(current), acquired

    def commit_success(
        self, cluster_id: str, section: str, owner: str, value: object
    ) -> LiveProbeSection:
        now = self.clock()
        value_json = json.dumps(value, separators=(",", ":"), sort_keys=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE overview_live_probes
                SET state = 'fresh', value_json = ?, last_success_at = ?,
                    last_error = NULL, lease_owner = NULL, lease_expires_at = NULL
                WHERE cluster_id = ? AND section = ? AND lease_owner = ?
                """,
                (value_json, now, cluster_id, section, owner),
            )
            row = connection.execute(
                """
                SELECT * FROM overview_live_probes
                WHERE cluster_id = ? AND section = ?
                """,
                (cluster_id, section),
            ).fetchone()
            connection.commit()
        return self._from_row(row)

    def commit_failure(
        self, cluster_id: str, section: str, owner: str, error: BaseException | str
    ) -> LiveProbeSection:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM overview_live_probes
                WHERE cluster_id = ? AND section = ?
                """,
                (cluster_id, section),
            ).fetchone()
            if row is None:
                raise RuntimeError("live probe row disappeared during refresh")
            state = "stale" if row["value_json"] is not None else "failed"
            connection.execute(
                """
                UPDATE overview_live_probes
                SET state = ?, last_error = ?,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE cluster_id = ? AND section = ? AND lease_owner = ?
                """,
                (state, _safe_error(error), cluster_id, section, owner),
            )
            current = connection.execute(
                """
                SELECT * FROM overview_live_probes
                WHERE cluster_id = ? AND section = ?
                """,
                (cluster_id, section),
            ).fetchone()
            connection.commit()
        return self._from_row(current)


class ClusterOverviewLiveService:
    def __init__(
        self,
        path: str | Path,
        *,
        collectors: dict[str, Callable[[], object]],
        ttls: dict[str, float],
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 60,
        repository: LiveProbeRepository | None = None,
    ):
        if not collectors:
            raise ValueError("collectors must not be empty")
        missing_ttls = set(collectors) - set(ttls)
        if missing_ttls:
            raise ValueError(f"missing TTLs for sections: {sorted(missing_ttls)}")
        self.clock = clock
        self.collectors = dict(collectors)
        self.ttls = dict(ttls)
        self.lease_seconds = lease_seconds
        self.owner = f"{uuid.uuid4().hex}:{id(self)}"
        self.repository = repository or LiveProbeRepository(path, clock=clock)
        self._sections = list(collectors)

    def _section_is_fresh(self, section: LiveProbeSection, *, force_refresh: bool) -> bool:
        if force_refresh:
            return False
        if section.state not in ("fresh", "stale"):
            # initializing/failed without a usable cached value still need refresh,
            # but respect an active lease.
            if section.value is None:
                lease_active = (
                    section.lease_owner is not None
                    and section.lease_expires_at is not None
                    and section.lease_expires_at > self.clock()
                )
                return lease_active
            return False
        reference = section.last_success_at
        if reference is None:
            return False
        ttl = self.ttls.get(section.section)
        if ttl is None:
            return False
        return self.clock() - reference < ttl

    def _empty_section(self, cluster_id: str, section: str) -> LiveProbeSection:
        return LiveProbeSection(
            cluster_id=cluster_id,
            section=section,
            state="initializing",
            value=None,
            last_attempt_at=None,
            last_success_at=None,
            last_error=None,
            lease_owner=None,
            lease_expires_at=None,
        )

    def _refresh_and_commit(self, cluster_id: str, section: str) -> LiveProbeSection:
        collector = self.collectors[section]
        try:
            value = collector()
            return self.repository.commit_success(cluster_id, section, self.owner, value)
        except Exception as error:  # noqa: BLE001 - handler logs; ruff misses that inside a class body
            _log.debug("_refresh_and_commit: ignoring error", exc_info=True)
            return self.repository.commit_failure(cluster_id, section, self.owner, error)

    def _start_background_refresh(self, cluster_id: str, section: str) -> None:
        def _run_refresh() -> None:
            try:
                self._refresh_and_commit(cluster_id, section)
            except Exception:  # noqa: BLE001 - handler logs; ruff misses that inside a class body
                _log.exception(
                    "background overview live probe refresh failed for %s/%s",
                    cluster_id,
                    section,
                    exc_info=True,
                )

        threading.Thread(
            target=_run_refresh,
            name=f"overview-live-{section}-{cluster_id[:8]}",
            daemon=True,
        ).start()

    def get_snapshot(
        self,
        cluster_id: str,
        force_refresh: bool = False,
        allow_refresh: bool = True,
    ) -> dict[str, object]:
        now = self.clock()
        existing = self.repository.list_sections(cluster_id, self._sections)
        sections: dict[str, dict[str, object]] = {}

        for name in self._sections:
            current = existing.get(name)
            if current is None:
                current = self._empty_section(cluster_id, name)

            if not allow_refresh:
                sections[name] = current.metadata(now=now)
                continue

            if self._section_is_fresh(current, force_refresh=force_refresh):
                sections[name] = current.metadata(now=now)
                continue

            leased, acquired = self.repository.acquire_refresh_lease(
                cluster_id,
                name,
                self.owner,
                self.lease_seconds,
            )
            if acquired:
                self._start_background_refresh(cluster_id, name)
            sections[name] = leased.metadata(now=now)

        return {"sections": sections}
