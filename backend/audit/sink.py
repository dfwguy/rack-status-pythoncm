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
"""The durable audit trail: one JSON object per line, size-bounded, owner-only.

The file is the system of record for "who did what, to which rack, when, and what came of it" —
the question every handover asks after the process that answered it is gone. Three properties are
load-bearing and each exists because of a specific failure:

* **Encoder-only serialisation** (NFR-10, threat T-49). Every line is produced by ``json.dumps``
  and never by string formatting. JSONL is line-oriented, so an unescaped newline in a
  client-supplied field — ``User-Agent`` is the easy one — would forge an entire additional audit
  record, including one attributed to another principal.
* **A budget that binds on time or bytes, whichever comes first** (FR-7b). A fixed byte budget
  silently encodes an operator count: 20 MiB at the measured 502 B/event is 41.8 days at 1,000
  events/day, but that rate is about *two* operators, and twenty gives 4.4 days. So the budget is
  derived from the observed rate against a retention target in days, capped by a hard ceiling, and
  the sink records which of the two is binding.
* **Independent of the application log** (FR-7d). Eighty-eight module loggers at INFO dwarf audit
  volume, so a shared budget would let a debug session rotate away the evidence.

Traces to FR-7, FR-7a, FR-7b, FR-7c, FR-7d, FR-11a, NFR-1, NFR-3, NFR-4, NFR-10.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logging_config as lc

#: The dedicated logger FR-7a fans out through. It propagates, so the sinks configured by FR-2
#: (stdout / file / syslog) also receive every audit event, which is what makes SIEM delivery work
#: without the audit file being involved.
AUDIT_LOGGER = "audit"

_OURS = "_nvis_audit_managed"

#: Marks a record as a real audit event. The JSONL file accepts nothing else.
_EVENT_MARK = "_nvis_audit_event"

#: Deliberately NOT under the ``audit`` namespace. ``logging.getLogger(__name__)`` here would be
#: ``audit.sink`` — a *child* of the audit logger — so every diagnostic this module logged would
#: propagate into the JSONL file as a non-JSON line, corrupting the trail precisely when something
#: had already gone wrong. Found by the encoder-only test.
_logger = logging.getLogger("rack_web.audit_sink")

#: Measured, not estimated: a representative event carrying the full FR-5 field set serialises to
#: this many bytes. Used only as the seed for the sizing calculation before the file has any
#: history of its own to measure.
NOMINAL_EVENT_BYTES = 502

BIND_RETENTION = "retention-target"
BIND_CEILING = "byte-ceiling"

# --- settings (registered with logging_config so the defaults test covers them, AC-25) ---------

AUDIT_ENABLED = lc.register_setting(lc.Setting("NVIS_AUDIT_ENABLED", True, lambda v: v not in ("0", "false", "no")))
AUDIT_FILE = lc.register_setting(lc.Setting("NVIS_AUDIT_FILE", "var/logs/audit/audit.jsonl"))
AUDIT_MAX_BYTES = lc.register_setting(lc.Setting("NVIS_AUDIT_MAX_BYTES", 5 * 1024 * 1024, int))
AUDIT_BACKUPS = lc.register_setting(lc.Setting("NVIS_AUDIT_BACKUPS", 24, int))
#: 24 rather than the original 3, decided 2026-09-10 when the trail became readable. Measured
#: 1.15 MB/day (~2,460 events/day) over 13 days, so 3 backups held roughly 17 days: a security
#: review asking about last month would have found it gone. 24 keeps 25 files x 5 MiB = 125 MiB,
#: about 90 days at that rate. A trail nobody can read has no reason to be long; one with a
#: reader does.
AUDIT_RETENTION_DAYS = lc.register_setting(lc.Setting("NVIS_AUDIT_RETENTION_DAYS", 30, int))
AUDIT_READS = lc.register_setting(lc.Setting("NVIS_AUDIT_READS", False, lambda v: v not in ("0", "false", "no")))
AUDIT_LONG_RETENTION = lc.register_setting(
    lc.Setting("NVIS_AUDIT_LONG_RETENTION", False, lambda v: v not in ("0", "false", "no"))
)
AUDIT_FLOOD_WINDOW_SEC = lc.register_setting(lc.Setting("NVIS_AUDIT_FLOOD_WINDOW_SEC", 60, int))
AUDIT_FLOOD_THRESHOLD = lc.register_setting(lc.Setting("NVIS_AUDIT_FLOOD_THRESHOLD", 10, int))

#: With long retention on, the ceiling is lifted by this factor rather than removed. FR-7c asks for
#: history beyond the default budget as a deliberate opt-in; it does not ask for unbounded growth,
#: and FR-7's whole point is that a full disk is a worse outcome than a short trail.
LONG_RETENTION_FACTOR = 12


@dataclass(frozen=True)
class Sizing:
    """The budget actually in force, and which constraint produced it (FR-7b)."""

    max_bytes: int
    backups: int
    binding: str
    event_bytes: int
    #: Days of history the budget holds at the observed rate, or None when the file has too little
    #: history to imply one. None is the honest answer on a fresh deployment, and it is why the
    #: ceiling binds there: assuming a rate would size the budget on a guess.
    implied_days: float | None

    @property
    def total_bytes(self) -> int:
        """The whole rotated set: the active file plus its backups.

        Counting the active file matters. The brief's sizing table originally said "15 MB (3 x 5
        MB)", which counted only the backups; ``backupCount=3`` keeps four files, so the real
        budget is 20 MiB. The error was in the conservative direction, but it was an error.
        """
        return self.max_bytes * (self.backups + 1)


class _EventsOnlyFilter(logging.Filter):
    """Only records produced by :func:`write` may reach the JSONL file.

    Belt to the naming brace above: the file's value depends on every line being parseable, so a
    stray record from anywhere — a future module logging under ``audit.*``, a library, a
    third-party handler — must not be able to land in it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return bool(getattr(record, _EVENT_MARK, False))


class JsonLineFormatter(logging.Formatter):
    """Emit the record's message and nothing else.

    The audit file has to be pure JSONL: a level or timestamp prefix would make every line
    unparseable. The event already carries its own timestamp, so nothing is lost.
    """

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def _observed_event_bytes(path: Path) -> int:
    """Mean serialised event size from the file's own tail, or the nominal seed when it is empty.

    Measuring beats trusting the constant: the field set is fixed but ``user_agent`` and
    ``resource`` are operator-supplied and vary by an order of magnitude between deployments.
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return NOMINAL_EVENT_BYTES
        with path.open("rb") as fh:
            tail = fh.read()[-65536:]
        lines = [ln for ln in tail.split(b"\n") if ln.strip()]
        if len(lines) < 2:  # one partial line tells us nothing reliable
            return NOMINAL_EVENT_BYTES
        return max(1, sum(len(ln) + 1 for ln in lines) // len(lines))
    except OSError:
        return NOMINAL_EVENT_BYTES


def _observed_events_per_day(path: Path, event_bytes: int) -> float | None:
    """Events/day implied by the file's own size and age, or None without enough history."""
    try:
        if not path.is_file():
            return None
        st = path.stat()
        if st.st_size < event_bytes * 2:
            return None
        created = getattr(st, "st_birthtime", st.st_ctime)
        age_days = max(time.time() - created, 0) / 86400.0
        if age_days < (1.0 / 24.0):  # under an hour of history extrapolates absurdly
            return None
        return (st.st_size / event_bytes) / age_days
    except OSError:
        return None


def sizing(path: Path | None = None) -> Sizing:
    """Resolve the budget in force and record which constraint binds (FR-7b, AC-10c, AC-10g)."""
    target_path = path or lc.resolve_path(str(AUDIT_FILE.resolve()))
    ceiling_max = max(1, int(AUDIT_MAX_BYTES.resolve()))
    backups = max(0, int(AUDIT_BACKUPS.resolve()))
    if AUDIT_LONG_RETENTION.resolve():
        backups *= LONG_RETENTION_FACTOR
    target_days = max(1, int(AUDIT_RETENTION_DAYS.resolve()))

    event_bytes = _observed_event_bytes(target_path)
    ceiling_total = ceiling_max * (backups + 1)
    rate = _observed_events_per_day(target_path, event_bytes)

    # No usable history: the ceiling is the only constraint we can honour, and implied_days is
    # genuinely unknown rather than zero.
    if rate is None or rate <= 0:
        return Sizing(ceiling_max, backups, BIND_CEILING, event_bytes, None)

    needed_total = rate * event_bytes * target_days
    if needed_total < ceiling_total:
        # The time target is the tighter constraint, so hold only what it needs.
        per_file = max(1, int(needed_total // (backups + 1)))
        return Sizing(per_file, backups, BIND_RETENTION, event_bytes, float(target_days))

    # The ceiling is tighter: keep the disk protection and report the shorter retention honestly,
    # which is the case FR-7b exists to make visible instead of silently untrue.
    return Sizing(ceiling_max, backups, BIND_CEILING, event_bytes,
                  (ceiling_total / event_bytes) / rate)


class _OwnerOnlyRotatingHandler(logging.handlers.RotatingFileHandler):
    """A rotating handler whose files are created 0600 inside a 0700 directory (NFR-4).

    ``RotatingFileHandler`` honours the process umask, which on a developer machine is usually 022
    and would leave an operator-behaviour record world-readable. Forcing the mode is cheaper than
    trusting every deployment's umask.
    """

    def _open(self):
        path = Path(self.baseFilename)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode if "a" in self.mode else "a", encoding=self.encoding)


def is_ours(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _OURS, False))


def installed_handler() -> logging.Handler | None:
    return next((h for h in logging.getLogger(AUDIT_LOGGER).handlers if is_ours(h)), None)


def configure(*, force: bool = False) -> bool:
    """Install the audit file handler on the ``audit`` logger. Idempotent; never raises.

    Independent of ``logging_config.configure`` on purpose: the trail must exist even if the
    application log configuration failed (NFR-1), and FR-11a says retention must not depend on
    stdout — which is what the application sinks are.
    """
    log = logging.getLogger(AUDIT_LOGGER)
    if installed_handler() is not None and not force:
        return False
    try:
        for h in [h for h in log.handlers if is_ours(h)]:
            log.removeHandler(h)
            h.close()
        if not AUDIT_ENABLED.resolve():
            return False

        path = lc.resolve_path(str(AUDIT_FILE.resolve()))
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        plan = sizing(path)

        handler = _OwnerOnlyRotatingHandler(
            path, maxBytes=plan.max_bytes, backupCount=plan.backups, encoding="utf-8"
        )
        handler.setFormatter(JsonLineFormatter())
        handler.addFilter(_EventsOnlyFilter())
        # NFR-3: the audit file is explicitly in scope for redaction, not just the app sinks.
        handler.addFilter(
            lc.RedactionFilter(
                lc.sensitive_values(tuple(lc.LOG_SENSITIVE_KEYS.resolve())),
                str(lc.LOG_REDACT_PLACEHOLDER.resolve()),
            )
        )
        setattr(handler, _OURS, True)
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        # propagate stays True: that is FR-7a, the fan-out to the FR-2 sinks.
        return True
    except Exception:
        _logger.warning("audit sink could not be configured; continuing without it", exc_info=True)
        return False


def write(event: dict[str, Any]) -> bool:
    """Append one event. Returns False on failure rather than raising (NFR-1).

    The single serialisation point in the package: exactly one ``json.dumps`` call, no formatting,
    no concatenation (NFR-10). ``ensure_ascii`` is left at its default so control characters —
    newlines above all — are escaped rather than emitted raw.
    """
    try:
        line = json.dumps(event, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        _logger.warning("audit event is not serialisable; dropping it", exc_info=True)
        return False
    try:
        logging.getLogger(AUDIT_LOGGER).info(line, extra={_EVENT_MARK: True})
        return True
    except Exception:  # noqa: BLE001 - see NFR-1; a sink failure is not the request's problem
        return False


def reset_for_tests() -> None:
    log = logging.getLogger(AUDIT_LOGGER)
    for h in [h for h in log.handlers if is_ours(h)]:
        log.removeHandler(h)
        h.close()
