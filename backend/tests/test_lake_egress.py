# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The lake egress path does not carry credentials, and does not block a response.

Author: Rafael M. Koike & Cursor Agent ("Ada")

A PLC design validation on 2026-08-24 found this path capturing ~100 mutating endpoints -- including
`POST /api/login`, whose body is `{"password": ...}` -- verbatim into a provenance store that retains
artifacts permanently and on purpose, over plain HTTP, enabled by default.

These cases pin the controls that closed it. They are written as refusals rather than as
"redaction works", because the thing that matters is what CANNOT leave: a test asserting a redacted
field is present would still pass if a second, unredacted copy were added beside it.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lake_sink


@pytest.fixture(autouse=True)
def _clean_lake_env(monkeypatch):
    """No RACK_LAKE_* leakage between cases, and never a real POST."""
    for key in list(os.environ):
        if key.startswith("RACK_LAKE"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(lake_sink, "_warn_plaintext_once", lambda url: None, raising=True)
    yield


class TestCredentialsCannotLeave:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/login",
            "/api/login/",
            "/api/logout",
            "/api/vault/nvftp-token",
            "/api/vault/anything-added-later",
            "/api/auth-check",
        ],
    )
    def test_credential_paths_are_refused_outright(self, path):
        """Refused, not redacted. The login password arrives under a key this module would have to
        guess, so not taking the body is the only control that does not depend on guessing right."""
        assert lake_sink.never_capture(path) is True
        assert lake_sink.scrub_record({"path": path, "request_body": '{"password":"hunter2"}'}) is None

    @pytest.mark.parametrize(
        "path", ["/api/rack-power", "/api/tools/run", "/api/status", "/api/validation/launch"]
    )
    def test_ordinary_paths_are_still_captured(self, path):
        """The control must not be so broad that it turns capture off by accident."""
        assert lake_sink.never_capture(path) is False
        assert lake_sink.scrub_record({"path": path}) is not None

    def test_the_login_password_does_not_appear_anywhere_in_the_output(self):
        """The strong form: scan the whole serialised record, not just the field we expected."""
        record = {
            "path": "/api/login",
            "request_body": '{"password":"hunter2-very-secret"}',
            "query": {"password": "hunter2-very-secret"},
        }
        assert lake_sink.scrub_record(record) is None

    def test_a_secret_on_an_ordinary_endpoint_is_redacted_by_key_name(self):
        record = {
            "path": "/api/tools/run",
            "request_body": json.dumps({"rack": "p5-r01", "api_key": "s3cr3t-value", "token": "t0ken-value"}),
            "query": {"secret": "q-secret-value", "rack": "p5-r01"},
        }
        out = lake_sink.scrub_record(record)
        blob = json.dumps(out)
        assert "s3cr3t-value" not in blob
        assert "t0ken-value" not in blob
        assert "q-secret-value" not in blob
        # Structure preserved and the non-secret survives, or the provenance record is useless.
        assert "p5-r01" in blob
        assert json.loads(out["request_body"])["rack"] == "p5-r01"

    def test_a_secret_from_the_environment_is_redacted_by_value(self, monkeypatch):
        """Catches a secret whose JSON key name gives nothing away, by reusing logging_config's
        env-sourced value list rather than a second copy of it."""
        monkeypatch.setenv("RACK_SOMETHING_TOKEN", "env-sourced-secret-value")
        record = {
            "path": "/api/tools/run",
            "request_body": json.dumps({"harmless_field_name": "env-sourced-secret-value"}),
        }
        out = lake_sink.scrub_record(record)
        assert "env-sourced-secret-value" not in json.dumps(out)

    def test_a_non_json_body_is_still_value_redacted(self, monkeypatch):
        monkeypatch.setenv("RACK_OTHER_SECRET", "plaintext-secret-here")
        record = {"path": "/api/tools/run", "request_body": "not json at all plaintext-secret-here"}
        out = lake_sink.scrub_record(record)
        assert "plaintext-secret-here" not in out["request_body"]

    def test_nested_secrets_are_reached(self):
        record = {
            "path": "/api/tools/run",
            "request_body": json.dumps({"outer": {"inner": [{"password": "deep-secret"}]}}),
        }
        assert "deep-secret" not in json.dumps(lake_sink.scrub_record(record))


class TestCaptureIsOptIn:
    def test_capture_defaults_to_off(self, monkeypatch):
        """It defaulted to "actions", so a site that never heard of the lake still shipped to it.
        The only thing that made that safe was nothing listening on the default port."""
        import app as app_module

        monkeypatch.delenv("RACK_LAKE_CAPTURE", raising=False)
        assert app_module._lake_capture_mode() == "off"
        assert app_module._lake_should_capture("/api/rack-power", "POST") is False

    def test_opting_in_captures_ordinary_mutations(self, monkeypatch):
        import app as app_module

        monkeypatch.setenv("RACK_LAKE_CAPTURE", "actions")
        assert app_module._lake_should_capture("/api/rack-power", "POST") is True

    @pytest.mark.parametrize("mode", ["actions", "all"])
    def test_no_mode_can_capture_a_credential_path(self, monkeypatch, mode):
        """`all` is a debug setting; it must not be able to widen scope onto the login body."""
        import app as app_module

        monkeypatch.setenv("RACK_LAKE_CAPTURE", mode)
        assert app_module._lake_should_capture("/api/login", "POST") is False
        assert app_module._lake_should_capture("/api/vault/nvftp-token", "PUT") is False


class TestTransportIsVerified:
    def test_plain_http_uses_no_ssl_context(self):
        assert lake_sink._ssl_context("http://host.docker.internal:9410") is None

    def test_https_gets_a_verifying_context(self):
        ctx = lake_sink._ssl_context("https://lake.example:9410")
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_a_ca_bundle_is_honoured(self, monkeypatch, tmp_path):
        """Lets an internal-CA or self-signed lake be verified rather than trusted blindly.

        Uses the CA the product already ships a generator for, rather than a fixture certificate:
        that is the CA an operator would actually point this at, so the test exercises the real
        path. `gen-tls-cert.py --mode ca` writes `ca.crt` alongside a leaf.
        """
        import subprocess

        gen = Path(__file__).resolve().parents[2] / "bin" / "gen-tls-cert.py"
        subprocess.run(
            [sys.executable, str(gen), "--mode", "ca", "--out-dir", str(tmp_path)],
            check=True, capture_output=True,
        )
        ca = tmp_path / "ca.crt"
        assert ca.is_file(), f"gen-tls-cert.py --mode ca produced {sorted(p.name for p in tmp_path.iterdir())}"

        monkeypatch.setenv("RACK_LAKE_CA_BUNDLE", str(ca))
        ctx = lake_sink._ssl_context("https://lake.example:9410")
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
        loaded = ctx.get_ca_certs()
        assert loaded, "the bundle was not loaded into the context"
        # The generated CA and nothing else: proves the bundle replaced system trust rather than
        # being appended to it, which is what an air-gapped site needs.
        assert len(loaded) == 1, f"expected only the generated CA, got {len(loaded)} certs"

    def test_an_unreadable_ca_bundle_fails_closed(self, monkeypatch, tmp_path):
        """A typo in the path must not silently fall back to system trust -- that would be an
        unverified connection wearing a verified-looking configuration."""
        monkeypatch.setenv("RACK_LAKE_CA_BUNDLE", str(tmp_path / "does-not-exist.crt"))
        with pytest.raises((FileNotFoundError, OSError, ssl.SSLError)):
            lake_sink._ssl_context("https://lake.example:9410")

    def test_there_is_no_skip_verify_switch(self):
        """Asserted so nobody adds one 'temporarily'. An unverified TLS connection to a provenance
        store authenticates nothing while looking secure in a review."""
        source = Path(lake_sink.__file__).read_text(encoding="utf-8")
        for footgun in ("CERT_NONE", "check_hostname = False", "_create_unverified_context"):
            assert footgun not in source, f"{footgun} appeared in lake_sink"


class TestCaptureDoesNotBlockTheResponse:
    def test_submit_returns_without_posting(self, monkeypatch):
        """The response must not wait on the lake. With a 20 s default timeout and ~100 capturable
        endpoints, a slow lake would make the dashboard feel broken with no way to tell why."""
        started = threading.Event()
        release = threading.Event()

        def _slow(**kwargs):
            started.set()
            release.wait(timeout=5)
            return {"ok": True}

        monkeypatch.setattr(lake_sink, "push_run", _slow, raising=True)
        monkeypatch.setattr(lake_sink, "_q", None, raising=False)
        monkeypatch.setattr(lake_sink, "_worker", None, raising=False)

        out = lake_sink.submit_run(run_id="r1", workload="w")
        assert out["queued"] is True
        assert started.wait(timeout=5), "the worker never picked the record up"
        release.set()

    def test_a_full_queue_drops_and_counts(self, monkeypatch):
        """Bounded on purpose: a wedged lake must not grow memory inside a web process. A dropped
        record is counted so the loss is visible rather than silent."""
        monkeypatch.setenv("RACK_LAKE_QUEUE_MAX", "1")
        monkeypatch.setattr(lake_sink, "_q", None, raising=False)
        monkeypatch.setattr(lake_sink, "_worker", None, raising=False)
        monkeypatch.setattr(lake_sink, "_dropped", 0, raising=False)
        # No worker: hold the queue by pointing the drain at a thread that never starts.
        monkeypatch.setattr(lake_sink, "_drain", lambda: None, raising=True)

        results = [lake_sink.submit_run(run_id=f"r{i}", workload="w") for i in range(6)]
        assert any(r.get("error") == "queue full" for r in results), results
        assert lake_sink.queue_stats()["dropped"] >= 1

    def test_disabled_does_not_queue(self, monkeypatch):
        monkeypatch.setenv("RACK_LAKE_DISABLED", "1")
        assert lake_sink.submit_run(run_id="r1", workload="w")["queued"] is False

    def test_push_run_is_still_synchronous_for_provenance_callers(self, monkeypatch):
        """The results-ingest path wants the honest status back; only the request-capture path went
        async. Asserting this keeps the two contracts from being merged by accident."""
        seen: list[str] = []
        monkeypatch.setattr(lake_sink, "_post", lambda p, payload: seen.append(p) or (200, "{}"))
        out = lake_sink.push_run(run_id="r1", workload="w", parsed={"a": 1})
        assert out["ok"] is True
        assert seen == ["/v1/artifacts"], "push_run should post inline, not enqueue"


class TestHealthReportsThePosture:
    def test_health_names_tls_and_queue_state(self, monkeypatch):
        monkeypatch.setenv("RACK_LAKE_DISABLED", "1")
        out = lake_sink.health()
        assert out["tls"] is False
        assert out["ca_bundle"] is None
        assert "queue" in out and "dropped" in out["queue"]

    def test_health_reports_tls_when_configured(self, monkeypatch):
        monkeypatch.setenv("RACK_LAKE_DISABLED", "1")
        monkeypatch.setenv("RACK_LAKE_URL", "https://lake.example:9410")
        assert lake_sink.health()["tls"] is True
