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
"""Watch whether the active cluster's scheduler can still accept work.

**It observes; it does not repair.** The daemon this was modelled on restarted a PythonCM
broker when the broker stopped answering, and that was its whole job. We did not take the
broker (`docs/explanation/why-no-pythoncm-broker.md`), so there is nothing here to restart
— and a component that claimed to be a watchdog while being unable to act would be worse
than one that says plainly what it is. The value left is real but narrower: an operator can
ask *when* the scheduler stopped answering, which is the question a failed launch raises and
a single on-demand probe cannot answer.

**Modularity is the design constraint, not a nicety.** Greenlight serves Slurm clusters and
PythonCM clusters from one process, and the active cluster can change while it runs. So this
never asks "is PythonCM up". It asks the *active cluster's provider* whether it is ready, and
a provider with no cheap readiness check reports `ready=None` rather than being skipped or
being guessed at. A Slurm-only deployment therefore gets an honest "not applicable" instead
of a check that silently does nothing, and switching clusters switches what is watched.

Samples are held **in memory only**, deliberately. The value of this record decays in
minutes — it answers "is it healthy now, and when did that change" — and a restart is itself
the kind of event that invalidates it. Persisting would mean a fifth storage location
(`storage-engines.mdc`) for data nobody queries after the fact.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from config_env import _eget, _eget_int

_logger = logging.getLogger(__name__)

#: Seconds between samples. Long enough that the check is never the load, short enough that
#: an outage is bounded by a couple of minutes rather than discovered by a failed launch.
DEFAULT_INTERVAL_SEC = 60

#: How many samples to keep. At the default interval this is about two hours, which covers
#: "it broke while I was in a meeting" without holding a session's worth of memory.
MAX_SAMPLES = 120

_samples: deque[dict[str, Any]] = deque(maxlen=MAX_SAMPLES)
_lock = threading.Lock()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def interval_sec() -> int:
    return max(10, _eget_int("RACK_SCHEDULER_WATCH_INTERVAL_SEC", DEFAULT_INTERVAL_SEC))


def enabled() -> bool:
    """Off in demo mode, and switchable off anywhere.

    Demo mode has no cluster to reach, so a watcher there would produce a wall of
    unreachable samples describing nothing.
    """
    if _eget("RACK_STATUS_DEMO", "").strip() in ("1", "true", "yes"):
        return False
    return _eget("RACK_SCHEDULER_WATCH_DISABLED", "").strip().lower() not in ("1", "true", "yes")


def sample_once() -> dict[str, Any]:
    """Take one reading. Never raises: an unreachable scheduler is the thing being recorded."""
    # Imported here rather than at module scope: `schedulers.factory` builds a connection,
    # which reads configuration that is not resolved yet while this module is being imported.
    from schedulers.factory import build_scheduler

    started = time.monotonic()
    reading: dict[str, Any] = {"at": _now(), "scheduler": "", "ready": None, "detail": ""}
    try:
        scheduler = build_scheduler()
        reading["scheduler"] = scheduler.kind
        probe = getattr(scheduler, "probe", None)
        if probe is None:
            # Not a failure and not a skip. A batch scheduler has no readiness call cheaper
            # than submitting, and "ready" asserted on no evidence is worse than "unknown".
            reading["detail"] = f"{scheduler.kind} has no readiness probe"
        else:
            result = probe()
            reading["ready"] = bool(result.get("ready", True))
    except Exception as exc:  # noqa: BLE001 - every backend fails differently; all are data
        reading["ready"] = False
        reading["error"] = f"{type(exc).__name__}: {exc}"[:300]
    reading["ms"] = int((time.monotonic() - started) * 1000)
    return reading


def record(reading: dict[str, Any]) -> None:
    with _lock:
        _samples.append(reading)


def history() -> list[dict[str, Any]]:
    with _lock:
        return list(_samples)


def summary() -> dict[str, Any]:
    """Current state plus when it last changed — the two things an operator asks for."""
    samples = history()
    if not samples:
        return {"watching": enabled(), "samples": 0, "current": None, "since": "", "changed_at": ""}

    latest = samples[-1]
    # Walk back while the verdict is unchanged, so `since` is when this state began rather
    # than when we last looked. "Unreachable for 40 minutes" and "unreachable once" are
    # different incidents and the newest sample alone cannot tell them apart.
    since = latest["at"]
    for previous in reversed(samples[:-1]):
        if previous.get("ready") != latest.get("ready"):
            break
        since = previous["at"]

    return {
        "watching": enabled(),
        "samples": len(samples),
        "current": latest,
        "since": since,
        "unhealthy_count": sum(1 for s in samples if s.get("ready") is False),
    }


def _loop() -> None:
    # A first sample immediately would race the startup bootstraps that resolve the active
    # cluster, and would record an unreachable scheduler that is merely not configured yet.
    time.sleep(30)
    while True:
        if enabled():
            try:
                reading = sample_once()
                record(reading)
                if reading.get("ready") is False:
                    _logger.warning(
                        "scheduler watch: %s not ready (%s)",
                        reading.get("scheduler") or "?",
                        reading.get("error") or reading.get("detail") or "no detail",
                    )
            except Exception:
                # The sampler already swallows backend failures, so reaching here means the
                # watcher itself is broken. Log and keep the thread alive: a dead watcher
                # that stops recording looks exactly like a healthy cluster.
                _logger.exception("scheduler watch loop error")
        time.sleep(interval_sec())


def ensure_background_thread() -> None:
    """Start the watcher, or restart it if it died."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        if _thread is not None:
            _logger.warning("scheduler watch thread died; restarting")
        _thread = threading.Thread(target=_loop, name="scheduler-watch", daemon=True)
        _thread.start()
        _logger.info(
            "scheduler watch thread started (interval %ss, enabled=%s)", interval_sec(), enabled()
        )
