#!/usr/bin/env python3
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
"""gen-tls-cert.py — issue the dashboard's TLS certificate.

WHY NOT ACME / LET'S ENCRYPT
    Not just air-gap, though that alone rules it out. Through an SSH tunnel the browser
    validates the name ``localhost``, so a publicly-trusted certificate for a dynamic-DNS name
    does not match either -- you would take on inbound port 80 or a DNS-provider API token on
    customer hardware, publish customer hostnames permanently to Certificate Transparency, and
    add a 90-day renewal that fails silently on a firewalled box, and *still* get a warning.

THE ONE DETAIL THAT MATTERS
    The leaf's SAN must contain ``localhost``, ``127.0.0.1`` and ``::1``. That is the name the
    browser actually checks when an operator reaches the dashboard through
    ``ssh -L 5156:localhost:5056``. Omit them and TLS is warnings-forever no matter how good
    the issuer -- which is why they are added unconditionally rather than being options.

TWO MODES
    ``self-signed`` (default) is one file pair and one browser warning per operator.
    ``ca`` creates a per-deployment CA, issues a leaf from it, and lets each operator install
    the CA certificate once -- after which verification is real and silent. Prefer it on the
    managed clusters, because training operators to click through TLS warnings is itself a
    regression: the warning that matters looks identical.

WHY THE CA IS PER-DEPLOYMENT, AND NEVER SHIPPED IN THIS REPO  (decided 2026-08-20)
    A CA is only useful for signing if its *private key* travels with it, and that key in git is
    a universal forgery key: every clone and fork could mint a certificate trusted by every
    deployment that installed it, forever, because history cannot be rotated. Same class as the
    BMC password already sitting in this repo's history. ``.gitignore`` excludes
    ``var/secrets/`` and ``scripts/check_no_vault_in_git.sh`` gates it in CI for that reason.

    Generating per deployment also contains the blast radius: one site's CA says nothing about
    the others. The tests likewise generate a throwaway CA and leaf per run -- hermetic, no
    secret to leak, and no expiry cliff that reddens CI on a morning with no related commit.

    The right answer at fleet scale is an **external PKI**: we generate a key and a CSR locally,
    something properly protected signs it, and this code never holds signing authority at all --
    then a pinned CA fingerprint is a real check rather than a shared secret. There is no such
    PKI for these deployments yet; when there is, add a ``--csr`` mode here and consume the
    signed certificate. Do not reintroduce a committed CA as a shortcut to that.

Usage:
    bin/gen-tls-cert.py                                  # self-signed, var/secrets/tls/
    bin/gen-tls-cert.py --mode ca --host nvis.example    # per-deployment CA + leaf
    bin/gen-tls-cert.py --host 192.168.10.3 --host ova   # extra SANs (IP or DNS, auto-detected)
    bin/gen-tls-cert.py --fingerprint                    # print the current cert's fingerprint

Then serve it:
    RACK_WEB_TLS_CERT=var/secrets/tls/server.crt RACK_WEB_TLS_KEY=var/secrets/tls/server.key
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import socket
import sys
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.exit(
        "cryptography is not importable.\n"
        "It is already a backend dependency, so the simplest fix is to run this inside the\n"
        "container (docker compose exec rack-status bin/gen-tls-cert.py) or from the repo venv."
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "var" / "secrets" / "tls"

#: Always in the SAN. See the module docstring -- this is the whole reason TLS works over a tunnel.
TUNNEL_NAMES = ("localhost",)
TUNNEL_IPS = ("127.0.0.1", "::1")

#: Ten years. A per-deployment CA that expires is an outage nobody is watching for, and its
#: security does not come from rotation -- it comes from never leaving the deployment.
CA_DAYS = 3650
#: Two years for the leaf: long enough not to be an operational trap, short enough to re-issue.
LEAF_DAYS = 730


def _fingerprint(cert: x509.Certificate) -> str:
    raw = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in raw)


def _write(path: Path, data: bytes, *, private: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the key with restrictive permissions from the start rather than chmod-ing after:
    # between the two there is a window where a private key is world-readable.
    if private:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    else:
        path.write_bytes(data)
        path.chmod(0o644)


def _san(hosts: list[str]) -> x509.SubjectAlternativeName:
    """Build the SAN, auto-sorting each value into a DNS name or an IP address.

    Duplicates are dropped rather than rejected so ``--host localhost`` is harmless.
    """
    dns: list[str] = []
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in [*TUNNEL_NAMES, *hosts]:
        name = (raw or "").strip()
        if not name:
            continue
        try:
            ips.append(ipaddress.ip_address(name))
        except ValueError:
            if name not in dns:
                dns.append(name)
    for raw in TUNNEL_IPS:
        ips.append(ipaddress.ip_address(raw))
    seen: set[str] = set()
    entries: list[x509.GeneralName] = []
    for d in dns:
        if d.lower() not in seen:
            seen.add(d.lower())
            entries.append(x509.DNSName(d))
    for ip in ips:
        if str(ip) not in seen:
            seen.add(str(ip))
            entries.append(x509.IPAddress(ip))
    return x509.SubjectAlternativeName(entries)


def _key() -> ec.EllipticCurvePrivateKey:
    """P-256. Universally supported, and far faster to generate than RSA on a small VM."""
    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _now() -> dt.datetime:
    # Backdated a little: a freshly issued certificate is otherwise "not yet valid" on any
    # machine whose clock lags the issuer's, which on an isolated cluster is common.
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)


def _make_ca(out: Path, force_ca: bool) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """Load the deployment's CA, creating it on first use.

    Reused rather than regenerated so a second leaf (a renewal, or another host) is trusted by
    every operator who already installed the CA.

    Note this takes ``--force-ca``, NOT ``--force``. Re-issuing a leaf is routine; replacing the
    CA means every operator has to install trust again, so the two cannot share one flag. They
    did in the first version of this script, which meant a routine ``--force`` re-issue quietly
    invalidated the whole deployment's trust -- caught by
    ``test_the_ca_is_reused_by_a_later_issue_not_regenerated``.
    """
    crt_path, key_path = out / "ca.crt", out / "ca.key"
    if crt_path.exists() and key_path.exists() and not force_ca:
        cert = x509.load_pem_x509_certificate(crt_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        print(f"[ca]   reusing existing CA {crt_path}")
        return cert, key  # type: ignore[return-value]

    key = _key()
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NVIDIA NVIS"),
        x509.NameAttribute(NameOID.COMMON_NAME, "NVIS Rack Status deployment CA"),
    ])
    start = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    _write(crt_path, cert.public_bytes(serialization.Encoding.PEM), private=False)
    _write(key_path, _key_pem(key), private=True)
    print(f"[ca]   created {crt_path}")
    return cert, key


def _make_leaf(
    out: Path,
    hosts: list[str],
    *,
    issuer: tuple[x509.Certificate, ec.EllipticCurvePrivateKey] | None,
) -> x509.Certificate:
    key = _key()
    cn = (hosts[0] if hosts else "localhost")[:64]
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NVIDIA NVIS"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])
    issuer_name = issuer[0].subject if issuer else subject
    signing_key = issuer[1] if issuer else key
    start = _now()
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=LEAF_DAYS))
        .add_extension(_san(hosts), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    )
    cert = builder.sign(signing_key, hashes.SHA256())
    crt_path, key_path = out / "server.crt", out / "server.key"
    # When a CA signed it, ship the chain in one file: gunicorn's --certfile takes a bundle, and
    # a leaf served without its issuer fails to verify even on a machine that trusts the CA.
    chain = cert.public_bytes(serialization.Encoding.PEM)
    if issuer:
        chain += issuer[0].public_bytes(serialization.Encoding.PEM)
    _write(crt_path, chain, private=False)
    _write(key_path, _key_pem(key), private=True)
    return cert


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("self-signed", "ca"), default="self-signed")
    ap.add_argument("--host", action="append", default=[],
                    help="extra SAN entry (DNS name or IP). localhost/127.0.0.1/::1 are always included.")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--force", action="store_true", help="overwrite an existing server cert")
    ap.add_argument("--force-ca", action="store_true",
                    help="ALSO replace the deployment CA. Every operator must re-install trust; "
                         "a routine re-issue wants --force alone.")
    ap.add_argument("--fingerprint", action="store_true", help="print the current cert's fingerprint and exit")
    args = ap.parse_args()

    out = Path(args.out_dir).expanduser()
    crt_path = out / "server.crt"

    if args.fingerprint:
        if not crt_path.is_file():
            print(f"no certificate at {crt_path}", file=sys.stderr)
            return 1
        cert = x509.load_pem_x509_certificate(crt_path.read_bytes())
        print(_fingerprint(cert))
        return 0

    if crt_path.exists() and not args.force:
        print(f"refusing to overwrite {crt_path} (pass --force)", file=sys.stderr)
        return 1

    hosts = list(args.host)
    # The deployment's own name is usually what a second operator browses to, so include it
    # rather than making every site remember to pass it.
    try:
        hostname = socket.gethostname().strip()
        if hostname and hostname not in hosts:
            hosts.append(hostname)
    except OSError:
        pass

    issuer = _make_ca(out, args.force_ca) if args.mode == "ca" else None
    cert = _make_leaf(out, hosts, issuer=issuer)

    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    print(f"[cert] {crt_path}")
    print(f"[key ] {out / 'server.key'} (0600)")
    print(f"[sans] {', '.join(str(g.value) for g in san)}")
    print(f"[sha256] {_fingerprint(cert)}")
    print()
    print("Serve it:")
    print(f"  RACK_WEB_TLS_CERT={crt_path} RACK_WEB_TLS_KEY={out / 'server.key'}")
    if issuer:
        print()
        print(f"Install {out / 'ca.crt'} on each operator's machine to get verification without a warning:")
        print(f"  macOS: sudo security add-trusted-cert -d -k /Library/Keychains/System.keychain {out / 'ca.crt'}")
        print("  Firefox trusts its own store: Settings -> Certificates -> View Certificates -> Import")
    else:
        print()
        print("Self-signed: every operator sees one warning. Compare this fingerprint before accepting,")
        print("or use --mode ca so the check is automatic instead of a habit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
