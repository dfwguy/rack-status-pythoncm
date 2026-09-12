---
diataxis: how-to
description: Write a policy, check what it grants, and attach it to a group.
---

# How to grant a team access

You have a team that needs to do something in Greenlight, and right now they cannot. This gets
them there.

You need an account that may write policies (`policy:create`, `policy:attach`) — an
administrator, normally.

## 1. Write the policy

**Identity → Policies → New policy.** The editor validates against the server's own schema as you
type, and completes over the real action vocabulary, so an action name cannot be mistyped
silently.

Start from what the team must do, not from what looks safe. A read-only role is one clause:

```json
{
  "id": "read-only",
  "description": "See everything, change nothing.",
  "statements": [
    { "sid": "reads", "effect": "allow", "actions": ["*:read", "*:list"] }
  ]
}
```

A team that operates particular racks wants a broad allow scoped by resource:

```json
{
  "id": "bringup-operator",
  "description": "Power and drain on the P17 bring-up racks; read everything.",
  "statements": [
    { "sid": "reads", "effect": "allow", "actions": ["*:read", "*:list"] },
    {
      "sid": "operate-p17",
      "effect": "allow",
      "actions": ["rack.power:*", "slurm:drain", "slurm:undrain"],
      "resources": ["rack.power:p17-*", "slurm:p17-*"]
    }
  ]
}
```

Give every statement a `sid`. It is what an audit reason names when a rule fires, and without it
a verdict can only name the document.

Write rack ids in **lower case** — see {doc}`../reference/identity/actions-and-resources`.

## 2. Carve out exceptions with a deny

To protect something from a grant the team otherwise has:

```json
{
  "id": "hands-off-acceptance",
  "description": "Nobody power-cycles the racks under customer acceptance.",
  "statements": [
    {
      "sid": "acceptance-racks",
      "effect": "deny",
      "actions": ["rack.power:*"],
      "resources": ["rack.power:p17-r01", "rack.power:p17-r02"]
    }
  ]
}
```

A deny beats every allow, from any document, regardless of order — so this can live in its own
policy and be attached alongside, which is usually easier to review than editing the grant.

## 3. Check what it actually grants

Before you attach it, and before you believe it:

```bash
curl -sk "https://localhost:5056/api/policies/preview?format=table"
```

This walks the entire action vocabulary, marks everything denied, then applies the policies
attached to that principal exactly as the engine would. Each allowed entry names the resource
patterns that qualify it and the policy and statement that decided — so the answer is "yes, for
these racks" rather than a bare `true`.

Add `?principal=user:someone` to inspect somebody else (authorized separately).

## 4. Attach it

A policy grants nothing until it is attached.

**Identity → Policies → Attach**, then pick a **group**. Over the API:

```bash
curl -sk -X POST "https://localhost:5056/api/policies/bringup-operator/attachments" \
  -H 'Content-Type: application/json' \
  -d '{"subject": "group:sre"}'
```

Attach to a group rather than to each person. It is one write per role, and adding somebody to
the team then grants them the role by membership — which is also the only record that stays
correct when people move teams.

To check who holds a policy, or what a subject holds:

```bash
curl -sk "https://localhost:5056/api/policies/bringup-operator/attachments"
curl -sk "https://localhost:5056/api/policies/subjects/group:sre"
```

## Sharing boards without letting peers overwrite your tools

A specific case worth knowing, because it uses the two-layer permission that board import has.
Importing a shared board is gated by `catalog.board:import`, and each item in it additionally
requires the action its own write would need. So this lets a team adopt boards that **add**
actions while refusing any that **replace** the site's own tuned definitions:

```json
{
  "id": "board-adopter",
  "description": "May import shared boards, but never overwrite our own tool definitions.",
  "statements": [
    {
      "sid": "import-and-create",
      "effect": "allow",
      "actions": ["catalog.board:import", "catalog.tool:create", "workflow:save"]
    },
    { "sid": "never-replace-our-tools", "effect": "deny", "actions": ["catalog.tool:update"] }
  ]
}
```

A refused item comes back as `skipped` with the reason and the rest of the import still applies.

## Suspending someone without deleting them

```json
{
  "id": "suspended",
  "statements": [{ "sid": "all-stop", "effect": "deny", "actions": ["*"] }]
}
```

Attach it to the user. It beats every allow they hold from any other document. A `*` is permitted
in a deny for exactly this.

## Next

- Something still refused? {doc}`diagnose-a-refused-request`
- Why the model behaves this way: {doc}`../explanation/why-authorization-is-default-deny`
