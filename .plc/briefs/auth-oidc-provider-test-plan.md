# PLC Test Plan — auth-oidc-provider (Phase 2.5)

**Brief:** `.plc/briefs/auth-oidc-provider-brief.md` — 47 FR/NFR, 53 AC.
**Jira:** [NVRACKSTAT-57](https://jirasw.nvidia.com/browse/NVRACKSTAT-57).
**Status:** written 2026-09-02, **after** implementation.

> ## This plan was written after the code, and that is a process deviation
>
> Phases 1 and 2 had a test plan between design and implementation. This one did not: the work went
> from brief straight to code, and this document was reconstructed from what exists.
>
> **What that costs is real.** A plan written first is a prediction, and a prediction that fails
> tells you something. A plan written afterwards can only describe what was built, so it cannot
> discover a case nobody thought of — it inherits the implementation's blind spots by construction.
> Every gap in §Coverage Gaps was found by writing this document, which is exactly the set that a
> plan-first order would have caught earlier and cheaper.
>
> Recorded here rather than in a commit message because the next person deciding whether to skip
> this step should be able to see what skipping it cost.

## Purpose

Verify that Phase 2.5 turns the bespoke JWT layer into a conforming OpenID Provider **without
changing the behaviour of a deployment that has not opted in**, and that the new attack surface a
published verification key creates is closed.

Two properties carry disproportionate weight:

1. **Flag off is identical, not similar** (AC-28). The flag adds a second path through the
   authentication gate, and the way that fails is a quiet drift in the default configuration.
2. **The published key is now attacker material.** Under HS256 there was nothing to hand out. From
   this phase on, `alg: HS256` signed with our own public key is a forgery we supplied the
   ingredients for.

## Entry Criteria

- The brief's requirements are settled (47 FR/NFR, all with at least one AC).
- The four feature-lead decisions are recorded: ES256, `var/secrets/`, `/token` beside
  `/api/auth/refresh`, flagged cutover.
- Phase 2's suite is green, since "unchanged" is measured against it.

## Exit Criteria

- Every AC in the brief maps to a passing automated test, an explicit N/A with a reason, or a named
  deferral.
- The Phase 2 token suites pass **with no edits**, which is the operative test of AC-28.
- `ruff`, `tsc`, the coverage floor and `check-tracked-tree.sh` pass.
- **Not met, and deliberately:** AC-32 (flag and HS256 path removed) cannot pass while the phase
  exists — FR-19 requires both paths. It gates *closing* the phase.

## Environments And Fixtures

| Fixture | Where | Provides |
|---|---|---|
| `key_dir` | `test_identity_keys.py` | `RACK_WEB_SIGNING_KEY_DIR` under `tmp_path`; resets the process key set |
| `month_tokens` | `test_identity_keys.py` | A 30-day refresh ceiling — today's real `T_max` |
| `provider` | `test_oidc_discovery.py`, `test_oidc_provider.py` | Flag on, issuer set, key bootstrapped, clients registered |
| `provider_off` | `test_oidc_discovery.py` | Flag absent — the default deployment |
| `oidc_mode` / `legacy_mode` | `test_tokens_oidc_mode.py` | The two signing modes, side by side in one run |
| `signed_in` | `test_oidc_provider.py` | An operator `/authorize` treats as authenticated |

**Two fixture caveats, stated because a fixture that hides the thing under test is how a hole
stays open.**

`signed_in` patches `oidc_provider._current_principal`, not the gate. The suite runs with
`RACK_WEB_AUTH_DISABLED=1`, and a disabled gate never resolves a principal at all — so patching the
gate's resolver has no effect. **The gate-to-handler wiring is therefore not covered by the happy
path**; TC-31 covers it separately.

Every date-sensitive test takes an explicit `now`. The lifetime design spans months, so a suite that
could only observe the present would verify the arithmetic at one point on a timeline whose
interesting points are the signing cutoff and the instant before it.

## Test Cases

### Unit Layer

| ID | Verifies | Design ref | Method | Setup | Action | Expected | Test artifact |
|---|---|---|---|---|---|---|---|
| TC-1 | FR-2, FR-18a, AC-3 | `identity/keys.py` | automated | empty `key_dir` | `ensure()`; again; again from a fresh `KeySet` | one key minted, then reused; survives a "restart" | `test_identity_keys.py::TestBootstrap` |
| TC-2 | FR-2a, AC-4 | `keys._write_private` | automated | `key_dir`; umask 0 | rotate three times; stat every PEM | all `0o600`; dir `0o700`; metadata `0o600` | `test_identity_keys.py::TestPermissions` |
| TC-3 | FR-3, AC-5 | `keys.thumbprint` | automated | — | recompute the thumbprint from the published JWK; RFC 7638 §3.1 vector | `kid` equals the independent recomputation, and the RFC's own stated result | `test_identity_keys.py::TestThumbprintKid` |
| TC-4 | FR-4c, FR-4d1, AC-6a, AC-6f | `SigningKey.signing_cutoff` | automated | parametrised over validity × `T_max` | read the cutoff | `not_after − T_max`, including the 18/6→12 month case | `test_identity_keys.py::TestTheDerivedSigningCutoff` |
| TC-5 | FR-4c, FR-4d, AC-6b, AC-6c | `keys._assert_validity_exceeds` | automated | controlled clock | mint at the last signable instant; then a validity below `T_max` | the token verifies to its own `exp`; the short validity refuses at startup naming both values | `test_identity_keys.py::TestTheInvariantThatProtectsOutstandingTokens` |
| TC-6 | FR-4, FR-4e, FR-4f, AC-6, AC-6d, AC-6e | `KeySet.rotate` | automated | controlled clock | rotate; cross the cutoff; pass `not_after` | previous key still verifies; newest signs; expiry rotates rather than refusing; expired key retired | `test_identity_keys.py::TestRotation` |
| TC-7 | FR-4d2, AC-6g, AC-6h | `MIN_INTENDED_SIGNING_PERIOD_SECONDS` | automated | 3-month validity, then the default | `ensure()` under `caplog` | short period warns and still works; the default warns **not at all** | `test_identity_keys.py::TestTheInvariantThatProtectsOutstandingTokens::test_a_short_signing_period_warns_but_still_works` |
| TC-8 | FR-6a, AC-9, AC-9a | `SigningKey.public_jwk` | automated | two keys | render the JWKS; rebuild the key from `x`/`y` and verify a real signature | no `d`/`p`/`q`/`dp`/`dq`/`qi` in **any** key nor in the rendered JSON; the rebuilt key verifies | `test_identity_keys.py::TestTheJwksNeverLeaksPrivateMaterial` |
| TC-9 | NFR-2a, AC-24 | `KeySet._prune_locked` | automated | one key | read the JWKS and look up an expired `kid` five times | metadata mtime unchanged; no PEM deleted by a read | `test_identity_keys.py::TestReadsDoNotWrite` |
| TC-10 | FR-2 (robustness) | `KeySet._load_locked` | automated | corrupt metadata; missing PEM; an RSA key | `ensure()` | corrupt **refuses**; missing PEM is **skipped with a warning**; RSA refused | `test_identity_keys.py::TestDamagedState` |
| TC-11 | FR-4c (config) | `keys._configured_validity` | automated | garbage env; changed dir | read | unparseable falls back with a warning; the process key set follows a directory change | `test_identity_keys.py::TestConfiguration` |
| TC-12 | FR-15, FR-15a | `identity/oidc_clients.py` | automated | a clients file | load a wildcard, a public client with a secret, a non-https URI | each refused at **load**, with the reason named | `test_oidc_provider.py::TestRedirectUriIsExact` |
| TC-13 | FR-11, FR-11a | `identity/oidc_codes.py` | automated | issued code | redeem; redeem again; wrong verifier; `plain` | single-use; replay revokes; PKCE enforced; `plain` refused | `test_oidc_provider.py::TestPkce`, `::TestCodeReplayRevokesWhatItBought` |

### Integration Layer

| ID | Verifies | Design ref | Method | Setup | Action | Expected | Test artifact |
|---|---|---|---|---|---|---|---|
| TC-14 | FR-1, FR-3, FR-19, AC-1, AC-28, AC-29 | `tokens.mint` | automated | both modes in one run | mint under each | legacy: HS256, no `kid`, `typ`, opaque `iss`. OIDC: ES256, `kid`, `token_use`, URL `iss` | `test_tokens_oidc_mode.py::TestTheTwoModesAreDistinct` |
| TC-15 | FR-19a, AC-31 | `tokens.verify` | automated | mint in one mode | verify in the other, both directions | refused each way | `test_tokens_oidc_mode.py::TestTheTwoModesAreDistinct::test_a_token_from_one_mode_is_refused_by_the_other` |
| TC-16 | FR-1, AC-2 | `tokens.verify` | automated | OIDC mode | verify with the **public key only** | succeeds — the property that distinguishes this phase from HS256 | `test_tokens_oidc_mode.py::TestRoundTrip::test_the_public_key_alone_verifies_a_token` |
| TC-17 | FR-7, FR-8, AC-11a..d, AC-12 | `tokens.mint` | automated | OIDC mode | mint; mint with `auth_time`; chain five renewals | `token_use`, `scope` on access only; `auth_time` preserved **exactly**; `iat` still mint time | `test_tokens_oidc_mode.py::TestClaims` |
| TC-18 | FR-7a, AC-11 | `tokens._required_claims` | automated | legacy mode | inspect the claim set | exactly the seven it always was — the flag leaks no new claim into a default deployment | `test_tokens_oidc_mode.py::TestClaims::test_no_claim_appears_in_legacy_mode_that_did_not_before` |
| TC-19 | FR-3, FR-4a, AC-5 | `tokens._verification_material` | automated | rotated key set | verify a token from a demoted key; forge an unknown `kid`; strip the `kid` | demoted verifies; unknown refused; **missing refused rather than falling back** | `test_tokens_oidc_mode.py::TestKidSelection` |
| TC-20 | FR-5, FR-6, FR-6b, AC-8, AC-8a, AC-10 | `api/oidc_discovery.py` | automated | flag on / off | fetch both `.well-known` paths | 200 with no credential when on; **404 when off, not an empty document**; every advertised endpoint resolves; advertised algorithms/grants/PKCE methods are the real ones | `test_oidc_discovery.py::TestDiscoveryDocument`, `::TestNoHalfSwitchedState` |
| TC-21 | FR-5 (issuer) | `identity/oidc.py` | automated | flag on | omit the issuer; supply http, a query, a fragment, no host; send a hostile `Host` header | each refused with the variable named; **`Host` never influences `iss`** | `test_oidc_discovery.py::TestTheIssuerMustBeConfigured` |
| TC-22 | NFR-2, NFR-2a, AC-24 | `oidc.discovery_document` | automated | flag on | fetch five times; then change the issuer | cache **hit count > 0**; a changed issuer is not served stale; no file write | `test_oidc_discovery.py::TestTheCacheActuallyServes` |
| TC-23 | FR-6b, FR-6c, AC-10a | `web_auth.AUTH_BYPASS_ENDPOINTS` | automated | flag on | call each bypassing route with no credential | `/token`, `/introspect`, `/revoke` → `invalid_client`; `/userinfo` → `invalid_token` + `WWW-Authenticate`; the bypass set is exactly the intended six | `test_oidc_discovery.py::TestTheGate`, `test_oidc_provider.py::TestTheBypassingRoutesAuthenticateThemselves` |

### System / End-to-End Layer

| ID | Verifies | Design ref | Method | Setup | Action | Expected | Test artifact |
|---|---|---|---|---|---|---|---|
| TC-24 | FR-10, FR-11, FR-7, FR-9 | `api/oidc_provider.py` | automated | registered confidential client | `/authorize` → `/token` → `/userinfo` | code issued, exchanged once, tokens usable; `azp`, `nonce`, `at_hash` correct; `state` echoed byte-for-byte | `test_oidc_provider.py::TestTheHappyPath` |
| TC-25 | FR-11a, AC-16 | code store + revocation | automated | one exchanged code | replay it, then re-use the first access token | replay refused **and the first exchange's tokens stop working** | `test_oidc_provider.py::TestCodeReplayRevokesWhatItBought` |
| TC-26 | FR-10 (refresh grant) | `_grant_refresh_token` | automated | exchanged code | `grant_type=refresh_token` | a new access token, different from the first | `test_oidc_provider.py::TestTheHappyPath::test_the_refresh_grant_returns_a_new_access_token` |
| TC-27 | FR-12, FR-12a, AC-17, AC-11e | `/userinfo` | automated | all three token kinds | present each; then add `?sub=` | access works; **id and refresh refused**; the subject comes from the token, never the query | `test_oidc_provider.py::TestUserinfo`, `::TestTokenTypeConfusion` |
| TC-28 | FR-13, FR-13a, AC-18, AC-18a | `/introspect`, `/revoke` | automated | active token | introspect with and without client auth; revoke; revoke an unknown token | unauthenticated refused; garbage is `active:false` not an error; revoked stops verifying; unknown still 200 | `test_oidc_provider.py::TestIntrospectionAndRevocation` |
| TC-29 | FR-19, AC-30 | every provider route | automated | flag off | call all five | 404 each — no configuration signs HS256 while advertising a JWKS | `test_oidc_provider.py::TestTheFlagGovernsEverything` |
| TC-30 | FR-14, NFR-7 | `web_auth.authenticated_principal` | automated | access token | present as cookie, then as `Authorization: Bearer` | both authenticate; SPA keeps `HttpOnly` cookies | `test_gate_token_auth.py` (Phase 2, unchanged) |

### Security Layer

| ID | Verifies | Design ref | Method | Setup | Action | Expected | Test artifact |
|---|---|---|---|---|---|---|---|
| TC-31 | FR-11 (authorization requires a session) | `/authorize` | automated | **no** patched principal | request a code | no code issued — this is the case the `signed_in` fixture bypasses | `test_oidc_provider.py::TestAuthorizeRequiresASignedInOperator` |
| TC-32 | FR-4b, AC-7 | `tokens._verification_material` | automated | OIDC mode | hand-roll `alg:HS256` HMAC'd with (a) our public-key PEM, (b) the published JWK; then `alg:none`; then tamper the payload | each refused. **Built by hand because PyJWT refuses to *encode* this**, which guards signing and says nothing about our verify | `test_tokens_oidc_mode.py::TestTheAttackThePublishedKeyCreates` |
| TC-33 | FR-15a, AC-20 | `Client.permits_redirect` | automated, parametrised | registered client | trailing slash, path suffix, query, fragment, subdomain prefix, suffix-domain, http-for-https, case change | all refused; **the refusal is not itself redirected** | `test_oidc_provider.py::TestRedirectUriIsExact` |
| TC-34 | FR-11 (code binding) | `CodeStore.redeem` | automated | code for client A | redeem as client B | refused | `test_oidc_provider.py::TestPkce::test_the_code_is_bound_to_the_client_it_was_issued_to` |
| TC-35 | NFR-3, AC-25a | key material handling | automated | a key set | inspect the JWKS, `repr`/`str` of the holder and the raw key, the metadata file, and an error raised on a damaged key set | the private scalar and `BEGIN` appear in none of them | `test_identity_keys.py::TestThePrivateKeyStaysOutOfEveryObservable` |
| TC-44 | NFR-5, AC-25 | `oidc_codes.CodeStore` | automated | capacity 16 | issue 200 codes; then issue past a code's TTL | outstanding never exceeds capacity; a lapsed code is **gone**, not merely unusable | `test_identity_keys.py::TestTheCodeTableIsBounded` |
| TC-45 | NFR-6, AC-24b | `_authorize_limiter` | automated | flag on, signed in | 40 authorization requests | 429 with `temporarily_unavailable` and `Retry-After`; **no code minted by a refused request**; five normal requests never shed | `test_oidc_provider.py::TestAuthorizeShedsLoad` |
| TC-46 | FR-1, FR-3, FR-6 (interoperability) | JOSE/JWK encoding | automated | flag on | verify every token kind using **no JWT library** — split on dots, base64url by hand, rebuild the key from the JWKS `x`/`y`, convert the JOSE raw `r‖s` signature to DER, check ECDSA directly; then configure from discovery alone | each verifies; signature is exactly 64 bytes; `kid` selects exactly one key after a rotation; a tampered payload fails | `test_oidc_conformance.py` |

### Performance / Accuracy Layer

| ID | Verifies | Design ref | Method | Setup | Action | Expected | Test artifact |
|---|---|---|---|---|---|---|---|
| TC-36 | NFR-1, AC-23 | ES256 vs HS256 verification | **measured, one-off** | PyJWT 2.13, 400 iterations, Apple silicon | time sign and verify for HS256/RS256/ES256 | RS256 +0.035 ms, ES256 +0.049 ms at p95 — both ~100× inside the 5 ms budget | recorded in the brief, §"Decision 1 in detail". **Not automated** — see §Coverage Gaps |

### Manual / Exploratory Layer

| ID | Verifies | Method | Expected | Status |
|---|---|---|---|---|
| TC-37 | end-to-end operator flow | manual | Enable the flag locally; sign in via SRP; fetch both `.well-known` documents | **PASS**, feature lead, 2026-09-02 ("tested the authentication and the well-known urls and all working") |
| TC-38 | FR-18, AC-27 | review | The upgrade note states every live session ends; `ENVIRONMENT.md` documents the flag, issuer, clients file and key directory | **PASS** — `docs/ENVIRONMENT.md` §"OIDC provider (Phase 2.5)" |

### Compatibility / Platform Layer

| ID | Verifies | Method | Expected | Test artifact |
|---|---|---|---|---|
| TC-39 | conformance without our own JWT library | automated | See TC-46. **Partially closes** the third-party gap: no PyJWT anywhere, but `cryptography` is still shared with it | `test_oidc_conformance.py` |
| TC-40 | tracked-tree parity | automated | Every new module imports from a tracked-only checkout | `bin/check-tracked-tree.sh` |

### Regression Layer

| ID | Verifies | Method | Expected | Test artifact |
|---|---|---|---|---|
| TC-41 | AC-28 — **the operative test of "off is identical"** | automated | The Phase 2 token suites pass **with no edits**: 154 tests across `test_identity_tokens.py`, `test_identity_token_invalidation.py`, `test_auth_srp_routes.py`, `test_gate_token_auth.py` | those four files, unmodified |
| TC-42 | route and action inventories | automated | The two snapshots move by exactly the 5 new routes and 3 new actions | `test_route_parity.py`, `test_action_coverage.py` |
| TC-43 | audit coverage (NFR-4, AC-24a) | automated | Every new mutating route declares an action, or the build fails | `test_action_coverage.py::test_every_mutating_route_declares_an_action` |

## Requirement Coverage

| Requirement | Covered by | Notes |
|---|---|---|
| FR-1, FR-2, FR-2a, FR-3 | TC-1, TC-2, TC-3, TC-14, TC-16 | |
| FR-4, FR-4a, FR-4b | TC-6, TC-19, TC-32 | |
| FR-4c, FR-4d, FR-4d1, FR-4d2, FR-4e, FR-4f | TC-4, TC-5, TC-6, TC-7, TC-11 | |
| FR-5, FR-6, FR-6a, FR-6b, FR-6c | TC-8, TC-20, TC-21, TC-23 | |
| FR-7, FR-7a, FR-8, FR-9 | TC-17, TC-18, TC-24 | |
| FR-10, FR-11, FR-11a | TC-13, TC-24, TC-25, TC-26, TC-34 | |
| FR-12, FR-12a, FR-13, FR-13a | TC-27, TC-28 | |
| FR-14 | TC-30 | satisfied by Phase 2's gate; verified, not rebuilt |
| FR-15, FR-15a | TC-12, TC-33 | |
| FR-16, FR-16a, FR-17 | — | **deferred**: federation needs an OIDC tenant |
| FR-18, FR-18a | TC-1, TC-38 | |
| FR-19, FR-19a | TC-14, TC-15, TC-29, TC-41 | |
| FR-19b | — | **deferred to cutover completion** (AC-32) |
| NFR-1 | TC-36 | measured once, not automated |
| NFR-2, NFR-2a | TC-9, TC-22 | |
| NFR-3 | TC-35 | |
| NFR-4 | TC-43 | |
| NFR-5 | TC-44 | single-worker assertion for the new table still relies on the shared `assert_single_worker_safe()` |
| NFR-6 | TC-45 | implemented as part of writing this plan |
| NFR-7 | TC-30 | |

## Layer Coverage

| Layer | Exercised | By |
|---|---|---|
| Unit | yes | TC-1..TC-13 |
| Integration | yes | TC-14..TC-23 |
| System / E2E | yes | TC-24..TC-30 |
| Security | yes | TC-31..TC-35, TC-45 |
| Performance | partially | TC-36, measured once |
| Manual | yes | TC-37, TC-38 |
| Compatibility | yes | TC-39/TC-46 (no JWT library), TC-40 |
| Regression | yes | TC-41..TC-43 |

## Coverage Gaps

Writing this document found five. **Four were closed rather than merely recorded**, which is the
argument for the plan-first order this phase skipped: each was cheap once named, and none had been
noticed while writing the code.

### Closed

1. **NFR-3 (private key in five observables)** — was covered only for the JWKS. Now TC-35 checks
   all five, including a deliberately damaged key set whose error message could have quoted the PEM.
2. **NFR-6 (`/authorize` shedding load)** — was **not implemented at all**, not just untested. Now
   a per-client token bucket ahead of the client lookup, with TC-45 asserting a refused request
   mints no code. Wiring it surfaced a live trap: `RateLimiter.check` returns `(allowed, retry_after)`
   and a tuple is always truthy, so the natural `if not limiter.check(...)` would have shed nothing,
   silently. Same shape of mistake `web_auth.authenticated_principal` returns a bare string to avoid.
3. **NFR-5 (bounded code table)** — `MAX_OUTSTANDING` was implemented and never exercised. TC-44
   covers the capacity and that a lapsed code is *removed* rather than merely refused.
4. **Third-party conformance** — the highest-value gap, since the phase exists to be verifiable by
   software we did not write. TC-46 verifies tokens using **no JWT library**: hand base64url, the
   key rebuilt from the JWKS coordinates, and the JOSE raw `r‖s` signature converted to DER. It
   catches swapped or wrongly-encoded coordinates (6 tests fail on either mutation) and configures
   itself from the discovery document alone.

   **It is not a complete closure and should not be recorded as one.** `cryptography` sits under
   both this and PyJWT, so a defect in the P-256 primitive is invisible to both. The genuinely
   independent check is a different language's implementation, which Grafana provides the first
   time it is pointed at a deployment.

### Still open

5. **NFR-1 is measured, not guarded.** A one-off benchmark on Apple silicon, in the brief. Nothing
   fails if verification gets slow, and AC-23 asks for a *recorded* baseline on the deployment's own
   hardware — a baseline measured in the same run can never detect a regression.

Two smaller ones, unchanged: `at_hash` is now recomputed from the spec (TC-46) but still never
checked by an external client, and the `signed_in` fixture's bypass means the gate-to-`/authorize`
wiring rests entirely on TC-31.

### What the mutation testing showed about ownership

Worth recording because it was not obvious. Mutating the **thumbprint** to little-endian fails
`test_identity_keys` and *nothing* in the conformance suite; mutating **`public_jwk`** the same way
fails six conformance tests and two key tests. The two files own different properties — one that
`kid` is a correct RFC 7638 thumbprint, the other that a stranger can verify the signature — and
neither substitutes for the other.

## Out-of-Scope Verification

- **Dynamic client registration** (RFC 7591) — non-goal; no client needs it, and a registration
  endpoint is an unauthenticated way to create trusted redirect targets.
- **Front/back-channel logout** — no relying party exists to log out.
- **Consent** — clients are operator-registered and first-party. If an external relying party is
  ever registered, its absence becomes a defect rather than a simplification.
- **Authorization semantics of `scope`** — Phase 3. `scope` is a claim here and gates nothing, which
  is why only `openid` is advertised.

## Open Questions

1. **Should gap 4 (third-party conformance) block closing the phase?** It is the only check that
   could catch a consistent misreading of the spec, and the phase exists to be interoperable.
2. **Is gap 2 (no `/authorize` rate limit) acceptable given the route requires a session?** NFR-6
   says shed before expensive work; the session requirement changes the threat but not the
   requirement.
3. **Where does AC-23's baseline live** so a regression can be detected rather than re-measured?
