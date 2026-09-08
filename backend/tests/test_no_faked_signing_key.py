# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""A test may not invent a signing key in order to authenticate.

**Why this is a build gate rather than a convention.** Setting `RACK_WEB_SECRET_KEY` looks like
arranging a prerequisite and is in fact the opposite: `app.py` bootstraps a real key on import, so
the assignment *repoints* one that already exists. The JWT signing key is HKDF-derived from it, so
every credential issued before the repoint stops verifying — INV-17 — and the failure surfaces as a
401 inside a test that never mentions authentication.

Thirteen files did it. Twelve invented a distinct per-file value, so the key changed repeatedly
within a single session and a credential built by a session-scoped fixture could be dead before a
test used it. That is the entire reason `AuthenticatedClient` had to start minting per request.

The replacement is `identity.testing.log_in(client)`: the real SRP exchange, a token signed by
whatever key this deployment bootstrapped, delivered in the cookie jar. Nothing to arrange.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: Files where the key IS the subject, each with the reason. An entry here is a claim that the test
#: is *about* key material — not that setting it was convenient.
ALLOWED = {
    # The token layer itself: `mint`, `verify`, key derivation, and the error raised when no secret
    # is configured. These cannot be tested without controlling the input.
    "test_identity_tokens.py",
    # INV-17 is literally one of the catalogued cases here, so this file must be able to repoint it.
    "test_identity_token_invalidation.py",
    # The bootstrap under test: load, create, persist, and the ephemeral fallback.
    "test_web_secret_bootstrap.py",
    # Switches between HS256 (legacy) and ES256 (OIDC), which is a statement about signing.
    "test_tokens_oidc_mode.py",
    # SRP session-key derivation, which shares the plumbing.
    "test_identity_srp.py",
    # Demonstrates the hazard, so it has to reproduce it.
    "test_identity_test_factory.py",
    # This file, which names the variable in order to search for it.
    "test_no_faked_signing_key.py",
    # --- Subprocess configuration, not in-process faking -----------------------------------------
    # These pass the key into a CHILD process environment, which is how a deployment configures one
    # and is not the trap above: the parent's own credentials are unaffected, and a child with an
    # isolated `var/` would otherwise bootstrap a key the parent cannot predict. Kept deliberately.
    "test_audit_e2e.py",
    "test_bench_auth.py",
    "test_cli.py",
}

#: Any assignment of the name, including inside a dict literal.
#:
#: The dict form is included on purpose, even though `{"RACK_WEB_SECRET_KEY": ...}` is usually a
#: child process's `env=`. Excluding it let a real in-process fake hide in a settings table that a
#: fixture later fed to `monkeypatch.setenv` one key at a time — which is the trap wearing a
#: different shape. The three files that genuinely configure a child are allowlisted by name, so
#: matching broadly costs nothing and closes the hole.
_IN_PROCESS = re.compile(
    r"""(monkeypatch\.setenv\(\s*["']RACK_WEB_SECRET_KEY["']"""
    r"""|os\.environ\[\s*["']RACK_WEB_SECRET_KEY["']\s*\]\s*="""
    r"""|os\.environ\.setdefault\(\s*["']RACK_WEB_SECRET_KEY["']"""
    r"""|["']RACK_WEB_SECRET_KEY["']\s*:)""",
)


def _offenders() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(TESTS.glob("*.py")):
        if path.name in ALLOWED:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        hits = [n for n, line in enumerate(lines, 1) if _IN_PROCESS.search(line)]
        if hits:
            found[path.name] = hits
    return found


def test_no_test_invents_a_signing_key():
    offenders = _offenders()
    assert not offenders, (
        "these tests set RACK_WEB_SECRET_KEY in-process:\n"
        + "\n".join(f"  {name}:{hits}" for name, hits in offenders.items())
        + "\n\nThe app bootstraps a real key on import, so this repoints it and invalidates every "
        "credential issued earlier (INV-17). Authenticate instead:\n"
        "    principal = identity_testing.enrol('someone')\n"
        "    identity_testing.log_in(client, principal.login)\n"
        "If the key genuinely IS the subject, add the file to ALLOWED with the reason."
    )


def test_the_allowlist_has_no_dead_entries():
    """An entry naming a file that no longer sets it reads as a live exemption to the next reader,
    and would silently permit a new fake if that file grew one."""
    stale = []
    for name in sorted(ALLOWED):
        if name == "test_no_faked_signing_key.py":
            continue
        path = TESTS / name
        if not path.exists():
            stale.append(f"{name} (file is gone)")
        elif "RACK_WEB_SECRET_KEY" not in path.read_text(encoding="utf-8"):
            stale.append(f"{name} (no longer sets it)")
    assert not stale, f"remove these from ALLOWED: {stale}"


def test_the_guard_would_catch_a_new_offender(tmp_path, monkeypatch):
    """Mutation check: without it, a guard that scanned nothing would pass identically."""
    intruder = TESTS / "test_zz_guard_selfcheck_generated.py"
    intruder.write_text(
        'import os\nos.environ["RACK_WEB_SECRET_KEY"] = "sneaky"\n', encoding="utf-8"
    )
    try:
        assert "test_zz_guard_selfcheck_generated.py" in _offenders()
    finally:
        intruder.unlink()
