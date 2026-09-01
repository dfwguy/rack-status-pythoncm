# PLC Feature Brief — auth-oidc-provider (Phase 2.5)

**Status:** requirements draft, 2026-09-01. Not yet reviewed.
**Programme:** `docs/AUTH_PROGRAMME_SCOPE.md` — Phase 2.5, between identity (Phase 2) and
authorization (Phase 3).
**Directed by:** feature lead, 2026-09-01 — "full OP", sequenced after Phase 2.

> **Requirement numbering is per-brief**, as in Phase 1 and Phase 2. `FR-4c` below is *this*
> document's FR-4c. Where a Phase 2 requirement is meant, it is written **`identity FR-4c`**. This
> is stated because the two briefs both have an FR-4c about JWT algorithms and they say different
> things — Phase 2's pins one algorithm, this one has to permit several at once.

## Purpose

Make the token layer conform to OpenID Connect so that **software we did not write can verify a
Rack Status token**, and so that Rack Status can act as an identity provider for the things around
it. Keep SRP exactly where it is.

**SRP and OIDC are not alternatives, and the whole phase rests on that.** SRP is an authentication
*method*: how a server satisfies itself that you know a password without the password crossing the
wire. OIDC standardises what happens *after* that check — token formats, claim names, endpoint
shapes, key publication, discovery. OIDC has never specified how a provider verifies a credential;
every provider does it privately. So SRP sits precisely where a provider's private credential check
has always sat, and replacing it with a form POST would be a downgrade dressed up as
standardisation.

The precedent is Cognito, which is what was asked for at the outset of this programme: its
`USER_SRP_AUTH` flow is SRP-6a internally, and what it publishes is an ordinary OIDC id/access/
refresh triad plus a JWKS endpoint. We arrived at the same split without planning to.

### What already conforms

Measured against the current implementation, not assumed:

- The **id/access/refresh triad is the OIDC triad**.
- All seven registered claims (`iss`, `aud`, `sub`, `exp`, `iat`, `jti`, `typ`) are present and
  **demanded** via PyJWT's `require` option rather than validated-if-present. That is stricter than
  most providers and stricter than PyJWT's default, which accepts a token with no `exp` and treats
  it as eternal.
- Refresh exists and is revocable by `jti`.

### What does not, and which part is structural

| Divergence | Consequence |
|---|---|
| **`HS256` symmetric signing** | **The blocker.** Verifying and minting are the same capability, so no key can ever be published |
| No `/.well-known/openid-configuration` | No client can self-configure |
| No `/.well-known/jwks.json` | Nothing external can verify a token |
| Custom `typ` claim | Standard is `token_use` (Cognito) or RFC 9068's *header* `typ` |
| No `auth_time`, `nonce`, `azp`, `scope`, `at_hash` | A relying party cannot tell how old the authentication is, nor bind a nonce against replay |
| `POST /api/auth/refresh` | OAuth 2.0 spells this `POST /token` with `grant_type=refresh_token` |
| No authorization-code flow | Nothing can delegate login to us |
| Cookie transport, not `Authorization: Bearer` | **Deliberate. Not a defect — see NFR-7** |

Only the first is structural. Everything else becomes straightforward once signing is asymmetric,
and is impossible while it is not.

## Assumptions

1. **The consumer list is short and known.** Today: nothing. Plausibly next: the Grafana embeds,
   which currently sit outside the auth story entirely, and a CLI or agent that needs a credential
   we can verify without sharing a minting secret. This phase should not build for hypothetical
   third parties — hence no dynamic client registration (see non-goals).
2. **Deployments remain single-worker** (`--workers 1`, asserted at startup by
   `assert_single_worker_safe()`). This phase *adds* to the in-process state that depends on it —
   see NFR-5, which is the requirement that stops this being discovered in production.
3. **One deployment, one issuer.** Rack Status is not multi-tenant. `iss` is derived from the
   deployment's own base URL.
4. **Air-gapped sites exist.** Anything requiring an outbound fetch is optional and degrades to a
   visible message, per identity FR-19a.
5. **A cutover that logs everyone out is acceptable if it is announced.** There is no way to make
   HS256 tokens verify under an asymmetric key; see FR-22.

## Proposed Functional Requirements

### Signing and keys

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-1 | Tokens shall be signed with an **asymmetric** algorithm — `RS256` or `ES256` — such that the key required to verify a token is not sufficient to mint one. | the structural blocker above | this is the requirement the rest of the phase depends on. Under HS256 "publish a verification key" and "publish a minting key" are the same sentence |
| FR-2 | The signing key pair shall be **generated on first boot** if absent, persisted in the deployment's own storage, and survive process and container restart. | operational; mirrors the identity store | resolves **Phase 2 design open question C**, which this phase forces: derived-from-`RACK_WEB_SECRET_KEY` is not available for an asymmetric key, so the key needs its own home |
| FR-2a | The private key file shall be owner-only at every observable moment, including during a rewrite. It shall be written by **creating the replacement with the target mode and then atomically replacing** — never by writing then correcting the mode. | `identity-auth-traps.mdc` trap 3, **re-measured 2026-09-01** | the trap is real but the rule states its cause imprecisely, and the imprecision led me to draft the workaround. **`os.replace` preserves the *source* inode's mode**; the 0644 comes from creating the temp with a plain `open()`, which takes the umask. Measured: `open()`+replace → 0644, `os.open(…, 0o600)`+replace → **0600**, `tempfile.mkstemp`+replace → 0600 (mkstemp defaults to 0600). So "re-apply the mode after every write" is a correction applied *after* a world-readable inode already exists — a window, not a fix. Creating the temp correctly gives atomicity **and** permissions with no window and no re-chmod. `bin/gen-tls-cert.py::_write` already does the non-atomic version of this (`os.open` in place) for exactly this reason. **Keep testing across several writes** — a single-write test passes on the broken version either way |
| FR-3 | Every minted token shall carry a **`kid`** in its JOSE header identifying the key that signed it. | RFC 7515 §4.1.4; prerequisite for FR-4 | without `kid` a verifier must trial-verify against every published key, and rotation cannot be non-disruptive |
| FR-4 | The product shall support **more than one valid verification key at a time**: a rotation shall publish the new key, sign new tokens with it, and continue to verify tokens signed by the previous key until they expire. | operational; consequence of FR-1 | rotation that invalidates outstanding tokens is an outage, and an outage nobody will schedule is a rotation nobody will perform. The overlap window is at least the maximum refresh lifetime (30 days, per the Phase 2 ceiling) |
| FR-4a | A token whose `kid` names no published key shall be refused. A token with **no** `kid` shall be refused. | consequence of FR-3 | a missing `kid` must not silently fall back to "try the current key", which would re-admit exactly the tokens rotation is meant to retire |
| FR-4b | The product shall refuse any token whose header names an algorithm outside the deployment's configured set, and shall **never** accept `alg: none`. | carried forward from **identity FR-4c**, restated because the set is no longer a single value | the classic break, and it gets *easier* to make here: with a published public key, an attacker who substitutes `alg: HS256` and signs with that public key as the HMAC secret is verifying against a value we handed them. Under HS256-only this attack had no material to work with; from this phase on it does |
| FR-4c | A signing key shall have a **signing period defaulting to one year**, after which it is demoted to verify-only and a successor becomes active. | feature-lead direction 2026-09-01 | the key is deliberately far longer-lived than any token. A JWKS that changes often is a JWKS that clients must re-fetch often, and the client that gets it wrong fails intermittently — a rotation is only invisible to a client that re-fetches on an unknown `kid`, and not all of them do. An ES256 key signing on the order of 10⁵ tokens a year is nowhere near a cryptographic limit, so the rotation cadence is an operational choice, not a cryptographic one |
| FR-4d | A demoted key shall remain a **valid verifier for at least the longest lifetime any token it signed can have** — i.e. no less than the configured refresh-token ceiling. The product shall **refuse to start** if the configured key tail is shorter than that ceiling. | **the invariant, and the reason this is a requirement rather than a constant** | get this wrong and tokens stop verifying *before the `exp` they advertise*. That does not present as a configuration error; it presents as operators being logged out at random, on a schedule nobody connects to a key setting. The check is cheap and the failure mode is expensive, so it is enforced at startup rather than documented |
| FR-4e | A signing key reaching the end of its signing period shall **rotate automatically**, not refuse to sign. | operational; consistent with the TLS CA's reasoning | `bin/gen-tls-cert.py` already records the principle for the deployment CA: "an expiry is an outage nobody is watching for". A signing key that expires into a refusal takes authentication down on a date chosen a year earlier, on a cluster with no one watching for it |
| FR-4f | Rotation shall be invocable **on demand**, independently of expiry. | consequence of FR-4c's one-year default | with a yearly cadence the rotation path would otherwise execute once a year in production and never in anger. The time you need it is immediately after a suspected key compromise, which is the worst moment to discover it does not work |

### Discovery and key publication

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-5 | The product shall serve **`GET /.well-known/openid-configuration`** conforming to OpenID Connect Discovery 1.0 §3, advertising only the endpoints, algorithms, scopes and grant types it actually implements. | OIDC Discovery 1.0 | "advertises only what it implements" is the testable half. A discovery document listing an endpoint that 404s is worse than no document, because a client configures itself and then fails at the point of use |
| FR-6 | The product shall serve **`GET /.well-known/jwks.json`** as a JWK Set containing the **public** half of every currently valid verification key, each with its `kid`, `kty`, `alg` and `use`. | RFC 7517 | |
| FR-6a | The JWKS response shall contain **no private key material** — no `d`, `p`, `q`, `dp`, `dq`, `qi` for RSA, no `d` for EC. | **the one mistake in this phase that is unrecoverable** | serialising a key object with the wrong helper publishes the private key to every unauthenticated caller, and the endpoint still looks correct in a browser. This is asserted by field name rather than by inspection, and asserted for every key in the set, not the first |
| FR-6b | Both `.well-known` endpoints shall be reachable **without authentication**, and shall be the only new unauthenticated routes this phase adds. | OIDC requires it; auth citizen 11 | they are on the gate's bypass list for the same reason `api_login` is. The second clause is what keeps that list reviewable |

### Claims

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-7 | Tokens shall carry the standard claims for their kind: **`token_use`** distinguishing id / access / refresh; **`auth_time`**; **`azp`** where a client is involved; **`scope`** on access tokens; **`nonce`** on an id token when one was requested; and **`at_hash`** on an id token issued alongside an access token. | OIDC Core 1.0 §2, §3.1.3.6 | **six claims, deliberately kept as one requirement but with one AC per claim** (AC-11a..f). Bundling was flagged in self-review: a single "tokens carry standard claims" AC would pass with only `token_use` implemented. `at_hash` was in the divergence table and missing from the requirements — a drafting gap, not a decision |
| FR-7a | The custom `typ` **claim** shall be retired in favour of `token_use`, and a token carrying the old claim shall not be honoured after the cutover. | consequence of FR-7 | `typ` is a *header* parameter in JOSE with a different meaning (RFC 9068 uses it for `at+jwt`); keeping our claim of the same name invites a verifier to check the wrong one. Since FR-22 invalidates every outstanding token anyway, there is no transition window to support — this is the cheapest moment in the product's life to make this change |
| FR-8 | **`auth_time` shall record when the user actually authenticated**, not when the token was minted, and shall be **preserved across refresh**. | OIDC Core 1.0 §2 | the subtle one. A refresh that resets `auth_time` makes a 30-day-old session claim it authenticated a moment ago, which defeats every `max_age` check a relying party might make. It is also invisible: the value is well-formed and plausible either way |
| FR-9 | Where an authentication request supplies a `nonce`, the id token shall echo it **unchanged**, and a client-supplied value shall never be interpreted as anything but an opaque string. | OIDC Core 1.0 §3.1.3.7 | the nonce is the client's replay defence and it only works if we return it verbatim |

### Endpoints and flows

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-10 | The product shall serve **`POST /token`** supporting `grant_type=authorization_code` and `grant_type=refresh_token`, with OAuth 2.0 form encoding and error responses (RFC 6749 §5.2). | RFC 6749 | the existing `POST /api/auth/refresh` may remain as the SPA's path; it becomes a thin caller of the same logic rather than a second implementation (citizen rule: one mechanism, many renderings) |
| FR-11 | The product shall implement the **authorization code flow with PKCE**, requiring `code_challenge_method=S256` and rejecting `plain`. | RFC 7636; OAuth 2.1 direction | `plain` offers no protection against an intercepted code and exists only for clients that cannot hash; we have no such client, so accepting it would be a weakness with no beneficiary |
| FR-11a | An authorization code shall be **single-use** and short-lived (≤ 60 s). A second presentation shall be refused **and shall revoke the tokens already issued from it**. | RFC 6749 §4.1.2; OAuth 2.1 | the second clause is the one that matters and the one usually missed: a replayed code means the code leaked, so the safe response is to withdraw what it bought, not merely to decline the replay |
| FR-12 | The product shall serve **`GET /userinfo`**, returning claims for the subject of the presented access token, and nothing about any other subject. | OIDC Core 1.0 §5.3 | |
| FR-12a | An **id token shall not be accepted where an access token is required**, and a refresh token shall not be accepted at `/userinfo` or on an API route. | **gap found in self-review** — the draft implied this via FR-7's `token_use` but never required the check | token-type confusion is the standard OIDC attack: the id token is handed to the browser as a claims carrier, is often logged by clients, and is not meant to be a credential. Phase 2's `verify` already demands a matching type (INV-09..11); this requirement is that the property survives the rename from `typ` to `token_use` rather than being lost in it |
| FR-13 | The product shall serve token **introspection** (RFC 7662), requiring client authentication. | RFC 7662 §2.1 | an unauthenticated introspection endpoint is a token-validity oracle. **Split from revocation in self-review**: two endpoints from two RFCs that fail independently, so one requirement could be half-implemented and still read as met |
| FR-13a | The product shall serve token **revocation** (RFC 7009), such that a revoked token stops verifying within the bound already set for revocation propagation. | RFC 7009; split from FR-13 | ties to the existing revocation machinery rather than inventing a second path — the identity store already owns this (citizen 8) |
| FR-14 | The product shall accept a valid access token presented as **`Authorization: Bearer`** on API routes, in addition to the existing cookie. | consequence of being an OP | this is what makes a CLI or a Grafana embed possible at all |
| FR-15 | Registered clients shall be **configured**, each with a `client_id`, a set of **exact** `redirect_uri` values, and a type (public or confidential). | RFC 6749 §3.1.2 | |
| FR-15a | A `redirect_uri` shall be matched by **exact string comparison**. Prefix, wildcard and subdomain matching shall not be implemented. | RFC 6749 §3.1.2.2 | open redirect on an authorization endpoint hands the authorization code to the attacker, which is a full account takeover. Every well-known instance of this bug is a relaxed matcher — this requirement exists to make the *permissive* version fail review, not merely to describe the strict one |

### Federation (the client half)

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-16 | The login screen shall offer local username-and-password entry and, when federation is configured, a clearly labelled button per configured provider — initially **NVIDIA SSO**. | **identity FR-19**, folded into this phase | it is the RP half and needs none of the OP work; it moved here because it lands in the same code |
| FR-16a | A configured-but-unreachable provider shall leave local login usable and shall say the provider is unavailable. | **identity FR-19a** | |
| FR-17 | When validating a token from a federated provider, the product shall verify `iss` against the expected issuer for that provider, and shall never accept a provider's token where its own is required, nor the reverse. | **the trap this phase introduces** | `identity-auth-traps.mdc` records that `iss`/`aud` do not separate deployments because they are product constants. **That stops being true here**, and the rule needs updating as part of this phase. Accepting the wrong issuer's token is a full authentication bypass, and PyJWT does not check `iss` at all unless `issuer=` is passed (trap 6, measured) |

### Cutover

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-18 | The migration from HS256 to asymmetric signing shall be **documented as a breaking change that ends every live session**, with the operator-visible consequence stated. | INV-17; assumption 5 | there is no dual-verification trick worth building: honouring HS256 tokens during a window means keeping the symmetric secret live, which is the thing being removed. One clean break, announced |
| FR-18a | On upgrade, a deployment with no key pair shall generate one and start normally — the upgrade shall not require an operator action beyond re-authenticating. | operational | |
| FR-19 | The asymmetric signing path shall be selectable by a **single deployment flag, defaulting to the existing HS256 behaviour**, so that merging this phase does not by itself end any session. With the flag off, the product shall behave exactly as before: HS256 tokens, no `kid`, and the OP endpoints absent from discovery. | **decision 4** | a flag that only half-switches is worse than none — a deployment signing HS256 while advertising a JWKS would publish a key that verifies nothing, and every external consumer would fail in a way that looks like our bug |
| FR-19a | Turning the flag on shall be **one-way in effect**: tokens minted under it do not verify with the flag off, and vice versa. The flag shall be documented as a scheduled cutover, not a runtime toggle. | consequence of FR-19 | stated because "there is a flag" invites someone to flip it back when something looks wrong, which signs everyone out a *second* time rather than restoring service |
| FR-19b | The flag and the HS256 path shall be **removed** once the cutover is complete; the brief's acceptance is not met while both paths remain in the tree indefinitely. | the cost accepted in decision 4 | a dead flag that nobody deletes becomes a permanently untested second code path through the authentication gate. Scheduling the removal here is what makes decision 4 affordable |

## Proposed Non-Functional Requirements

| ID | Requirement | Source | Notes |
|---|---|---|---|
| NFR-1 | Token verification shall add **no more than 5 ms** at the 95th percentile over the current HS256 path. | Phase 1 precedent (the 5 ms revocation budget) | **Measured 2026-09-01 (see decision 1): RS256 +0.035 ms, ES256 +0.049 ms at p95 — both roughly 100× inside the budget.** So this NFR is a *regression guard*, not a selection criterion; the draft claimed it would decide FR-1's algorithm and it cannot. Keep it, because a future change that makes verification slow should still fail, but choose the algorithm on size and signing cost instead |
| NFR-2 | The JWKS and discovery documents shall be cacheable, and shall not be regenerated per request. | | |
| NFR-2a | Generating the discovery and JWKS responses shall **not** perform a write. | `identity-auth-traps.mdc` trap 2 | the revocation snapshot pruned on read, TinyDB's `remove()` rewrote the file even when nothing matched, the mtime changed, and the cache it fed **never hit once** while every functional test passed. Same shape of bug is available here |
| NFR-3 | The private key's PEM body shall not appear in: the JWKS response, the `repr` or `str` of any object holding it, the body of any 4xx/5xx response produced on a signing or verification failure path, or any audit event. | FR-6a; **narrowed in self-review** | the draft said "never appear in a log line", which no test can establish — it quantifies over every future log statement. These five are each an observable a test can actually check, and they are where it would realistically leak. The unbounded version reads stronger and verifies nothing, which is the worse of the two |
| NFR-4 | Every new endpoint shall be declared in the action vocabulary and audited, per the Phase 1 mechanism. | Phase 1 NFR-8 | the build fails on an undeclared action, so this is enforced rather than remembered |
| NFR-5 | Any in-process state added by this phase (the authorization-code table) shall be **bounded**, and the single-worker dependency shall be **asserted at startup**, not assumed. | Phase 2 design open question A, which this phase makes worse | the SRP challenge table already has this dependency. Authorization codes add a second: with two workers, a code issued by one and redeemed against the other fails **about half the time**, which presents as an intermittent client bug rather than a configuration error. `assert_single_worker_safe()` exists; this requirement is that the new table is covered by it |
| NFR-6 | The authorization endpoint shall shed load before any expensive operation, as the SRP `begin` route does. | Phase 2 FR-4g | |
| NFR-7 | The SPA shall continue to receive tokens in `HttpOnly` cookies. Bearer acceptance (FR-14) shall be **additional**, not a replacement. | deliberate deviation | `HttpOnly` beats a Bearer token in `localStorage`, which XSS can read and a cookie it cannot. This is a divergence from how OIDC apps are usually *demonstrated*, not from the specification — a provider may set cookies for its own first-party UI and serve Bearer tokens to API clients. Recorded as a requirement so it is not "corrected" later by someone reading a tutorial |

## Acceptance Criteria

| ID | Verifies | Criterion | Method |
|---|---|---|---|
| AC-1 | FR-1 | A minted token's header names `RS256` or `ES256`, and verification succeeds with the public key alone. | automated |
| AC-2 | FR-1 | The public key **cannot** mint a token that the product will then accept. | automated — the property that distinguishes this phase from HS256 |
| AC-3 | FR-2, FR-18a | Starting with no key pair generates one and the product serves traffic; restarting reuses it rather than generating a second. | automated |
| AC-4 | FR-2a | After **three** successive writes the private key file is still owner-only. | automated — three, because one passes on the broken version |
| AC-5 | FR-3, FR-4a | Every minted token has a `kid`; a token with an unknown `kid` is refused; a token with no `kid` is refused. | automated |
| AC-6 | FR-4 | After rotation, a token signed by the previous key still verifies, a new token is signed by the new key, and JWKS contains both. | automated |
| AC-6a | FR-4c | The default signing period is one year, and is configurable. | automated |
| AC-6b | FR-4d | A token minted immediately before its key is demoted still verifies at any point up to its own `exp`, including after the demotion. | automated with a controlled clock — **the invariant made concrete**, and the case a "does rotation work" test misses by only checking freshly minted tokens |
| AC-6c | FR-4d | Startup refuses when the configured key tail is shorter than the refresh-token ceiling, and the message names both values. | automated — naming both is what turns this from a puzzle into a fix |
| AC-6d | FR-4e | A key past its signing period rotates rather than failing to sign. | automated with a controlled clock |
| AC-6e | FR-4f | Rotation can be triggered on demand, and the previous key keeps verifying its outstanding tokens afterwards. | automated |
| AC-7 | FR-4b | `alg: none` is refused; an `HS256` token signed with the published **public key** as the HMAC secret is refused. | automated — the attack the public key enables |
| AC-8 | FR-5 | Every endpoint named in the discovery document resolves to a route that exists. | automated — walks the document against the URL map |
| AC-8a | FR-5 | Every algorithm, scope and `grant_type` advertised in the discovery document is actually accepted, and every one accepted is advertised. | automated — **added in self-review**: AC-8 checked only the endpoints, so a document advertising `RS256` on an ES256-only deployment, or a `grant_type` we reject, would have passed. Both directions, because silently supporting something undeclared is how an unreviewed flow stays unreviewed |
| AC-9 | FR-6a | For every key in the JWKS, no private field (`d`, `p`, `q`, `dp`, `dq`, `qi`) is present. | automated |
| AC-9a | FR-6 | The JWKS contains one entry per currently valid verification key, each carrying `kid`, `kty`, `alg` and `use`; and a token minted by the product verifies against the JWKS entry its `kid` names, using nothing but that entry. | automated — **added in coverage check**: AC-9 asserted what the JWKS must *not* contain and nothing asserted it is usable. "Verifies using nothing but that entry" is the property an external consumer actually depends on |
| AC-10 | FR-6b | Both `.well-known` paths return 200 with no credential, and no other new route does. | automated |
| AC-11 | FR-7a | A token carrying only the retired `typ` claim is refused. | automated |
| AC-11a | FR-7 | Each token kind carries the correct `token_use`. | automated |
| AC-11b | FR-7 | An id token issued alongside an access token carries a correct `at_hash` (left-most half of the SHA-256 of the access token, base64url). | automated |
| AC-11c | FR-7 | `azp` is present when a client is involved and names that client. | automated |
| AC-11d | FR-7 | `scope` is present on access tokens. | automated |
| AC-11e | FR-12a | An id token is refused on an API route and at `/userinfo`; a refresh token is refused at both. | automated |
| AC-12 | FR-8 | `auth_time` after a refresh equals the value from the original authentication, not the refresh time. | automated — **the one most likely to pass by accident**, since either value is well-formed; the test must compare against the recorded original, not merely assert presence |
| AC-13 | FR-9 | A supplied `nonce` is echoed byte-for-byte in the id token. | automated |
| AC-13a | FR-9 | A `nonce` containing characters that are meaningful in JSON, HTML or a URL is returned unchanged and does not alter the token's structure. | automated — the claim is opaque and client-controlled, so it is an injection surface as well as a replay defence |
| AC-14 | FR-10 | `POST /token` with each supported `grant_type` succeeds; an unsupported one returns an RFC 6749 §5.2 error body. | automated |
| AC-15 | FR-11 | `code_challenge_method=plain` is rejected; `S256` succeeds; a wrong `code_verifier` is rejected. | automated |
| AC-16 | FR-11a | A code redeemed twice is refused **and** the tokens issued from the first redemption stop working. | automated |
| AC-17 | FR-12 | `/userinfo` returns the bearer's own claims; a token for user A cannot retrieve user B. | automated |
| AC-18 | FR-13 | Introspection without client authentication is refused; with it, an active token reports `active: true` and an expired one `active: false`. | automated |
| AC-18a | FR-13a | A revoked token stops verifying. | automated |
| AC-19 | FR-14 | The same access token authenticates as a cookie and as `Authorization: Bearer`. | automated |
| AC-19a | FR-15 | An authorization request naming an unregistered `client_id` is refused; a registered one with a registered `redirect_uri` proceeds. | automated — **added in coverage check** |
| AC-20 | FR-15a | A `redirect_uri` differing by trailing slash, subdomain, path suffix or query string is rejected. | automated, parametrised — the permissive matcher must fail |
| AC-21 | FR-17 | A token from the federated provider is refused where our own is required, and ours is refused where the provider's is required. | automated |
| AC-22 | FR-16, FR-16a | The login screen renders local entry plus one button per configured provider; an unreachable provider leaves local login usable and says so. | automated render test |
| AC-23 | NFR-1 | Measured p95 verification overhead against a **recorded** HS256 baseline is within budget, on the deployment's own hardware. | measured, recorded in the evidence log — "recorded" because a regression guard compared against a baseline measured in the same run cannot detect a regression |
| AC-25a | NFR-3 | The private key's PEM body appears in none of the five observables NFR-3 names. | automated, one assertion per observable |
| AC-24 | NFR-2, NFR-2a | The discovery and JWKS caches are shown to **actually serve** reads — hit count > 0 under repeated calls — and repeated reads perform **no write** (file mtime unchanged). | automated — a cache that never hits cannot be shown to be stale, so staleness tests alone prove nothing. The mtime half is trap 2 exactly: TinyDB's `remove()` rewrote on every read, changing the mtime that was part of the cache key, so the cache invalidated itself and never hit once while every functional test passed |
| AC-24a | NFR-4 | Every route added by this phase appears in the action vocabulary, and an added route without a declared action fails the build. | automated — the Phase 1 mechanism already enforces this; the AC records that it covers the new routes too |
| AC-24b | NFR-6 | Under sustained unauthenticated load, the authorization endpoint refuses beyond its limit **without** performing the expensive operation. | automated — measured by counting the expensive call, not by timing, so the test does not turn flaky on a loaded runner |
| AC-25 | NFR-5 | The authorization-code table is bounded under sustained issuance, and startup refuses a multi-worker configuration. | automated |
| AC-26 | NFR-7 | After this phase the SPA still receives `HttpOnly` cookies and no token is written to `localStorage`. | automated |
| AC-27 | FR-18 | The upgrade note states that every live session ends, and `ENVIRONMENT.md` documents the new key material. | review |
| AC-28 | FR-19 | With the flag off, tokens are HS256 with no `kid`, the OP endpoints are absent, and the Phase 2 token tests pass unchanged. | automated — "Phase 2's tests pass unchanged" is the real assertion: the flag is only safe if off means *identical*, not *similar* |
| AC-29 | FR-19 | With the flag on, tokens are ES256 with a `kid` and the OP endpoints are present in discovery. | automated |
| AC-30 | FR-19 | There is no configuration in which the product signs HS256 while advertising a JWKS, or signs ES256 while advertising none. | automated — the half-switched state, which is the one that would strand every external consumer |
| AC-31 | FR-19a | A token minted with the flag on is refused with it off, and the reverse. | automated |
| AC-32 | FR-19b | After the cutover, no HS256 signing path and no flag remain in the tree. | **deferred — verified at cutover completion, not at implementation.** This criterion cannot pass while the phase is being built, by construction: FR-19 requires both paths to exist. It is recorded as an explicit gate on *closing* the phase rather than omitted, because the alternative is a requirement with no criterion, which is how a temporary flag becomes permanent. Same treatment as identity FR-18c |

## Testability self-review, 2026-09-01

The independent verifier could not run — the model it requires is unavailable in this team's
inference region — so this is a **self-review, and it should be treated as weaker evidence than
Phase 2's**, which had a second reader. Worth re-running when the verifier is available.

Seven findings, all applied above:

1. **NFR-3 asserted an unbounded negative.** "Shall never appear in a log line" quantifies over
   every future log statement, so no test can establish it. Narrowed to five named observables.
   The original reads stronger and verifies nothing.
2. **FR-7 bundled six claims behind one AC.** An implementation delivering only `token_use` would
   have passed AC-11. Now one AC per claim.
3. **`at_hash` was in the divergence table but in no requirement** — a drafting gap. Added.
4. **FR-13 bundled introspection and revocation**: two RFCs, two endpoints, independent failure
   modes. Split into FR-13 / FR-13a.
5. **AC-8 under-verified FR-5.** It checked that advertised endpoints exist but not that advertised
   *algorithms, scopes and grant types* are real, which is most of what FR-5 says. Added AC-8a,
   covering both directions.
6. **Token-type confusion was implied but never required.** FR-7 introduces `token_use` and the
   draft assumed the check came with it. Phase 2 has that property today (INV-09..11); the rename
   from `typ` is exactly where it could be silently lost. Added FR-12a and AC-11e.
7. **AC-23 could not detect a regression** — comparing against a baseline measured in the same run
   always passes. Now compares against a recorded baseline.

Two things I looked for and did **not** find: no requirement here restates a standard without an
observable property of our own system, and no AC verifies something adjacent to the requirement it
cites. The `nonce` requirement gained a second AC (AC-13a) because an opaque client-controlled
value echoed into a token is an injection surface as well as a replay defence.

## Decisions taken, 2026-09-01

Settled by the feature lead. Recorded with what each one costs, because the cost is the part that
gets forgotten.

| # | Decision | Consequence accepted |
|---|---|---|
| 1 | **ES256** | 23 µs slower verification per request than RS256, and narrower support in very old client libraries. Bought: 359-byte tokens instead of 615, and 32× faster signing |
| 2 | **Key pair in `var/secrets/`**, beside `RACK_WEB_SECRET_KEY` | The identity store, the vault master key and now the signing key share one directory and one volume, so losing `rack-data` loses all three at once. That is already true of the first two and the recovery is the same: regenerate, everyone re-authenticates |
| 3 | **`POST /token` sits beside `/api/auth/refresh`**, which delegates to shared logic | Two URLs for one operation, which must not become two implementations. The existing route becomes a thin caller — if it ever grows its own behaviour, the drift is the bug |
| 4 | **Cutover behind a flag, default off** | A dead flag to remove afterwards, and a window in which both signing paths exist in the code. FR-19 below specifies it so the removal is scheduled rather than forgotten |

### Decision 1 in detail — the measurement, and how it corrected the draft

Measured on this machine, PyJWT 2.13 / `cryptography`, 400 iterations, RSA 2048 and P-256:

   | | sign p50 | verify p50 | verify p95 | token bytes |
   |---|---|---|---|---|
   | HS256 (today) | 0.008 ms | 0.010 ms | 0.011 ms | 316 |
   | RS256 | **0.924 ms** | 0.032 ms | 0.050 ms | **615** |
   | ES256 | **0.029 ms** | 0.055 ms | 0.065 ms | **359** |

   Three things this changes. **NFR-1 does not discriminate** — both are ~100× inside the 5 ms
   budget, so the number I wrote to decide this cannot decide it. **RS256 verifies slightly
   *faster*** than ES256, which is the opposite of the draft's claim; that is RSA's small public
   exponent, and it is the operation we perform most (every request, versus signing only at login
   and refresh). And **RS256 signs 32× slower and produces 71% larger tokens**.

   At ~20 operators the signing gap is irrelevant in absolute terms, and the verify gap is 23
microseconds. So the honest deciding factor is **token size**: 615 vs 359 bytes, doubled because
access and refresh both ride in cookies on every request. **ES256**, and worth re-confirming on the
deployment's own hardware, since these are Apple silicon numbers.

Decision 2 also resolves **Phase 2 design open question C**, which this phase forced: an asymmetric
key cannot be derived from `RACK_WEB_SECRET_KEY` the way the HMAC key was, so it needed a home.

## Design Delta

### Decision 5 — instance identity is one bootstrap, but not one key

Feature-lead direction, 2026-09-01: the deployment already mints a TLS certificate on first boot,
and the token-signing key should be bootstrapped by that same act, because both answer *which
Rack Status is this*.

That framing is right and it is worth naming, because the two halves are usually treated as
unrelated plumbing. A deployment has a **cryptographic instance identity** with two faces:

- the **TLS key and certificate** — the identity you check when you connect, already fingerprintable
  via `bin/gen-tls-cert.py --fingerprint`;
- the **token signing key** — the identity you check when you're handed a token that claims to come
  from it. Its public half is the JWKS, and from this phase on it is what makes a Rack Status token
  verifiable by anything other than Rack Status.

**They bootstrap together. They are not the same key**, and that is the load-bearing part of this
decision:

| | TLS key | Signing key |
|---|---|---|
| Exposure | presented in every handshake; held by gunicorn | never leaves the app process |
| Compromise means | impersonate the server to a client, MITM the session | **mint a token for any user** — a total authentication bypass |
| Rotation | 730-day leaf, re-issued as a unit | overlapping, driven by the 30-day refresh ceiling (FR-4) |
| Published half | the certificate, to whoever connects | the JWK, to anyone at all |

Reusing one key for both would couple those rows: a TLS re-issue would invalidate every token, and
the key with the larger attack surface would be the one that can forge identity. Key separation is
the standard rule and this is a clean instance of why it exists.

**What is shared is the mechanism, not the material.** One module owns "generate, load, persist,
permission and rotate a P-256 key pair"; TLS and signing are two callers. The existing
`bin/gen-tls-cert.py` already generates **P-256** (`_key()` → `SECP256R1`) and already writes private
material with `os.open(..., 0o600)` rather than chmod-after — the same curve we chose in decision 1,
and the correct write pattern from FR-2a. So this is extending a mechanism that exists, not adding
one beside it.

**Both live under `var/secrets/`** (decision 2), which is already gitignored and already on the
`rack-data` volume. Note the canonical TLS path is `var/secrets/tls/`, **not** `var/tls/` — a stray
`var/tls/` in a developer tree is a manual run, not a product path.

### Bootstrap sequence

Ordered by dependency; each step is a no-op if its material already exists, so a restart changes
nothing:

1. `RACK_WEB_SECRET_KEY` → `var/secrets/.rack_web_secret_key` *(exists today)*
2. TLS key + certificate → `var/secrets/tls/` *(exists today, Phase 0.5)*
3. **Signing key pair → `var/secrets/oidc/` (new)**
4. Identity store + bootstrap admin *(exists today, Phase 2)*

Step 3 sits before step 4 deliberately: the admin bootstrap prints a one-time password and the
operator's first act is to sign in, which mints a token — so the key must exist before the first
credential does.

### `kid` derivation

The `kid` is the **base64url SHA-256 JWK thumbprint (RFC 7638)** of the public key, not a counter or
a uuid. Three reasons, in order of how much they matter:

- It is **derivable by the verifier** from the JWK itself, so a client can check that the `kid` in a
  header names the key it thinks it does, rather than trusting our label.
- It is **stable across restarts** without persisting anything extra — a counter needs state, and
  state that can reset produces two different keys claiming `kid: 1`, where the second silently
  fails to verify the first's tokens.
- Rotation gets a natural identity: two keys cannot collide unless they are the same key.

### Rotation, and the one property that makes it non-disruptive

Keys live in a small set, not a single slot. Exactly one is **active** (signs); all valid ones
**verify** and appear in the JWKS. A rotation appends a new active key and demotes the previous one
to verify-only.

The failure this avoids is worth stating: rotate by *replacing* the key and every outstanding token
becomes invalid at once, which is the same event as the FR-18 cutover. A rotation that logs everyone
out is a rotation nobody will ever schedule, which means the key is never rotated at all.

### Key lifetime — three durations, and only one of them is a free choice

Feature-lead direction 2026-09-01: the key is long-lived, because a JWKS that changes often is one
that clients must re-fetch often, and a client that gets that wrong fails intermittently.

Three durations, which are routinely conflated into one:

| | Meaning | Value |
|---|---|---|
| **Signing period** `S` | how long this key is the active signer | **1 year**, default; a free operational choice |
| **Verification tail** `V` | after demotion, how long it still verifies | **derived: ≥ the refresh-token ceiling** — not a free choice |
| **Total validity** | `S + V` | ~13 months at the defaults |

**`V` is forced, and this is the part worth checking.** A key stops signing at the end of `S`, so
the last token it signs is minted at that instant and lives until `S_end + T_max`, where `T_max` is
the longest configurable token lifetime — today the 30-day refresh ceiling from Phase 2. So the key
must verify until then. Set `V` shorter and tokens stop verifying **before the `exp` they
advertise**, which does not look like a misconfiguration: it looks like operators being logged out
at random. FR-4d makes it a startup refusal for that reason.

The stated example was "6-month tokens, 1-year key". The ratio and the absolute point in slightly
different directions against our actual numbers, so both are honoured rather than averaged: the
**1 year is taken as the signing period**, and the tail is **derived from the real ceiling (30 days)
rather than fixed at half the key life**. Deriving it matters more than the number — if someone
later raises the refresh ceiling to 6 months, `V` must grow with it, and a hard-coded tail would
silently start truncating tokens instead.

**One consequence to accept:** a compromised key stays valid for up to a year unless someone
rotates it. That is what FR-4f is for — on-demand rotation, so the response to a suspected
compromise is a command rather than a wait. Long-lived keys are only safe when rotation is a
routine operation rather than an emergency procedure, which is also why the path should be
exercised rather than left to fire once a year.

### What this changes in existing citizens

- **Auth (citizen 11)** keeps its single gate. `tokens.signing_key()` becomes "the active private
  key"; `tokens.verify` gains `kid` lookup against the key set. The gate itself does not change.
- **Configuration (citizen 5)**: the flag (FR-19), the algorithm and the key directory are settings,
  read through the accessors — not `os.environ.get` at the call site.
- **API modules (citizen 0)**: the OP endpoints are new files under `backend/api/`. The two
  `.well-known` paths need the auth bypass list (FR-6b), which is the only new bypass this phase adds.
- **The action vocabulary** grows by the OP endpoints; undeclared actions fail the build (NFR-4).
- **`identity-auth-traps.mdc` needs two edits** as part of this phase: trap 3's cause is stated
  imprecisely (see FR-2a), and the note that `iss`/`aud` do not separate deployments stops being
  true once we verify a federated provider's tokens alongside our own (FR-17).

### Design open questions

- **A. Where the authorization-code table lives.** In-process is simplest and inherits the SRP
  challenge table's single-worker dependency (NFR-5). The identity store is durable but a code has
  a 60-second life, so persisting it is mostly write amplification. Leaning in-process **with the
  startup assertion**, since that is already the state of things for SRP and one shared constraint
  is easier to hold than two mechanisms.
- **B. Whether the id token's `aud` should be the client or the product.** OIDC says the client;
  today `aud` is the constant `nvis-rack-status`. Changing it affects the existing verify path, so
  it interacts with the FR-19 flag.
- **C. What `scope` values exist before Phase 3.** A scope that appears in a token and gates nothing
  must not be presented as access control. Possibly only `openid` until the policy engine lands.

## Explicit non-goals for this phase

- **Dynamic client registration** (RFC 7591). Assumption 1 says the consumer list is short and
  known; a registration endpoint is an unauthenticated way to create trusted redirect targets, and
  we have no client that needs it.
- **Front-channel or back-channel logout** (OIDC Session Management). No relying party exists to
  log out yet.
- **Authorization.** Still Phase 3. `scope` gets a *claim* in this phase (FR-7); nothing evaluates
  it. A scope that appears in a token and gates nothing must not be described as access control.
- **Multi-tenancy.** One deployment, one issuer (assumption 3).
- **Replacing the SPA's cookie transport.** NFR-7, deliberately.
