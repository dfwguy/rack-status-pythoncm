# API response codes

The contract for what a Rack Status endpoint returns, and — the part that needed writing down — what
the **authentication and authorization** layer is allowed to return and why each code is not one of
the others.

Scope: the 221 operations under `/api/`, plus the WebSocket upgrade at `/ws/terminal`. The generated
OpenAPI document (`GET /api/openapi.json`) is derived from the code, so it is the machine-readable
form of this page; where the two disagree, the code is right and one of them is a bug.

Audited 2026-08-26 against the shipped tree. Two real defects were found and fixed in the same pass,
both recorded below under *What the audit found*.

---

## 1. The general contract

**Every error is JSON**, with `ok: false` and a human-readable `error`. There is exactly one
deliberate exception in the product: an unbuilt frontend serves an HTML 503 at the four SPA shell
routes, because its audience is a person in a browser (`local-vs-ci-tree.mdc` §1).

**An error never travels in a 200.** Audited: zero endpoints return an `ok: false` body with an
implicit 200. Keep it that way — a client that has to inspect the body to know whether the call
worked cannot use ordinary HTTP error handling, and every retry policy built on it is wrong.

**No non-standard codes.** Audited: zero uses of 419, 440, 498, 499, 598 or 599. `419 Session
Expired` and friends are invented, are not in the IANA registry, and are handled unpredictably by
proxies. A session that has ended is a **401**.

| Class | Meaning here |
| :---- | :---- |
| `200` | Success. Every operation declares it |
| `202` | Accepted; work continues on a background thread. The response carries the id to poll |
| `400` | Malformed request — missing or unparseable input |
| `401` | **Not authenticated.** See §2 |
| `403` | **Refused although authenticated.** See §3 — this code currently carries two meanings |
| `404` | No such resource. Also, today, "feature disabled on this deployment" — see §3 |
| `409` | Conflict: not ready yet, or a stale identifier |
| `429` | Rate limited. Carries `Retry-After`. See §4 |
| `500` | A fault in this product |
| `502` / `504` | An upstream (cluster manager, Slurm, Grafana, VictoriaMetrics) failed or timed out |
| `503` | A dependency this endpoint needs is unavailable — including, now, "authentication cannot be evaluated" (§2) |

---

## 2. Authentication: 401, and the one case that must not be 401

**401 means the caller is not authenticated and re-authenticating is the remedy.** Every one of the
catalogued invalidation causes `INV-01`..`INV-17` (SADD, *Token invalidation*) maps to 401:

| Causes | Why they share one code |
| :---- | :---- |
| `INV-01`..`INV-04` — expiry, tampering, foreign deployment, wrong algorithm | the credential is not one we issued, or not valid |
| `INV-05`..`INV-08` — missing claim, wrong issuer, wrong audience, malformed subject | same |
| `INV-09`..`INV-11` — token type confusion | ours, being misused |
| `INV-12`..`INV-16` — revoked subject, revoked token, logout-everywhere, disabled, deleted | was valid, has been withdrawn |
| `INV-17` — deployment secret rotated | everything outstanding died at once |

**The body must not say which.** All five groups produce the same status and the same message. This
is the response-code half of FR-4b: distinguishing "no such user" from "wrong password", or "revoked"
from "expired", turns the login surface into an oracle for account enumeration and for confirming
that a stolen token was once real.

Two rules that follow, and both were violations at audit time:

**A 401 must carry `WWW-Authenticate`.** RFC 9110 §11.6.1 makes it mandatory. The form is
`Bearer realm="nvis-rack-status"`, plus `error="invalid_token"` when the caller *did* present
something (RFC 6750 §3). That distinction discloses nothing — the caller already knows whether it
sent a credential — and `error_description` is deliberately never emitted, because expired, revoked,
malformed and wrong-audience all collapse to `invalid_token`, which is precisely the uniformity
above.

**`INV-18` is a 503, not a 401.** If the revocation record cannot be consulted, the product refuses
every token (FR-6a, fail-closed). But the credential may be perfectly valid — what has failed is the
*deployment*, not the caller. Answering 401 tells an operator to log in again; they will, it will
fail again, and they will conclude the password is wrong. 503 with `Retry-After` says the truth: the
service cannot evaluate authentication right now, and no credential will help until someone fixes it.

> **The distinction that matters: 401 is "your credential is not good", 503 is "we cannot tell".
> Only the first is fixable by the caller.**

`INV-19` has no response code. It is the one cause that *grants* rather than denies — replacing the
identity store un-revokes — so it produces a 200 that should have been a 401. It cannot be detected
from a response, only prevented operationally.

### HTML versus API

The gate answers `/api/*` and `/ws/*` with a JSON 401. A browser navigating to an HTML route gets a
**302 to `/login`** instead, because a 401 there would show a raw browser auth dialog for a scheme we
do not use. Same verdict, two renderings — the split is on path prefix, not on `Accept`.

---

## 3. Authorization: 403, and a collision to resolve before Phase 3

**403 will mean "authenticated, but this principal may not do this."** Phase 3's policy evaluation is
default-deny, so 403 becomes the common refusal.

**It already means something else.** All 32 current uses of 403 are deployment-posture refusals —
`RACK_SYSINFO_DISABLED`, `RACK_ANALYTICS_DISABLED`, `RACK_BUG_REPORT_DISABLED` and so on. That is a
legitimate 403 under RFC 9110 §15.5.4, which does not require 403 to be identity-related. But the two
meanings have **opposite remediations**:

| Meaning | Who can fix it | What the UI should say |
| :---- | :---- | :---- |
| Feature disabled by configuration | an operator, by changing deployment config | "this is turned off on this deployment" |
| Not permitted for this principal | an administrator, by changing policy — *or* a different user succeeds | "you do not have permission" |

A client cannot tell them apart from the status alone, and it needs to: one is worth hiding the
button for permanently, the other is per-user.

**Decision: keep 403 for both and discriminate in the body**, via a stable machine-readable field:

```json
{ "ok": false, "reason": "feature_disabled", "error": "analytics disabled (RACK_ANALYTICS_DISABLED)" }
{ "ok": false, "reason": "not_authorized",   "error": "not permitted" }
```

Rejected alternatives, and why: **moving feature-disabled to 404** hides information the UI genuinely
wants and conflates it with a bad URL; **501** is specified for an unsupported *method*, not a
disabled feature; **changing 44 endpoints' status codes** would break the SPA and every site's
automation for no gain, since the status is already correct — what is missing is only the
discriminator.

`reason` is additive and optional to read, so it can land incrementally. **Status quo: the auth paths
emit it; the 32 feature-disabled sites do not yet.** Tracked as follow-up
[FR-4h in the identity brief](../.plc/briefs/auth-identity-brief.md); do not add a new
feature-disabled 403 without it.

---

## 4. Rate limiting: 429 on the login route

The SRP exchange's first leg performs a 4096-bit modular exponentiation for an **unauthenticated**
caller (~52 ms of server CPU), and FR-4b forbids making the unknown-user path cheaper, so every
caller gets the expensive one. That makes the login route a CPU amplifier: one worker saturates a core
at roughly 19 anonymous requests per second.

- **429** with `Retry-After`, never 403. 403 says "never"; 429 says "not this fast".
- **The refusal must happen before the exponentiation** (AC-4i). A limiter that rejects after doing
  the work has not removed the amplification.
- A rate-limited login must not disclose whether the login exists — same uniformity as §2.

---

## 5. 500 versus 502/504, and what 500 must never be

**500 means a fault in this product.** 72 operations declare it. **502/504 mean an upstream failed** —
`cmsh`, Slurm, Grafana, VictoriaMetrics — and 24 operations declare those. Getting this wrong turns a
cluster problem into a bug report against Rack Status, and vice versa.

**No authentication or authorization failure may be reported as 500.** It is a client-side condition
with a defined code. Worth stating because the failure mode is easy to reach by accident: the identity
layer raises `TokenError` for every refusal, so a route that lets it escape produces a 500 and an
alarming stack trace for what is an ordinary bad credential. `identity/tokens.py` keeps a single
exception type precisely so a caller can catch it — that bug already happened once during
implementation, where `verify()` leaked `PrincipalError` for a malformed subject.

---

## 6. What the audit found

Measured across all 221 operations on 2026-08-26.

**Already correct**, and worth recording so it is not re-litigated: no non-standard status codes; no
error bodies smuggled inside a 200; a consistent JSON error envelope; 502/504 used for upstream
failures rather than 500.

**Two defects, both fixed in this pass:**

1. **The OpenAPI document claimed authentication failure was impossible on 219 of 221 operations.**
   Responses are derived by scraping `return` statements in each handler, and the gate's 401 is
   produced by middleware *above* the handler, so no handler mentions it — only the two endpoints that
   401 from their own logic declared it. Anything generated from the spec inherited the error. Fixed
   by adding gate-level responses to every non-bypassed operation, reading the bypass set from the app
   rather than a second list that could drift: 401 now on 221 of 221, 503 on the 219 that are gated.

2. **The gate's 401 carried no `WWW-Authenticate` header**, which RFC 9110 §11.6.1 makes mandatory.
   Fixed in `web_auth.www_authenticate()`, shared by the Flask gate and the WSGI terminal path.

**One decision recorded rather than changed:** the 403 collision in §3, resolved by a body
discriminator instead of a status-code migration.

---

## 7. Adding an endpoint

- Return the JSON envelope with an explicit status. Never an `ok: false` in a 200.
- Do **not** hand-write 401 or 503-for-auth — the gate owns them, and the spec adds them for you.
- Distinguish your own fault (500) from an upstream's (502/504).
- If you refuse for deployment posture, use 403 **with `reason: "feature_disabled"`**.
- A docstring becomes the operation's summary in the generated spec, so write the first line for a
  reader of the API reference.
- `backend/tests/test_openapi.py` and `backend/tests/test_api_response_codes.py` enforce the parts of
  this page that can be checked mechanically.
