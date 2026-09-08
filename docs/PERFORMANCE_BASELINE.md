# Performance baseline — NVIS Greenlight authentication and authorization

Measured 2026-09-02. Every number here was produced on the machine described below, by the code in
this repository, with the commands shown so they can be re-run rather than trusted.

**Read the two findings first.** They matter more than the table, and one of them contradicts the
intuition that a bigger server will go faster.

---

## Finding 1 — more cores do not raise the login ceiling

The deployment runs **`gunicorn --workers 1 --threads 24 --worker-class gthread`**
(`Dockerfile`). One process, threads for concurrency.

That is not an oversight, it is a correctness requirement: SRP challenge state and the OIDC
authorization-code table are **in-process**, so a second worker means round trip 2 can land on a
worker that never saw round trip 1 — an intermittent login failure that reads as a client bug.
`identity.srp.assert_single_worker_safe()` refuses to start with more.

Threads do not help either, for the expensive part. Login is CPU-bound modular arithmetic, and
CPython's GIL serialises that. **So the login path is effectively single-core, whatever the machine
has.** Twenty-four threads help concurrent *waiting* — SSH fan-outs, cluster queries, disk — and do
nothing for a 4096-bit exponentiation.

The practical consequence, stated plainly because it is the opposite of the usual assumption:

> A server with more cores will **not** log operators in faster. It may log them in *more slowly*,
> because sustained single-thread throughput on many server CPUs is lower than on an M-series
> laptop. The number that predicts login capacity is **single-thread performance**, not core count.

## Finding 2 — the SRP backend is chosen by `dlopen`, and it is the dominant cost

The `srp` package prefers an OpenSSL-backed C implementation (`_ctsrp`) and falls back to pure
Python (`_pysrp`). Which one binds is decided at import by whether `libssl` can be opened — not by
configuration, and not by us.

On this machine `_ctsrp` **cannot** load, so every measurement below is pure-Python bignum math:

```
bound SRP backend: srp._pysrp
_ctsrp importable: NO -> OSError: dlopen(libssl.32.dylib, 0x0006): no such file
```

This is almost certainly why the figure recorded elsewhere in the repo disagrees with the table
below. `identity-auth-traps.mdc` and the identity brief both state **~52 ms per `begin()`, ~19
requests/second per core**; this machine measures **106–112 ms and ~9/second**, almost exactly 2×
slower. The likeliest explanation is that the earlier figure came from a container where `_ctsrp`
bound. **Both numbers may be right for their own environment**, which is exactly why the backend
has to be checked before quoting either:

```bash
python -c "import srp; print(srp._mod.__name__)"
```

`_pysrp` → expect this table. `_ctsrp` → expect login to be materially faster, and re-measure.

> ⚠️ `identity-auth-traps.mdc` §4 warns that installing `libssl-dev` **flips the backend**, and
> treats that as a hazard because the RFC 5054 padding flag is per-backend. That warning stands: a
> faster login is not worth an accidental protocol change. If the backend is deliberately switched
> for performance, re-run the SRP interop tests before believing the result.

---

## The machine these numbers came from

| | |
|---|---|
| CPU | Apple silicon, `arm64`, 14 logical cores |
| RAM | 48 GB |
| OS | Darwin 25.6.0 (macOS) |
| Python | 3.12.13 |
| SRP backend | `srp._pysrp` (**pure Python** — see finding 2) |
| Server model | `--workers 1 --threads 24`, `gthread` |

A developer laptop, deliberately: it is the machine the numbers were taken on, and pretending
otherwise would make the baseline unfalsifiable. Treat it as a *floor for single-thread work* and
re-measure on the target.

## Measured cost per operation

`p50` / `p95` in milliseconds of CPU. "Ceiling" is `1000 / p50` — the theoretical single-core rate
for that operation alone, with no other work happening. It is an upper bound, not a throughput
promise.

Two independent runs are shown, because the spread between them is the honest precision of this
measurement. **Run-to-run variance is roughly ±5%** on an unloaded laptop, and more under load — so
treat these as one significant figure for the fast operations and "about 110 ms" for `srp.begin`.
A single number quoted to three decimals would imply a precision this method does not have.

| Operation | p50 (run 1) | p50 (run 2) | p95 | Single-core ceiling |
|---|---:|---:|---:|---:|
| **`srp.begin` — login leg 1** | **106.6 ms** | **112.3 ms** | 118 ms | **~9 /s** |
| `srp.enroll` — create/reset a credential | 6.5 ms | 6.8 ms | 7.4 ms | ~150 /s |
| `mint` HS256 (provider off) | 0.019 ms | 0.022 ms | 0.023 ms | ~46,000 /s |
| `verify` HS256 (provider off) | 0.023 ms | 0.025 ms | 0.028 ms | ~40,000 /s |
| `mint` ES256 (provider on) | 0.040 ms | 0.043 ms | 0.049 ms | ~23,000 /s |
| `verify` ES256 (provider on) | 0.079 ms | 0.079 ms | 0.094 ms | ~12,600 /s |
| JWKS render | 0.010 ms | 0.010 ms | 0.010 ms | ~100,000 /s |
| Policy evaluation, 50 statements | 0.023 ms | 0.023 ms | 0.028 ms | ~44,000 /s |
| Resource resolution (`rack.power:off`) | 0.001 ms | 0.001 ms | 0.001 ms | ~900,000 /s |

> **Correction, 2026-09-08 — the previous policy figure measured an error path.**
>
> The row above read **0.056 ms / p95 0.084 ms**, and it was not measuring policy evaluation.
> `bin/bench-auth.py` wrote its 50 policies in the *retired inline schema* (document-level
> `effect`/`principals`/`actions`/`resources`), which the current `PolicyFile` refuses. Every
> iteration therefore returned `allowed=False, reason="the policy file could not be loaded; all
> requests are refused"` — the benchmark was timing the parse-failure denial path and reporting it
> as the cost of authorization.
>
> Two things worth taking from it. The false number was **pessimistic**, not optimistic: real
> evaluation is 0.023 ms, so the conclusion "authorization is free" happened to survive — by luck,
> and with no ability to detect a regression, which is what NFR-1 asked the row to provide. And the
> failure was silent because the benchmark never checked its own verdict; it now refuses to report a
> number unless the evaluation it timed returned `allow`.
>
> `resources.resolve` is measured for the first time here. It runs on every governed request ahead
> of the engine and had never been benchmarked — at 0.001 ms it is the cheapest thing in the table.

### What the table actually says

**One operation costs more than all the others combined, by three orders of magnitude.** `srp.begin`
is ~1,400× a token verification. Every capacity question about this product reduces to "how many
logins per second", and everything else is free by comparison.

**The OIDC cutover is not a performance event.** ES256 verification is 0.079 ms against HS256's
0.023 ms — 3.4× slower in relative terms and **56 µs** in absolute ones. At a realistic 5 requests
per second per operator that is 0.3 ms of extra CPU per second. This is why the brief records
NFR-1's 5 ms budget as a regression guard rather than a decision criterion: it is ~60× looser than
the actual difference.

**Authorization is free.** 0.056 ms for 50 policy statements, evaluated per request. Policy count is
not a scaling concern at any plausible size.

## Reproducing this

```bash
# Which SRP backend is bound — do this first; nothing else means much without it.
.venv/bin/python -c "import srp; print(srp._mod.__name__)"

# The measurements. Writes nothing outside a temp dir.
D=$(mktemp -d) && RACK_WEB_SECRET_KEY=k NVIS_IDENTITY_FILE=$D/i.json \
  RACK_WEB_SIGNING_KEY_DIR=$D/oidc .venv/bin/python bin/bench-auth.py; rm -rf "$D"
```

## Tracking this over time in CI

The `auth-benchmark` job runs `bin/bench-auth.py` on every pipeline in `python:3.12-slim` and
publishes two things:

- **`auth-metrics.txt`** as a GitLab `reports:metrics` artifact. GitLab diffs it against the target
  branch and shows the change **on the merge request**, so a regression appears where it was
  introduced rather than in a dashboard nobody opens.
- **`auth-bench.json`**, kept for a year, carrying the environment beside the numbers. A latency
  history whose hardware is unrecorded cannot be reasoned about later — which is exactly the
  problem finding 2 above had to unpick.

**It records; it does not gate.** `allow_failure: true`, and the script returns 0 unconditionally.
A runner is a noisy shared machine, so a latency assertion would fail on commits that changed
nothing — and a perf job that cries wolf is one somebody silences and then stops reading. The
`allow_failure` covers the job's own plumbing, not the numbers.

**The first line of the log is the one to read**, because the SRP backend decides what the rest
means. `python:3.12-slim` ships `libssl.so.3` while the `srp` package asks for `libssl.so.1.1`, so
the pure-Python fallback is expected in CI — but that is a property of the base image, not a
guarantee. If a bump ever makes `_ctsrp` bind, login times will roughly halve, the
`auth_srp_backend_is_openssl` metric flips from 0 to 1, and this job is how anyone finds out. That
metric exists precisely so the halving is not mistaken for a code change.

`backend/tests/test_bench_auth.py` guards the harness rather than the timings: that results are
recorded, that the environment travels with them, that the OpenMetrics lines parse, and that the
**metric names have not changed** — GitLab keys a series by name, so a rename silently discards the
history this job exists to build.

## Service expectations at this spec

Derived from the table, for the deployment shape this product actually has (one worker, ~20
operators, tunnelled access).

| | Expectation | Basis |
|---|---|---|
| Interactive login | **≤ 250 ms** server CPU end to end | `begin` ~110 ms + `finish` + `enroll`-class work + token minting |
| Concurrent logins | **~9 per second** sustained, hard ceiling | single-core `srp.begin`; does not improve with cores |
| Authenticated request overhead | **< 0.2 ms** added by auth + authz | verify + revocation check + policy eval |
| Token renewal | **< 0.2 ms** | verify refresh + mint access |
| JWKS / discovery | **< 0.05 ms** | memoised; a cache hit does no I/O |

**Where the real latency comes from.** For everything except login, authentication and authorization
are not measurable next to the work the request actually does — a `cmsh` call, an SSH fan-out, a
Slurm query. If the dashboard feels slow, the cause is on the cluster side of `host_exec`, not here.
The one exception is a login storm: 20 operators signing in within the same second is ~2 seconds of
queued CPU, and they will notice.

## Scaling to a different server

| If you change | Login capacity | Everything else |
|---|---|---|
| **More cores** | **No effect** — `--workers 1` + GIL | Helps concurrent I/O (fan-outs), not CPU |
| **Faster single-thread clock** | Scales roughly linearly | Scales linearly |
| **`_ctsrp` binds instead of `_pysrp`** | **Materially faster — re-measure** | No effect |
| More RAM | No effect | No effect; nothing here is memory-bound |
| Faster disk | No effect | Marginal: identity store reads are small and cached |

**To actually raise the login ceiling** there are three levers, in order of value:

1. **Get `_ctsrp` to bind** — the largest single win, and it needs no code change. Verify the SRP
   interop tests still pass afterwards (finding 2).
2. **Share the in-process state** (challenge table, authorization codes) so `--workers N` becomes
   legal. That is the only change that makes core count matter, and it is a real piece of work:
   Redis or the store itself, plus `assert_single_worker_safe` retired.
3. **Lower the SRP group size.** Do not. 4096-bit is a deliberate choice (FR-4e: 2048-bit
   finite-field crypto is 112-bit security, which NIST SP 800-57 disallows after 2030), and
   changing the group invalidates **every enrolled verifier** — a password reset for every user at
   every site.

---

## Rate limits: what they are and where to change them

Enforced in one place (`app._enforce_request_throttle`) and declared per route
(`@throttle.throttle`). Full rationale in `backend/throttle.py`.

**Keyed by principal when authenticated, by client address otherwise.** This matters operationally:
operators reach these deployments through an SSH tunnel, so every request arrives from
`127.0.0.1`. An address-keyed limit would treat the whole team as one client and refuse a
colleague's work during a bulk operation.

### Current limits

| Route | Limit | Sustained | Why this number |
|---|---:|---:|---|
| `auth_srp.srp_begin` | 10 / 60 s | 0.17 /s | ~110 ms of CPU each; the only route where the limit is all that stands between an anonymous caller and the CPU |
| `auth_srp.srp_finish` | 20 / 60 s | 0.33 /s | one login is a begin *and* a finish, plus a retry |
| `oidc_provider.authorize` | 30 / 60 s | 0.5 /s | mints into a bounded table; a flood evicts codes before redemption |
| `auth.login`, `oidc_provider.token` | 60 / 60 s | 1 /s | page shell; credential issuance |
| `auth.api_auth_check`, `oidc.jwks`, `oidc.openid_configuration`, `/userinfo`, `/introspect`, `/revoke` | 120 / 60 s | 2 /s | cheap reads and verifications |
| **Everything authenticated (default)** | **900 / 60 s** | **15 /s** | 3× the measured worst-case SPA load of ~5 /s |
| Static assets and SPA shell | *exempt* | — | a page load fetches dozens of hashed chunks; throttling file serving is the wrong layer |

### How to change them

**A route's limit** — edit the declaration next to the view and say why in the `rationale`, which is
what makes the number reviewable later:

```python
@bp.route("/srp/begin", methods=["POST"])
@throttle.throttle(25, rationale="raised for a 60-operator site; begin is ~110ms so 25/min is "
                                 "still under half the single-core ceiling")
def srp_begin():
```

**The authenticated default** — `DEFAULT_AUTHENTICATED_LIMIT` in `backend/throttle.py`. Raise it if
a site has many operators or adds faster polling; the SPA's busiest poll is `ANALYTICS_POLL_MS =
1000`, so budget ~5 requests/second per concurrently active operator.

**A new unauthenticated route must declare one**, or `test_throttle_coverage.py` fails the build.
That is deliberate: no default can be right for both a ~110 ms exponentiation and a memoised JWKS
read.

### Before raising a limit, check it is the limit

A 429 means the caller exceeded its allowance. It does not mean the server is out of capacity, and
the two want opposite responses:

- **429s with low CPU** → the limit is too tight for legitimate use. Raise it.
- **429s with a saturated core** → the limit is doing its job. Raising it converts refusals into
  timeouts, which is worse: a refused request is cheap and honest, a queued one holds a thread.

`srp_begin` is the case to be careful with. Its ceiling is ~9/s on this machine, so a limit above
~500/60 s stops being a limit and starts being a queue.

---

## Known gaps in this baseline

Recorded rather than glossed, because a baseline that overstates its own coverage is worse than none.

1. **Measured on a developer laptop, not the target.** AC-23 in the OIDC brief asks for a *recorded*
   baseline on the deployment's own hardware. This is not that; it is the reproducible starting
   point for it.
2. **Nothing guards these numbers.** No test fails if a change makes verification ten times slower.
   Closing that needs a committed baseline to compare against — which is the same gap as (1).
3. **Operation costs, not end-to-end latency.** These are CPU times for the crypto and policy work,
   measured in-process. They exclude HTTP parsing, TLS, gunicorn's dispatch and the identity
   store's disk I/O. The login expectation above adds a margin for those rather than measuring them.
4. **No sustained-load or soak test.** Every figure is a short burst on an otherwise idle machine.
   Behaviour under a genuine login storm, or with 24 threads all active, is unmeasured.
5. **The container's own `begin()` cost is still unmeasured.** The `~52 ms` figure in
   `identity-auth-traps.mdc` and the identity brief is now annotated with the backend it assumes
   (both were updated 2026-09-02), so the two numbers no longer contradict each other — but nobody
   has yet run `bin/bench-auth.py` inside the deployment image to confirm `_ctsrp` binds there and
   that ~52 ms is what it costs.
