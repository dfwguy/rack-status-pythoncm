---
description: Identity/auth traps in backend/identity — coarse-value comparisons, read paths that write, unsafe library defaults, fail-closed controls
alwaysApply: true
---

# `backend/identity/` — traps that cost real time, don't relearn them

Built Aug 2026 (Phase 2 of the auth programme). Six modules: `principals`, `models`, `store`, `srp`,
`tokens`, `revocation`. Full design in the SADD's *Identity design* section; requirements and
measurements in `.plc/briefs/auth-identity-brief.md`. **Every item below was found by a test, not by
reasoning** — which is the reason each one is written down rather than trusted to be obvious.

## 1. Never widen a margin to fix an ambiguous comparison — widen the value

The most generalisable one. *Log out everywhere* compares a token's `iat` against a stored cutoff.
With whole-second `iat` you cannot tell a token minted just **before** the cutoff from one minted just
**after**, so the first implementation set the cutoff to `now + 1` "to be safe".

That is safe in the direction you check and broken in the direction you don't: it also refuses the
user's **own next login** for up to a second, so a password change presents as a broken account —
*intermittently*, depending on sub-second timing, which is the worst possible bug report to receive.

The fix is to remove the ambiguity, not to pad around it: record `iat` with **sub-second precision**
(RFC 7519 §2 explicitly permits fractional NumericDate) and set the cutoff to exactly `now`.

> **When a comparison is ambiguous because a value is too coarse, increase the value's resolution.
> Adding a safety margin to one side just moves the defect to the other side, where nobody is
> looking.**

Verified over 300 logout-then-relogin cycles — a single-shot test passes either way, because this
class of bug presents as flakiness rather than failure. **If you touch the `not_before` cutoff or
`iat`, run the cycle test, not one login.**

## 2. A read path that writes will silently defeat its own cache

`revocation_snapshot()` pruned expired rows while reading. **TinyDB's `remove()` rewrites the file
even when nothing matches**, so every read performed a write — and that write changed the file mtime,
which is part of the cache key. The cache was invalidated by its own reads and **never hit once**,
quietly putting the NFR-3 5 ms budget at risk while every functional test passed.

- Pruning belongs on **write** paths (`_prune_expired_tokens_locked` says so in its docstring);
  reads filter in memory.
- **Assert the cache actually hits.** `test_the_cache_is_actually_serving_reads` is what caught this;
  without it, staleness tests prove nothing, because a cache that never hits cannot go stale.

## 3. An atomic write installs the *temp file's* mode — so create the temp with the mode you want

The symptom is real and has bitten twice: a `chmod 0o600` in a constructor is undone by the first
save, and the file holding SRP verifiers quietly becomes world-readable. `audit/sink.py` hit the
identical trap with log rotation.

**But the cause is not `os.replace`, and the distinction changes the fix.** `os.replace` preserves
the *source* inode's mode. The 0644 comes from creating the temp with a plain `open()`, which takes
the umask. Measured 2026-09-01, umask 022:

| how the temp is created | mode after `os.replace` |
|---|---|
| `open(tmp, "w")` | **0644** — the trap |
| `os.open(tmp, …, 0o600)` | **0600** |
| `tempfile.mkstemp()` | 0600 (mkstemp defaults to 0600; safe by accident) |

So re-applying the mode *after* the write is a correction applied once a world-readable inode
already exists — a narrower window, not no window. **Create the temp with the target mode and the
window never opens**, and you keep atomicity. This is the same lesson as trap 1: fix the value, do
not shrink the margin around the defect.

`_OwnerOnlyAtomicStorage` currently does the re-apply form. It is not wrong, but new code should
create correctly instead. `bin/gen-tls-cert.py::_write` already does (`os.open(..., 0o600)`),
though without a temp, so it trades atomicity for the same guarantee.

**Test permissions across several writes, not after one** — a single-write test passes on the
broken version regardless of which mechanism is used.

## 4. The `srp` library's defaults are unsafe, and its wire format is off-standard

- `hash_alg=0` is **SHA-1**; `salt_len=4` is a **32-bit salt**. A perfectly reasonable-looking
  `create_salted_verification_key(user, pw)` enrols a SHA-1 credential and nothing fails. Pass
  `hash_alg` / `ng_type` / `salt_len` explicitly, always.
- **RFC 5054 §2.6 padding defaults OFF**, producing a variant that interoperates only with itself.
  Mainstream JS/TS SRP libraries follow the RFC, so a mismatch breaks the browser client — and it
  does not error, it produces a proof that fails to verify, so **every login looks exactly like a
  wrong password**.
- **The padding flag is per-backend.** `srp/__init__.py` prefers the OpenSSL `_ctsrp` and falls back
  to `_pysrp`; each keeps its own `_rfc5054_compat`. Toggling `_pysrp` directly is a silent no-op
  whenever the C backend is active. Call package-level `srp.rfc5054_enable()` and assert against
  `srp._mod`.
- **Which backend binds is decided by `dlopen`, not by the package.** We get `_pysrp` on macOS and in
  `python:3.12-slim` only because `_ctsrp` asks for `libssl.so` / `libssl.so.1.1.0` and both ship
  `libssl.so.3`. **Installing `libssl-dev` creates that symlink and flips the backend.**

## 5. Changing the SRP group is a credential migration

`v = g**x mod N`, so a new group invalidates **every** enrolled verifier. Done at 4096-bit while no
identity store existed anywhere — free then, a migration for every user at every site afterwards.
Padding mode, by contrast, does **not** affect verifiers (it changes `u` and `k`, not `gen_x`), which
is why that flag could be turned on safely in place.

Naming: **"group 5" in the IKE/RFC 3526 registry is the 1536-bit group** — *weaker* than 2048. The
4096-bit modulus is group 16; the 5 is the generator. Read a request for "group 5" as "the group
whose generator is 5" and confirm before implementing.

## 6. PyJWT validates less than it looks like it does (2.13.0, measured)

- **`iss` is not checked at all** unless you pass `issuer=`. The claim can name anyone.
- **A token with no `exp` is accepted** and never expires.

Neither raises or looks wrong at the call site. Pass `issuer=`, `audience=`, **and** an explicit
`options={"require": [...]}` naming every claim. Pinning `algorithms=[...]` *does* correctly refuse
`alg: none` and an HS512→HS256 substitution — the pin works, it just has to be written.

Also: **`iss`/`aud` did not separate deployments — and from Phase 2.5 that changes.** They were
product constants (`nvis-rack-status`), identical everywhere, with the HKDF-derived signing key as
the only boundary.

With the OIDC provider enabled, `iss` becomes **this deployment's URL**, because OIDC Discovery
§4.3 requires it and conforming clients enforce it. Two consequences:

- **`iss` is now load-bearing.** Once federation lands and we verify NVIDIA SSO's tokens alongside
  our own, accepting a token from the wrong issuer is a full authentication bypass — and PyJWT does
  not check `iss` at all unless `issuer=` is passed (trap 6). Two issuers, one verify path, and the
  parameter that separates them is easy to omit.
- **It is configured, never derived from the request.** Taking it from the `Host` header would hand
  an attacker the issuer of our tokens: they set a header, we mint a token claiming their origin,
  and any relying party trusting that origin accepts it. `RACK_WEB_OIDC_ISSUER` is validated for
  scheme, host, query and trailing slash at startup, because clients compare it by exact string.

The signing key is still the real boundary between deployments. `iss` is what a *client* checks.

## 7. Fail-closed controls: don't "fix" them by defaulting to permissive

`tokens.verify` refuses **everything** until `revocation.install()` registers a checker (FR-6a). If
you see that error, wire the checker — do **not** add a default that allows. The precedent is Phase
1's config materialise hook: unregistered, it produced wrong values on some clusters and an error on
none, and needed a test purely to assert the wire existed. A loud total refusal is the correct failure
direction for a security control.

Related: the revocation checker runs **only after** signature and claim validation. A checker handed
attacker-controlled claims from an unverified token is a new attack surface, not a control.

## 8. "Why is this token suddenly invalid?" — and the one where it's suddenly *valid*

Nineteen catalogued causes, one row each, in `backend/tests/test_identity_token_invalidation.py`
(ids `INV-01`..`INV-19`) with the table in the SADD. Read those before debugging a token. **Four are
routinely triggered by accident, three of them from fixtures:**

| Symptom | Likely cause |
| :---- | :---- |
| *Every* token refused, including freshly minted ones | `revocation.install()` was never called — fail-closed (INV-18), not a bug |
| A token valid a moment ago is refused | `RACK_WEB_SECRET_KEY` changed since it was minted; `monkeypatch.setenv` after a mint does this (INV-17) |
| A token refused with no revocation in sight | wrong expected token type — `verify` demands it, and the message names both (INV-09..11) |
| **A revoked token works again** | the identity store was repointed or replaced (INV-19) |

That last one is the only cause that *grants* rather than denies, so it is the one to be careful
with. Revocation state lives in the identity file; a store with no revocation row honours what the
previous store refused.

Scope it correctly, because the obvious version is safe and the realistic one is not.
`var/secrets/.rack_web_secret_key` and `var/identity/identity.json` share the `rack-data` named
volume, so **losing the volume is harmless** — the secret regenerates and every signature fails.
The hazard is replacing the identity file *while the secret survives*: **restoring `identity.json`
from a backup predating an offboarding re-honours that person's tokens** until expiry (≤15 min
access, ≤12 h refresh). A restore of the identity store is not purely additive. If you must, rotate
`RACK_WEB_SECRET_KEY` alongside it and everything outstanding dies with it.

When reading a refusal, the category tells you what happened: **Crypto/Claims** = never ours or never
valid; **Type** = ours, being misused; **Revocation/Implicit** = was valid, withdrawn; **Operational**
= the deployment moved. Only the third should ever reach a user as "your session ended".

## 9. Two operational constraints that are not obvious from the code

- **SRP challenge state is in-process**, so `--workers 1` is load-bearing;
  `assert_single_worker_safe()` refuses more. A second worker splits the table and login succeeds
  about half the time, which reads as a client bug.
- **The unauthenticated login route is a CPU amplifier, and the cost depends on which SRP backend
 bound.** A 4096-bit `begin()` costs **~52 ms** where `_ctsrp` (OpenSSL) binds and **~110 ms** where
 the pure-Python `_pysrp` does — measured 2026-09-02, so one worker saturates a core at ~19/s or
 ~9/s respectively. Check before quoting either: `python -c "import srp; print(srp._mod.__name__)"`.
 FR-4b makes the cost unavoidable — a cheap path for unknown logins would be an enumeration oracle,
 so every caller gets the expensive one. **The route needs a rate limit that sheds before the
 exponentiation** (FR-4g; now `@throttle.throttle(10)` plus the global hook), and the bounded
 challenge table caps memory only, never CPU.

 Two consequences worth holding together. The 2× gap means trap 4's warning cuts both ways:
 installing `libssl-dev` flips the backend, which is a **performance win and a protocol risk** in
 the same change, so re-run the SRP interop tests if it happens. And because the deployment is
 `--workers 1` with a GIL, **this ceiling does not improve with more cores** — full numbers and the
 scaling table in `docs/PERFORMANCE_BASELINE.md`.

## 10. The gate must judge the string the handler acts on — or a deny is evadable

Found and fixed 2026-09-08. This is a **class**, not one bug: any transformation a handler applies
*after* authorization is a way to reach a resource the gate did not judge.

The instance measured here. `_normalize_rack_id` lower-cases, and the authorization gate judged the
case the caller sent. So with the commonest policy shape there is — a broad allow plus a narrow
deny — `P17-R01` was judged as `rack.power:P17-R01`, which `fnmatchcase` does **not** match against
`deny rack.power:p17-r01`. The broad allow applied, the handler folded the id, and the denied rack
was powered off by a caller who only had to hold shift.

- **Fix the value, not the comparison.** `authz/resources.clean_identifier` folds case for the types
  whose handlers fold, so the two agree exactly. Making the *engine* match case-insensitively would
  have been the wrong repair: for a type whose handler is case-*sensitive*, an allow on `foo` would
  then permit acting on `Foo` — a different resource. Same lesson as trap 1.
- **Folding is per-type on purpose.** `help` content ids are camelCase (`singleRack`, `rackTable`)
  and their handler does not fold, so lower-casing them would refuse every help read while fixing
  nothing. `_FOLDED_TYPES` names the ones that fold; adding a rack-scoped type without adding it
  there is caught by `test_the_folded_set_covers_the_rack_family`.
- **`RequestFacts.first()` already strips**, so whitespace was never a vector. A repro that used a
  hand-rolled `RequestFacts` "proved" it was — if you are demonstrating a gate bug, drive the real
  extractor, not a stub that reimplements it.
- **Before adding a resolver, ask what the handler does to the id afterwards.** Trailing dots and
  traversal shapes are refused by `canonical_rack_id`, so they were never exploitable; case was,
  because both sides accepted it and disagreed about its meaning.

## 11. Bootstrap before the gate, and gate serving rather than importing

Both halves shipped wrong in the first draft of `backend/startup_gate.py`, and both are ordering
mistakes rather than logic ones.

**Gate-then-bootstrap refuses a brand-new deployment for lacking exactly what starting would have
created.** The gate requires an administrator, a policy and TLS; `prepare()` creates all three on a
fresh system and only then calls `require()`. Reversed, a first boot can never succeed — and the
error message is maximally confusing, because it names prerequisites the product knows how to make.

**The gate hangs off *serving*, not `import app`.** `bin/check-tracked-tree.sh` imports all 164
modules to prove the committed tree stands alone, `verify-deploy.sh` imports them in the container,
and the route and action snapshots import `app` to enumerate its URL map. None of them serves a
request, so none can leak a credential or accept traffic: refusing them buys no safety and breaks
every tool that inspects the app without running it. The three real entry points each call
`prepare()` and none trusts another to have done it — gunicorn's `on_starting` (container and
`start.sh`), `cli.py serve`, and `app.py`'s development-server `__main__`. `on_starting` is a fixed
gunicorn name, so `test_the_gunicorn_hook_is_the_one_gunicorn_calls` asserts it rather than trusting
the spelling; a typo there is a gate that silently is not there.

**A certificate is generated, not demanded.** Phase 0.5 already decided "self-signed by default", so
refusing to start for want of a certificate the product can make is a gate that teaches operators to
route around it. `startup_gate.ensure_tls()` shells out to `bin/gen-tls-cert.py` rather than growing a second
generator, because the SAN rules there (`localhost`, `127.0.0.1`, `::1` added unconditionally) are
load-bearing and would be reimplemented from memory.

**An existing deployment with no policy is refused, deliberately.** On a store that already holds
accounts we cannot know which of them should hold full access, so inventing a policy either locks
the operators out or hands somebody everything. Bootstrap therefore covers only the brand-new case,
and `cli.py check` reports the refusal before a restart discovers it.

## 12. Authentication has no off switch — and the switch's real damage was to our own tests

`RACK_WEB_AUTH_DISABLED` is **gone** (2026-09-08), with `_auth_enabled()`, the `_SAW_A_USER` latch,
`_store_holds_a_claimed_account()`, `_auth_user()` and `RACK_WEB_AUTH_USER`. Nothing disables the
gate. `conftest.py` installs an authenticated client as `app.test_client_class`, so **opting out is
the explicit act** — `identity.testing.unauthenticated_client(app)` for a suite whose subject is the
refusal.

**The lesson is about measurement, not about the flag.** AC-28b's guard was built to stop the switch
reaching a customer deployment, which was the right instinct aimed at the wrong target: the switch
was already off *here*. `start.sh` exported it on macOS and 25 test modules hard-assigned `="1"`, and
because the environment is process-global and pytest imports every test module during **collection**,
one assignment turned the gate off for the whole session. So an earlier measurement of "17 failures
with the gate on" was really 17 gated *fixtures* meeting the enforcing policy; the true cost was
**304**. Removing the flag is what made the state unambiguous — no guard fixes a switch whose problem
is that it works.

Assert the state, do not infer it. One probe settles it and would have caught the wrong claim
immediately:

```python
with app.test_client() as c: assert c.get("/api/status").status_code == 200
with testing.unauthenticated_client(app) as a: assert a.get("/api/status").status_code == 401
```

**Two mechanisms the migration needed, both non-obvious:**

- **The test client mints per request, not once.** Dozens of fixtures set their own
  `RACK_WEB_SECRET_KEY`; the signing key is derived from it, so a bearer captured when the client was
  built stops verifying the moment a fixture repoints the secret. That is INV-17 arriving as a 401 in
  a test that never mentions authentication.
- **The authorization engine must be restored after every test, by capture.** It is module state, so
  `monkeypatch` cannot: a test proving the unregistered engine fails closed leaves that engine
  installed for everything after it — putting `test_authz_engine.py` before `test_endpoint_smoke.py`
  turned 491 passes into 14 failures. Restore the captured value; calling `install_default()` reads
  the *test's* policy file and made it worse (29 → 62 failures).

**Policy enforcement is deliberately not suite-wide.** A verdict needs an attachment in whichever
identity store the test built, and dozens build their own, so seeding made 49 tests fail on
authorization inside files about something else. Authentication is enforced everywhere;
authorization is tested in `test_authz_*` and `test_policies_api`.

When a test does fail on authorization, the shape to look for is **a test asserting its own
configuration rather than its subject**:

- `install_default()` chooses by environment — `AllowAllEngine` when no policy file exists,
  `PolicyEngine` when one does. A test that wants the permissive engine must construct it by name;
  calling `install_default()` and expecting permissiveness asserts the suite's configuration.
- A fixture that owns its identity store but shares the session's policy file has documents without
  attachments, so every authenticated request is a 403 — authorization failing inside a test about
  authentication. Attach explicitly; `ensure_admin_policy` deliberately does nothing once a policy
  set exists.
- Hardcoding `policy_engine == "allow-all"` in an audit assertion pins the configuration. Assert
  against `authz.engine_name()`; the intent is that the record names *which* engine decided.

`enrol(authorize=False)` exists for the tests whose point is a refusal, and
`unauthenticated_client(app)` for those that must carry no credential at all. There is no longer a
bypass to reach for instead — which was the point.

Also retired with the flag: `RACK_WEB_AUTH_MODE` and `RACK_WEB_AUTH_TOKEN`. Several fixtures still
set them to "enable auth", which had been configuring names nothing reads since FR-18. A fixture
that appears to arrange authentication and does nothing is worse than one that arranges nothing.

## 13. A token claim may say how you authenticated, never what you may do

Written down before the mistake rather than after it, because policy **conditions**
(NVRACKSTAT-61) will make it tempting and the wrong version looks like a feature.

Phase 2 keeps grants out of tokens on purpose — `test_no_policy_grant_rides_inside_a_token`
pins it — so a withdrawn grant takes effect on the *next request* rather than at token expiry.
That is the whole reason a one-hour access token is safe. `scope` is the one deliberate
exception, and only because it bounds claim release at `/userinfo`, not access.

So when a condition needs an attribute, the line is what the attribute *describes*:

| Reads a fact about | Example | Sound? |
|---|---|---|
| the **authentication event** | `auth_time`, `azp`, `amr`, an address bound at login | **Yes** — historical by nature, so a snapshot is the correct semantics |
| **current entitlement** | groups, roles, permissions | **No** — reintroduces the staleness keeping grants out of the token exists to avoid |

An entitlement claim would also be an attractive *performance* fix — the store read disappears
from the hot path — which is exactly why it needs to be refused in writing. Read entitlement
live; the measured cost is 0.023 ms.

**Two consequences worth knowing before designing a condition:**

- **`auth_time` is already on the access token** and its provenance is pinned
  (`test_oidc_auth_time_provenance.py`: sampled once at login, zero drift across refresh). Step-up
  authentication — "a destructive action requires having authenticated in the last N seconds" — is
  therefore available with no new plumbing, and it closes a real gap: with an hour-long access token
  and a day-long refresh, a session can be many hours old when somebody powers off a rack.
- **An address condition must be relative, not absolute.** Operators reach these deployments through
  `ssh -L`, so `src_ip` is the tunnel endpoint (`127.0.0.1`) for everyone. `source_ip in 10.0.0.0/8`
  is then uniformly true or uniformly false *while looking like access control*. Comparing the
  request address against an address **bound into the token at login** instead degrades to a
  harmless no-op on a tunnel and still refuses a token replayed from somewhere else. Prefer the form
  that becomes inert when its input is uninformative over the one that becomes a lie — and consult
  `src_ip_trust`, not just `src_ip`, or the comparison is between two values nobody vouches for.
