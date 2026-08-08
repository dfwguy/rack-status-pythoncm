#!/usr/bin/env python3
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Backward-compatible shim — delegates to ``validation_activities``.

New code should import ``validation_activities`` directly. This module preserves
``RACK_MULTIRACK_JOBS_FILE`` and the ``record_job`` / ``list_jobs`` API used by
MultirackView until callers are fully migrated.
"""
from __future__ import annotations

from validation_activities import (  # noqa: F401
    ACTIVITIES_FILE as JOBS_FILE,
)
from validation_activities import (
    list_activities,
)

# Legacy kind tuple for importers that referenced multirack_jobs.KINDS
KINDS = ("nemo", "llmb")


def list_jobs(log_dir: str | None = None, limit: int = 100) -> list[dict]:
    """Multirack compat: multirack-scoped activities only."""
    return list_activities(log_dir=log_dir, scope="multirack", limit=limit)
