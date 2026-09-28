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
"""The three prerequisites Greenlight refuses to serve without.

Two properties carry the design and each has a test that would fail if it were reversed:
**bootstrap runs before the gate** (the other order refuses a brand-new deployment for lacking the
administrator that starting would have created), and **the gate hangs off serving rather than
importing** (importing `app` is how the route snapshots and the tracked-tree check work, and
neither accepts a request).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import startup_gate

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def deployment(tmp_path, monkeypatch):
    """An empty deployment: no store, no policy, no certificate."""
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity" / "identity.json"))
    monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(tmp_path / "store" / "policies.json"))
    monkeypatch.setenv("NVIS_CATALOG_FILE", str(tmp_path / "catalog.json"))
    monkeypatch.setenv("RACK_WEB_TLS_DIR", str(tmp_path / "tls"))
    # Isolates the operator-supplied rung (`config/tls/`) from the developer's real tree.
    monkeypatch.setenv("NVIS_RACKSTATUS_CONFIG", str(tmp_path / "config" / "rackstatus.toml"))
    monkeypatch.delenv("RACK_WEB_TLS_CERT", raising=False)
    monkeypatch.delenv("RACK_WEB_TLS_KEY", raising=False)
    return tmp_path


class TestItNamesEveryMissingPrerequisiteAtOnce:
    def test_an_empty_deployment_reports_all_three(self, deployment):
        faults = startup_gate.collect()
        summary = " ".join(f.what for f in faults)
        assert "TLS" in summary
        assert "identity" in summary
        assert "policy" in summary

    def test_each_fault_carries_a_fix(self, deployment):
        """An operator bringing up a deployment should not have to guess. The whole value of
        reporting all three at once is lost if none of them says what to do."""
        for fault in startup_gate.collect():
            assert fault.fix.strip(), fault.what

    def test_require_refuses_with_the_config_exit_code(self, deployment):
        with pytest.raises(SystemExit) as raised:
            startup_gate.require()
        # A string payload, so the message reaches stderr and the process still exits non-zero.
        assert "refusing to start" in str(raised.value)

    def test_require_can_report_without_exiting(self, deployment):
        assert startup_gate.require(exit_on_fault=False)


class TestBootstrapRunsBeforeTheGate:
    """The ordering bug the first version shipped: gate-then-bootstrap refuses a brand-new
    deployment for lacking exactly what starting would have created."""

    def test_a_brand_new_deployment_is_allowed_to_start(self, deployment):
        assert startup_gate.prepare() == []

    def test_it_creates_the_administrator_and_the_policy(self, deployment):
        startup_gate.prepare()

        from authz import store as policy_store
        from identity import bootstrap as identity_bootstrap
        from identity.store import IdentityStore

        store = IdentityStore()
        try:
            assert not identity_bootstrap.needed(store), "no administrator was created"
        finally:
            store.close()
        assert [d.id for d in policy_store.read_all()] == [identity_bootstrap.ADMIN_POLICY_ID]

    def test_it_generates_a_self_signed_certificate(self, deployment):
        """Phase 0.5's decision is already "self-signed by default", so refusing to start for want
        of a certificate the product can generate would be a gate that teaches people to route
        around it."""
        startup_gate.prepare()
        cert = Path(startup_gate.os.environ["RACK_WEB_TLS_CERT"])
        assert cert.is_file()
        assert cert.read_text().startswith("-----BEGIN CERTIFICATE-----")

    def test_it_is_idempotent(self, deployment):
        startup_gate.prepare()
        first = Path(startup_gate.os.environ["RACK_WEB_TLS_CERT"]).read_bytes()
        assert startup_gate.prepare() == []
        assert Path(startup_gate.os.environ["RACK_WEB_TLS_CERT"]).read_bytes() == first


class TestAnExistingDeploymentWithNoPolicyIsRefused:
    """The one case bootstrap deliberately does not cover.

    On a store that already holds accounts we cannot know which of them should hold full access, so
    inventing a policy would either lock the operators out or hand somebody everything. Refusing and
    naming the fix is the reviewed decision.
    """

    def test_it_refuses_rather_than_guessing(self, deployment):
        from identity import bootstrap as identity_bootstrap
        from identity.store import IdentityStore

        store = IdentityStore()
        try:
            identity_bootstrap.ensure_admin(store, emit=False)
        finally:
            store.close()

        # `bootstrap=False` also declines to generate a certificate, so TLS is reported too. The
        # assertion is about the policy fault being present and the *identity* fault being absent —
        # the store has an administrator, so only the policy is genuinely missing.
        faults = {f.what for f in startup_gate.prepare(bootstrap=False, exit_on_fault=False)}
        assert "no authorization policy" in faults
        assert not any("identity" in what for what in faults), faults

    def test_no_bootstrap_creates_nothing(self, deployment):
        startup_gate.prepare(bootstrap=False, exit_on_fault=False)
        assert not Path(startup_gate.os.environ["NVIS_IDENTITY_FILE"]).exists()


class TestAMalformedPolicySetIsWorseThanAnAbsentOne:
    def test_it_is_reported_rather_than_treated_as_empty(self, deployment):
        """Treating unreadable as absent would let bootstrap overwrite a policy set somebody wrote,
        which is the one outcome worse than refusing to start."""
        policy_file = Path(startup_gate.os.environ["RACK_WEB_POLICY_FILE"])
        policy_file.parent.mkdir(parents=True, exist_ok=True)
        policy_file.write_text("{ this is not json")

        summary = " ".join(f.what for f in startup_gate.collect())
        assert "cannot be read" in summary


class TestTlsMustBeWholeOrAbsent:
    @pytest.mark.parametrize("present", ["RACK_WEB_TLS_CERT", "RACK_WEB_TLS_KEY"])
    def test_half_configured_is_named_as_such(self, deployment, monkeypatch, present):
        """A certificate without its key downgrades one server and crashes the other, which is why
        `start.sh` has refused the combination since Phase 0.5."""
        monkeypatch.setenv(present, str(deployment / "something.pem"))
        summary = " ".join(f.what for f in startup_gate.collect())
        assert "half-configured" in summary

    def test_a_configured_but_missing_file_is_named(self, deployment, monkeypatch):
        monkeypatch.setenv("RACK_WEB_TLS_CERT", str(deployment / "absent.crt"))
        monkeypatch.setenv("RACK_WEB_TLS_KEY", str(deployment / "absent.key"))
        summary = " ".join(f.what for f in startup_gate.collect())
        assert "unreadable" in summary


class TestTheGateHangsOffServingNotImporting:
    """Scoped deliberately. `bin/check-tracked-tree.sh` imports all 164 modules, `verify-deploy.sh`
    imports them in the container, and the route and action snapshots import `app` to enumerate its
    URL map. None serves a request, so none can leak a credential or accept traffic — refusing them
    would buy no safety and break every tool that inspects the app without running it.
    """

    def test_importing_app_does_not_invoke_the_gate(self):
        """This whole suite imports `app` with no certificate configured. If the gate ran at import,
        collection itself would have died."""
        import app  # noqa: F401

    def test_every_serving_entry_point_calls_it(self):
        """Three entry points, and none may trust another to have run it: gunicorn's arbiter covers
        the container and `start.sh`, `cli.py serve` covers the CLI, and `app.py`'s `__main__`
        covers the development server."""
        for path, needle in (
            ("gunicorn.conf.py", "startup_gate.prepare()"),
            ("backend/cli.py", "startup_gate.prepare("),
            ("backend/app.py", "startup_gate.prepare()"),
        ):
            assert needle in (REPO_ROOT / path).read_text(), path

    def test_the_gunicorn_hook_is_the_one_gunicorn_calls(self):
        """`on_starting` is a fixed gunicorn name. A typo would be a hook that never runs and a
        gate that silently is not there — so the name is asserted, not assumed."""
        tree = ast.parse((REPO_ROOT / "gunicorn.conf.py").read_text())
        hooks = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert "on_starting" in hooks


class TestTheCliCheckAgreesWithTheGate:
    def test_check_reports_the_same_prerequisites_it_would_refuse_for(self, deployment):
        """Two readers of one state; if they disagreed, `check` would bless a deployment that then
        refused to start."""
        import os as _os
        import subprocess
        import sys

        got = subprocess.run(
            [sys.executable, "backend/cli.py", "check", "--json",
             "--identity-file", _os.environ["NVIS_IDENTITY_FILE"],
             "--policy-file", _os.environ["RACK_WEB_POLICY_FILE"],
             "--catalog-file", _os.environ["NVIS_CATALOG_FILE"]],
            capture_output=True, text=True, cwd=REPO_ROOT, check=False,
            env={**_os.environ, "RACK_STATUS_DEMO": "1"},
        )
        payload = json.loads(got.stdout)
        assert payload["would_create_admin"] is True
        assert payload["would_create_policy"] is True
        assert payload["tls"] is False


class TestAMultiWorkerServeIsRefused:
    """NFR-5. `srp.assert_single_worker_safe()` was defined, documented in three places and unit
    tested — and called from nowhere, so the dependency was pinned only by `--workers 1` appearing
    in the Dockerfile and the CLI. That is the assumption NFR-5 exists to forbid.

    Two per-process tables depend on it: the SRP challenge table, where round trip 2 must reach the
    worker that served round trip 1, and the OIDC authorization-code store. Split either and logins
    fail about half the time, which reads as a client bug.
    """

    def test_a_second_worker_refuses_the_launch(self, monkeypatch, deployment):
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with pytest.raises(SystemExit) as caught:
            startup_gate.prepare(exit_on_fault=False)
        assert caught.value.code == 78

    def test_one_worker_is_fine(self, monkeypatch, deployment):
        monkeypatch.setenv("WEB_CONCURRENCY", "1")
        startup_gate.prepare(exit_on_fault=False)

    def test_an_absent_count_is_one_worker(self, monkeypatch, deployment):
        """The default and the pin. Refusing here would break every launch that does not set it."""
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
        startup_gate.prepare(exit_on_fault=False)

    def test_a_typo_does_not_become_an_authentication_error(self, monkeypatch, deployment):
        """A non-numeric count is gunicorn's to report. Refusing it here would turn a typo into a
        confusing message about SRP state."""
        monkeypatch.setenv("WEB_CONCURRENCY", "lots")
        startup_gate.prepare(exit_on_fault=False)

    def test_the_refusal_names_the_cause_and_the_remedy(self, monkeypatch, deployment):
        """An operator who hits this needs to know it is not a bug in their config syntax."""
        from identity import srp

        with pytest.raises(srp.SrpError) as caught:
            srp.assert_single_worker_safe(2)
        message = str(caught.value)
        assert "cannot span" in message
        assert "--workers 1" in message
