---
diataxis: reference
description: The policy document shape, field by field, with the constraints the schema enforces.
---

# Policy schema

Authoritative definition: `backend/authz/schema.py`. The running product serves it at
`GET /api/policies/schema`, which is what the in-app policy editor validates against as you type.

## Shape

```json
{
  "version": 1,
  "policies": [
    {
      "id": "rack-operator",
      "description": "Power and drain on the bring-up racks; read everything else.",
      "statements": [
        {
          "sid": "reads",
          "effect": "allow",
          "actions": ["*:read", "*:list"]
        },
        {
          "sid": "power-bringup-racks",
          "effect": "allow",
          "actions": ["rack.power:*", "slurm:drain", "slurm:undrain"],
          "resources": ["rack.power:p17-*", "slurm:p17-*"]
        }
      ]
    }
  ]
}
```

## Fields

`version`
: Optional, default `1`. File format version.

`policies[].id`
: **Required.** Pattern `^[a-z0-9][a-z0-9._-]{0,63}$`. Referenced by attachments and echoed in
  audit reasons, so it is constrained to something safe to log and to put in a URL path.

`policies[].description`
: Optional, up to 512 characters. Write one — it is what the next operator reads.

`policies[].statements`
: **Required.** Between 1 and 64 statements.

`statements[].sid`
: Optional statement id, unique within the document. Echoed in the audit reason so a verdict
  names the rule that decided it: `denied by rack-operator/power-bringup-racks`. Without a sid,
  a verdict can only name the document — which is why you want one on every statement.

`statements[].effect`
: **Required.** `allow` or `deny`.

`statements[].actions`
: **Required.** 1–64 action patterns. See {doc}`actions-and-resources`.

`statements[].resources`
: Optional, default `["*"]`. 1–64 resource patterns.

## Constraints worth knowing

**`resources` defaults rather than being required.** Most rules are type-wide, and forcing
`["*"]` everywhere would train operators to type it without thinking — which is exactly the
habit that makes an accidental global grant likely.

**Unknown fields are rejected, not ignored.** The models are `extra="forbid"`. A misspelled key
is a typo you want to hear about, not a rule that silently does nothing.

**A bare `*` in an allow is refused.** See {doc}`actions-and-resources` and
{doc}`../../explanation/why-authorization-is-default-deny`.

**Duplicate statement ids are refused.** Two statements sharing a sid would make an audit reason
ambiguous about which rule fired.

**Duplicate policy ids are refused.** Attachments address a policy by id; two documents with one
id would make an attachment ambiguous.

## Limits

| Limit | Value |
| --- | --- |
| Statements per policy | 64 |
| Patterns per `actions` or `resources` field | 64 |
| Description length | 512 characters |
