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

import argparse
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


#: Filled by `bench`, drained by the writers. A module-level accumulator rather than threading a
#: collector through every call, because the alternative is a signature change on every line of
#: `main` for no readability gain.
RESULTS: list[dict[str, object]] = []


def _metric_name(label: str) -> str:
    """A stable OpenMetrics-safe name for a human label.

    Stability matters more than prettiness: GitLab tracks a metric across pipelines by name, so
    renaming one silently starts a new series and loses the history this job exists to build.
    """
    keep = []
    for ch in label.lower():
        keep.append(ch if (ch.isalnum() or ch == "_") else "_")
    return "auth_" + "_".join(filter(None, "".join(keep).split("_")))


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
    RESULTS.append({
        "label": label,
        "metric": _metric_name(label),
        "p50_ms": round(p50, 4),
        "p95_ms": round(p95, 4),
        "ceiling_per_s": round(ceiling, 1),
        "iterations": iterations,
    })
    return p50, p95


def _environment() -> dict[str, object]:
    import srp as srp_lib

    return {
        "arch": platform.machine(),
        "system": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "cores": os.cpu_count(),
        # The single most important field. `srp.begin` dominates every other cost and the two
        # backends differ by ~2x, so a run without this label is not comparable to anything.
        "srp_backend": srp_lib._mod.__name__,
        "container": os.path.exists("/.dockerenv"),
        "ci": bool(os.environ.get("CI")),
        "commit": os.environ.get("CI_COMMIT_SHORT_SHA", ""),
        "pipeline": os.environ.get("CI_PIPELINE_ID", ""),
    }


def write_metrics(path: Path, env: dict[str, object]) -> None:
    """OpenMetrics text, which is what GitLab's `reports:metrics` consumes.

    That format is the point of this job: GitLab diffs these values against the target branch and
    shows the change on the merge request, so a regression is visible where it is introduced rather
    than in a dashboard nobody opens.

    The SRP backend rides along as a label on every metric. Without it a 2x jump would look like a
    performance regression when it is really a base-image change, and somebody would spend a day
    on it.
    """
    backend = str(env["srp_backend"]).rsplit(".", 1)[-1]
    lines = [
        "# Auth path latency. Lower is better. See docs/PERFORMANCE_BASELINE.md.",
        f'auth_srp_backend_is_openssl{{backend="{backend}"}} {1 if backend == "_ctsrp" else 0}',
    ]
    for row in RESULTS:
        labels = f'{{backend="{backend}",arch="{env["arch"]}"}}'
        lines.append(f'{row["metric"]}_p50_ms{labels} {row["p50_ms"]}')
        lines.append(f'{row["metric"]}_p95_ms{labels} {row["p95_ms"]}')
    path.write_text("\n".join(lines) + "\n")
    print(f"  wrote {len(RESULTS) * 2 + 1} metrics to {path}")


def write_json(path: Path, env: dict[str, object]) -> None:
    """The durable record. Keeps the environment beside the numbers, permanently.

    A latency series whose hardware is unrecorded cannot be reasoned about later — which is the
    mistake `docs/PERFORMANCE_BASELINE.md` had to unpick when our own ~52 ms figure turned out to
    have been measured on a different SRP backend.
    """
    payload = {
        "schema": "nvis-greenlight/auth-bench/v1",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": env,
        "results": RESULTS,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  wrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, help="write OpenMetrics text for GitLab")
    parser.add_argument("--json", type=Path, dest="json_out", help="write the durable record")
    parser.add_argument(
        "--quick", action="store_true",
        help="fewer iterations — a smoke check, NOT a recorded baseline",
    )
    args = parser.parse_args()

    # An ephemeral signing key when the caller supplied none, so the script runs standalone.
    #
    # It measured `srp.begin` and then crashed on the first `mint` with "RACK_WEB_SECRET_KEY is not
    # set" — the token section only ever worked because `test_bench_auth.py` passes a key into the
    # child environment. A benchmark that cannot be run by hand does not get run.
    #
    # Generated rather than a fixed literal, and not persisted: the derivation cost is what is being
    # measured, and HKDF does not care what the input is. A deployment's real key is bootstrapped by
    # `app.py` into `var/secrets/`; this process never touches it.
    if not (os.environ.get("RACK_WEB_SECRET_KEY") or "").strip():
        import secrets as _secrets

        os.environ["RACK_WEB_SECRET_KEY"] = _secrets.token_hex(32)
        print("note: generated an ephemeral RACK_WEB_SECRET_KEY for this run\n")

    import srp as srp_lib
    from authz import policy as authz_policy
    from identity import keys as keys_mod
    from identity import revocation, tokens
    from identity import srp as srp_mod

    srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
    revocation.install()

    # `--quick` trades precision for wall time. Named as unsuitable for a baseline in the help
    # text because a 3-sample p95 is noise, and a number that looks like a measurement is worse
    # than an obviously missing one.
    srp_iterations = 3 if args.quick else SRP_ITERATIONS
    fast_iterations = 40 if args.quick else FAST_ITERATIONS

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
          lambda: srp_mod.enroll("bench", password), srp_iterations)

    def one_begin() -> None:
        user = srp_lib.User("bench", password,
                            hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE)
        _name, a_value = user.start_authentication()
        srp_mod.begin("bench", salt, verifier, a_value.hex())

    begin_p50, _ = bench("srp.begin (login leg 1)", one_begin, srp_iterations)

    print()
    print("=== tokens ===")
    keys_mod.key_set().ensure()

    bench("mint  HS256 (provider off)",
          lambda: tokens.mint("user:bench", tokens.TOKEN_ACCESS), fast_iterations)
    hs_token = tokens.mint("user:bench", tokens.TOKEN_ACCESS)
    bench("verify HS256 (provider off)",
          lambda: tokens.verify(hs_token, tokens.TOKEN_ACCESS), fast_iterations)

    os.environ["RACK_WEB_OIDC_ENABLED"] = "1"
    os.environ.setdefault("RACK_WEB_OIDC_ISSUER", "https://bench.example.com")
    from identity import oidc as oidc_mod

    oidc_mod.reset_for_tests()

    bench("mint  ES256 (provider on)",
          lambda: tokens.mint("user:bench", tokens.TOKEN_ACCESS), fast_iterations)
    es_token = tokens.mint("user:bench", tokens.TOKEN_ACCESS)
    bench("verify ES256 (provider on)",
          lambda: tokens.verify(es_token, tokens.TOKEN_ACCESS), fast_iterations)
    bench("jwks render", lambda: keys_mod.key_set().public_jwks(), fast_iterations)

    print()
    print("=== authorization ===")
    with tempfile.TemporaryDirectory() as tmp:
        policy_file = Path(tmp) / "policies.json"
        # Fifty statements, and the query is aimed at the LAST one so the walk is not short-
        # circuited by an early match — otherwise this measures the best case and reports it as
        # typical.
        #
        # **The document is in the attachment schema, and this used to be wrong in a way that
        # invalidated the published number.** It wrote the retired inline shape (document-level
        # `effect`/`principals`/`actions`/`resources`), which the current `PolicyFile` refuses with
        # 250 validation errors — so `evaluate` returned `allowed=False, reason="the policy file
        # could not be loaded"` and the benchmark timed the *parse-failure denial path*. The figure
        # it produced went into docs/PERFORMANCE_BASELINE.md as evidence that authorization is
        # free. A benchmark that silently measures an error path is worse than none, so assert the
        # verdict before timing it.
        policy_file.write_text(json.dumps({"policies": [{
            "id": "bench",
            "statements": [
                {"sid": f"s{i}", "effect": "allow",
                 "actions": [f"svc{i}.act:*"], "resources": ["rack:*"]}
                for i in range(50)
            ],
        }]}))
        engine = authz_policy.PolicyEngine(
            policy_file,
            group_lookup=lambda _p: ("ops",),
            # Attachment-based since Phase 3: a document nobody is attached to is never examined,
            # so without this the walk has nothing to walk and the number is meaningless again.
            attachment_lookup=lambda _subject: ("bench",),
        )
        verdict = engine.evaluate("user:bench", "svc49.act:do", "rack:a01")
        if not verdict.allowed:
            raise SystemExit(
                f"bench-auth: the policy benchmark is not measuring evaluation — "
                f"{verdict.reason!r}. Fix the document before trusting any number below."
            )
        bench("policy eval (50 statements)",
              lambda: engine.evaluate("user:bench", "svc49.act:do", "rack:a01"), fast_iterations)

        # The resolver runs on every governed request, ahead of the engine, and was never measured.
        import authz.resources as authz_resources

        facts = authz_resources.RequestFacts({}, {"rack": "a01"}, {})
        bench("resource resolve",
              lambda: authz_resources.resolve("rack.power:off", facts), fast_iterations)

    print()
    print(f"=== summary: srp.begin is {begin_p50:.1f} ms, so the login ceiling is "
          f"{1000.0 / begin_p50:.1f}/s on ONE core ===")
    print("  More cores will not raise it. See docs/PERFORMANCE_BASELINE.md, finding 1.")

    env = _environment()
    if args.metrics:
        write_metrics(args.metrics, env)
    if args.json_out:
        write_json(args.json_out, env)

    # Deliberately always 0. This job RECORDS, it does not gate.
    #
    # A CI runner is a noisy shared machine; a latency assertion on one would fail on unrelated
    # commits, and a perf test that cries wolf is a perf test somebody disables with `allow_failure`
    # and then ignores. Making the numbers visible on the merge request is the useful half, and it
    # costs nobody a red pipeline they cannot act on.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
