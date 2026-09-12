---
diataxis: reference
description: Where the authorization machinery is described — actions, resources, the policy schema, and the decision procedure.
---

# Identity and authorization

Who a caller is, what they may do, and how that is written down.

```{note}
Two of the references here are **generated from the code** and are authoritative:
`docs/ACTION_VOCABULARY.md` lists every action the product declares, and `docs/RESOURCE_MODEL.md`
every resource type. The running product serves live copies at `GET /api/policies/vocabulary` and
`GET /api/policies/schema`, so an operator writing a policy does not need the repository.

These pages describe the *grammar and semantics* around those lists rather than restating them —
a hand-copied list of 177 action names would be wrong within a week.
```

## The pages

{doc}`actions-and-resources`
: What an action name means, the verb vocabulary, `kind` and `scope`, wildcards, and how a
  resource identifier is derived from a request.

{doc}`policy-schema`
: The policy document shape, field by field, with the constraints the schema enforces and why.

{doc}`evaluation`
: The decision procedure, its failure modes, and what a refusal does and does not tell the
  caller.

## Related

- {doc}`../../how-to/grant-a-team-access` — writing and attaching a policy
- {doc}`../../how-to/diagnose-a-refused-request` — when something returns 403
- {doc}`../../explanation/why-authorization-is-default-deny` — why the model is shaped this way
