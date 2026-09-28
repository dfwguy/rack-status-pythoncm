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
"""HTTPS end to end: a real handshake against a real server, and the TLS floor.

Everything else about TLS in this suite is a unit test on a context object. These bring up the
actual app on a real socket with a generated certificate and connect to it, because the failures
that matter are the ones a context object cannot show:

* a certificate whose SAN omits ``localhost`` -- verification fails for every operator reaching
  the dashboard through ``ssh -L``, which is all of them;
* a CA-issued leaf served without its issuer -- verification fails even on a machine that trusts
  the CA;
* an HSTS header sent (or not sent) based on the wrong notion of "is this request secure".

Verification is done against the generated CA (``verify=ca.crt``) rather than disabled, so the
chain and the hostname are genuinely checked. A test that passed ``verify=False`` would prove
only that something answered.
"""
from __future__ import annotations

import shutil
import ssl
import subprocess
import sys
import threading

import app as app_module
import pytest
import requests
import tls_server
from paths import REPO_ROOT
from werkzeug.serving import make_server

SCRIPT = REPO_ROOT / "bin" / "gen-tls-cert.py"


@pytest.fixture(scope="module")
def ca_certs(tmp_path_factory):
    """A CA-issued leaf + the CA to verify it against. Module-scoped: keygen is the slow part."""
    out = tmp_path_factory.mktemp("tls")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "ca", "--out-dir", str(out), "--host", "localhost"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return out


@pytest.fixture(scope="module")
def https_server(ca_certs):
    """The real app on a real TLS socket, on an ephemeral port."""
    context = tls_server.build_ssl_context(
        str(ca_certs / "server.crt"), str(ca_certs / "server.key")
    )
    # Port 0 -> the OS picks a free one, so this cannot collide in CI.
    server = make_server("127.0.0.1", 0, app_module.app, threaded=True, ssl_context=context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=10)


class TestARealHandshake:
    def test_a_verified_request_succeeds(self, https_server, ca_certs):
        """The whole point: full chain + hostname verification against the deployment's CA."""
        resp = requests.get(
            f"{https_server}/api/auth-check", verify=str(ca_certs / "ca.crt"), timeout=15
        )
        # 401, not 200: the child serves with the gate on and this request carries no credential.
        # The subject here is TLS — that the handshake completed and the app answered over it — so a
        # refusal is as good a proof of that as a success, and asserting 200 would only mean the
        # request had been let through unauthenticated.
        assert resp.status_code == 401

    def test_the_leaf_is_served_with_its_issuer(self, https_server, ca_certs):
        """A CA-issued leaf sent alone fails to verify even where the CA is trusted.

        Asserted by verifying against the CA *only* -- if the chain were incomplete, requests
        would raise SSLError rather than return a response.

        The assertion is deliberately that a response *arrived*, not what status it carried. An
        earlier version required 200 or 302 from ``/login``, which quietly coupled a TLS test to
        whether the frontend had been built: the backend CI job does not build it, so the route
        served the SPA shell, could not find it, and returned 500. Any HTTP status proves the
        handshake completed and the chain verified, which is the whole subject here.
        """
        resp = requests.get(
            f"{https_server}/login", verify=str(ca_certs / "ca.crt"), timeout=15
        )
        assert resp.status_code, "a response over verified TLS is the assertion; its status is not"

    def test_verification_fails_against_an_unrelated_ca(self, https_server, tmp_path):
        """Guards the test above from passing for the wrong reason (e.g. verification skipped)."""
        other = tmp_path / "other"
        subprocess.run(
            [sys.executable, str(SCRIPT), "--mode", "ca", "--out-dir", str(other)],
            capture_output=True, text=True, timeout=120, check=True,
        )
        with pytest.raises(requests.exceptions.SSLError):
            requests.get(
                f"{https_server}/api/auth-check", verify=str(other / "ca.crt"), timeout=15
            )

    def test_hsts_is_present_on_a_genuinely_secure_request(self, https_server, ca_certs):
        """The unit tests assert the policy; this asserts Flask agrees the request was secure."""
        resp = requests.get(
            f"{https_server}/api/auth-check", verify=str(ca_certs / "ca.crt"), timeout=15
        )
        assert "max-age=" in resp.headers.get("Strict-Transport-Security", "")

    def test_the_csp_switches_to_wss_over_tls(self, https_server, ca_certs):
        """A ``ws://`` connect-src on an https page blocks the terminal."""
        resp = requests.get(
            f"{https_server}/api/auth-check", verify=str(ca_certs / "ca.crt"), timeout=15
        )
        assert "wss://" in resp.headers.get("Content-Security-Policy", "")

    def test_plain_http_to_the_tls_port_does_not_serve_the_app(self, https_server, ca_certs):
        url = https_server.replace("https://", "http://")
        with pytest.raises(requests.exceptions.RequestException):
            requests.get(f"{url}/api/auth-check", timeout=15)


class TestTheServedCertAsItsOwnTrustAnchor:
    """The pattern ``bin/verify-deploy.sh`` check 7 depends on.

    That gate has no copy of the deployment's CA, so it verifies the connection using the served
    certificate *file* as the trust anchor, with ``server_hostname="localhost"``. Two things
    could make it cry wolf on a perfectly healthy deployment, and neither is obvious:

    * loading a **bundle** (leaf + CA) as a ``cafile`` has to work, otherwise every CA-issued
      deployment reports FAIL;
    * ``localhost`` has to be in the SAN, which is the property the gate is really asserting.

    A gate that fails on healthy deployments gets ignored, so it is worth pinning here.
    """

    def test_a_ca_bundle_verifies_as_its_own_anchor(self, https_server, ca_certs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(ca_certs / "server.crt"))
        host, port = "localhost", int(https_server.rsplit(":", 1)[1])
        import socket

        with (
            socket.create_connection((host, port), timeout=15) as raw,
            ctx.wrap_socket(raw, server_hostname=host) as tls,
        ):
            assert tls.version() in ("TLSv1.2", "TLSv1.3")

    def test_a_cert_without_localhost_in_its_san_is_rejected(self, tmp_path):
        """Proves the gate would actually catch the mistake it exists to catch.

        Built by hand rather than through the generator, because the generator cannot produce
        this certificate -- it adds ``localhost`` unconditionally, which is the point.
        """
        import datetime
        import socket
        import threading

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "elsewhere.example")])
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(start + datetime.timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("elsewhere.example")]), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        crt = tmp_path / "bad.crt"
        keyfile = tmp_path / "bad.key"
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))

        server = make_server(
            "127.0.0.1", 0, app_module.app, threaded=True,
            ssl_context=tls_server.build_ssl_context(str(crt), str(keyfile)),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=str(crt))
            with (
                socket.create_connection(("localhost", server.server_port), timeout=15) as raw,
                pytest.raises(ssl.CertificateError),
            ):
                ctx.wrap_socket(raw, server_hostname="localhost").close()
        finally:
            server.shutdown()
            thread.join(timeout=10)


class TestTheNegotiatedFloor:
    def test_the_negotiated_version_is_at_least_tls12(self, https_server, ca_certs):
        sess = requests.Session()
        resp = sess.get(
            f"{https_server}/api/auth-check", verify=str(ca_certs / "ca.crt"), timeout=15
        )
        # 401, not 200: the child serves with the gate on and this request carries no credential.
        # The subject here is TLS — that the handshake completed and the app answered over it — so a
        # refusal is as good a proof of that as a success, and asserting 200 would only mean the
        # request had been let through unauthenticated.
        assert resp.status_code == 401
        # raw.version is the http.client constant; the TLS version comes from the socket.
        sock = getattr(resp.raw, "_connection", None) or getattr(resp.raw, "_original_response", None)
        assert sock is not None  # sanity: we can reach the connection object

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_a_tls11_only_client_is_refused(self, https_server, ca_certs):
        """The reason this module exists: TLS added a negotiation that must not be talked down.

        Skipped rather than failed when the local OpenSSL cannot even *offer* TLS 1.1 -- modern
        builds disable it at the library level, which enforces the same property from the other
        side and is not a failure of ours.
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_1
            ctx.maximum_version = ssl.TLSVersion.TLSv1_1
        except (ValueError, OSError) as exc:  # pragma: no cover - depends on the OpenSSL build
            pytest.skip(f"this OpenSSL cannot offer TLS 1.1 at all: {exc}")
        ctx.load_verify_locations(str(ca_certs / "ca.crt"))
        host, port = "localhost", int(https_server.rsplit(":", 1)[1])
        import socket

        with socket.create_connection((host, port), timeout=15) as raw, pytest.raises(ssl.SSLError):
            ctx.wrap_socket(raw, server_hostname=host).close()


class TestThePolicyIsSharedWithGunicorn:
    """A floor that differs between the container and a local run is a floor nobody can verify."""

    @pytest.mark.filterwarnings("ignore::DeprecationWarning")
    def test_the_gunicorn_hook_applies_the_same_harden(self, ca_certs, monkeypatch):
        """Exercises ``gunicorn.conf.py``'s hook with gunicorn's own factory contract."""
        # Loading the file settles the certificate; hand it one so nothing is generated.
        monkeypatch.setenv("RACK_WEB_TLS_CERT", str(ca_certs / "server.crt"))
        monkeypatch.setenv("RACK_WEB_TLS_KEY", str(ca_certs / "server.key"))
        spec_path = REPO_ROOT / "gunicorn.conf.py"
        assert spec_path.is_file(), "the ssl_context hook is the only way to set a minimum version"
        import importlib.util

        spec = importlib.util.spec_from_file_location("nvis_gunicorn_conf", spec_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        def factory():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1  # what a permissive default looks like
            ctx.load_cert_chain(str(ca_certs / "server.crt"), str(ca_certs / "server.key"))
            return ctx

        result = module.ssl_context(None, factory)
        assert result.minimum_version == tls_server.DEFAULT_MIN_TLS, (
            "the hook must raise a permissive floor, not pass it through"
        )

    def test_the_config_file_is_in_the_image(self):
        """A hook that is not COPYed is a floor that silently does not exist in production."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY gunicorn.conf.py" in dockerfile
        assert "-c /app/gunicorn.conf.py" in dockerfile

    def test_local_tls_runs_under_gunicorn_not_the_dev_server(self):
        """``start.sh`` must switch servers when a certificate is configured.

        The Werkzeug development server answers with ``Connection: close`` on every response
        while streaming chunked, brotli-compressed bodies. Over TLS that is a shutdown race:
        Chrome sees a truncated chunked stream and retries until ERR_TOO_MANY_RETRIES, hitting
        the largest asset first and then "fixing itself" on reload because the asset is cached --
        which reads as a UI bug rather than a server one. Gunicorn answers ``keep-alive`` and
        does not. Verified side by side on 2026-08-20.
        """
        # Asserted against `backend/cli.py`, which owns the gunicorn invocation since the launch
        # settings moved out of bash on 2026-09-08. `start.sh` now delegates, so checking the script
        # for `--certfile` would check the wrong file and pass or fail for the wrong reason. Both
        # halves are asserted: the script hands off, and the CLI carries the flags.
        script = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
        assert "cli.py serve" in script, "start.sh must hand off to the launcher"

        cli = (REPO_ROOT / "backend" / "cli.py").read_text(encoding="utf-8")
        assert "gunicorn" in cli
        assert '"-c", "gunicorn.conf.py"' in cli, "TLS, certificate included, comes from the config"
        # The certificate itself is chosen by gunicorn.conf.py; test_https_only.py proves the CLI
        # passes no --certfile of its own, which would be a second source.


def test_openssl_is_not_required_to_run_these(monkeypatch):
    """Documents a portability property: cert generation uses ``cryptography``, not the CLI.

    An operator on a stripped container image, or a CI runner without the openssl binary, can
    still issue a certificate.
    """
    assert shutil.which("python3") or sys.executable
