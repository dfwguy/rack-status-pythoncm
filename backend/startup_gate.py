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
"""The three things Greenlight refuses to serve without: an administrator, a policy, and TLS.

**Checked when the server starts, not when `app` is imported**, and the distinction is deliberate
rather than a loophole. `bin/check-tracked-tree.sh` imports all 164 modules to prove the committed
tree stands on its own, `bin/verify-deploy.sh` imports them to prove the container can, and the
route and action snapshots import `app` to enumerate its URL map. None of those serve a request, so
none of them can leak a credential or accept traffic — refusing them would buy no safety and would
break every tool that inspects the app without running it.

So the gate hangs off the paths where "start" means starting: gunicorn's `on_starting` hook, which
covers the container and `start.sh`; `cli.py serve`; and `app.py`'s development-server `__main__`.

**Every fault is reported at once.** The same rule `config_preflight` follows, for the same reason:
an operator bringing up a new deployment should learn about all three in one attempt rather than
discovering them one restart at a time.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: `EX_CONFIG` from sysexits.h — the deployment is misconfigured rather than the program having
#: crashed, so a supervisor reports it instead of restart-looping something that cannot come up.
EXIT_CONFIG = 78

#: Set by the test suite and by `bin/gen-tls-cert.py`'s own tests, which start no server. It is not
#: a way to serve without TLS: nothing reads it on any serving path, because the serving paths call
#: `require()` and `require()` does not consult it.
_INSPECT_ONLY = "NVIS_STARTUP_GATE_INSPECT_ONLY"


@dataclass(frozen=True)
class Fault:
    """One missing prerequisite, with the action that fixes it."""

    what: str
    why: str
    fix: str

    def __str__(self) -> str:
        return f"{self.what}\n      {self.why}\n      fix: {self.fix}"


def collect() -> list[Fault]:
    """Everything that would stop this process from serving. Read-only."""
    faults: list[Fault] = []
    faults.extend(_tls_faults())
    faults.extend(_identity_faults())
    return faults


def _tls_faults() -> list[Fault]:
    cert = (os.environ.get("RACK_WEB_TLS_CERT") or "").strip()
    key = (os.environ.get("RACK_WEB_TLS_KEY") or "").strip()

    if not cert and not key:
        return [Fault(
            "TLS is not configured",
            "operators reach this dashboard over a forwarded port and authenticate with SRP; the "
            "browser needs a secure context for crypto.subtle, and a session cookie without "
            "Secure is a cookie that travels in clear.",
            "bin/gen-tls-cert.py --out-dir var/secrets/tls, then set RACK_WEB_TLS_CERT and "
            "RACK_WEB_TLS_KEY (bin/run-local-auth.sh does both).",
        )]

    # Both or neither. One alone silently downgrades one server and crashes the other, which is why
    # `start.sh` has refused this combination since Phase 0.5.
    missing = [n for n, v in (("RACK_WEB_TLS_CERT", cert), ("RACK_WEB_TLS_KEY", key)) if not v]
    if missing:
        return [Fault(
            f"TLS is half-configured: {missing[0]} is not set",
            "a certificate without its key cannot serve, and a key without its certificate is not "
            "a configuration anything can use.",
            f"set {missing[0]}, or unset both to see the full TLS fault instead.",
        )]

    return [
        Fault(
            f"TLS file is unreadable: {path}",
            "the path is configured but this process cannot read it, so the listener would fail "
            "after the bootstrap had already run.",
            "check the path and its permissions; the key must be readable by the service user.",
        )
        for path in (cert, key)
        if not Path(path).is_file()
    ]


def _identity_faults() -> list[Fault]:
    """An administrator and a policy set.

    Both are normally satisfied by the bootstrap that runs just before this on a brand-new
    deployment, so this fires for the case bootstrap deliberately does not cover: a store that
    already holds accounts and has never been given a policy. We cannot invent one there — nothing
    tells us which of its accounts should hold full access — so the answer is to say so and stop,
    rather than fall back to permitting everything.
    """
    faults: list[Fault] = []

    from authz import store as policy_store
    from identity import bootstrap as identity_bootstrap
    from identity import store as identity_store_module
    from identity.models import IdentityError
    from identity.store import IdentityStore

    identity_path = Path(identity_store_module.identity_file())
    if not identity_path.is_file():
        faults.append(Fault(
            "no identity store",
            "there is nobody to authenticate, so nobody could sign in.",
            "start once and let the bootstrap create the administrator, or point "
            "--identity-file at an existing store.",
        ))
    else:
        store = None
        try:
            store = IdentityStore(str(identity_path))
            if identity_bootstrap.needed(store):
                faults.append(Fault(
                    "the identity store holds no users",
                    "there is nobody to authenticate.",
                    "remove the empty store and start again so the bootstrap can create the "
                    "administrator.",
                ))
        except (OSError, ValueError, IdentityError) as exc:
            faults.append(Fault(
                f"the identity store cannot be read: {exc}",
                "authentication cannot be evaluated, which is a fail-closed condition.",
                "check the path and permissions of the store.",
            ))
        finally:
            if store is not None:
                store.close()

    try:
        if not policy_store.read_all():
            faults.append(Fault(
                "no authorization policy",
                "with no policy the engine permits every authenticated request and records "
                "policy_engine: allow-all. Honest, but it means shipping with authorization off.",
                f"write {policy_store.path()} — the Identity > Policies view creates one, or "
                "attach the `administrators` policy to an account.",
            ))
    except (policy_store.PolicyStoreError, OSError) as exc:
        faults.append(Fault(
            f"the policy set cannot be read: {exc}",
            "an unreadable policy set denies every request, so serving would refuse all traffic.",
            "fix or remove the file; a malformed policy is worse than an absent one.",
        ))

    return faults


def prepare(*, bootstrap: bool = True, exit_on_fault: bool = True) -> list[Fault]:
    """Create what a first boot is entitled to create, then refuse if anything is still missing.

    **Bootstrap has to come before the gate, not after.** Ordered the other way round — which is
    how the first version of this read — a brand-new deployment is refused for lacking the very
    administrator and policy that starting would have created, so it could never start at all.

    All three prerequisites are things the product knows how to make on a fresh system:

    * the administrator, and the policy attached to them (`identity.bootstrap`);
    * a **self-signed certificate**, because Phase 0.5's decision is already "self-signed by
      default, an internal CA as an opt-in upgrade". Refusing to start for want of a certificate
      the product can generate would be a gate that teaches operators to work around it.

    `bootstrap=False` turns all of that off, for a deployment that provisions these out of band and
    wants a missing one to be an error rather than a silent creation.
    """
    if not bootstrap:
        return require(exit_on_fault=exit_on_fault)

    _ensure_tls()
    _ensure_identity()
    return require(exit_on_fault=exit_on_fault)


def _ensure_tls() -> None:
    """Generate a self-signed certificate when none is configured, and point the process at it."""
    if (os.environ.get("RACK_WEB_TLS_CERT") or "").strip():
        return

    from paths import REPO_ROOT

    out_dir = Path(os.environ.get("RACK_WEB_TLS_DIR") or (REPO_ROOT / "var" / "secrets" / "tls"))
    cert, key = out_dir / "server.crt", out_dir / "server.key"

    if not (cert.is_file() and key.is_file()):
        # Out of process, so this borrows `bin/gen-tls-cert.py` rather than growing a second
        # certificate generator beside it — the SAN rules there (localhost, 127.0.0.1, ::1 added
        # unconditionally) are load-bearing and must not be reimplemented from memory.
        logging.getLogger("startup").warning(
            "startup: no TLS certificate configured; generating a self-signed one in %s", out_dir
        )
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "gen-tls-cert.py"), "--out-dir", str(out_dir)],
            check=True, capture_output=True,
        )

    os.environ["RACK_WEB_TLS_CERT"] = str(cert)
    os.environ.setdefault("RACK_WEB_TLS_KEY", str(key))


def _ensure_identity() -> None:
    """The administrator and the policy attached to them, for a brand-new deployment only."""
    from identity import bootstrap as identity_bootstrap
    from identity.store import IdentityStore

    store = IdentityStore()
    try:
        if identity_bootstrap.ensure_admin(store) is not None:
            identity_bootstrap.ensure_admin_policy(store)
    finally:
        store.close()


def require(*, exit_on_fault: bool = True) -> list[Fault]:
    """Refuse to start unless all three are in place. Returns the faults it found.

    Raises `SystemExit(78)` rather than returning a flag, because every caller is an entry point
    whose only reasonable response is to stop — and a returned flag is one a future caller forgets
    to check.
    """
    faults = collect()
    if not faults or not exit_on_fault:
        return faults

    lines = [
        "",
        "  greenlight: refusing to start.",
        "",
        f"  {len(faults)} prerequisite(s) are missing. Every one is listed so this takes one",
        "  attempt rather than one restart each:",
        "",
    ]
    lines += [f"    - {fault}\n" for fault in faults]
    lines.append("  `python backend/cli.py check` reports this without starting anything.\n")
    raise SystemExit("\n".join(lines))
