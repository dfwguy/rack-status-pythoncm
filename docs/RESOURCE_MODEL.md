# Resource model — what the product's authorization decisions will be about

The vocabulary a policy engine needs: every resource type Rack Status governs, how each one is
identified, and where its identifier comes from. Written for **Phase 3** of the auth programme
(`AUTH_PROGRAMME_SCOPE.md`), because a policy that says *who may do what to which thing* cannot be
written until the third of those is named.

**This is a survey, not an invention.** The 35 types outside the Identity section already exist in
the shipped product and are already enumerated: `audit/actions.py` declares a service per resource
type, `backend/tests/action_snapshot.json` pins all 106 endpoint/method pairs, and a startup gate
fails the build if a mutating route has no declaration. The taxonomy was built in Phase 1 for the
audit trail; Phase 3 binds to the same names rather than defining a second set — which is the whole
reason the vocabulary was made a declared thing rather than derived from endpoint names.

Verified mechanically: all 35 snapshot services appear below, and the counts here were measured
against the live URL map rather than estimated. `identity.user`, `identity.group` and
`identity.membership` are what Phase 2 adds.

---

## The gap this document exists to close

The Phase 1 audit event has a `resource` field, and today it holds **the URL path**:

```python
def _resource(path: str) -> str:
    """The path, without its query string (NFR-11)."""
    return path
```

That is right for an audit trail — a human reading it wants to know what was called. It is not enough
for authorization. `/api/rack-power` does not say *which rack*, so a policy cannot express "may power
on racks in row A but not row B", which is the first thing anyone will ask for. A typed resource is
`(type, id)` — `rack.power` + `p5-r01` — and the id is the part the product currently discards at the
audit boundary.

So Phase 3 needs a resource *resolver* alongside the existing action resolver: something that, given
a request, returns the typed resource it acts on. This document is the specification of what that
resolver must be able to produce.

---

## The addressing grammar

Principals and resources share one form, because a policy language with two addressing models is a
policy language nobody writes correctly:

```
principal ::= "user:" <uuid4> | "group:" <uuid4> | "service:" <name> | "anonymous"
resource  ::= <type> ":" <id> [ ":" <id> … ]     # type from the table below
```

So `user:8f14e45f-…`, `rack.power:p5-r01`, `validation.activity:<uuid>`, `slurm.job:mayo:127391`.

**Deliberately not `urn:nvis:…`.** Measured, not preferred: a full URN principal is 59 bytes against
`anonymous`'s 9, which is **+50 bytes on a 397-byte audit event — 12.6%** — and `principal` appears in
every event, so it cuts FR-7b's retention from ~41.8 days to ~37 at the same byte ceiling. The
`urn:nvis:identity:` prefix is identical for every principal in the system, so it is the most-repeated
and least-informative bytes in the file. The colon form keeps the type discriminator, the single parse
point and the extensibility without paying for a constant.

**The pair is the storage form; the string is a rendering.** `(type, id)` in memory and on disk, with
one module owning format and parse. Nothing else splits on `":"` — the same rule as the action
vocabulary, for the same reason: structure encoded in a string acquires a regex at the third call site
and drifts from the second.

**Principals carry no scope; resources sometimes must.** A user means the same thing whichever cluster
is active, because the identity store is instance-wide (it cannot live in the catalog — see the Phase 2
brief, Decision 1). A Slurm job id, by contrast, is not unique across clusters, so a resource
identifier may need a cluster segment. That asymmetry is a consequence of where identity lives, not a
separate design choice.

## Identifier shapes in use

Six shapes cover everything. Worth naming because the differences are what make a policy language
awkward if ignored.

| Shape | Example | Stability | Notes |
|---|---|---|---|
| **Site-defined slug** | `p5-r01`, `ink50-dh01-h15` | stable, cluster-scoped | rack ids come from the cluster manager; the pattern differs per site (`cluster-naming-conventions.mdc`) |
| **Real hostname** | `bos01-a05-11-s1-dgx-01-c01` | stable | **not derivable from a rack id** — the naming rule exists because deriving it produced targets that do not resolve |
| **uuid4** | `4f9c…` | stable, opaque | activities, workflow runs, audit events |
| **Catalog slug** | `gb200-nvl72`, `sysinfo`, `default` | stable, operator-editable | human-chosen, unique within its table |
| **Scheduler id** | `127391` | stable per cluster, reused across clusters | Slurm's own numbering; only meaningful with a cluster qualifier |
| **Singleton** | — | n/a | one instance per deployment (settings, the token vault) |

Two consequences a policy language must handle: a Slurm job id is **not globally unique**, so any
rule about jobs needs the cluster in scope; and catalog slugs are operator-editable, so a policy
naming one can be orphaned by a rename — which is exactly the argument for FR-2's stable id on users.

---

## Resource types

Grouped by root, as the action vocabulary groups them. **Verbs** are the actions declared today;
`read` is shown where a policy will need it even though Phase 1 only declared mutating actions.

### Identity — new in Phase 2

| Type | Identifier | Shape | Verbs | Notes |
|---|---|---|---|---|
| `identity.user` | stable opaque id, **not** the login name | uuid4 | create, read, update, disable, delete, revoke | FR-2. The login name is a mutable *attribute*; the id is what audit events and policy attachments reference, so a rename orphans nothing. Note the two renderings: **as a resource** it is `identity.user:<uuid>` (the thing being administered); **as a principal** it is `user:<uuid>` (the thing acting). Same record, two roles — and a policy about who may edit users needs the first while the actor is the second |
| `identity.group` | group id | uuid4 | create, read, update, delete | FR-8: flat, cannot contain a group |
| `identity.membership` | (group id, user id) pair | composite | add_member, remove_member | Not independently identified — it is an edge, and modelling it as an entity would invite a policy that grants membership editing without naming the group |

**Answering the direct question — why a uuid4 and not the login name.** The login name is what an
operator types and what an audit event should *display*, and it is also renameable (FR-2a permits
rename, refusing only collisions). If policy attached to the login name, renaming `jdoe` to
`j.doe` would silently detach every policy and orphan every prior audit event. So the record carries
both: an immutable id for reference, a unique case-insensitive login name for humans. This is the same
split the product already makes between a **rack id** and a **node hostname** — one for grouping and
reference, one for reaching the thing.

A federated subject resolves to the same shape (FR-7a), so `identity.user` has one identifier
regardless of how the principal authenticated.

### Cluster and hardware

| Type | Identifier | Shape | Verbs |
|---|---|---|---|
| `rack` | rack id | site slug | read, uptime |
| `rack.power` | rack id (+ optional node list) | site slug | on |
| `rack.gpu` | rack id + node list | site slug + hostnames | reset |
| `rack.bmc` | node list | hostnames | recover, set_mac |
| `rack.services` | rack id + node list | site slug + hostnames | restart |
| `rack.lock` | rack id | site slug | claim, release |
| `node` | hostname | hostname | read |
| `node.issues` | hostname | hostname | sync |
| `cluster` | active cluster id | catalog slug | read, reconnect |

**The rack/node distinction is load-bearing here, not cosmetic.** A policy scoped to a rack must
expand to nodes through the naming citizen, never by string arithmetic on the rack id — the rule that
exists because a naive `<rack>-[01-NN]` produces hostnames that do not resolve. So a rule like *may
reset GPUs in `p5-r01`* is evaluated against the resolved node set, and the resolution is part of the
decision rather than a detail after it.

### Fabric

| Type | Identifier | Shape | Verbs |
|---|---|---|---|
| `fabric` | node list or rack id | hostnames / site slug | map, preflight |
| `fabric.imex` | node list | hostnames | check, reset |
| `fabric.nvswitch` | node list | hostnames | audit, check, fix, reset_sdn, reset_system |

### Validation

| Type | Identifier | Shape | Verbs |
|---|---|---|---|
| `validation` | the launch target (rack id, rack set, or node set) + preset id | site slug(s) / hostnames + catalog slug | prepare_launch, prepare_preset, run, run_* (11 tool-specific verbs) |
| `validation.activity` | `activity_id` | uuid4 | read, adopt, annotate, delete |

`activity_id` **is** `job_id` — the same value under two names, kept for backward compatibility
(SRD REQ-41). A policy should reference one; the other is an alias.

Note the shape of the `run_*` verbs: eleven of them, one per tool. That is a Phase 3 decision waiting
to happen — whether policy is written against eleven verbs or against one `run` verb with the tool as
a resource attribute. The second is more expressive and needs the resolver to surface the preset id.

### Scheduler

| Type | Identifier | Shape | Verbs |
|---|---|---|---|
| `slurm.job` | job id | scheduler id, **cluster-scoped** | read, cancel |
| `slurm.node` | hostname | hostname | read, drain, undrain |

### Results, reports and telemetry

| Type | Identifier | Shape | Verbs |
|---|---|---|---|
| `results` | log path or activity id | path / uuid4 | capture, scan, read |
| `report` | report id | uuid4 | read, delete |
| `report.node` | hostname (+ run context) | hostname | generate, mark |
| `analytics` | rack set or cluster | site slugs | aggregate |
| `logs` | log path (+ job id) | path | tail, tail_job |
| `comments` | rack id, or `mr:<log basename>` | site slug / prefixed basename | read, update, delete |

`comments` is the one type with a **two-form key**: a rack comment is stored under the rack id, a
multirack-run comment under `mr:<basename>`. A policy language either treats these as two types or
carries the discriminator; pretending they are one thing is how a rule intended for racks starts
matching run notes.

### Catalog and configuration

| Type | Identifier | Shape | Verbs |
|---|---|---|---|
| `catalog` | singleton (the active selection) | — | read, set_active, set_topology |
| `catalog.platform` | platform id | catalog slug (`gb200-nvl72`) | create, read, update, delete |
| `catalog.tool` | tool id | catalog slug (`sysinfo`) | create, read, update, delete, import |
| `catalog.project` | project id | catalog slug (`default`) | create, read, update, delete |
| `settings` | singleton, two scopes | — | read, update_dashboard, update_job_env |

`catalog:set_active` deserves attention when Phase 3 is designed: it changes which cluster every
subsequent request acts on, so it is closer to a privilege-escalation surface than to a preference.

### Operations and tooling

| Type | Identifier | Shape | Verbs |
|---|---|---|---|
| `workflow` | workflow id (`uuid4[:8]`) and run id (uuid4) | uuid4 | read, save, delete, run, advance, cancel, generate |
| `tools` | tool/workspace name | fixed set | download_containers, install_* (5), launch_grafana |
| `diag` | node list or rack id | hostnames / site slug | hw_check, precheck, sysinfo |
| `bringup` | node set | hostnames | run |
| `support` | rack id + node list | site slug + hostnames | bug_report |
| `nvftp` | singleton | — | sync, test |
| `vault` | token name (one today: `nvftp-token`) | fixed set | set_token, delete_token |
| `ai` | singleton (per-session chat) | — | chat, chat_resume, configure, test |
| `auth` | the acting principal | — | login, logout |
| `logs`/`terminal` | node or head | hostname / role | connect |

`workflow` has **two** identifiers — a definition id and a run id — and they are not
interchangeable. A policy permitting `workflow:run` is about the definition; one permitting
`workflow:cancel` is about a run.

---

## What Phase 3 has to add

Recorded here so it is scoped rather than discovered:

1. **A resource resolver**, mirroring the action resolver. Given a request, return `(type, id…)`. Most
   ids are already in the request body (`rack`, `nodes`, `job_id`, `activity_id`), so this is mostly
   extraction — but it must be declared per route like `@action` is, or it decays the same way a
   per-route audit convention would have.
2. **A decision on rack→node expansion.** Whether a rack-scoped grant covers the nodes in that rack
   is a policy semantics question, and the expansion must go through the naming citizen.
3. **A decision on the 11 `validation:run_*` verbs** — keep them, or collapse to `run` with the tool
   as a resource attribute.
4. **A qualifier for cluster-scoped ids.** Slurm job ids are not unique across clusters, and one
   instance serves several clusters, so a rule about a job needs the cluster in scope.
5. **`read` verbs.** Phase 1 declared only mutating actions, because only those needed auditing.
   Authorization needs read verbs too, and that is the largest single piece of Phase 3's surface:
   **109 GET-only `/api` routes have no declared action** (counted against the live URL map, not
   estimated). Reading is where most of the sensitive disclosure lives — validation results, node
   reports, the audit trail itself — so this is not a tidy-up after the mutating rules work.

## Related

- `AUTH_PROGRAMME_SCOPE.md` — phases, and why groups are Phase 2
- `.plc/briefs/auth-identity-brief.md` — Phase 2 requirements, including the identity identifiers
- `PHASE3_POLICY_ENGINE_SPIKE.md` — engine comparison and the revocation-freshness design
- `.cursor/rules/cluster-naming-conventions.mdc` — why rack id and hostname are different namespaces
- `backend/audit/actions.py`, `backend/tests/action_snapshot.json` — the committed action vocabulary
