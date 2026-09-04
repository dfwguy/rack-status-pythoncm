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
# MIGRATING OFF THIS, FILE BY FILE. `identity/testing.py` (FR-18b) now issues real credentials
# and every test client presents one, so a file can run with the gate genuinely ON by taking the
# `gated` fixture below. That is the end state: a suite that switches authentication off is not
# testing the product, it is testing a configuration no deployment uses (SUTP gap G-10).
#
# Flipping all ~2,500 at once was measured and is a migration, not a switch: 154 tests fail in
# three identifiable clusters -- files that reset the revocation checker (fail-closed then refuses
# every token, correctly), browser routes that redirect rather than 401, and requests whose target
# the resource resolver cannot name. Each cluster needs its own fix, and landing a red suite would
# block everyone else meanwhile.
#
# So the bypass stays until FR-18c, and migrated files opt IN. Track the remaining clusters in
# `.plc/briefs/auth-identity-brief.md` FR-18c.
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_WEB_SECRET_KEY", "nvis-test-suite-signing-key-not-a-real-secret")
os.environ.setdefault("RACK_EXEC_MODE", "local")
# Force a fresh, isolated catalog store (seeded on first access -> "default" project
# with the default demo rack-id convention). Drop any inherited rack-id template.
os.environ.pop("NVIS_RACK_ID", None)
os.environ["NVIS_CATALOG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="nvis-catalog-test-"), "catalog.json"
)

# Same reasoning as the catalog, with sharper teeth: the startup bootstrap mints a real
# administrator credential into whatever this points at. Unset, that is `var/identity/identity.json`
# inside the worktree -- so every test run would write a live credential into the repo and print it
# into the test output. Isolating it here also keeps the 4096-bit SRP enrolment (~50 ms) off every
# session but the first.
os.environ["NVIS_IDENTITY_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="nvis-identity-test-"), "identity.json"
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


@pytest.fixture(autouse=True)
def _reset_request_throttle():
    """Clear the global throttle buckets between tests.

    The limiter is deliberately process-global -- that is what makes it a limit -- so without this
    one test's burst spends the allowance the next test needs, and the failure surfaces in an
    unrelated file. Forty-five tests failed exactly that way when the throttle first landed.
    """
    import throttle

    throttle.registry().reset_for_tests()
    yield
    throttle.registry().reset_for_tests()


# --- Authenticated by default (FR-18b) -----------------------------------------------------------


@pytest.fixture(scope="session")
def nvis_test_principal():
    """One real enrolled account for the session (FR-18b).

    Session-scoped because SRP enrolment is a 4096-bit exponentiation (~50 ms); per-test it would
    add minutes and no test needs a *different* credential unless it asks. A test that wants its
    own principal -- different groups, a second identity -- calls `identity.testing.enrol()`.

    NOT autouse, and the test client is NOT globally authenticated. The first version installed an
    authenticating `app.test_client_class` for the whole suite, and it silently turned **six real
    401 tests green** in `test_web_auth_gate.py`: a client that always presents a credential cannot
    test the unauthenticated path. That is the same mistake as the blanket bypass, pointed the
    other way -- so authentication is opt-IN, per test, and a file that does not ask for it is
    unchanged.
    """
    from identity import testing as identity_testing

    return identity_testing.enrol()


@pytest.fixture
def authed_client(nvis_test_principal, monkeypatch):
    """A test client that authenticates for real, through the real gate (FR-18b).

    The migration handle: a file moved off the bypass takes this instead of building its own
    client, and gets the product's actual behaviour -- the gate verifies a genuine token, the
    authorizer runs, and the resource resolver names the target.

    Installs the revocation checker because `tokens.verify` refuses **everything** until one is
    registered (FR-6a; `identity-auth-traps.mdc` INV-18). Without it every request 401s and reads
    like a gate bug rather than a missing wire.

    `identity.testing.unauthenticated()` is how a test using this client asks for the 401 path.
    """
    from flask.testing import FlaskClient
    from identity import revocation
    from identity import testing as identity_testing

    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "0")
    revocation.install()

    class _NvisAuthedClient(identity_testing.AuthenticatedClient, FlaskClient):
        nvis_bearer = nvis_test_principal.bearer

    import app as app_module

    app_module.app.config["TESTING"] = True
    monkeypatch.setattr(app_module.app, "test_client_class", _NvisAuthedClient)
    with app_module.app.test_client() as client:
        client.nvis_principal = nvis_test_principal
        yield client


@pytest.fixture
def test_principal(nvis_test_principal):
    """The session's authenticated identity, for a test that needs its login or password."""
    return nvis_test_principal


@pytest.fixture
def gated(monkeypatch, nvis_test_principal):
    """Turn the real authentication gate ON for one test or file (FR-18b).

    The migration handle. A file that has been moved off the bypass takes this and gets the
    product's actual behaviour: the gate verifies a real token, the authorizer runs, and the
    resource resolver names the target. `identity.testing.unauthenticated()` is how a test inside
    such a file asks for the 401 path.

    Installs the revocation checker, because `tokens.verify` refuses **everything** until one is
    registered (FR-6a, and `identity-auth-traps.mdc` INV-18). A file that resets revocation state
    and forgets this sees every request 401 and reads it as a gate bug.
    """
    from identity import revocation

    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "0")
    revocation.install()
    return nvis_test_principal
