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
"""Central logging configuration — the one place sinks, level, format and redaction are decided.

Applied once at startup so the 88 existing ``logging.getLogger`` call sites route somewhere
without any of them changing (FR-1, NFR-6). Before this module there was no logging
configuration at all: with no handler on the root logger, Python falls back to
``logging.lastResort`` at WARNING, so every INFO and DEBUG record in the backend was being
discarded silently. "Turn logging on" is closer to the truth than "improve logging".

Settings are read from the process environment directly rather than through
``config_env._eget`` (NFR-5): an authenticated dashboard operator must not be able to lower the
log level, redirect a sink or empty the sensitive-key list at runtime. Same exception already
taken for ``RACK_WEB_ALLOWED_ORIGINS`` and ``RACK_WEB_TLS_*``.

Traces to FR-1..FR-4, FR-13, NFR-1, NFR-3, NFR-6, NFR-9 of
``.plc/briefs/auth-audit-logging-brief.md``.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Marks a handler this module installed, so ``configure`` can replace its own work without
#: touching handlers somebody else attached — pytest's ``caplog`` being the case that matters.
_OURS = "_nvis_logging_managed"

SINK_STDOUT = "stdout"
SINK_FILE = "file"
SINK_SYSLOG = "syslog"
ALL_SINKS = (SINK_STDOUT, SINK_FILE, SINK_SYSLOG)

#: Sinks that lose records silently. Only syslog qualifies: it is UDP, so a dropped datagram is
#: indistinguishable from one that was never sent. FR-2a forbids it being the only path.
#:
#: stdout is deliberately *not* here. It is not lossy in transit — the container runtime captures
#: it — so a stdout-only selection is a legitimate configuration and AC-3 requires it to deliver to
#: stdout and nothing else. stdout is however not *retained* across a container recreate, which is
#: a different problem and belongs to the audit trail rather than the application log: FR-11a is
#: satisfied by the audit sink's own file, not by adding a sink here.
LOSSY_SINKS = (SINK_SYSLOG,)

#: A record is redacted when it contains the *value* of an environment variable whose *name*
#: matches one of these. Note the absence of a bare ``KEY``: it would match ``RACK_SSH_KEY``,
#: ``RACK_WEB_TLS_KEY`` and ``RACK_NVFTP_SFTP_STRICT_HOST_KEY``, which hold file paths and a
#: boolean, and redacting a path makes logs harder to read while protecting nothing.
SENSITIVE_NAME_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|CREDENTIAL)", re.IGNORECASE)

#: Values shorter than this are ignored by the redaction filter. A two-character secret would
#: match inside ordinary words and redact most of the log; refusing to redact it is the lesser
#: harm, and a secret that short is a different problem.
MIN_REDACTABLE_LEN = 8

_configured = False
_config_error_reported = False
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    """One environment-backed knob, with the default that makes it optional (FR-13)."""

    name: str
    default: Any
    parse: Callable[[str], Any] = str

    def resolve(self) -> Any:
        raw = os.environ.get(self.name)
        if raw is None or raw == "":
            return self.default
        try:
            return self.parse(raw)
        except (TypeError, ValueError):
            _logger.warning("%s=%r is not usable; falling back to %r", self.name, raw, self.default)
            return self.default


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


#: Every knob the feature introduces, so a test can enumerate them rather than restate them
#: (AC-25). ``backend/audit`` registers its own settings here when it loads, which is what keeps
#: that test honest as the feature grows instead of silently covering only this module's half.
SETTINGS: dict[str, Setting] = {}


def register_setting(setting: Setting) -> Setting:
    SETTINGS[setting.name] = setting
    return setting


LOG_LEVEL = register_setting(Setting("NVIS_LOG_LEVEL", "INFO"))
LOG_SINKS = register_setting(Setting("NVIS_LOG_SINKS", (SINK_STDOUT, SINK_FILE), _csv))
LOG_FORMAT = register_setting(Setting("NVIS_LOG_FORMAT", "text"))
LOG_FILE = register_setting(Setting("NVIS_LOG_FILE", "var/logs/rack-status.log"))
LOG_MAX_BYTES = register_setting(Setting("NVIS_LOG_MAX_BYTES", 5 * 1024 * 1024, int))
LOG_BACKUPS = register_setting(Setting("NVIS_LOG_BACKUPS", 3, int))
LOG_SYSLOG_HOST = register_setting(Setting("NVIS_LOG_SYSLOG_HOST", "localhost"))
LOG_SYSLOG_PORT = register_setting(Setting("NVIS_LOG_SYSLOG_PORT", 514, int))
LOG_SENSITIVE_KEYS = register_setting(Setting("NVIS_LOG_SENSITIVE_KEYS", (), _csv))
LOG_REDACT_PLACEHOLDER = register_setting(Setting("NVIS_LOG_REDACT_PLACEHOLDER", "[REDACTED]"))


def resolve_settings() -> dict[str, Any]:
    """Every registered setting's effective value. Used by the defaults test (AC-25)."""
    return {name: setting.resolve() for name, setting in SETTINGS.items()}


# ---------------------------------------------------------------------------
# Redaction (FR-4, NFR-3)
# ---------------------------------------------------------------------------


def sensitive_values(extra_names: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Values of environment variables whose names look sensitive, longest first.

    Longest first matters: if one secret contains another as a substring, redacting the short
    one first would leave a recognisable tail of the long one behind.
    """
    names = {n for n in os.environ if SENSITIVE_NAME_PATTERN.search(n)}
    names.update(extra_names)
    values = {
        v for n in names if (v := os.environ.get(n, "")) and len(v) >= MIN_REDACTABLE_LEN
    }
    return tuple(sorted(values, key=len, reverse=True))


def scrub_text(text: str, values: tuple[str, ...] | None = None, placeholder: str | None = None) -> str:
    """Replace known secret values in *text*.

    Public because redaction is needed outside logging now: the JSON error handler
    (``api/json_errors.py``) puts an exception's message in an HTTP response, and that message is
    arbitrary text from anywhere in the process. Resolving the value list here rather than at the
    call site is the point -- this module already decides which env names look sensitive, already
    refuses values under ``MIN_REDACTABLE_LEN``, and already sorts longest-first so a secret
    containing another does not leave a recognisable tail.
    """
    if values is None:
        values = sensitive_values(tuple(LOG_SENSITIVE_KEYS.resolve()))
    if placeholder is None:
        placeholder = str(LOG_REDACT_PLACEHOLDER.resolve())
    for secret in values:
        text = text.replace(secret, placeholder)
    return text


class RedactionFilter(logging.Filter):
    """Replace known secret values with a placeholder before a record reaches a sink.

    Attached to **handlers**, never to a logger. A filter on a logger only sees records logged
    directly to it, not records propagated up from its children — so a logger-level filter would
    miss all 88 module loggers, which is every record that matters. Handler-level catches
    everything that reaches a sink, which is exactly what FR-4 asks for.
    """

    def __init__(self, values: tuple[str, ...], placeholder: str) -> None:
        super().__init__()
        self._values = values
        self._placeholder = placeholder

    def _scrub(self, text: str) -> str:
        # Values and placeholder are resolved once at construction, so they are passed in rather
        # than re-resolved per record.
        return scrub_text(text, self._values, self._placeholder)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        if isinstance(record.msg, str) and any(s in record.msg for s in self._values):
            record.msg = self._scrub(record.msg)
        if record.args:
            args = record.args
            if isinstance(args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v for k, v in args.items()
                }
            elif isinstance(args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in args
                )
        return True


# ---------------------------------------------------------------------------
# Formatters (FR-3)
# ---------------------------------------------------------------------------

TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for SIEM ingest.

    Serialised with ``json.dumps``, which escapes control characters — so a newline inside a
    logged value cannot forge a second line. The audit sink depends on the same property for a
    stronger reason (NFR-10, threat T-49); here it is what keeps the stream parseable.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _formatter(kind: str) -> logging.Formatter:
    return JsonFormatter() if kind.strip().lower() == "json" else logging.Formatter(TEXT_FORMAT)


# ---------------------------------------------------------------------------
# Sink construction (FR-2, FR-2a)
# ---------------------------------------------------------------------------


def resolve_path(raw: str) -> Path:
    """Resolve a configured path, treating a relative one as relative to the repo root.

    Relative-by-default is what lets the defaults work with no configuration at all (FR-13), and
    resolving against the repo root rather than the process CWD means a worker started from
    somewhere else still writes to the same place.
    """
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


def _selected_sinks() -> tuple[tuple[str, ...], list[str]]:
    """The sinks to install, plus any notes to report once (AC-3a).

    A configuration naming only ``syslog`` is not an error to refuse outright — refusing would
    mean no logging at all — but it cannot stand either, because UDP syslog drops silently and
    FR-2a forbids it being the only path a record is retained on. So the file sink is added back
    and the substitution is reported. Any other single sink, stdout included, is left exactly as
    asked: AC-3 requires a sink selected alone to deliver to that sink and no other.
    """
    requested = tuple(s.lower() for s in LOG_SINKS.resolve())
    notes: list[str] = []

    unknown = [s for s in requested if s not in ALL_SINKS]
    if unknown:
        notes.append(f"ignoring unknown log sink(s) {unknown}; known sinks are {list(ALL_SINKS)}")
    selected = tuple(s for s in requested if s in ALL_SINKS)

    if not selected:
        notes.append(f"no usable log sink configured; falling back to {list(LOG_SINKS.default)}")
        selected = LOG_SINKS.default

    if selected and all(s in LOSSY_SINKS for s in selected):
        notes.append(
            f"sink selection {list(selected)} is lossy only: syslog is UDP and a dropped datagram "
            f"cannot be distinguished from one never sent, so {SINK_FILE!r} is being added (FR-2a)"
        )
        selected = (*selected, SINK_FILE)

    return selected, notes


def _build_handlers(selected: tuple[str, ...]) -> list[logging.Handler]:
    fmt = _formatter(str(LOG_FORMAT.resolve()))
    handlers: list[logging.Handler] = []

    if SINK_STDOUT in selected:
        handlers.append(logging.StreamHandler(sys.stdout))

    if SINK_FILE in selected:
        path = resolve_path(str(LOG_FILE.resolve()))
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path,
                maxBytes=int(LOG_MAX_BYTES.resolve()),
                backupCount=int(LOG_BACKUPS.resolve()),
                encoding="utf-8",
            )
        )

    if SINK_SYSLOG in selected:
        handlers.append(
            logging.handlers.SysLogHandler(
                address=(str(LOG_SYSLOG_HOST.resolve()), int(LOG_SYSLOG_PORT.resolve())),
                socktype=__import__("socket").SOCK_DGRAM,
            )
        )

    redaction = RedactionFilter(
        sensitive_values(tuple(LOG_SENSITIVE_KEYS.resolve())),
        str(LOG_REDACT_PLACEHOLDER.resolve()),
    )
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redaction)
        setattr(h, _OURS, True)
    return handlers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def is_ours(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _OURS, False))


def configure(*, force: bool = False) -> bool:
    """Install the logging configuration. Idempotent; never raises.

    Returns True when a configuration was applied. ``force`` re-applies it, which the tests need
    because ``app`` is imported once per session while a test wants a fresh configuration.

    Two properties are load-bearing and easy to lose:

    * **Additive.** Only handlers this module installed are replaced; anything else attached to
      the root logger is left alone and re-attached. That is what keeps pytest's ``caplog``
      working and is why the 94 existing test files need no changes (NFR-6).
    * **Non-fatal.** A bad sink configuration must not stop the application from serving
      (NFR-1). The failure is reported once and the process continues, because an app that
      refuses to boot because a log path is wrong has turned observability into an outage.
    """
    global _configured, _config_error_reported
    if _configured and not force:
        return False

    root = logging.getLogger()
    foreign = [h for h in root.handlers if not is_ours(h)]

    try:
        selected, notes = _selected_sinks()
        handlers = _build_handlers(selected)
        level = str(LOG_LEVEL.resolve()).upper()

        for h in list(root.handlers):
            if is_ours(h):
                root.removeHandler(h)
                h.close()
        for h in handlers:
            root.addHandler(h)
        root.setLevel(getattr(logging, level, logging.INFO))
        _configured = True

        for note in notes:
            _logger.warning("logging configuration: %s", note)
        return True
    except Exception:  # noqa: BLE001 - a logging failure must never take the app down (NFR-1)
        if not _config_error_reported:
            _config_error_reported = True
            print(
                "WARNING: logging configuration failed; continuing with default handling",
                file=sys.stderr,
            )
        return False
    finally:
        for h in foreign:
            if h not in root.handlers:
                root.addHandler(h)


def reset_for_tests() -> None:
    """Drop this module's handlers and let the next ``configure`` start clean."""
    global _configured, _config_error_reported
    root = logging.getLogger()
    for h in list(root.handlers):
        if is_ours(h):
            root.removeHandler(h)
            h.close()
    _configured = False
    _config_error_reported = False
