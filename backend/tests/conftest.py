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
