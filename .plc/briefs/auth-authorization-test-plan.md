# PLC Test Plan — auth-authorization (Phase 3)

**Brief:** `.plc/briefs/auth-authorization-brief.md` — 34 requirements, 32 AC.
**Jira:** [NVRACKSTAT-58](https://jirasw.nvidia.com/browse/NVRACKSTAT-58).
**Written:** 2026-09-02, **before implementation** — unlike Phase 2.5, where writing it afterwards
found five gaps the code had already shipped with.

## Purpose

Verify that a policy can express something narrower than "everything", and that the narrowing is
enforced. Everything else in this phase serves that.

**Three properties carry the weight.** They are listed first because the rest of the plan is
scaffolding around them:

1. **A resource-scoped grant actually scopes.** This is the defect the phase exists to fix, and it
   fails *silently* today — the policy parses, the engine runs, and the refusal reads like a missing
   grant. TC-1 is the regression test for a bug that never raised.
2. **Evaluation considers only attached policies.** Asserted by counting matcher invocations, never
   by timing. A timing assertion would pass on a broken implementation on a fast machine.
3. **Refusal is the default at every layer.** Unparseable policy, unknown verb, unvalidated id,
   missing attachment — each must deny. The failure direction is the whole design.

## Entry Criteria

- Brief reviewed; the four feature-lead decisions recorded (resolver first, full V-model, node grants
  explicit, attachment model).
- Phase 2.5 green: 3007 backend tests.

## Exit Criteria

- Every AC maps to a passing automated test, or an explicit N/A with a reason.
- `ruff`, `tsc`, `eslint`, coverage floor and `check-tracked-tree.sh` pass.
- **The 112 GET routes declare actions and the build gate covers them** (AC-10) — the largest single
  piece, and the one most likely to be declared "mostly done".

## Environments And Fixtures

| Fixture | Provides | Note |
|---|---|---|
| `policy_file` | a temp policy document set | mtime-keyed reload means tests must force distinct mtimes, not race the clock |
| `attachments` | an injected attachment lookup | injected, per FR-7c — a fixture that imported the identity store would defeat the constraint under test |
| `as_principal` | a request executed as a named principal, **gate enabled** | FR-12/13. Does not perform SRP: `begin` costs ~110 ms and a per-test login would price a policy test by its crypto |
| `matcher_spy` | counts statement evaluations | how "only attached policies are considered" is measured |

## Test Cases

### Unit — the resolver (FR-1..FR-5)

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-1 | FR-1, AC-1 | Policy `resources: ["rack.power:a01"]`; request against `a01`, then `b02` | permits `a01`, refuses `b02`. **Fails on today's code** |
| TC-2 | FR-1, AC-2 | Inspect the live `authorize()` call site | no URL path is ever passed as a resource |
| TC-3 | FR-2, AC-3 | A route declaring a resource beside its action | the registry resolves both |
| TC-4 | FR-3, AC-4 | A route with an action and no resource declaration | type-only id (`rack.power:*`); `type:*` policies match, instance policies do not; **never** the path, **never** bare `*` |
| TC-5 | FR-4, AC-5 | Caller-supplied ids: `*`, `rack.power:*`, `a01:extra`, `../`, empty, 10 KB | each refused before reaching the matcher |
| TC-6 | FR-5, AC-6 | Same Slurm job id under two active clusters | two distinct resource ids |

### Unit — the schema (FR-17a, FR-17b)

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-7 | FR-17a, AC-15e | Post documents missing `effect`, with an unknown field, with a bad effect | each rejected, offending field named, store unchanged |
| TC-8 | FR-7a, AC-15b | A document carrying the retired `principals` field | **rejected** — the inline model must not half-work |
| TC-9 | FR-17b, AC-15f | Validate the exported JSON Schema against known-good and known-bad documents **using a JSON Schema validator** | agrees with pydantic. Validating with pydantic would prove only that pydantic agrees with itself |

### Unit — evaluation (FR-6..FR-7c)

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-10 | FR-6, AC-7 | Rack-scoped grant; request against a node in that rack | **refused** — node grants are explicit |
| TC-11 | FR-6a, AC-8 | Fan-out over three nodes, one denied | whole request refused |
| TC-12 | FR-7, AC-9 | Deny and allow both matching, in both file orders | denied either way |
| TC-13 | FR-7b, AC-15c | 500 unattached policies present | **zero** matcher calls against them |
| TC-14 | FR-7b, AC-15a | Attach to a group, then detach | permitted, then refused on the **next request**, no re-login |
| TC-15 | NFR-5, AC-19 | 1,000 evaluations plus a reload | identical verdicts |

### Integration — API (FR-17, FR-18)

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-16 | FR-17 | Create, read, update, delete, list | round-trips; delete honours open question C's answer |
| TC-17 | FR-18, AC-15g | Attach and detach on a user and on a group | audited events naming policy and principal |
| TC-18 | FR-18a | Every policy-admin route | declared action; build fails without one |
| TC-19 | FR-16, AC-16 | Preview for self; preview for another principal | self answered; other refused unless the caller may administer policy |

### Integration — read verbs (FR-8..FR-11)

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-20 | FR-8, FR-9, AC-10 | Walk the URL map | every GET `/api` route declares an action; an undeclared one fails the build |
| TC-21 | FR-10, AC-11 | One read-only clause | grants read across types without enumerating 112 routes |
| TC-22 | FR-11, AC-12 | Poll a GET route 100× at defaults | audit volume unchanged. Phase 1 sized retention against mutating events; a 1-second poll cadence would multiply the trail ~100× |

### System — the credential facility (FR-12, FR-13)

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-23 | FR-12, FR-13, AC-13 | Authenticate as `user:a` with the **gate enabled**, no SRP | one action permitted, another refused. The facility proving itself; without it nothing below is testable |

### Security

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-24 | FR-15, AC-15 | Malformed policy file | every request refused, loudly |
| TC-25 | NFR-3, AC-16a | Refusal for a nonexistent resource vs a forbidden one | **byte-identical** — an oracle is a difference, so the test is that there is none |
| TC-26 | FR-7c, AC-15d | Import graph of `authz/` | no module-level import of `identity/` |
| TC-27 | FR-4 | A policy whose own `resources` came from caller data | cannot widen another principal's grant |

### Frontend (FR-19, FR-20)

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-28 | FR-19, AC-15g2 | Invalid structure; unknown verb | both flagged inline, before submit. Two mechanisms (JSON Schema, registry), so both asserted |
| TC-29 | FR-19a, AC-15h | Editor verb list vs server registry | exact match, including actions added by this build |
| TC-30 | FR-19b, AC-15i | Built bundle | no external origin — the existing "no external requests" check |
| TC-31 | FR-20, AC-15j | Policy table | lists each policy and what it is attached to |

### Performance

| ID | Verifies | Action | Expected |
|---|---|---|---|
| TC-32 | NFR-1, AC-17 | Measure resolve + evaluate | ≤ 1 ms p95, against a **recorded** baseline. Budget is ~14× today's 0.056–0.073 ms because the resolver adds per-request work |
| TC-33 | NFR-2, AC-18 | Repeated authorization | no write, no outbound connection |

## Coverage

| Requirement group | Covered by |
|---|---|
| FR-1..FR-5 (resolver) | TC-1..TC-6, TC-27 |
| FR-6..FR-7c (evaluation) | TC-10..TC-15, TC-26 |
| FR-8..FR-11 (read verbs) | TC-20..TC-22 |
| FR-12, FR-13 (testability) | TC-23 |
| FR-14..FR-16 | TC-19, TC-24, and the existing reload tests |
| FR-17..FR-18a (API) | TC-7..TC-9, TC-16..TC-18 |
| FR-19..FR-20 (UI) | TC-28..TC-31 |
| NFR-1..NFR-5 | TC-15, TC-25, TC-32, TC-33, and AC-16b |

## Known risks in this plan

1. **TC-23 gates almost everything.** The credential facility is the recorded blocker; until it
   exists, policy tests can only call the engine directly and not through a real request. Direct
   tests are weaker: they cannot catch a resolver wired to the wrong call site.
2. **TC-20's number will move.** 112 GET routes today, 109 when `RESOURCE_MODEL.md` was written.
   Any count in a test is stale on arrival, which is why the criterion is a build gate over the live
   URL map rather than an assertion on a number.
3. **TC-13 counts calls, not time.** Chosen deliberately, but it means the test depends on a spy
   seam in the engine — a refactor that inlines the matcher would silently void it.
4. **TC-32 needs a recorded baseline**, and Phase 2.5 left the same gap open (AC-23). A budget
   compared against a figure measured in the same run cannot detect a regression.
