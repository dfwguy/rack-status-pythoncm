# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Session-wide test setup.

Isolate the catalog TinyDB store to a throwaway temp file so the suite never
inherits the developer's *active project*. The active project materializes
``NVIS_RACK_ID`` (and other identity knobs) into ``os.environ`` at ``app`` import
time; without isolation a local Boston/MEL01 store would override the demo-mode
rack-id convention and break tests that assume ids like ``p5-r01``.

This module is imported by pytest before any test module (and thus before
``from app import app``), so setting ``NVIS_CATALOG_FILE`` here is authoritative.
"""
import os
import tempfile

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")
# Most historical unit tests exercise the retained compatibility implementation.
# Production defaults fail safe to PythonCM; legacy test intent is explicit here.
os.environ.setdefault("RACK_SCHEDULER", "slurm")
# Force a fresh, isolated catalog store (seeded on first access -> "default" project
# with the default demo rack-id convention). Drop any inherited rack-id template.
os.environ.pop("NVIS_RACK_ID", None)
os.environ["NVIS_CATALOG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="nvis-catalog-test-"), "catalog.json"
)

# Validation-activity writes also maintain the append-only multi-rack ledger. Keep its
# generated document/outbox out of the worktree and never let unit tests call lakewriter.
_ledger_test_root = tempfile.mkdtemp(prefix="nvis-multirack-ledger-test-")
os.environ["RACK_MULTIRACK_LEDGER_FILE"] = os.path.join(_ledger_test_root, "runs.json")
os.environ["RACK_MULTIRACK_LEDGER_MARKDOWN_FILE"] = os.path.join(
    _ledger_test_root, "MULTIRACK_VALIDATION_RUNS.md"
)
os.environ["RACK_MULTIRACK_LEDGER_EXPORT_STATUS_FILE"] = os.path.join(
    _ledger_test_root, "parquet-status.json"
)
os.environ["RACK_MULTIRACK_LEDGER_LAKE_DISABLED"] = "1"
os.environ["RACK_VALIDATION_ACTIVITIES_ARCHIVE_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="nvis-validation-activity-history-test-"),
    "validation_activities.sqlite3",
)

# Isolate AI settings so tests exercise the deterministic demo/registry path and do
# not inherit a developer's locally-configured API key (which would enable live AI).
os.environ.pop("RACK_AI_API_KEY", None)
os.environ["RACK_AI_SETTINGS_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="nvis-ai-test-"), "ai_settings.json"
)

# Keep the NVFTP sync daemon's *work* out of the suite. Importing ``app`` starts a
# thread that sleeps 30s and then calls run_sync() on a timer; a full run takes
# about a minute, so it wakes mid-suite and races the nvftp tests, which
# monkeypatch the very module globals (CACHE_DIR, SYNC_STATE_FILE) it reads —
# producing a failure that moved between tests from run to run.
#
# The loop body is replaced rather than ``ensure_background_thread`` because tests
# cover the thread lifecycle itself and patch this same hook locally. Done at
# import time because the thread starts when ``app`` is imported, during
# collection, before any fixture could intervene. RACK_NVFTP_SYNC_DISABLED is not
# equivalent: it would also switch off the sync behaviour the tests assert on.
import nvftp_cache_sync  # noqa: E402

nvftp_cache_sync._background_loop = lambda: None

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_naming_caches():
    """Drop the naming / job-env caches around every test.

    ``native_status`` resolves ``NVIS_RACK_ID`` through its own mtime-keyed subset of
    ``job_env_overrides.json`` and feeds it to an ``lru_cache``'d ``get_nvis_naming()``. Both
    outlive a test, so a module that monkeypatches the templates could read a naming instance
    another module had already warmed -- the same "failure that moved between tests from run to
    run" this file documents above, seen on ``test_extract_rack_ids_from_cmsh_text_cbr_rk_lines``
    and the catalog context tests.

    ``invalidate_job_env_file_cache()`` is the module's own documented reset hook (it clears the
    subset, the ds regex and the naming cache), so this weakens no assertion -- it only stops one
    test's naming state from leaking into the next.
    """
    import native_status

    native_status.invalidate_job_env_file_cache()
    yield
    native_status.invalidate_job_env_file_cache()
