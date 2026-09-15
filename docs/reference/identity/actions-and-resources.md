---
diataxis: reference
description: The action grammar, the verb vocabulary, kind and scope, wildcards, and how a resource identifier is derived.
---

# Actions and resources

A policy statement names **actions** and **resources**. This page describes both vocabularies.

The full lists live elsewhere and are generated from the code: `docs/ACTION_VOCABULARY.md` for
actions, `docs/RESOURCE_MODEL.md` for resource types. Live copies:
`GET /api/policies/vocabulary`.

## Where the complete list is

This page is the **grammar** — what a verb means, how scope works, when a wildcard applies. It
deliberately does not enumerate the services, because a hand-written list of 53 of them drifts
from the code on the first rename.

[Action vocabulary](action-vocabulary.md) is the enumeration, generated from the live
registry on every build: every service with what it governs, its actions, and the request keys a
policy names one instance by. A route that declares no action cannot appear in it, and
`backend/tests/test_action_coverage.py` fails the build if one exists.

## Actions

An action names *what is being done to what kind of thing*:

```text
service[.resource]:verb       rack.power:off      catalog.tool:update      policy:attach
```

The grammar is pinned by `ACTION_PATTERN` in `backend/authz/schema.py`. A policy may only name
actions that exist, and the vocabulary is generated from the live route registry — so a route
without a declaration cannot appear, and a typo cannot silently match nothing.

**177 actions across 52 services: 61 reads and 116 mutations.**

### The verbs

88 distinct verbs exist, but five account for 84 of the 177:

| Verb | Count | Kind | Means |
| --- | --- | --- | --- |
| `read` | 34 | read | fetch one thing |
| `list` | 27 | read | fetch a collection |
| `delete` | 10 | mutate | remove one |
| `update` | 7 | mutate | change an existing one |
| `create` | 6 | mutate | make a new one |

The rest are domain verbs that say exactly what they do — `rack.power:on`, `slurm:drain`,
`fabric.imex:reset`, `validation:run_nccl`, `catalog.board:import`.

They are spelled out individually rather than folded into `update` on purpose: "may drain a node"
and "may rename a comment" are both mutations, and a vocabulary that could not tell them apart
would force every policy to choose between too much and nothing.

### `kind` and `scope`

Each action carries two attributes, both visible in the generated vocabulary.

`kind`
: `read` or `mutate`. This is what makes a read-only role expressible in one clause —
  `["*:read", "*:list"]` covers all 61 reads. Enumerating them would be unmaintainable.

`scope`
: `instance` (128 actions) or `singleton` (49). An instance-scoped action acts on a thing with an
  id, so a grant can be narrowed to particular racks, tools or users. A singleton is one per
  deployment, so there is no id to narrow to and its resource resolves to `<type>:default`.

### Wildcards

`*` is allowed in the verb position (`rack.power:*`), in the service position (`*:read`), and
alone (`*`) — with one refusal:

```{warning}
A bare `*` in an **allow** statement is rejected by the schema. It grants every action the
product will ever add, including ones that do not exist yet.

In a **deny** it is permitted and useful. See
{doc}`../../explanation/why-authorization-is-default-deny` for the reasoning.
```

## Resources

A resource is `type:id`:

```text
rack.power:p17-r01        catalog.tool:nvloom        policy:read-only        settings:default
```

The identifier is derived from the request by `backend/authz/resources.py` — from a path
parameter, a query parameter or a body field depending on the type. `docs/RESOURCE_MODEL.md`
surveys every type and says where each id comes from.

Three things a policy author needs to know.

### A collection is not an instance

A `:list` verb resolves to the sentinel `<type>:_all`. A grant on a single instance does **not**
cover it:

```text
allow catalog.tool:read  on catalog.tool:nvloom   →  may read that tool
                                                  →  may NOT list tools
```

That is deliberate: listing reveals what exists. A policy pattern of `catalog.tool:*` *does*
match `_all`, which is also correct — "may read all tools" should include enumerating them. So
grant the wildcard, or name `_all` explicitly, when a table needs to load.

### Some identifiers are case-folded, and it matters for `deny`

Handlers that lower-case an identifier — the rack family — have their resources folded to match,
because the gate must judge the string the handler acts on.

```{warning}
Write rack ids in lower case. Before this was fixed, `deny rack.power:p17-r01` did **not** match a
request for `P17-R01`, so a broad allow plus a narrow deny — the commonest policy shape there is —
could be evaded by holding shift.
```

Folding is per-type, not global: `help` content ids are camelCase (`singleRack`, `rackTable`) and
their handler does not fold, so lower-casing them would refuse every help read. The folded set is
`_FOLDED_TYPES` in `backend/authz/resources.py`.

### An unnamed target may fall back to the collection

Many read routes take their target from a query parameter, and some have no target at all —
a deployment-level health probe, for instance. Refusing those would mean a 403 on a health check,
so an unnamed read is authorized against the collection instead. Only a fixed set of verbs may
fall back this way (`COLLECTION_FALLBACK_VERBS`); a mutation may not.

A request whose target cannot be identified at all is refused with `400` and
`reason: unresolvable_target`, not `403` — the caller omitted something, and answering "not
authorized" would send them looking for a permission problem they do not have.
