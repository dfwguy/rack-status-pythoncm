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
from pathlib import Path

os.environ.setdefault("RACK_STATUS_DEMO", "1")
# NO `RACK_WEB_SECRET_KEY` is set here, deliberately. `app.py` bootstraps a real one on import —
# loading or creating `var/secrets/.rack_web_secret_key` — so a suite-wide fake was never supplying
# something missing. It was *repointing* the signing key, which is derived from it by HKDF, and that
# is INV-17: every token issued before the repoint stops verifying. Twelve files then invented their
# own per-file secret on top, so the key changed repeatedly within one run and a credential built by
# a session fixture could be dead by the time a test used it. That is the whole reason the test
# client had to start minting per request.
#
# The suite authenticates instead: `identity.testing.log_in(client)` drives the real SRP exchange
# and the token arrives in the cookie jar signed by whatever key this deployment bootstrapped, which
# is what an operator gets. `test_no_faked_signing_key.py` keeps the fakes from creeping back.
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

# The policy file needs the same isolation as the identity store, and for a sharper reason than
# tidiness: first boot now writes an administrator policy beside the administrator it creates, and
# `_bootstrap_identity_at_startup` runs when `app` is *imported*. Unset, that wrote a real
# `var/store/policies.json` into the worktree on the first test that imported the app — which then
# switched the whole suite from the permissive engine to default-deny and failed ten gate tests.
# Isolating the path is not enough on its own; see `_pre_bootstrapped_admin` below.
os.environ["RACK_WEB_POLICY_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="nvis-policy-test-"), "policies.json"
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

# Put an administrator in the isolated store *before* anything imports ``app``.
#
# Two things follow from it, and the second is the load-bearing one. It makes the suite's store
# realistic — a deployed system always has an administrator, so a test running against an empty
# one is testing a state that only exists for a few milliseconds on first boot. And because
# ``ensure_admin`` is idempotent on a populated store, the import-time bootstrap now returns
# ``None`` and the *policy* seeding beside it never fires, so the suite keeps the permissive
# engine.
#
# That is deliberately a consequence of the store being populated rather than a switch that turns
# seeding off under test. A "skip this when testing" flag would mean the seeding path the product
# depends on is the one path the suite never takes; here it is skipped for exactly the reason a
# second real boot skips it. The seeding itself is covered by
# ``test_identity_bootstrap.py::TestABrandNewDeploymentEnforcesFromTheFirstBoot``, which points
# both paths at its own tmp files.
import identity.bootstrap as _identity_bootstrap  # noqa: E402
from identity.store import IdentityStore as _IdentityStore  # noqa: E402

_bootstrap_store = _IdentityStore(os.environ["NVIS_IDENTITY_FILE"])
try:
    _identity_bootstrap.ensure_admin(_bootstrap_store, emit=False)
finally:
    _bootstrap_store.close()


# NO policy documents are seeded, so the engine stays `AllowAllEngine` for the suite at large.
#
# That is a deliberate boundary between two migrations. Removing the authentication bypass is done:
# every client presents a real credential and the gate verifies it. Making the *whole* suite run
# under default-deny is a different and much larger job, because a policy decision needs an
# attachment in whichever identity store the test is using — and dozens of fixtures build their own.
# Seeding here made 49 tests fail on authorization inside files that are about something else.
#
# Authorization is therefore tested where it belongs: `test_authz_*.py` and `test_policies_api.py`
# build a real policy file and assert real verdicts. Track the suite-wide enforcement separately
# from FR-18c.
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _every_test_client_authenticates():
    """Install the authenticated client globally, so authentication is the default (FR-18c).

    **Opting out is now the explicit act, which is the right way round.** Previously opting *in* was
    explicit and almost nobody did: 25 modules hard-assigned `RACK_WEB_AUTH_DISABLED=1`, and because
    the environment is process-global and pytest imports every module during collection, one of them
    switched the gate off for the whole session. The suite reported 3,400 passes against a product
    with authentication disabled.

    `app.test_client()` already routes through `test_client_class`, which is why this reaches the
    ~50 places that build their own client without editing any of them.

    A test that wants the 401 path asks for it: `headers=identity.testing.unauthenticated()`.
    """
    # `app` FIRST, and the order is load-bearing: importing it runs
    # `_bootstrap_rack_web_secret_key()`, which loads or creates `var/secrets/.rack_web_secret_key`
    # and puts it in the environment. Enrolling before that point tries to derive a signing key from
    # a secret that does not exist yet and raises `TokenError`. This used to be hidden by a faked
    # key set at module scope, which is exactly what INV-17 punishes.
    import app as app_module
    from flask.testing import FlaskClient
    from identity import revocation
    from identity import testing as identity_testing

    # `tokens.verify` refuses everything until a checker is registered (FR-6a, INV-18), and without
    # this every request 401s in a way that reads like a gate bug rather than a missing wire.
    revocation.install()
    principal = identity_testing.enrol("suite-operator", display_name="Suite Operator")

    class _SuiteClient(identity_testing.AuthenticatedClient, FlaskClient):
        nvis_bearer = principal.bearer

    app_module.app.config["TESTING"] = True
    previous = app_module.app.test_client_class
    app_module.app.test_client_class = _SuiteClient
    try:
        yield principal
    finally:
        app_module.app.test_client_class = previous


#: Committed catalogs for unit tests, so a test never reads the developer's own `var/store`.
FIXTURES = Path(__file__).resolve().parent / "fixtures"


#: The published action vocabulary — `docs/action-vocabulary.json`, generated from the live registry
#: by `bin/gen-action-vocabulary.py` and gated in CI.
VOCABULARY = Path(__file__).resolve().parents[2] / "docs" / "action-vocabulary.json"


@pytest.fixture(scope="session")
def vocabulary() -> dict:
    """Services and actions, read from the document rather than named by hand.

    **A test should not spell an action name from memory.** One did — `rack:read`, which has never
    existed; the real action is `rack:list` — and it passed review because nothing checks a string
    literal against the vocabulary. The registry was always the runtime authority, but it was not
    written down anywhere a test could read without starting the application, so the alternative to
    guessing was importing `app`.

    Use `an_allowed_read` / `a_mutation` below for the common case: a test that needs "some read"
    and "some mutation" should not care which, and picking one from the document means a renamed
    action changes the fixture rather than breaking an unrelated assertion.
    """
    import json

    assert VOCABULARY.is_file(), (
        f"{VOCABULARY} is missing. Run bin/gen-action-vocabulary.py — it is generated from the "
        f"action registry and gated in CI."
    )
    return json.loads(VOCABULARY.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def an_allowed_read(vocabulary) -> str:
    """A read action that genuinely exists, chosen deterministically.

    Sorted and first, not random: a test that picks a different action per run reports a different
    failure per run, and the first thing anyone does with that is disbelieve the test.
    """
    reads = sorted(
        entry["action"]
        for service in vocabulary["services"]
        for entry in service["actions"]
        if entry["kind"] == "read"
    )
    assert reads, "the vocabulary declares no read actions"
    return reads[0]


@pytest.fixture(scope="session")
def a_mutation(vocabulary) -> str:
    """A mutating action that genuinely exists, chosen the same way."""
    mutations = sorted(
        entry["action"]
        for service in vocabulary["services"]
        for entry in service["actions"]
        if entry["kind"] == "mutate"
    )
    assert mutations, "the vocabulary declares no mutating actions"
    return mutations[0]


@pytest.fixture
def catalog_from_fixture(tmp_path, monkeypatch):
    """Point the catalog at a committed fixture, copied so the tracked file stays pristine.

    **A unit test must not depend on a file outside the tests.** Several did, invisibly: the
    required-setting predicates resolve the *active cluster's* connection — correctly, so a cluster
    supplying a value through its overlay is not refused — which meant they were reading whatever
    `var/store/catalog.json` happened to hold. Running the app locally activated a cluster with a
    real `ssh_host`, and tests asserting that `RACK_SSH_HOST` is required began failing on that
    machine and only that machine.

    Copied rather than used in place because the store *seeds* platforms and tools into whichever
    file it opens; pointing at the fixture directly would have a test rewrite a tracked input.

    The generation bump is the non-obvious half. `catalog/context.overlays_for_active()` caches the
    overlays keyed on the store's generation counter, so repointing `NVIS_CATALOG_FILE` alone
    changes nothing that has already been resolved — which is why isolating the path looked like it
    had no effect at all.

    Integration tests that validate a real deployment should NOT take this fixture: exercising the
    deployment's own configuration is their entire purpose.
    """
    def use(name: str) -> Path:
        source = FIXTURES / name
        assert source.is_file(), f"no such catalog fixture: {name}"
        target = tmp_path / "catalog.json"
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setenv("NVIS_CATALOG_FILE", str(target))

        from catalog import store as catalog_store

        # Drop the open repo, or nothing reopens. The store is a module-level singleton, so
        # repointing `NVIS_CATALOG_FILE` alone leaves the *previous* file open and the active
        # project is whatever was seeded when the session started — which is why isolating the path
        # appeared to have no effect at all.
        catalog_store.set_repo(None)
        catalog_store._bump_generation()
        return target

    yield use

    from catalog import store as catalog_store

    # Dropped again on the way out, so the next test does not inherit a repo pointing at a tmp file
    # that is about to be deleted.
    catalog_store.set_repo(None)
    catalog_store._bump_generation()


#: One pinned image per launch that stages one, keyed by the id its catalog ``required_by`` names.
#: Deliberately NOT the shipped digests: a launch path that still carried a literal would pass a
#: test that used the real value, so these are fakes only the catalog can supply.
PRESET_IMAGES = {
    "nemo-26.04.01": ("nvidia+nemo+26.04.01-arm.sqsh", 42245001216, "a1" * 32),
    "exemplar-system-info": ("nvidia+nemo+26.04.01-arm.sqsh", 42245001216, "a1" * 32),
    "llmb-nccl-container": ("nvidia+nemo+26.04.01-arm.sqsh", 42245001216, "a1" * 32),
    "llama31-405b-fp8": ("nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh", 42488647680, "b2" * 32),
    "mnubergemm-26.03-v1.17": ("mnubergemm-26.03-v1.17-arm.sqsh", 13949554688, "c3" * 32),
    "gpt-oss-cpu": ("tensorrt-llm+release+1.1.0rc5.sqsh", 30284881920, "d4" * 32),
}


@pytest.fixture
def preset_image_catalog(monkeypatch):
    """Give the active cluster an image catalog that names an image for every staging launch.

    A preset's image is the catalog entry whose ``required_by`` names it (``validation_presets
    .launch_image``), so a test that prepares one needs a catalog, exactly as a cluster does.
    Installs the full catalog and returns ``use``: ``use()`` re-installs a fresh full catalog and
    returns it, ``use(catalog)`` installs a test's own.
    """
    import p2p_catalog
    from catalog import models

    def full() -> models.P2pCatalog:
        by_file: dict[str, models.P2pCatalogImage] = {}
        for consumer, (filename, size, sha) in PRESET_IMAGES.items():
            image = by_file.setdefault(filename, models.P2pCatalogImage(
                order=len(by_file) + 1, name=filename, filename=filename, bytes=size, sha256=sha))
            image.required_by.append(consumer)
        return models.P2pCatalog(cluster_id="default", images=list(by_file.values()))

    def use(catalog: models.P2pCatalog | None = None) -> models.P2pCatalog:
        installed = catalog if catalog is not None else full()
        monkeypatch.setattr(p2p_catalog, "get_catalog", lambda _cid, _repo=None: installed)
        return installed

    use()
    return use


@pytest.fixture(autouse=True)
def _restore_the_authorization_engine():
    """Put a known engine back after every test.

    The engine is **module state**, so `monkeypatch` cannot restore it: a test that installs
    `DenyAllEngine`, or calls `reset_for_tests()` to prove the unregistered case fails closed,
    leaves that engine installed for everything that runs afterwards. The symptom is a 403 in an
    unrelated file, only in a full run — reproduced by putting `test_authz_engine.py` before
    `test_endpoint_smoke.py`, which turned 491 passes into 14 failures.

    **Captured and put back, not re-derived.** Calling `install_default()` instead was measured and
    is worse: it reads whichever policy file the *test* had configured, so a file whose fixture
    writes documents got an enforcing engine installed between its own cases and 403ed itself. That
    took the suite from 29 failures to 62.
    """
    import authz

    before = authz.current_engine()
    try:
        yield
    finally:
        authz.set_engine(before)


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
    revocation.install()
    return nvis_test_principal
