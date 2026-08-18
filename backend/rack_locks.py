# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Rack-level advisory locks for multi-operator coordination.

Two lock sources are merged into a single snapshot served by ``GET /api/rack-locks``:

1. **Implicit** -- derived from in-flight operations (async job dicts, sync handlers wrapped
   in ``_track_sync_op``, write-y stream handlers, and workflow ``host:`` steps). Operation-
   bounded with a defensive ``RACK_LOCK_MAX_AGE_SEC`` (default 6 h) filter so a stuck job
   never wedges a rack forever.

2. **Explicit claims** -- persisted to ``var/cache/rack_locks.json`` with ``fcntl`` advisory
   flock, TTL-based (default 30 min, refreshable, capped at 6 h). A claim is also released
   early when the recording browser ``client_id`` has been absent from ``_app_clients_last_seen``
   for more than ``OPERATOR_OFFLINE_SEC`` so closed tabs don't leave ghost claims.

The whole subsystem is **advisory**. Authorization is operator-string equality, treated as
display metadata, not authentication. The SPA intercepts conflicts in
``RackCommandsStoreContext``; the server never returns 409. Designed for <= 10 operators on
a single Flask process.

Path override: ``RACK_LOCKS_FILE``.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import config_env

_logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
# Lock claims persist under repo-root var/ (parent of backend/).
REPO_ROOT = APP_DIR.parent
CLAIMS_FILE = Path(
    os.environ.get("RACK_LOCKS_FILE", "")
    or str(REPO_ROOT / "var" / "cache" / "rack_locks.json")
)

CLAIM_DEFAULT_TTL_SEC = 1800       # 30 min
CLAIM_MAX_TTL_SEC = 21600          # 6 h cap on caller-requested TTL
OPERATOR_OFFLINE_SEC = 300         # 5 min: drop claim when owning client_id absent
OPERATOR_LABEL_MAX_LEN = 64
NOTE_MAX_LEN = 512
CLIENT_ID_MAX_LEN = 80

RACK_ID_RE = re.compile(r"^p[0-9]+-r[0-9]+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers (env, time, sanitization)
# ---------------------------------------------------------------------------


def _env_int(key: str, default: int) -> int:
    """No validity rule: the one caller floors the result itself (``max(60, ...)``).

    One implementation, in ``config_env``.
    """
    return config_env.env_int(key, default)


def rack_lock_max_age_sec() -> int:
    """Defensive max-age filter on implicit locks; never below 60 s."""
    return max(60, _env_int("RACK_LOCK_MAX_AGE_SEC", 21600))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_to_ts(iso: str) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def normalize_rack_id(rack: str) -> str:
    """Lowercase and validate ``pN-rMM``. Raises ``ValueError`` on bad input."""
    r = (rack or "").strip().lower()
    if not RACK_ID_RE.match(r):
        raise ValueError(f"invalid rack id (expected e.g. p17-r01): {rack!r}")
    return r


def sanitize_operator(operator: str | None) -> str:
    """Strip and truncate an operator label. Always returns a string (possibly empty)."""
    if not isinstance(operator, str):
        return ""
    s = operator.strip().replace("\n", " ").replace("\r", " ")
    return s[:OPERATOR_LABEL_MAX_LEN]


def _sanitize_client_id(cid: str | None) -> str | None:
    if not isinstance(cid, str):
        return None
    s = cid.strip()[:CLIENT_ID_MAX_LEN]
    return s or None


# ---------------------------------------------------------------------------
# Claims file (var/cache/rack_locks.json) -- fcntl flock, same pattern as comments
# ---------------------------------------------------------------------------


def _ensure_dir() -> None:
    CLAIMS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _read_claims_locked(f) -> dict[str, dict]:
    """Read claims from an already-locked handle. Bad keys/entries are dropped."""
    f.seek(0)
    blob = f.read()
    if not blob.strip():
        return {}
    try:
        raw = json.loads(blob)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        try:
            rk = normalize_rack_id(k)
        except ValueError:
            continue
        out[rk] = v
    return out


def _write_claims_locked(f, claims: dict[str, dict]) -> None:
    blob = json.dumps(claims, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    f.seek(0)
    f.truncate()
    f.write(blob)
    f.flush()
    try:
        os.fsync(f.fileno())
    except OSError:
        pass


def _is_claim_expired(claim: dict, now: float) -> bool:
    ts = _iso_to_ts(claim.get("expires_at") or "")
    return ts is None or ts <= now


def _is_operator_offline(claim: dict, active_clients: dict[str, float], now: float) -> bool:
    """A claim with a recorded ``client_id`` is considered orphaned when that id is absent
    from the heartbeat map (or last-seen > OPERATOR_OFFLINE_SEC). Claims without a
    ``client_id`` (e.g. workflow-engine claims) skip this check."""
    cid = _sanitize_client_id(claim.get("client_id"))
    if not cid:
        return False
    last = active_clients.get(cid)
    if last is None:
        return True
    return (now - float(last)) > OPERATOR_OFFLINE_SEC


def list_claims(active_clients: dict[str, float] | None = None) -> dict[str, dict]:
    """Return the current map of rack_id -> claim, filtered for expiry and orphan."""
    if not CLAIMS_FILE.is_file():
        return {}
    now = time.time()
    ac = active_clients or {}
    try:
        with open(CLAIMS_FILE, "r", encoding="utf-8") as f:
            locked = False
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                pass
            try:
                claims = _read_claims_locked(f)
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except (OSError, UnicodeDecodeError):
        return {}
    return {
        rk: claim
        for rk, claim in claims.items()
        if not _is_claim_expired(claim, now)
        and not _is_operator_offline(claim, ac, now)
    }


def purge_expired_claims(active_clients: dict[str, float] | None = None) -> int:
    """Rewrite the file dropping expired and orphaned entries. Returns number dropped."""
    if not CLAIMS_FILE.is_file():
        return 0
    _ensure_dir()
    now = time.time()
    ac = active_clients or {}
    dropped = 0
    with open(CLAIMS_FILE, "r+", encoding="utf-8") as f:
        locked = False
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass
        try:
            claims = _read_claims_locked(f)
            keep: dict[str, dict] = {}
            for rk, claim in claims.items():
                if _is_claim_expired(claim, now) or _is_operator_offline(claim, ac, now):
                    dropped += 1
                    continue
                keep[rk] = claim
            if dropped:
                _write_claims_locked(f, keep)
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass
    return dropped


def upsert_claim(
    rack: str,
    operator: str,
    *,
    client_id: str | None = None,
    note: str = "",
    ttl_sec: int | None = None,
    force: bool = False,
) -> dict:
    """Create or refresh a claim.

    Raises :class:`PermissionError` if the rack is currently held by a different
    (non-expired) operator, **unless** ``force=True`` is passed in which case the
    caller takes over: the previous operator is recorded under ``stolen_from`` and
    ``since`` is reset to ``now`` so the takeover is visible in the UI/audit trail.

    Returns the persisted claim record.
    """
    rk = normalize_rack_id(rack)
    op = sanitize_operator(operator)
    if not op:
        raise ValueError("operator name is required to claim a rack")
    requested = int(ttl_sec) if ttl_sec is not None else CLAIM_DEFAULT_TTL_SEC
    ttl = max(60, min(requested, CLAIM_MAX_TTL_SEC))
    now = time.time()
    cid = _sanitize_client_id(client_id)
    note_clean = (note or "").strip()[:NOTE_MAX_LEN] if isinstance(note, str) else ""

    _ensure_dir()
    if not CLAIMS_FILE.exists():
        CLAIMS_FILE.write_text("{}\n", encoding="utf-8")

    with open(CLAIMS_FILE, "r+", encoding="utf-8") as f:
        locked = False
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass
        try:
            claims = _read_claims_locked(f)
            existing = claims.get(rk)
            takeover_from: str | None = None
            if existing and not _is_claim_expired(existing, now):
                existing_op = sanitize_operator(existing.get("operator", ""))
                if existing_op and existing_op != op:
                    if not force:
                        raise PermissionError(
                            f"rack {rk} is claimed by {existing_op!r}"
                        )
                    takeover_from = existing_op
            # Preserve ``since`` for refresh by the same operator; reset on a takeover.
            since = None
            if takeover_from is None and isinstance(existing, dict):
                prev_since = existing.get("since")
                if isinstance(prev_since, str) and prev_since:
                    since = prev_since
            if not since:
                since = _now_iso()
            claim: dict = {
                "operator": op,
                "client_id": cid,
                "note": note_clean,
                "since": since,
                "refreshed_at": _now_iso(),
                "expires_at": datetime.fromtimestamp(now + ttl, tz=timezone.utc).isoformat(),
            }
            if takeover_from:
                claim["stolen_from"] = takeover_from
            claims[rk] = claim
            _write_claims_locked(f, claims)
            return {"rack": rk, **claim}
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass


def release_claim(rack: str, operator: str) -> bool:
    """Delete a claim. Returns True when removed; False when no claim existed. Raises
    :class:`PermissionError` if the requesting operator is not the owner (empty
    ``operator`` is allowed to release expired entries, useful for cleanup)."""
    rk = normalize_rack_id(rack)
    op = sanitize_operator(operator)
    if not CLAIMS_FILE.is_file():
        return False

    with open(CLAIMS_FILE, "r+", encoding="utf-8") as f:
        locked = False
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass
        try:
            claims = _read_claims_locked(f)
            existing = claims.get(rk)
            if not existing:
                return False
            now = time.time()
            existing_op = sanitize_operator(existing.get("operator", ""))
            still_active = not _is_claim_expired(existing, now)
            if op and existing_op and still_active and existing_op != op:
                raise PermissionError(
                    f"rack {rk} is claimed by {existing_op!r}; only the owner can release"
                )
            claims.pop(rk, None)
            _write_claims_locked(f, claims)
            return True
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Snapshot derivation (combines active-jobs + claims)
# ---------------------------------------------------------------------------


def _coerce_racks(job: dict) -> list[str]:
    """Pull rack id(s) out of a job dict in canonical form. Drops malformed entries."""
    racks_raw = job.get("racks")
    if not isinstance(racks_raw, (list, tuple)) or not racks_raw:
        single = job.get("rack")
        racks_raw = [single] if isinstance(single, str) and single else []
    out: list[str] = []
    for raw_rack in racks_raw:
        try:
            out.append(normalize_rack_id(str(raw_rack)))
        except ValueError:
            continue
    return out


def _derive_active_jobs_by_rack(active_jobs: Iterable[dict]) -> dict[str, dict]:
    """Group active jobs by rack. For the same rack, the oldest still-running job wins."""
    max_age = rack_lock_max_age_sec()
    now = time.time()
    out: dict[str, dict] = {}
    for job in active_jobs:
        phase = str(job.get("phase") or "").lower()
        if phase not in {"starting", "running"}:
            continue
        started_iso = str(job.get("started_at") or "")
        started = _iso_to_ts(started_iso) or now
        if (now - started) > max_age:
            continue
        for rk in _coerce_racks(job):
            existing = out.get(rk)
            if existing is not None:
                ex_started = _iso_to_ts(str(existing.get("since") or "")) or now
                if ex_started <= started:
                    continue
            out[rk] = {
                "operator": sanitize_operator(str(job.get("operator") or "")),
                "kind": "active_job",
                "detail": str(job.get("kind") or "operation"),
                "job_id": str(job.get("job_id") or ""),
                "since": started_iso or _now_iso(),
                "sync": bool(job.get("sync")),
            }
    return out


def build_lock_snapshot(
    operator: str | None,
    active_jobs: Iterable[dict],
    active_clients: dict[str, float] | None = None,
) -> dict:
    """Public snapshot served by ``GET /api/rack-locks``.

    Active jobs take precedence over claims for the same rack -- the running operation is
    the more concrete signal.
    """
    me = sanitize_operator(operator)
    job_locks = _derive_active_jobs_by_rack(active_jobs)
    claims = list_claims(active_clients)

    locks: dict[str, dict] = dict(job_locks)
    for rk, claim in claims.items():
        if rk in locks:
            continue
        entry: dict = {
            "operator": sanitize_operator(claim.get("operator", "")),
            "kind": "claim",
            "detail": (claim.get("note") or "manual claim"),
            "since": claim.get("since") or claim.get("refreshed_at") or _now_iso(),
            "expires_at": claim.get("expires_at"),
        }
        stolen_from = sanitize_operator(claim.get("stolen_from", ""))
        if stolen_from:
            entry["stolen_from"] = stolen_from
        locks[rk] = entry

    return {
        "ok": True,
        "now": _now_iso(),
        "me": me,
        "locks": locks,
    }


def is_lock_held_by_other(
    rack: str,
    operator: str,
    active_jobs: Iterable[dict],
    active_clients: dict[str, float] | None = None,
) -> dict | None:
    """Return the lock entry if *rack* is locked by someone other than *operator*; else ``None``."""
    try:
        rk = normalize_rack_id(rack)
    except ValueError:
        return None
    snapshot = build_lock_snapshot(operator, active_jobs, active_clients)
    entry = snapshot.get("locks", {}).get(rk)
    if not entry:
        return None
    if sanitize_operator(entry.get("operator", "")) == sanitize_operator(operator):
        return None
    return entry
