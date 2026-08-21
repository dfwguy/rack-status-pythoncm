# Phase 3 policy engine — comparison spike

**Status: decision-support only. Nothing here is agreed.** Written to inform the Phase 3 gate, after
the Phase 1 threat model (T-62, T-64) and the action vocabulary made the choice concrete enough to
compare properly. The original plan was "ship the Cedar interface now, wire a real engine later"; the
question this spike answers is whether the engine that eventually lands should be Cedar at all.

Related: `.plc/briefs/auth-audit-logging-brief.md` (Decision 2, Decision 2a, the vocabulary, threats
T-62/T-63/T-64), `.cursor/rules/core-designs.mdc` §2–3.

---

## 1. What the engine has to answer

Phase 1 already fixed the shape of the question, which is why this is now comparable. Every audited
request resolves to:

| Field | Source | Example |
|---|---|---|
| `principal` | authenticated session (Phase 2) | `rkoike`, `svc-campaign` |
| `principal groups` | group membership resolved from the identity store (Phase 2) | `boston-oncall`, `validation-leads` |
| `action` | declared vocabulary (Phase 1) | `rack.power:on`, `fabric.nvswitch:reset_sdn` |
| `variant` | declared alongside the action | `sync`, `async`, `stream`, `legacy`, `form` |
| `resource` | request path / body | rack `bos01-a05`, node `bos01-a05-11-s1-dgx-01-c01`, workflow id |
| `context` | request + active cluster | cluster `Boston Markley`, `src_ip`, time, campaign id |

The 89 actions across 35 services (22 top-level roots) are enumerable at startup, and `FR-14b` fails the build if a
mutating route lacks one. **That is the property that makes either engine viable** — a policy set can
be checked for completeness against a known finite action list, rather than hoping every route was
remembered.

## 2. The decision table this has to satisfy

Written first, deliberately. Whichever engine is chosen, this is the fixture — it is the artifact
that makes "the engine works" a testable claim rather than a demo.

| # | Principal's policies | Request | Expected | Why |
|---|---|---|---|---|
| 1 | *(none)* | `rack.power:on` on `bos01-a05` | **DENY** | default deny; no policy is not permission |
| 2 | permit `rack.power:*` | `rack.power:on` | ALLOW | verb wildcard within a service |
| 3 | permit `rack.power:*` | `rack.gpu:reset` | **DENY** | sibling service is not covered by `rack.power` |
| 4 | permit `fabric.nvswitch:*` | `fabric.nvswitch:reset_system` | ALLOW | sub-service prefix grant |
| 5 | permit `fabric:*` | `fabric.nvswitch:reset_system` | **decide** | does a parent service imply its sub-services? See §6 Q1 |
| 6 | permit `validation:run_nccl` | `validation:run_nemo` | **DENY** | per-tool granularity is the point of nine named runs |
| 7 | permit `tools:install_hpcx` | same action, `variant=stream` | ALLOW | twins share one action (FR-14c), so a transport cannot be a bypass |
| 8 | permit `slurm.node:drain` on rack `bos01-a05` | drain on `bos01-b06` | **DENY** | resource scoping; per-rack authority is the real operational case |
| 9 | permit `slurm.node:drain` + forbid `slurm.node:drain` on `bos01-a05` | drain on `bos01-a05` | **DENY** | explicit deny beats permit |
| 10 | permit `*:*` (admin) + forbid `rack.power:*` | `rack.power:on` | **DENY** | deny wins even against a wildcard admin grant |
| 11 | permit `rack.power:on`, policy document fails schema validation | `rack.power:on` | **DENY** | unparseable policy is deny, never ignore-and-continue (T-64) |
| 12 | permit `rack.power:on`, evaluator raises | `rack.power:on` | **DENY** | fail closed (T-64) |
| 13 | permit `rack.power:on` in cluster `Firmus` | same action in cluster `Boston Markley` | **DENY** | one instance serves several clusters (`core-designs.mdc` §3) |
| 14 | permit `workflow:run` | `workflow:run` on a workflow with a `host:` shell step | **decide** | arbitrary shell via workflow steps is a privileged capability; see §6 Q2 |
| 15 | permit `catalog.tool:create` | `catalog.tool:create` | ALLOW, **and** audited as a policy-changing action | editing the catalog changes what is launchable |
| 16 | group `boston-oncall` permits `slurm.node:drain`; user has no direct policy | drain, user in that group | ALLOW | group policies are inherited, or groups buy nothing |
| 17 | group permits `slurm.node:drain`; user-level forbid `slurm.node:drain` | drain | **decide** | user-level forbid versus group permit — see §6 Q6 |
| 18 | group forbids `slurm.node:drain`; user-level permit | drain | **decide** | the mirror case, and the answer need not be symmetric — §6 Q6 |
| 19 | group `a` contains group `b`; `b` permits `rack.power:on`; user in `a` | `rack.power:on` | **decide** | is membership transitive? §6 Q7 |
| 20 | group `a` contains `b`, `b` contains `a` (cycle); either permits an action | that action | **DENY, and refuse the group set at load** | a membership cycle must fail closed and loudly, never spin |
| 21 | user removed from a group that granted the action, membership cached, no revocation issued | that action | ALLOW until the cache expires | honest: a grant removal cannot be made instant, see §5b |
| 22 | same, but a revocation is issued for that principal | that action | **DENY within the revocation refresh interval** | the fast-path deny of §5b — grants may be stale, denials may not |
| 23 | revocation issued at time T; a token or session minted **before** T | any action | **DENY** | revocation compares against issue time, so it needs no token registry |
| 24 | revocation issued at T; principal re-authenticates after T | an action they still hold | ALLOW | a revocation is a cutoff, not a ban — otherwise it is indistinguishable from disabling the account |

Rows 5 and 14 are genuinely undecided and are the two questions worth a human at the gate.

## 3. Option A — pydantic policy document + default-deny evaluator

The shape the feature lead has already built once (AWS S3 RBAC workshop, S3 Object Lambda): pydantic
models define and enforce the policy-document schema; the evaluator starts at DENY and walks the
statements attached to the principal.

```python
class Effect(StrEnum):
    permit = "permit"
    forbid = "forbid"

class Statement(BaseModel):
    model_config = ConfigDict(extra="forbid")     # an unknown key is a rejected document, not a shrug
    effect: Effect
    actions: list[ActionPattern]                  # "rack.power:on", "fabric.nvswitch:*", "*:*"
    resources: list[str] = ["*"]                  # rack / node / workflow ids, or a pattern
    clusters: list[str] = ["*"]                   # multi-cluster instance (core-designs §3)
    variants: list[str] = ["*"]                   # rarely needed; twins normally share the grant
    condition: Condition | None = None            # campaign id, time window, src_ip trust

class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["nvis-policy/v1"]
    principals: list[str]                         # users or roles
    statements: list[Statement]
```

`ActionPattern` is a constrained string validated against the **live registry** at load time, so a
policy naming an action that does not exist is a load error rather than a rule that silently never
matches. That check is only possible because of FR-14b, and it is the single strongest argument for
having built the vocabulary first.

Evaluation: start DENY → collect statements whose action, resource, cluster and variant all match →
if any `forbid` matches, DENY → else if any `permit` matches, ALLOW → else DENY. Any exception, any
validation failure, DENY.

**For:** `pydantic` is already resolved in this repo's dependency tree (via `mcp`), so no new
supply-chain decision; the idiom matches the catalog's document handling, so operators and reviewers
meet one pattern rather than two; policy documents become catalog records, which is where
`core-designs.mdc` §2–3 says deployment-specific configuration belongs; and validating patterns
against the registry catches the most likely authoring mistake at load.

**Against:** the evaluator is ours to get right. Precedence bugs are subtle and silent — a wrong
answer looks like a working system. Group hierarchy sharpens this: Cedar models principal parentage
natively, whereas a pydantic evaluator has to implement transitive membership, a cycle guard and a
user-versus-group precedence rule itself (rows 16–21, §6 Q6–Q8). That is a real cost against Option A
and it was not visible before groups were scoped. `pydantic-core` is a compiled wheel, so it is not literally
dependency-free, though it is mainstream and already present.

## 4. Option B — Cedar via `cedarpy`

**For:** formal semantics, and policy *analysis* rather than only evaluation — Cedar can answer "is
this policy set equivalent to that one" and "can this policy ever allow X", which no hand-rolled
evaluator will do. Well-specified precedence that we neither design nor defend.

**Against:** there is no AWS-maintained Python SDK; `cedarpy` is a community binding over the Rust
implementation, which makes it a supply-chain and build-toolchain decision for a product that ships
as an air-gapped image tarball. The policy language is a second authoring surface for operators, with
its own schema format that has to be kept in step with the action registry by hand — the drift
`core-designs.mdc` exists to prevent. And Cedar's entity model wants a shape we would have to define
anyway; the vocabulary we already have does not map to it for free.

## 5. Recommendation to put to the gate

Adopt **Option A** as the engine, and keep the **interface** Cedar-shaped — a single
`authorize(principal, action, resource, context) -> Decision` chokepoint with no policy structures
leaking past it — so Option B remains a swap rather than a rewrite if formal analysis becomes worth
its cost.

Two obligations if Option A is chosen, both aimed squarely at "the evaluator is ours to get right":

1. **The §2 decision table is the acceptance test**, extended with every ambiguity found during
   implementation. Not example-by-example tests written after the code.
2. **A completeness check in CI**: every one of the 89 actions is named by at least one statement in
   the shipped default policy set, or is explicitly listed as intentionally ungranted. An action no
   policy mentions is denied by default — correct, and invisible until an operator is blocked.

## 5a. Lessons from the prior implementation (reviewed, not assumed)

Option A is not hypothetical: the feature lead built this shape in
[aws-samples/s3-object-lambda-workshop](https://github.com/aws-samples/s3-object-lambda-workshop)
(`amplify/backend/function/`). Reviewing it confirmed the design and produced four transferable
lessons — three of them from behaviour reproduced by running the code, not from reading it.

What it already got right, and §3 above copies: `extra='forbid'` so an unknown key rejects the
document; PascalCase wire format via field aliases; `Action` validated against an allowlist constant
(`VALID_ACTION`) — which is precisely §3's registry check, generalized from one action to 89; and an
evaluator that opens at DENY and returns early on explicit deny.

| Lesson | What happened there | What Phase 3 must do |
|---|---|---|
| **Validate on read, not only on write** | The schema module is imported by the policy CRUD Lambda but *not* by the authorizer, whose Pipfile pins nothing. Policies are evaluated as raw dicts, so any write that bypasses the API is never schema-checked, and a malformed document raises `KeyError` instead of denying — closed by accident, not by design. | Validate at load *and* treat any validation failure as an explicit DENY (decision table rows 11–12). A schema enforced only on write is advisory. |
| **Never interpolate a policy string into a regex** | `match_resource` builds a pattern from the policy's resource with no `re.escape` and matches with `.match()`. Reproduced: a grant on `…/public` also matches `…/public-salaries.csv` (no end anchor); `…/data.csv` matches `…/dataXcsv`; `…/report(1).csv` matches `…/report1.csv` (both characters are permitted by the resource validator); and an interior `*` in `…/*/secret.csv` reads as "zero or more slashes", so it over-matches `…/secret.csv` *and* fails the `…/team/secret.csv` it was written for. | Match on **structure, not patterns**. `service[.subservice]:verb` is already segmented, so compare segments; expand `*` explicitly per segment. This is what makes the dotted vocabulary load-bearing rather than cosmetic. |
| **Decide the multi-match merge rule** | The loop assigns `effect` and `attrs` on every matching permit and only returns early on deny, so when several permits match, the `Condition` that drives the data transform comes from whichever matched **last** in DynamoDB scan order. The allow/deny decision stays safe; which transform applies does not. | Define precedence for two matching permits with different scopes or conditions, and make it a decision-table row rather than an emergent property of iteration order. See §6 Q5. |
| **A silent read cap becomes a silent denial** | `get_policies()` scans with a 1 MB response cap and no pagination, so past that size policies are simply absent. With default deny that is fail-closed, but it presents as a permission that stopped working for no reason. | Load the policy set completely or fail loudly. Never let "no policy found" and "policy not loaded" reach the evaluator as the same state. |

Note also that the prior implementation is pydantic **v1** (`validator`, `class Config`); this repo
has pydantic 2.13 in tree, and v1→v2 differs precisely on the constructs that code leans on most
(`field_validator`, `ConfigDict`).

## 5b. Revocation: grants may be stale, denials may not

**The problem is not solvable as stated, and pretending otherwise is the mistake.** Row 21 originally
asserted that a group removal "must take effect at the next decision". That is unachievable with any
cache, and the industry does not achieve it either. Kerberos carries group membership inside the
ticket's PAC, so in Active Directory a group change does not take effect until a new TGT is issued —
default ticket lifetime ten hours, renewable seven days. Microsoft's answer is not instant
invalidation; it is shorter lifetimes plus a way to force re-authentication. Stateless JWTs have the
identical property by construction: a signed token is valid until it expires, which is why revocation
lists and introspection endpoints exist at all.

**The design (feature-lead direction, 2026-08-20).** Split the decision into two tiers with different
freshness guarantees, and make the asymmetry the invariant:

| Tier | Contents | Cost | Cacheable? |
|---|---|---|---|
| Slow path — **grants** | policy documents, group membership, transitive resolution | expensive: several store reads plus hierarchy walk | yes, minutes |
| Fast path — **revocations** | one small map of `principal → revoked_at` timestamp | tiny: a dict lookup against a short-TTL shared record | refreshed in seconds, never minutes |

The evaluator consults the fast path **first** and denies before it does any grant work. So a grant
that was removed lingers for the cache TTL, which is acceptable and honest; a revocation lands within
the refresh interval, which is what an operator actually needs when something is going wrong on a rack.

**Why a timestamp rather than a session or token list.** The revocation record is
`principal → revoked_at`, and the check is `token.iat < revoked_at → DENY`. That needs no registry of
issued tokens, no session table, and no cooperation from the token itself beyond the standard `iat`
claim — which is what makes it work for stateless JWTs, the case Phase 2 would otherwise have no answer
for. It is the same mechanism as AWS IAM's "revoke active sessions", which attaches an inline policy
denying everything conditioned on `aws:TokenIssueTime` predating the revocation instant.

Row 24 matters as much as row 22: a revocation is a **cutoff, not a ban**. A principal who
re-authenticates after `revoked_at` gets a token with a later `iat` and resumes whatever they still
hold. That keeps revocation distinct from disabling an account, which is a separate and more permanent
act, and it makes revocation safe to use — an operator will not hesitate over a button that boots a
session but does not delete a person.

**Implementation constraints specific to this product, none of them optional:**

- **An in-process revocation map is safe today, and that is a precondition rather than a property.**
  The container runs `gunicorn --workers 1 --threads 24 --worker-class gthread`, deliberately: the
  Dockerfile records that multiple workers would each duplicate the SSH work and the in-process state.
  One process means one revocation map, so no sharing is needed. But the moment worker count rises
  above one, an in-process map denies on one worker and allows on another — a privileged action
  succeeding on retry with probability 1/N, with nothing in the logs looking wrong. Either keep
  `--workers 1` as a documented constraint of this design, or put the record somewhere shared before
  changing it. (An earlier draft of this section asserted the deployment already runs several workers.
  It does not.)
- **Revoking is itself a privileged, audited action.** It belongs in the vocabulary
  (`identity.user:revoke`), which means it is governed by policy like anything else, and it is one of
  the actions whose own authorization matters most — alongside `policy:attach` and
  `identity.group:add_member`.
- **The refusals it causes must be audited, and the Phase 1 flood guard is what makes that bearable.**
  A revoked client retrying in a loop produces one counted summary event rather than thousands
  (FR-10a), so the revocation and its effect are both legible in the trail without the effect
  evicting the cause.
- **A failed fast-path read must deny, not skip.** If the shared revocation record is unreachable, the
  evaluator has no basis for asserting the principal is *not* revoked. Failing open here would make
  the security feature into a bypass with a network partition as its trigger.

**Still open:** the refresh interval (a few seconds is the intent; the number is a security parameter
and belongs in the threat model, not a performance note), and where the record lives — a small file
with an mtime check is enough for a single-host deployment and is consistent with how this product
already stores durable state, whereas a multi-host deployment would need something shared.

## 6. Open questions for the gate

1. **Does a parent service grant imply its sub-services?** Row 5. `fabric:*` covering
   `fabric.nvswitch:reset_system` is convenient and is how most people read it; it also means adding
   a sub-service silently widens existing grants. The safer rule is that `fabric:*` covers only
   `fabric`'s own verbs and sub-services need naming — at the cost of surprising the author.
2. **Is `workflow:run` one capability or many?** Row 14. A workflow's `host:` steps execute arbitrary
   shell on the head node, so `workflow:run` is potentially the broadest grant in the product,
   regardless of how narrow the other 88 actions are. Options: treat any workflow containing a `host:`
   step as a distinct action; require the steps' own actions to be authorized; or accept
   `workflow:run` as an admin-only capability and say so.
3. **Where do policy documents live?** Catalog records (consistent with §2–3, editable in the UI,
   per-cluster) versus files on disk (reviewable in git, not editable by a compromised session). The
   audit consequence differs: a catalog-stored policy is itself a mutating action —
   `catalog.tool:create` is already row 15 — and NFR-5's reasoning says a security control an
   authenticated operator can edit at runtime is weaker than it looks.
4. **Are roles first-class, or just principal groups?** The original request said RBAC; the schema
   above accepts either, and this is the point where that has to be decided rather than deferred.
   Worth noting from §5a: in the prior implementation the role path resolved a principal to a *dict*
   and so never matched a role ARN, which failed closed and stayed invisible. Whatever roles are,
   they need a decision-table row that fails if they stop resolving.
5. **When two permits match, whose conditions apply?** Raised by §5a. Options: most-specific
   resource wins; intersection of all matching conditions (safest, hardest to explain); or reject the
   policy set at load as ambiguous. Only the last one is order-independent *and* obvious to the
   author.
6. **Does a user-level statement override a group-level one?** Rows 17–18. "Explicit deny wins" is a
   rule about *statements*, not about *principals*, so it does not answer this. Two coherent models:
   flatten — collect every statement from the user and all their groups into one set and apply
   deny-wins across it, so a group forbid cannot be escaped by a user permit; or tier — user-level
   statements shadow group-level ones, which is more intuitive to an administrator and is also how a
   privilege escalation gets written by accident. Flattening is the safer default and is what row 18
   assumes if nothing else is decided.
7. **Is group membership transitive, and how deep?** Row 19. Nested groups are convenient and are
   where cycle bugs and surprising grants live. Options: no nesting (simplest, and enough for a
   handful of operator teams); one level; or arbitrary depth with a cycle guard and a depth cap.
   Whatever is chosen, row 20 must hold — a cycle fails the policy set at load rather than at
   evaluation time.
8. **How is membership resolved at decision time?** Rows 21–24. **Resolved 2026-08-20 (feature lead):
   two tiers — see §5b.** Membership and grants may be cached; revocations may not. The remaining
   open parameters are the revocation refresh interval and where the revocation record lives, both
   in §5b.
