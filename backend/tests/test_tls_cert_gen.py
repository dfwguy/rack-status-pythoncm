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
"""``bin/gen-tls-cert.py`` — the certificate an operator actually gets.

Driven as a **subprocess**, deliberately: the CLI is the contract, and a test that imported the
module would not catch a broken argument, a bad exit code, or the file permissions.

The assertion that matters most is the SAN. Operators reach these deployments through
``ssh -L 5156:localhost:5056``, so ``localhost`` is the name the browser validates -- a
certificate without it produces a warning on every connection no matter how it was issued, and
that is precisely the kind of detail that is invisible until a customer sees it.
"""
from __future__ import annotations

import ipaddress
import stat
import subprocess
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from paths import REPO_ROOT

SCRIPT = REPO_ROOT / "bin" / "gen-tls-cert.py"


def _run(*args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if expect_ok:
        assert proc.returncode == 0, f"exit {proc.returncode}\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
    return proc


def _leaf(out_dir) -> x509.Certificate:
    """The first certificate in server.crt (the leaf; a CA-issued file is a bundle)."""
    return x509.load_pem_x509_certificate((out_dir / "server.crt").read_bytes())


def _san(cert: x509.Certificate) -> set[str]:
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(g.value) for g in ext}


@pytest.fixture
def self_signed(tmp_path):
    out = tmp_path / "tls"
    _run("--out-dir", str(out))
    return out


class TestTheSanIsWhatMakesTlsWorkOverATunnel:
    def test_localhost_and_loopback_are_present_without_being_asked_for(self, self_signed):
        """Unconditional, not options. ``localhost`` is the name a tunnelled browser checks."""
        san = _san(_leaf(self_signed))
        assert "localhost" in san
        assert "127.0.0.1" in san
        assert "::1" in san

    def test_an_extra_dns_name_is_added_as_a_dns_entry(self, tmp_path):
        out = tmp_path / "tls"
        _run("--out-dir", str(out), "--host", "nvis.example")
        cert = _leaf(out)
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert "nvis.example" in ext.get_values_for_type(x509.DNSName)

    def test_an_extra_ip_is_added_as_an_ip_entry_not_a_dns_name(self, tmp_path):
        """An IP recorded as a DNS name does not match when a browser connects by address."""
        out = tmp_path / "tls"
        _run("--out-dir", str(out), "--host", "192.168.10.3")
        ext = _leaf(out).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert ipaddress.ip_address("192.168.10.3") in ext.get_values_for_type(x509.IPAddress)
        assert "192.168.10.3" not in ext.get_values_for_type(x509.DNSName)

    def test_repeating_a_builtin_name_does_not_duplicate_it(self, tmp_path):
        out = tmp_path / "tls"
        _run("--out-dir", str(out), "--host", "localhost")
        ext = _leaf(out).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert ext.get_values_for_type(x509.DNSName).count("localhost") == 1


class TestKeyMaterialIsNotWorldReadable:
    def test_the_private_key_is_0600(self, self_signed):
        mode = stat.S_IMODE((self_signed / "server.key").stat().st_mode)
        assert mode == 0o600, oct(mode)

    def test_the_ca_key_is_0600_too(self, tmp_path):
        out = tmp_path / "tls"
        _run("--mode", "ca", "--out-dir", str(out))
        assert stat.S_IMODE((out / "ca.key").stat().st_mode) == 0o600

    def test_the_certificate_itself_is_readable(self, self_signed):
        """It is public material, and gunicorn may run as a different user than the generator."""
        assert stat.S_IMODE((self_signed / "server.crt").stat().st_mode) & stat.S_IROTH


class TestSelfSignedMode:
    def test_the_cert_is_its_own_issuer(self, self_signed):
        cert = _leaf(self_signed)
        assert cert.issuer == cert.subject

    def test_only_one_certificate_is_written(self, self_signed):
        body = (self_signed / "server.crt").read_text()
        assert body.count("BEGIN CERTIFICATE") == 1

    def test_it_is_a_server_cert_and_not_a_ca(self, self_signed):
        cert = _leaf(self_signed)
        assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku


class TestCaMode:
    @pytest.fixture
    def ca_issued(self, tmp_path):
        out = tmp_path / "tls"
        _run("--mode", "ca", "--out-dir", str(out), "--host", "nvis.example")
        return out

    def test_the_ca_pair_is_created(self, ca_issued):
        assert (ca_issued / "ca.crt").is_file()
        assert (ca_issued / "ca.key").is_file()

    def test_the_leaf_is_issued_by_the_ca(self, ca_issued):
        ca = x509.load_pem_x509_certificate((ca_issued / "ca.crt").read_bytes())
        assert _leaf(ca_issued).issuer == ca.subject

    def test_the_served_file_is_a_chain_not_a_bare_leaf(self, ca_issued):
        """gunicorn's --certfile takes a bundle, and a leaf served alone fails to verify even on
        a machine that trusts the CA -- the exact failure this bundling avoids."""
        assert (ca_issued / "server.crt").read_text().count("BEGIN CERTIFICATE") == 2

    def _ca_serial(self, out) -> int:
        return x509.load_pem_x509_certificate((out / "ca.crt").read_bytes()).serial_number

    def test_the_ca_is_reused_by_a_later_issue_not_regenerated(self, ca_issued):
        """``--force`` re-issues the LEAF. The CA must survive it.

        This caught a real bug: ``--force`` originally regenerated the CA as well, so a routine
        certificate renewal silently invalidated trust for every operator who had installed the
        old CA -- with no error and no way to notice until browsers started warning again.
        """
        before = self._ca_serial(ca_issued)
        leaf_before = _leaf(ca_issued).serial_number
        _run("--mode", "ca", "--out-dir", str(ca_issued), "--force")
        assert self._ca_serial(ca_issued) == before, "the CA must survive re-issuing a leaf"
        assert _leaf(ca_issued).serial_number != leaf_before, "the leaf should have been re-issued"

    def test_replacing_the_ca_needs_its_own_flag(self, ca_issued):
        """Possible, but never as a side effect: it invalidates trust everywhere."""
        before = self._ca_serial(ca_issued)
        _run("--mode", "ca", "--out-dir", str(ca_issued), "--force", "--force-ca")
        assert self._ca_serial(ca_issued) != before

    def test_a_leaf_issued_after_reuse_still_verifies_against_the_original_ca(self, ca_issued):
        """The point of reuse, stated as the property an operator actually depends on."""
        ca = x509.load_pem_x509_certificate((ca_issued / "ca.crt").read_bytes())
        _run("--mode", "ca", "--out-dir", str(ca_issued), "--force")
        assert _leaf(ca_issued).issuer == ca.subject

    def test_the_ca_can_sign_certificates(self, ca_issued):
        ca = x509.load_pem_x509_certificate((ca_issued / "ca.crt").read_bytes())
        bc = ca.extensions.get_extension_for_class(x509.BasicConstraints).value
        assert bc.ca is True
        assert ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign is True


class TestTheCliContract:
    def test_it_refuses_to_clobber_an_existing_cert(self, self_signed):
        """Overwriting silently would break every operator who trusts the current fingerprint."""
        proc = _run("--out-dir", str(self_signed), expect_ok=False)
        assert proc.returncode != 0
        assert "refusing to overwrite" in proc.stderr

    def test_force_overwrites(self, self_signed):
        before = _leaf(self_signed).serial_number
        _run("--out-dir", str(self_signed), "--force")
        assert _leaf(self_signed).serial_number != before

    def test_fingerprint_matches_the_certificate(self, self_signed):
        """Operators compare this string before accepting a self-signed cert; it has to be real."""
        printed = _run("--out-dir", str(self_signed), "--fingerprint").stdout.strip()
        raw = _leaf(self_signed).fingerprint(hashes.SHA256())
        assert printed == ":".join(f"{b:02X}" for b in raw)

    def test_fingerprint_of_a_missing_cert_fails_loudly(self, tmp_path):
        proc = _run("--out-dir", str(tmp_path / "nope"), "--fingerprint", expect_ok=False)
        assert proc.returncode != 0

    def test_the_output_tells_the_operator_how_to_serve_it(self, self_signed):
        """The two variables are the whole interface; printing them removes a docs round trip."""
        out = _run("--out-dir", str(self_signed), "--force").stdout
        assert "RACK_WEB_TLS_CERT=" in out
        assert "RACK_WEB_TLS_KEY=" in out
