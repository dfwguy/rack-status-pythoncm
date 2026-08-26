# Auth programme — phase scope

The map of the authentication and authorization work: what each phase owns, and what is deliberately
deferred to a later one. It exists because two constructs were **invisible rather than deferred** until
2026-08-20 — principal **groups**, and the **admin UI** — and an unscoped construct is one nobody
notices is missing until it is expensive.

Per-phase requirements live in PLC feature briefs under `.plc/briefs/`. This file is the index and the
scope record; it is not a spec and it does not carry acceptance criteria.

| Phase | Owns | State |
|---|---|---|
| 0 | Defect fixes: the unauthenticated `/ws/terminal`, `target` command injection, `0.0.0.0` exposure | done |
| 0.5 | HTTPS: self-signed by default, internal CA opt-in, TLS 1.2 floor, CSP/HSTS, Origin policy | done |
| 1 | Central logging configuration; durable JSONL audit trail; the 89-action vocabulary Phase 3 binds to | **done** — all 7 work items, 40/40 test cases, merged to `main` 2026-08-25 |
| 2 | **Identity**: local user store (SRP), **groups and membership**, JWT id/access tokens, OIDC federation, first-boot admin credential | requirements in progress |
| 3 | **Authorization**: policy engine, policy documents, attach-to-principal, enforcement before dispatch | not started; `docs/PHASE3_POLICY_ENGINE_SPIKE.md` is the decision-support draft. **Gated on a test credential facility** — see below |

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
| 3 | The 103 `@action` declarations across 25 modules, plus the committed `action_snapshot.json` | TC-18 | **done**, 6 tests; gate live |
| 4 | `backend/audit/sink.py` + `audit/__init__.py` — event schema, bounded rotating JSONL, flood guard | TC-6…TC-14, TC-32, TC-33 | **done** |
| 5 | Client-address resolver + trusted-proxy validation (`core.py`, `api/_common.py`) | TC-19, TC-20 | **done** |
| 6 | The `after_request` chokepoint + the WSGI terminal refusal hook | TC-22…TC-31 | **done** |
| 7 | Performance, compatibility and regression cases | TC-34…TC-40 | **done** — incl. TC-39 inspection and TC-40 demonstration |

Two things fixed along the way that were not work items and are worth not rediscovering: a missing
`static/spa/index.html` made `serve_spa_asset` hand off a file it had just found absent, returning
500 instead of a 503 that says the frontend was never built; and `.scratch/` held ~2,000 untracked,
unignored files, one `git add .` from the repo.

After item 7 come the closing V-model steps — verification, security scan, acceptance validation and
the traceability matrix — which are phases in the state file rather than work items here. As of
2026-08-25 **verification and the security scan are complete**; acceptance validation and the
traceability matrix are deliberately deferred, not attempted and failed (both inputs exist).

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

## Phase 3 is gated on tests that actually authenticate

Recorded as a gate rather than a task, because it is the kind of prerequisite that gets discovered
half-way through the phase that needs it.

**Today the suite bypasses authentication.** `backend/tests/conftest.py` sets
`RACK_WEB_AUTH_DISABLED=1`, so **2,286 of 2,339 tests reach every endpoint unauthenticated**; the 53
that do exercise the gate use the shared-token mode Phase 2 removes. The product's own test suite has
therefore never authenticated as anybody.

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

Tracked as SUTP gap **G-10** (High).

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
