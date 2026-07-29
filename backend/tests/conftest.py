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
# Force a fresh, isolated catalog store (seeded on first access -> "default" project
# with the default demo rack-id convention). Drop any inherited rack-id template.
os.environ.pop("NVIS_RACK_ID", None)
os.environ["NVIS_CATALOG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="nvis-catalog-test-"), "catalog.json"
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
