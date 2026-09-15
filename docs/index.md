---
description: Operator and administrator documentation for NVIS Greenlight.
---

# NVIS Greenlight

Observability, validation and automation for NVIDIA AI factory deployments — the tool operators
use to bring a cluster from racked-and-powered to passing acceptance.

This site is organised by [Diátaxis](https://diataxis.fr/): four kinds of page for four different
needs. Every page says which kind it is, under its title, because the kind changes how you should
read it.

::::{grid} 1 1 2 2
:gutter: 3

:::{grid-item-card} How-to guides
:link: how-to/grant-a-team-access
:link-type: doc

Steps to a goal, for someone already at work.
:::

:::{grid-item-card} Reference
:link: reference/identity/index
:link-type: doc

The machinery, described. Look things up here.
:::

:::{grid-item-card} Explanation
:link: explanation/why-authorization-is-default-deny
:link-type: doc

Background and reasoning. No instructions.
:::

::::

## Start here

If you are setting up who may do what, read
{doc}`explanation/why-authorization-is-default-deny` first — it is short, and the model makes
much more sense before you write a policy than after. Then
{doc}`how-to/grant-a-team-access`.

If a request is being refused, go straight to {doc}`how-to/diagnose-a-refused-request`.

## What is not here yet

This site is new. Most of the project's written material is still in hand-written files under
`docs/` in the repository — architecture notes, cluster runbooks, incident write-ups — and is
being migrated a page at a time rather than dumped in wholesale. `docs/README.md` describes the
boundary and how to move a page across.

The generated references are authoritative, and the first of them is now **in this site**:

- **every action** the product declares, with what each service governs and how a policy names
  one instance: [Action vocabulary](reference/identity/action-vocabulary.md). The same content
  lives at `docs/ACTION_VOCABULARY.md` for repository readers, and `GET
  /api/policies/vocabulary` serves it to a client.
- **every resource type**: `docs/RESOURCE_MODEL.md` — not yet migrated.
- **the policy schema**: `GET /api/policies/schema`
