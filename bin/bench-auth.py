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
"""bench-auth.py — measure the authentication and authorization operation costs.

Produces the table in `docs/PERFORMANCE_BASELINE.md`. Exists so that document is reproducible
rather than a set of claims: run this on the target hardware and compare.

**Print the SRP backend first, and read it.** The `srp` package binds either an OpenSSL-backed C
implementation or a pure-Python fallback, decided at import by whether `libssl` can be opened.
`srp.begin` is the dominant cost in the whole product and the two backends differ by roughly 2×, so
a number quoted without its backend is not comparable to anything.

Usage:
    D=$(mktemp -d) && RACK_WEB_SECRET_KEY=k NVIS_IDENTITY_FILE=$D/i.json \
      RACK_WEB_SIGNING_KEY_DIR=$D/oidc .venv/bin/python bin/bench-auth.py; rm -rf "$D"

Writes nothing outside the temp dir it is given. Safe on a live deployment, though it will consume
CPU — the SRP section alone is a few seconds of a saturated core.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

logging.disable(logging.WARNING)

#: Iteration counts differ by cost: the SRP work is ~100 ms a call, so a large sample would take
#: minutes for no extra precision, while token operations are microseconds and need many samples
#: before the timer resolution stops dominating.
SRP_ITERATIONS = 20
FAST_ITERATIONS = 300


def bench(label: str, fn, iterations: int) -> tuple[float, float]:
    """Return (p50, p95) in milliseconds.

    One warm-up call, discarded: the first invocation pays import-time lazy work, key loading and
    branch prediction that no later call does, and including it makes p50 depend on sample size.
    """
    fn()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    ceiling = 1000.0 / p50 if p50 else float("inf")
    print(f"  {label:<36} p50={p50:9.3f}ms  p95={p95:9.3f}ms  ceiling={ceiling:10.1f}/s")
    return p50, p95


def main() -> int:
    import srp as srp_lib
    from authz import policy as authz_policy
    from identity import keys as keys_mod
    from identity import revocation, tokens
    from identity import srp as srp_mod

    srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
    revocation.install()

    print("=== environment ===")
    print(f"  arch/system  {platform.machine()} / {platform.system()} {platform.release()}")
    print(f"  python       {sys.version.split()[0]}")
    print(f"  cores        {os.cpu_count()} logical")
    print(f"  SRP backend  {srp_lib._mod.__name__}", end="")
    print("   <-- pure Python; expect ~2x the C path" if srp_lib._mod.__name__.endswith("_pysrp")
          else "   <-- OpenSSL path")
    print("  NOTE the server runs --workers 1, so the login ceiling below is also the")
    print("       deployment ceiling: CPU-bound SRP does not parallelise across threads.")
    print()

    password = "benchmark-password-not-a-credential"
    salt, verifier = srp_mod.enroll("bench", password)

    print("=== SRP (4096-bit) — the dominant cost in the product ===")
    bench("srp.enroll (create/reset credential)",
          lambda: srp_mod.enroll("bench", password), SRP_ITERATIONS)

    def one_begin() -> None:
        user = srp_lib.User("bench", password,
                            hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE)
        _name, a_value = user.start_authentication()
        srp_mod.begin("bench", salt, verifier, a_value.hex())

    begin_p50, _ = bench("srp.begin (login leg 1)", one_begin, SRP_ITERATIONS)

    print()
    print("=== tokens ===")
    keys_mod.key_set().ensure()

    bench("mint  HS256 (provider off)",
          lambda: tokens.mint("user:bench", tokens.TOKEN_ACCESS), FAST_ITERATIONS)
    hs_token = tokens.mint("user:bench", tokens.TOKEN_ACCESS)
    bench("verify HS256 (provider off)",
          lambda: tokens.verify(hs_token, tokens.TOKEN_ACCESS), FAST_ITERATIONS)

    os.environ["RACK_WEB_OIDC_ENABLED"] = "1"
    os.environ.setdefault("RACK_WEB_OIDC_ISSUER", "https://bench.example.com")
    from identity import oidc as oidc_mod

    oidc_mod.reset_for_tests()

    bench("mint  ES256 (provider on)",
          lambda: tokens.mint("user:bench", tokens.TOKEN_ACCESS), FAST_ITERATIONS)
    es_token = tokens.mint("user:bench", tokens.TOKEN_ACCESS)
    bench("verify ES256 (provider on)",
          lambda: tokens.verify(es_token, tokens.TOKEN_ACCESS), FAST_ITERATIONS)
    bench("jwks render", lambda: keys_mod.key_set().public_jwks(), FAST_ITERATIONS)

    print()
    print("=== authorization ===")
    with tempfile.TemporaryDirectory() as tmp:
        policy_file = Path(tmp) / "policies.json"
        # Fifty statements, and the query is aimed at the LAST one so the walk is not short-
        # circuited by an early match — otherwise this measures the best case and reports it as
        # typical.
        policy_file.write_text(json.dumps({"policies": [
            {"id": f"p{i}", "effect": "allow", "principals": ["group:ops"],
             "actions": [f"svc{i}.act:*"], "resources": ["rack:*"]}
            for i in range(50)
        ]}))
        engine = authz_policy.PolicyEngine(policy_file, group_lookup=lambda _p: ("ops",))
        bench("policy eval (50 statements)",
              lambda: engine.evaluate("user:bench", "svc49.act:do", "rack:a01"), FAST_ITERATIONS)

    print()
    print(f"=== summary: srp.begin is {begin_p50:.1f} ms, so the login ceiling is "
          f"{1000.0 / begin_p50:.1f}/s on ONE core ===")
    print("  More cores will not raise it. See docs/PERFORMANCE_BASELINE.md, finding 1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
