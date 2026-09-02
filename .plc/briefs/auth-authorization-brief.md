# PLC Feature Brief — auth-authorization (Phase 3)

**Status:** requirements draft, 2026-09-02. Not yet reviewed.
**Programme:** `docs/AUTH_PROGRAMME_SCOPE.md` — Phase 3, the last phase.
**Jira:** [NVRACKSTAT-58](https://jirasw.nvidia.com/browse/NVRACKSTAT-58).
**Directed by:** feature lead, 2026-09-02 — resource resolver first, full V-model, node grants explicit.

> Requirement numbering is per-brief, as in every prior phase. `identity FR-n` and `oidc FR-n` name
> requirements in the Phase 2 and Phase 2.5 briefs.

## Purpose

Make authorization *mean* something. The mechanism already exists and is already enforced; what is
missing is the ability to write a policy narrower than "everything".

**Three things are already done, and stating that is the point of this section** — Phase 3 is not a
green field and treating it as one would rebuild working parts:

- **Enforcement.** `app._require_dashboard_auth` calls `authz.authorize()` before dispatch, refuses
  with 403, and records the verdict and engine in the audit event.
- **The engine.** `authz/policy.py` — default-deny, deny-overrides, groups as the unit of grant,
  reload-on-change, 32 tests.
- **The rollout.** A policy file turns enforcement on; without one, `AllowAllEngine` keeps today's
  behaviour and says so in the trail.

## The defect this phase exists to fix

**Resource-scoped policies silently deny everything.** Measured, not inferred:

| Resource the engine receives | Policy `resources: ["rack:*"]` |
|---|---|
| `rack:a01` | allowed |
| `/api/rack/power` ← *what the gate actually passes* | **denied** |

The call site passes `request.path`. Policies are written against the identifiers in
`docs/RESOURCE_MODEL.md` (`rack.power:p5-r01`, `slurm.job:mayo:127391`). They never match.

**The failure mode is worse than the failure.** Nothing errors. The engine works, the policy parses,
and the refusal reads `no policy grants rack.power:cycle` — which looks like a missing grant rather
than a broken resolver. The obvious remedy is to widen `resources` to `["*"]`, which makes the
policy work and discards the scoping that was the reason for writing it. So today the product has
exactly two reachable states: **grant globally, or grant nothing.**

## Assumptions

1. **~20 operators per site, a handful of roles.** Policies are written by an operator in a file,
   not generated. A model needing a UI to be usable is too complex for this scale.
2. **One instance serves several clusters** (Mayo, Firmus, Boston, Nscale). A resource id without a
   cluster qualifier is ambiguous where the underlying id is not globally unique.
3. **Rack id and node hostname are different namespaces** (`cluster-naming-conventions.mdc`), and
   converting between them is per-cluster configuration, not arithmetic.
4. **Reads matter.** Phase 1 declared only mutating actions because only those needed auditing.
   Authorization needs read verbs, and that is the larger half of this phase.

## Proposed Functional Requirements

### The resource resolver — the defect above

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-1 | The product shall resolve each request to a **resource identifier** in the `docs/RESOURCE_MODEL.md` grammar, and pass that identifier — not the URL path — to the authorization engine. | the measured defect | this single change is what makes every `resources:` clause in a policy start working |
| FR-2 | The resolver shall be **declared per route**, in the same shape as `@action`, and the declaration shall live next to the route it governs. | `RESOURCE_MODEL.md` §"What Phase 3 has to add" | a per-route convention that is not *declared* decays exactly as a per-route audit convention would have; the `@action` registry exists because that was already learned once |
| FR-3 | A route with a declared action and **no declared resource** shall resolve to a **type-only identifier** (e.g. `rack.power:*`), never to the URL path and never to `*`. | consequence of FR-1 | the path is what is broken today; a bare `*` would silently widen every policy that names that type. A type-only id keeps type-level rules working and makes instance-level rules visibly inapplicable |
| FR-4 | Resource ids drawn from request data shall be **validated against their type's grammar** before reaching the engine, and a request whose id fails validation shall be refused. | security | an unvalidated id is caller-controlled input reaching a policy matcher. `resources: ["rack:a*"]` against a caller-supplied `rack:a01` is a grant; against `rack:*` smuggled in a body it is a privilege escalation |
| FR-5 | Ids for cluster-scoped types shall carry the **active cluster** as a qualifier (`slurm.job:<cluster>:<id>`). | assumption 2; `RESOURCE_MODEL.md` item 4 | Slurm job ids are not unique across clusters and one instance serves several, so an unqualified rule about job 127391 is a rule about a different job on every other site |

### Grant semantics

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-6 | A grant on a rack shall **not** implicitly grant its member nodes. Node-scoped grants shall be explicit. | **feature-lead decision 2026-09-02** | the alternative — expanding rack grants through the naming templates — makes the *meaning of a grant depend on per-cluster naming configuration*, so the same policy file would confer different access at Mayo and Boston, and a naming change would silently widen access. Chosen for least surprise; the cost is that a rack-wide operator needs two clauses |
| FR-6a | Where a request acts on several resources (a fan-out across nodes), the request shall be permitted **only if every resource is permitted**. | consequence of FR-6 | any-of would let one permitted node carry a fan-out across a hundred forbidden ones. All-of can refuse a partially-permitted request, which is the safe direction and is diagnosable |
| FR-7 | A `deny` shall override any `allow`, irrespective of specificity or file order. | already implemented; restated because it is load-bearing | "never power-cycle rack 7" must hold against grants written later by someone who has not read it |

### Read verbs

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-8 | Every `/api` route reachable by a GET shall declare an action, so a read can be authorized. | **measured 2026-09-02: 112 of 230 `/api` routes are GET-only and *none* declares an action** | the `RESOURCE_MODEL.md` estimate was 109; it is 112 now because this programme added routes. The number will keep moving, which is why FR-9 is a build gate rather than a checklist |
| FR-9 | A GET route without a declared action shall **fail the build**, as a mutating route already does. | consequence of FR-8 | the existing gate covers mutating routes only. Extending it is what stops this surface reopening the moment somebody adds a view |
| FR-10 | Read actions shall be **distinguishable from mutating actions** by name, so a policy can grant read-only access without enumerating verbs. | usability at assumption 1's scale | a role that can look at everything and change nothing is the single most common grant an operator will want; if it needs 112 clauses nobody will write it |
| FR-11 | Declaring read actions shall **not** change the audit trail's volume by default. | Phase 1 NFR-7 (retention) | Phase 1 sized retention against mutating events. Auditing every GET at a 1-second poll cadence would multiply the trail by roughly two orders of magnitude and silently blow the retention budget |

### Testability — the recorded blocker

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-12 | The suite shall provide a facility to execute a request **as a named principal with a chosen policy set**, without disabling the gate. | `AUTH_PROGRAMME_SCOPE.md` records Phase 3 as blocked on exactly this | most of the suite runs with `RACK_WEB_AUTH_DISABLED=1`, so there is currently no way to assert "operator A may do X and may not do Y". Without it, a policy engine's tests cannot exercise the thing that makes it a policy engine |
| FR-13 | The facility shall be usable without a real SRP login. | consequence of the performance baseline | `srp.begin` costs ~110 ms; a per-test login would add minutes to the suite and make the cost of a policy test proportional to the crypto rather than the assertion |

### Policy administration

| ID | Requirement | Source | Notes |
|---|---|---|---|
| FR-14 | Policy changes shall take effect **without a restart**. | already implemented (mtime-keyed reload) | restated because a restart to change a rule means an outage to fix a lockout |
| FR-15 | A policy file that cannot be parsed shall cause **every request to be refused**, loudly, rather than falling back to permissive. | already implemented | the alternative is a typo that silently opens the product |
| FR-16 | The product shall provide a way to ask **"what would this principal be allowed to do"** without performing the action. | operability | the first question after a refusal, and the only alternative is trial and error against a live cluster. Read-only, and it must not become an oracle: it answers only about the caller unless the caller may administer policy |

## Proposed Non-Functional Requirements

| ID | Requirement | Source | Notes |
|---|---|---|---|
| NFR-1 | Authorization shall add no more than **1 ms** at p95 to a request. | `docs/PERFORMANCE_BASELINE.md` measures 0.056–0.073 ms for 50 statements today | deliberately ~14× the measured cost: the resolver adds work per request, and a budget set at today's number would fail on the first honest implementation |
| NFR-2 | Resolution and evaluation shall perform **no writes** and no network I/O. | Phase 2 trap 2 | a read path that writes defeats any cache above it; the revocation cache was invalidated by its own reads and never hit once |
| NFR-3 | A refusal shall not disclose whether the resource exists. | existence oracle | already partly held: the current reason names the action, never the resource |
| NFR-4 | The policy file shall be **operator-editable**: a text format, diffable, reviewable in a merge request. | assumption 1 | a binary or UI-only policy store cannot be code-reviewed, and an authorization change is exactly the change that wants review |
| NFR-5 | Evaluation shall be **deterministic** — the same principal, action and resource shall yield the same verdict for a given policy set and group membership. | auditability | a verdict that depends on evaluation order or dict iteration cannot be explained after the fact, and "why was I refused" is the question this phase must be able to answer |

## Acceptance Criteria

| ID | Verifies | Criterion | Method |
|---|---|---|---|
| AC-1 | FR-1 | A policy with `resources: ["rack.power:a01"]` permits an action on that rack and refuses the same action on `rack.power:b02`. **This fails today.** | automated |
| AC-2 | FR-1 | No URL path is ever passed to `authorize()`, asserted against the live call site. | automated |
| AC-3 | FR-2 | A route declares its resource beside its action; the registry resolves it. | automated |
| AC-4 | FR-3 | A route with no declared resource yields a type-only id, and a policy naming `type:*` still matches while one naming an instance does not. | automated |
| AC-5 | FR-4 | A caller-supplied id containing a wildcard, a separator or a traversal sequence is refused rather than matched. | automated, parametrised — the escalation path |
| AC-6 | FR-5 | The same Slurm job id under two active clusters produces two different resource ids. | automated |
| AC-7 | FR-6 | A rack-scoped grant does **not** permit an action on a node in that rack. | automated |
| AC-8 | FR-6a | A fan-out over three nodes where one is denied refuses the whole request. | automated |
| AC-9 | FR-7 | A deny beats an allow regardless of order and specificity. | automated (exists) |
| AC-10 | FR-8, FR-9 | Every GET `/api` route declares an action, and adding one without a declaration fails the build. | automated |
| AC-11 | FR-10 | A single policy clause grants read access to every resource type without enumerating routes. | automated |
| AC-12 | FR-11 | With default settings, authorizing reads does not increase audit event volume. | automated |
| AC-13 | FR-12, FR-13 | A test authenticates as `user:a`, is permitted one action and refused another, with the gate **enabled** and no SRP exchange. | automated — the facility proving itself |
| AC-14 | FR-14 | Editing the policy file changes the next request's verdict with no restart. | automated (exists) |
| AC-15 | FR-15 | A malformed policy file refuses every request and says why. | automated (exists) |
| AC-16 | FR-16 | The preview endpoint answers for the caller, and refuses to answer about another principal unless the caller may administer policy. | automated |
| AC-16a | NFR-3 | A refusal for a resource that does not exist is byte-identical to one for a resource the caller may not touch. | automated — an oracle is a difference, so the test is that there is none |
| AC-16b | NFR-4 | The policy file is text, parses from a merge-request diff, and a syntactically valid file written by hand loads without tooling. | automated |
| AC-17 | NFR-1 | Measured p95 authorization overhead is within budget, recorded on the deployment's hardware. | measured — and recorded, or it cannot detect a regression |
| AC-18 | NFR-2 | Repeated authorization performs no write and no outbound connection. | automated |
| AC-19 | NFR-5 | The same inputs yield the same verdict across 1,000 evaluations and a reload. | automated |

## Decisions taken, 2026-09-02

| # | Decision | Cost accepted |
|---|---|---|
| 1 | **Resource resolver first** | The brief lands before the fix, so the silent defect survives another increment. Accepted because a resolver designed after the read-verb work would have to be redesigned around it |
| 2 | **Full V-model** — brief, design, test plan, then code | Slower. Chosen against the Phase 2.5 precedent, where skipping the test plan produced five gaps that writing it afterwards then found |
| 3 | **Rack grants do not cover nodes** (FR-6) | A rack-wide operator writes two clauses instead of one. Bought: a grant's meaning does not depend on per-cluster naming configuration, so the same policy file means the same thing at every site |

## Open questions

1. **The 11 `validation:run_*` verbs** — keep them, or collapse to `run` with the tool as a resource
   attribute? Affects FR-10's read/write naming split.
2. **Does `catalog:set_active` need its own treatment?** It changes which cluster every subsequent
   request addresses, so it is closer to a session operation than a resource mutation.
3. **Where does the policy file live** — `var/store/` beside the catalog, or `var/secrets/`? It is not
   secret, but it is security-relevant, and the two directories have different backup stories.
4. **Should FR-16's preview be a route or a CLI?** A route is discoverable; a CLI cannot be reached
   by an attacker at all.

## Explicit non-goals

- **A policy editor UI.** `PoliciesView` waits until the model has stopped moving; a UI over a
  changing schema is rework, and NFR-4 keeps the file reviewable meanwhile.
- **Attribute-based rules** beyond principal/action/resource — no time-of-day, no source address.
  Not needed at assumption 1's scale, and each one is a new thing to get right.
- **Delegation.** No operator granting another operator a subset of their own rights.
- **Per-request policy overrides.** Nothing in a request may alter the policy applied to it.
