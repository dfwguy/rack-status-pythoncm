# Auth programme — phase scope

The map of the authentication and authorization work: what each phase owns, and what is deliberately
deferred to a later one. It exists because two constructs were **invisible rather than deferred** until
2026-08-20 — principal **groups**, and the **admin UI** — and an unscoped construct is one nobody
notices is missing until it is expensive.

Per-phase requirements live in PLC feature briefs under `.plc/briefs/`. This file is the index and the
scope record; it is not a spec and it does not carry acceptance criteria.

**Jira:** the programme is tracked under epic
[NVRACKSTAT-53](https://jirasw.nvidia.com/browse/NVRACKSTAT-53), one ticket per phase. Created
2026-09-01, retroactively — phases 0–2 were built before the tickets existed, so **the tickets
follow this document rather than the other way round**. If the two disagree, this file is the
source of truth and the ticket is stale.

| Phase | Jira | Owns | State |
|---|---|---|---|
| 0 | [NVRACKSTAT-54](https://jirasw.nvidia.com/browse/NVRACKSTAT-54) | defect fixes | delivered |
| 0.5 | [NVRACKSTAT-55](https://jirasw.nvidia.com/browse/NVRACKSTAT-55) | HTTPS | delivered |
| 1 | [NVRACKSTAT-52](https://jirasw.nvidia.com/browse/NVRACKSTAT-52) | audit trail | delivered |
| 2 | [NVRACKSTAT-56](https://jirasw.nvidia.com/browse/NVRACKSTAT-56) | identity | delivered; federation deferred to 2.6 |
| 2.5 | [NVRACKSTAT-57](https://jirasw.nvidia.com/browse/NVRACKSTAT-57) | OIDC provider | delivered; unconditional since 2026-09-08 (`RACK_WEB_OIDC_ENABLED` retired) |
| 2.6 | [NVRACKSTAT-60](https://jirasw.nvidia.com/browse/NVRACKSTAT-60) | OIDC **federation** — consuming another issuer's tokens | not started; scoped below |
| 3 | [NVRACKSTAT-58](https://jirasw.nvidia.com/browse/NVRACKSTAT-58) | authorization | **delivered and audited** 2026-09-09. All 32 acceptance criteria satisfied, amended or accepted; AC-6 (per-cluster job ids) accepted as ambiguous and carried to [NVRACKSTAT-61](https://jirasw.nvidia.com/browse/NVRACKSTAT-61) |

Since then, two things have landed that the table above does not have a row for, both on `feat/auth-identity`: the **audit trail reader** (`GET /api/audit-trail` and Observability > Audit Trail), which closes Phase 1's one deferred work item — the record was written from the start and could not be read — and **policy conditions** scoped as [NVRACKSTAT-61](https://jirasw.nvidia.com/browse/NVRACKSTAT-61).

Every one of them sits in **Backlog**, including the phases that are finished. That is deliberate,
not an oversight: under PLC a workflow transition *is* the release owner's attestation, so the
`plc-jira-ticket-creation` skill forbids tooling from moving a ticket's state. Advancing them is a
human action.

| Phase | Owns | State |
|---|---|---|
| 0 | Defect fixes: the unauthenticated `/ws/terminal`, `target` command injection, `0.0.0.0` exposure | done |
| 0.5 | HTTPS: self-signed by default, internal CA opt-in, TLS 1.2 floor, CSP/HSTS, Origin policy | done |
| 1 | Central logging configuration; durable JSONL audit trail; the action vocabulary Phase 3 binds to (89 at the time, 175 today — the current count is a generated fact in `docs/action-vocabulary.json`, not a number to restate here) | **closed** — 7/7 work items merged to `main` 2026-08-25; the four closing V-model steps completed 2026-08-28, acceptance verdict **PASS** on all 34 criteria, no open findings |
| 2 | **Identity**: local user store (SRP), **groups and membership**, JWT id/access tokens, OIDC federation, first-boot admin credential | **delivered except federation.** Store, SRP login, groups and membership, all three tokens, first-boot credential, admin-initiated reset and the Users/Groups UI are in; acceptance audit 47/74 PASS. **Federation is deferred to Phase 2.6** and is the only open scope — FR-7, FR-7a, FR-19, FR-19a with AC-13, AC-14, AC-15, AC-29b, AC-29c |
| 2.5 | **OIDC Provider**: asymmetric signing + JWKS, discovery, standard claim names, authorization-code flow with PKCE, `/token`, `/userinfo`, introspection and revocation endpoints | **delivered and working for local users.** ES256 with a rotating key set and RFC 7638 `kid`; discovery and JWKS; authorization-code flow with mandatory PKCE S256; `/token` for both grants; `/userinfo` releasing only the granted scope; introspection and revocation. Acceptance audit 41/54 PASS. It is an OIDC **Provider** — it issues tokens for accounts in the local store. Consuming *another* issuer's tokens is federation, which is Phase 2.6 |
| 3 | **Authorization**: policy engine, policy documents, attach-to-principal, enforcement before dispatch | **delivered**, acceptance audit outstanding. Attachment-based engine (deny-overrides-allow, default-deny), `/api/policies` CRUD + attach, resource resolver, read governance, and the Monaco policy editor. The test-credential gate below is **lifted** |

## Phase 1 — implementation work items

The brief says *what* and the test plan says *how it is verified*; this is the order the code lands
in, and it is here because it was previously only in a task list and a series of commit messages.
Each item is independently testable, and the dependency order is real — item 3 cannot precede item 2,
and item 6 needs everything above it.

Live status is in `.plc/v-model-state.json` (`phases.implementation.last_substep`). Requirements and
design references are to `.plc/briefs/auth-audit-logging-brief.md`; TC numbers are from
`.plc/briefs/auth-audit-logging-test-plan.md`.

| # | Work item | Verifies | State |
|---|---|---|---|
| 1 | `backend/logging_config.py` — sinks, level, format, redaction filter, settings registry | TC-1…TC-5, TC-21 | **done**, 24 tests |
| 2 | `backend/audit/actions.py` — the `@action` decorator, grammar, `build_registry` startup validation | TC-15…TC-17 | **done**, 27 tests |
| 3 | The `@action` declarations (103 mutating routes at the time; reads joined in Phase 3, so the registry now holds 175 across 51 services) plus the committed `action_snapshot.json` | TC-18 | **done**, 6 tests; gate live |
| 4 | `backend/audit/sink.py` + `audit/__init__.py` — event schema, bounded rotating JSONL, flood guard | TC-6…TC-14, TC-32, TC-33 | **done** |
| 5 | Client-address resolver + trusted-proxy validation (`core.py`, `api/_common.py`) | TC-19, TC-20 | **done** |
| 6 | The `after_request` chokepoint + the WSGI terminal refusal hook | TC-22…TC-31 | **done** |
| 7 | Performance, compatibility and regression cases | TC-34…TC-40 | **done** — incl. TC-39 inspection and TC-40 demonstration |

Two things fixed along the way that were not work items and are worth not rediscovering: a missing
`static/spa/index.html` made `serve_spa_asset` hand off a file it had just found absent, returning
500 instead of a 503 that says the frontend was never built; and `.scratch/` held ~2,000 untracked,
unignored files, one `git add .` from the repo.

After item 7 come the closing V-model steps — verification, security scan, acceptance validation and
the traceability matrix — which are phases in the state file rather than work items here. **All four
are now complete (2026-08-28).** Phase 1 is closed.

Two things that ran ahead of them and are the reason this took a second pass:

- **The security-scan artefacts were dated 20 August — before any of the seven work items landed.**
  A stale scan reads exactly like a current one. Re-run scoped to Phase 1's delivered 82-file work
  unit: SAST clean; dependencies 0 new against 3 pre-existing; licence flags `paramiko` for OSRB.
  The secret scan is a recorded **SKIP** — the Pulse image is not local and the registry pull could
  not be authorised in-session — never a silent omission.
- **Evidence ingestion had never run, so the evidence log held no row referencing any AC.** Run
  as-is, the acceptance gate scored *every* criterion UNVERIFIED — not because the work was unproven
  but because nothing connected the passing tests to the criteria they satisfy. Ingestion had to come
  first; it is now 55 rows keyed to ACs and requirements.

**Result, after the two gaps below were closed: all 34 acceptance criteria PASS against evidence
that was executed, not assumed** — 103 pytest node ids resolved and run, 122 outcomes, all passing.
The acceptance gate's verdict is `PASS`.

**The gate found a real hole on the first run, and it is worth knowing what kind.** The plan's
*Coverage Gaps* section said "None. All 39 requirements and all 54 acceptance criteria have at least
one covering test case" — true of the plan, false of the tree. **Two test cases had no
implementation at all**, and the whole suite passed regardless, because a test that does not exist
cannot fail:

- **TC-31** (NFR-8 / AC-26) — the end-to-end case, and the reason AC-26 came back MISSING. Now
  `backend/tests/test_audit_e2e.py`: a real gunicorn child with **token authentication actually on**,
  driven with a permitted action, an auth refusal, and an unauthenticated `/ws/terminal` upgrade,
  each asserted present in the trail while every audit hardening option stays at its default. That
  last one is the point — `/ws/terminal` is answered by WSGI middleware *above* Flask's router, so
  it is the one surface no `after_request` hook reaches, and it is the product's highest-privilege
  endpoint.
- **TC-25** (FR-10b / AC-14d) — the terminal refusal hook. Now
  `TestTC25TerminalRefusalsAreAudited`, parametrized over all three reasons `_refuse_reason` can
  return, plus a case proving the three do not collapse into a single flood-guard entry.

Both were checked by **mutation**, not by passing alone: deleting the `_audit_terminal_refusal` call
in `api/terminal.py` makes the end-to-end case fail. A refusal test that still passes with the audit
call removed is worse than no test, because it reports coverage that is not there.

**The test plan's pointers had also gone stale — 25 of its 54 pytest node ids resolved to nothing.**
Three named files were never created (`test_audit_e2e.py`, `test_audit_injection.py`,
`test_audit_settings.py`) and 18 named functions renamed when the tests were reorganised into
`TestTC<N>...` classes. The coverage was intact throughout, which is what made it invisible: every
test passed, and only a reader who tried to *follow* a citation would discover it led nowhere.
**All 103 citations were regenerated from the tree and now collect cleanly** (119 tests, zero
unresolvable). The `TestTC<N>` convention is the durable anchor — the TC id is in the class name, so
renaming a method cannot break the mapping and the column can be re-derived from source.

**38 of 40 test cases now resolve to executing tests.** The two that do not are TC-39 (inspection)
and TC-40 (demonstration), which verify by method rather than by pytest; their evidence is the
committed checklist at [`docs/AUDIT_PHASE1_VERIFICATION.md`](AUDIT_PHASE1_VERIFICATION.md). NFR-6
consequently shows no executed test, which is correct rather than a gap.

Reports: [`acceptance-validation`](../.plc/briefs/auth-audit-logging-acceptance-validation.md) and
[`traceability`](../.plc/briefs/auth-audit-logging-traceability.md). The bundled traceability CLI
marks all 39 requirements `GAP` because it accepts no `--test-plan` and does not scan source; a
companion matrix built from the plan, the resolved tests and the evidence log is appended to the same
file, with the tool's own output left visible beside it.

**A tooling limitation worth knowing before the next phase:** the evidence CLI validates ids against
`^(FR|NFR|AC)-\d+$`, which cannot express the letter-suffixed ids these briefs use throughout
(`AC-14a`, `FR-7b`, `AC-28a`…). Twenty of Phase 1's 55 criteria are therefore keyed to their base
requirement with the exact criterion named in `notes`, and the acceptance CLI — which reads the id
fields, not the notes — parses only the 34 unsuffixed ACs. Phase 2's brief uses the same suffix
convention, so this recurs unless the tool is fixed or the convention changes.

One item-7 result worth carrying forward, because the final number hides it: the 5 ms p95 audit
budget (NFR-2a) was **breached at +8.66 ms** until the active cluster label was cached.
`catalog_context.resolve()` was reading TinyDB on every audited request to attach a label that
changes only when an operator switches project. Caching it on the store's generation counter brought
it to +0.37 ms. Any future per-request enrichment should assume the same trap.

## Phase 2 — identity, and why groups belong here

Requirements: **[`.plc/briefs/auth-identity-brief.md`](../.plc/briefs/auth-identity-brief.md)** —
42 requirements, 48 acceptance criteria, drafted 2026-08-25. Decisions taken at kickoff, recorded in
that brief rather than here: SRP is **kept** rather than replaced by an argon2id verifier over TLS;
JWTs are used for the browser too but transported in an `HttpOnly` cookie rather than `localStorage`;
OIDC is **in** this phase and needs no new backend dependency beyond the PyJWT our own tokens
require; groups are flat with user-level denial winning.

> ⚠️ **This phase removes the existing authentication methods, it does not extend them.** Shared-token
> and PAM login, and the five `RACK_WEB_AUTH_*` / `RACK_WEB_PAM_*` variables, cease to exist. Every
> deployment in service authenticates by shared token today, so **the upgrade is a credential
> change**: the same start that removes the legacy path bootstraps an `admin` user and prints a
> generated password once (FR-10b). Read it from the container's startup output, log in, change it,
> create real users. This needs to reach the deployment documentation before the release, not after.
> Full rationale and blast radius: the brief's *Breaking change* section.

The identity store owns **users, groups, and membership**. Groups are not a Phase 3 concern despite
being consumed by policy, for a scheduling reason: adding group membership to an SRP-backed user store
*after* it holds real credentials is a migration with an authentication outage in it. The cost of
deciding now is a schema field; the cost of deciding later is a migration.

Three properties Phase 2 has to settle, because Phase 3's evaluator depends on all three:

- **Nesting.** Can a group contain a group? If yes, membership resolution is transitive and the
  evaluator needs a cycle guard.
- **Precedence.** What wins when a user-level `forbid` meets a group-level `permit`, and vice versa?
  This is not answered by "explicit deny wins" — that rule is about statements, not principals.
- **Membership at decision time.** Cached, deliberately — and paired with a revocation fast path,
  because instant grant invalidation is not achievable with a cache and no one achieves it (Kerberos
  carries group membership in the ticket PAC and needs a new TGT; stateless JWTs are valid until they
  expire). The invariant instead: **grants may be stale, denials may not.** A `principal → revoked_at`
  record is checked before any grant work and refreshed in seconds, so a revocation lands promptly
  while a removed grant lingers for the cache TTL. Design in
  `docs/PHASE3_POLICY_ENGINE_SPIKE.md` §5b; `identity.user:revoke` is itself an audited, governed
  action.

**What each refusal returns over HTTP is part of this phase, not a detail left to routes.**
[`docs/API_RESPONSE_CODES.md`](API_RESPONSE_CODES.md) is the contract: 401 for an unauthenticated
caller with a uniform body and a mandatory `WWW-Authenticate`, 403 for an authenticated one refused by
policy, 429 with `Retry-After` for the login rate limit (FR-4g), and **503 where authentication cannot
be evaluated** — the fail-closed case, which must not be a 401 because the credential may be valid and
re-authenticating cannot help.

Two things there need settling before Phase 3 ships, both recorded rather than deferred silently:

- **403 already means "feature disabled by configuration"** at all 32 current sites, and Phase 3 gives
  it a second meaning with the opposite remediation. Resolved by a `reason` discriminator in the body
  (`feature_disabled` / `not_authorized`) instead of migrating 44 endpoints' status codes; the auth
  paths emit it, the feature-disabled sites do not yet (FR-4h).
- **A 403 must not become an existence oracle.** Phase 3 has to decide, per resource type, whether an
  unauthorised read returns 403 or 404 — the same reasoning as FR-4b for logins, applied to racks,
  runs and users.

The decision table in `docs/PHASE3_POLICY_ENGINE_SPIKE.md` §2 carries rows for these so they are
answered by a test rather than by an implementation accident.

## Phase 2.5 — becoming an OIDC Provider, with SRP kept underneath

Directed 2026-09-01. **SRP stays.** It is not in tension with OIDC, and the distinction is the whole
basis of this phase: SRP is an authentication *method* — how a server satisfies itself that you know
a password without the password crossing the wire — while OIDC standardises everything downstream of
that check. OIDC has never specified how a provider verifies a password; every provider does it
privately. So SRP sits exactly where a provider's private credential check has always sat, and
replacing it with a form POST would be a downgrade, not a standardisation.

The precedent is Cognito, which is what was asked for at the outset: its `USER_SRP_AUTH` flow is
SRP-6a internally, and what it publishes is an ordinary OIDC id/access/refresh triad plus a JWKS
endpoint. We arrived at the same split without planning to.

**Two motivations, only one of which is this phase.** Being an OIDC *client*, so operators sign in
with NVIDIA SSO, is FR-19 and needs none of this. Being an OIDC *provider*, so other software can
consume our tokens, is this phase. The consumers worth naming: the Grafana embeds, which today sit
outside the auth story entirely, and a future CLI or agent that needs a credential we can verify
without sharing a minting secret.

### What already conforms

More than expected. The id/access/refresh triad *is* the OIDC triad. All seven registered claims are
present and **demanded** rather than validated-if-present, which is stricter than most providers.
Refresh works and is revocable by `jti`.

### The divergences, and which one is structural

| Divergence | Consequence |
|---|---|
| `HS256` symmetric signing | **The blocker.** Verification and minting are the same capability, so no key can ever be published |
| No `/.well-known/openid-configuration` | No client can self-configure |
| No `/.well-known/jwks.json` | Nothing external can verify a token |
| Custom `typ` claim | Standard is `token_use` (Cognito) or RFC 9068's header `typ` |
| No `auth_time`, `nonce`, `azp`, `scope` | A relying party cannot tell how old the authentication is, or bind a nonce against replay |
| `POST /api/auth/refresh` | OAuth 2.0 spells this `POST /token` with `grant_type=refresh_token` |
| No authorization-code flow | Nothing can delegate login to us |
| Cookie transport, not `Authorization: Bearer` | **Deliberate — do not "fix" it.** See below |

Only the first is structural. Everything else on that list becomes straightforward once signing is
asymmetric, and is blocked while it is not.

### Three things to get right

**The algorithm change invalidates every outstanding token, and it is bigger than a constant.** The
signing key is HKDF-derived from `RACK_WEB_SECRET_KEY`; asymmetric signing needs a real key pair,
durable storage for it, a `kid` in the header so more than one can be live at once, and a rotation
procedure. This is INV-17 in `identity-auth-traps.mdc` — every token minted before the switch fails
verification after it, so the cutover logs everyone out and that has to be planned rather than
discovered.

**Being both provider and client makes `iss` load-bearing for the first time.** The traps rule
currently records that `iss` and `aud` do not separate deployments, because they are product
constants. That stops being true the moment we verify NVIDIA SSO's tokens alongside our own:
accepting a token from the wrong issuer is a full authentication bypass, and the check that prevents
it is a parameter PyJWT ignores unless passed (also INV territory — see trap 6).

**Keep cookies for the SPA.** `HttpOnly` + `Secure` + `SameSite` beats a Bearer token in
`localStorage`, because XSS can read `localStorage` and cannot read an `HttpOnly` cookie. This is a
deviation from how OIDC apps are usually *demonstrated*, not from the specification — a provider may
set cookies for its own first-party UI and serve Bearer tokens to API clients. The phase should
*add* Bearer acceptance for programmatic callers, not migrate the browser to it.

### Sequencing

After Phase 2 closes, before Phase 3. Phase 3 binds policy to principals and will want `scope`, so
the claim vocabulary should settle first — but Phase 2's remaining work does not depend on any of
this, and the OP surface is a coherent slice on its own.

## Phase 2.6 — federation, and why it is its own increment

**The distinction that made this look done when it was not.** `backend/identity/oidc.py` is the
OIDC **Provider**: it mints tokens for accounts in the local store, publishes JWKS, answers
discovery and `/userinfo`, and runs the authorization-code flow. Federation is the mirror image —
accepting an ID token *another* issuer minted — and shares almost no code with it. On a filename
scan the two are indistinguishable, which is how four requirements sat inside a phase reported as
delivered with nothing implementing them.

Open scope, carried over from Phase 2: **FR-7** (federation as a configurable alternative,
disabled by default), **FR-7a** (a federated subject resolves to a local record so groups, policy
attachment and the audit trail behave identically), **FR-19** (a labelled button per configured
provider), **FR-19a** (a configured-but-unreachable provider leaves local login usable). Criteria:
AC-13, AC-14, AC-15, AC-29b, AC-29c.

**The one trap to design against before writing any of it.** PyJWT does not check `iss` at all
unless `issuer=` is passed (`identity-auth-traps.mdc` §6). Today that is survivable because there
is exactly one issuer and the signing key is the real boundary. With federation there are **two
issuers on one verify path**, and accepting a token from the wrong one is a full authentication
bypass — an attacker who can get any token from any issuer we trust becomes any user. So the
verify path must take the issuer as a required argument rather than a default, and the test that
matters is the negative one: a well-formed token from issuer B presented where issuer A is expected.

`AUTH_FEDERATED` already exists in `identity/models.py` as a constant with nothing resolving it,
which is the shape to watch: a placeholder that makes a scan look complete.

## Phase 3's test-credential gate — LIFTED 2026-09-08

**Kept rather than deleted, because the measurement in it is the reason the gate was right.** The
prerequisite is met: `backend/identity/testing.py` issues real credentials, `conftest.py` installs an
authenticated client for every test, and `RACK_WEB_AUTH_DISABLED` no longer exists (commit 86b4b4b).
The suite runs 3,474 tests with the gate enforcing.

One correction to the numbers below, which matters more than the gate itself. They said 53 tests
exercised the gate and 2,286 did not. The real figure was worse: because the environment is
process-global and pytest imports every test module during **collection**, 25 modules that
hard-assigned `RACK_WEB_AUTH_DISABLED=1` switched the gate off for the *entire session* — including
for the 53. Removing the switch cost **304 failing tests** to repair, against the 154 this brief
predicted and the 17 an intermediate measurement claimed. A flag readable at any point in a session
cannot be measured by reading `conftest.py`; it has to be probed at run time.

**The original entry, as written:**

> **Today the suite bypasses authentication.** `backend/tests/conftest.py` sets
> `RACK_WEB_AUTH_DISABLED=1`, so **2,286 of 2,339 tests reach every endpoint unauthenticated**; the 53
> that do exercise the gate use the shared-token mode Phase 2 removes. The product's own test suite has
> therefore never authenticated as anybody.

**That is survivable for Phase 2 and fatal for Phase 3.** An authorization assertion has the shape
*user A may do this and user B may not*, which needs two authenticated principals with different
group memberships in the same test run. A bypass cannot produce one principal, let alone two. So the
work is not "write authorization tests" — it is a **test credential facility** that issues or
retrieves a real credential for a named user, after which the suite logs in like an operator does
(`.plc/briefs/auth-identity-brief.md` FR-18b, with FR-18c removing the switch once it exists).

This is also what the phrase *first-class citizen* means for auth, in the sense `core-designs.mdc`
uses it: a concern the product exercises, not one it switches off so its own tests will pass. An
authentication gate that no test authenticates through is a gate whose behaviour is asserted 53 times
and assumed 2,286 times.

Tracked as SUTP gap **G-10** (High) — **closed 2026-09-08**.

## The admin UI — named, so it stops being invisible

Backend-only phases produced no UI, correctly. But the identity system the original request described
needs CRUD surfaces, and they had no home in any phase until now. All of these are new KUI views
consuming the existing frontend contexts (`core-designs.mdc` §12) — not local state duplicating them.

| View | Owns | Phase | Notes |
|---|---|---|---|
| `UsersView` | User CRUD; disable/enable; credential reset | 2 | The only creation path is an authenticated request, per the original requirement. No self-registration |
| `GroupsView` | Group CRUD; membership add/remove | 2 | Membership editing is the surface that makes groups worth having; without it they are a schema field |
| `PoliciesView` | Policy document CRUD; attach/detach to a user or group | 3 | Meaningless before an engine exists — a UI that edits documents nothing evaluates invites false confidence |
| Audit trail viewer | Reading the Phase 1 trail without shell access | 3+ | Already recorded as a Phase 1 non-goal: authentication alone is insufficient, because with no authorization model every operator could read every other operator's actions |

Two constraints that apply to all four, both from existing decisions rather than new ones:

- **Every mutating route these views call needs an `@action` declaration** (Phase 1, FR-14b) or the
  build fails. So the vocabulary grows with the UI — `identity.user:create`, `identity.group:add_member`,
  `policy:attach` — and those actions are themselves governed by policy. Editing who can do what is
  itself a privileged action.
- **Policy administration is a privilege-escalation surface.** Whoever can attach a policy can grant
  themselves anything, so `policy:attach` and `identity.group:add_member` are the two actions whose
  own authorization matters most. Threat-model them when Phase 3 is designed, not after.

## Scale target, and the read/write plane split

The identity model is sized for **up to 20 simultaneous operators**, and at that scale the binding
constraint is not Rack Status but the head node's SSH session ceiling — which a shared service account
already exceeds at 20 operators (`MaxSessions` 10 per connection, one shared ControlMaster). Per-user
credentials raise it 20x; moving read traffic off SSH to `pythoncm` removes most of the demand. The two
planes therefore want different identity models: a service credential for reads, per-user certificates
for privileged writes and the terminal. Sizing, numbers and caveats:
**[MULTI_USER_MULTI_CLUSTER_PLAN.md](MULTI_USER_MULTI_CLUSTER_PLAN.md)** — "What scale this supports".

## What this programme does not cover

- Per-user cluster state and the removal of the `os.environ` materialisation. That is
  **[docs/MULTI_USER_MULTI_CLUSTER_PLAN.md](MULTI_USER_MULTI_CLUSTER_PLAN.md)**, whose step 1 is
  blocked on Phase 2 here. The two efforts meet at "an operator is distinguishable".
- Tamper evidence for the audit file (threat T-51, candidate NFR-12) — recorded as deferred in the
  Phase 1 brief, not scheduled.
- Secrets management beyond what `token_vault.py` already does.
