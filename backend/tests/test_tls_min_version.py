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
"""The TLS floor, asserted rather than inherited.

Adding TLS added a negotiation, and a negotiation that can be talked down to 1.0/1.1 is a
downgrade surface the app did not have while serving plain HTTP inside a tunnel. Both servers
default to something *other* than an explicit floor -- Python's ``SSLContext`` default is a
language-version behaviour that has moved twice, and gunicorn 23 still defaults ``ssl_version``
to the permissive ``PROTOCOL_TLS`` -- so the value is set in one place and pinned here.
"""
from __future__ import annotations

import ssl

import pytest
import tls_server


class TestTheDefaultFloor:
    def test_the_default_is_tls12(self):
        assert tls_server.DEFAULT_MIN_TLS == ssl.TLSVersion.TLSv1_2

    def test_a_built_context_carries_the_floor(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RACK_WEB_TLS_MIN_VERSION", raising=False)
        cert, key = _issue(tmp_path)
        ctx = tls_server.build_ssl_context(cert, key)
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_tls10_and_tls11_are_below_the_floor(self, tmp_path, monkeypatch):
        """Stated as the property, so the test still means something if the enum changes."""
        monkeypatch.delenv("RACK_WEB_TLS_MIN_VERSION", raising=False)
        cert, key = _issue(tmp_path)
        ctx = tls_server.build_ssl_context(cert, key)
        assert ctx.minimum_version > ssl.TLSVersion.TLSv1_1
        assert ctx.minimum_version > ssl.TLSVersion.TLSv1


class TestHardeningAnExistingContext:
    """gunicorn hands us a context it already built, so the policy must apply to that object."""

    def test_a_permissive_context_is_raised_to_the_floor(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with pytest.warns(DeprecationWarning):
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        tls_server.harden(ctx)
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_compression_is_disabled(self):
        """TLS compression is the CRIME oracle and buys nothing for JSON on a fast link."""
        ctx = tls_server.harden(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
        assert ctx.options & ssl.OP_NO_COMPRESSION

    def test_renegotiation_is_disabled_where_the_flag_exists(self):
        ctx = tls_server.harden(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
        flag = getattr(ssl, "OP_NO_RENEGOTIATION", None)
        if flag is None:  # pragma: no cover - depends on the OpenSSL build
            pytest.skip("this OpenSSL exposes no OP_NO_RENEGOTIATION")
        assert ctx.options & flag


class TestTheConfiguredOverride:
    def test_13_can_be_required(self):
        assert tls_server.configured_min_version("1.3") == ssl.TLSVersion.TLSv1_3

    @pytest.mark.parametrize("raw", ["1.2", "TLSv1.2", " 1.2 "])
    def test_12_spellings(self, raw):
        assert tls_server.configured_min_version(raw) == ssl.TLSVersion.TLSv1_2

    @pytest.mark.parametrize("raw", ["", None])
    def test_unset_means_the_default(self, raw, monkeypatch):
        monkeypatch.delenv("RACK_WEB_TLS_MIN_VERSION", raising=False)
        assert tls_server.configured_min_version(raw) == tls_server.DEFAULT_MIN_TLS

    @pytest.mark.parametrize("raw", ["1.1", "1.0", "sslv3", "yes", "garbage"])
    def test_a_bad_value_keeps_the_floor_rather_than_widening_it(self, raw):
        """A misconfiguration must not be a way to *lower* the floor. Fail safe, and warn."""
        assert tls_server.configured_min_version(raw) == ssl.TLSVersion.TLSv1_2

    def test_the_environment_is_honoured(self, monkeypatch):
        monkeypatch.setenv("RACK_WEB_TLS_MIN_VERSION", "1.3")
        assert tls_server.configured_min_version() == ssl.TLSVersion.TLSv1_3


def test_a_missing_key_fails_loudly_rather_than_serving_plaintext(tmp_path):
    """A deployment that believes it is encrypted must not silently be serving HTTP."""
    cert, _key = _issue(tmp_path)
    with pytest.raises((FileNotFoundError, ssl.SSLError, OSError)):
        tls_server.build_ssl_context(cert, str(tmp_path / "absent.key"))


def _issue(tmp_path) -> tuple[str, str]:
    """A throwaway cert/key pair via the real generator, so these agree with what ships."""
    import subprocess
    import sys

    from paths import REPO_ROOT

    out = tmp_path / "tls"
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "gen-tls-cert.py"), "--out-dir", str(out)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return str(out / "server.crt"), str(out / "server.key")
