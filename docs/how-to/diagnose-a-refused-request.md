---
diataxis: how-to
description: Work out why Greenlight answered 403, in the order the causes actually occur.
---

# How to diagnose a refused request

Something returned `403`, or a page will not load its data. This narrows it down.

## First: is it authorization at all?

Check the status code.

`403`
: Authorization. You are who you say you are and may not do this. Continue below.

`401`
: Authentication — the credential itself was refused, which is a different problem. There are
  nineteen distinct causes catalogued in `backend/tests/test_identity_token_invalidation.py`
  (ids `INV-01`..`INV-19`); the four that happen by accident are called out there. The commonest
  is that `RACK_WEB_SECRET_KEY` changed since the token was minted.

`400` with `reason: unresolvable_target`
: Not a permission problem. The request did not name a target the gate could identify, so it was
  refused before authorization ran. Supply the rack, node or id the endpoint expects.

## 1. Read the audit event

It is the first place to look, not the last. Every decision records the principal, action,
resource, engine and the deciding rule:

```bash
curl -sk "https://localhost:5056/api/audit/trail?decision=deny&limit=20"
```

A reason of `denied by <policy>/<sid>` means a **deny statement** matched — you are looking for
an exception somebody wrote, possibly in a policy you have not read. A reason of
`no policy grants <action>` means nothing allowed it.

If the reason names no statement id, the policy's author omitted `sid`. Add them.

## 2. See the whole picture for that principal

```bash
curl -sk "https://localhost:5056/api/policies/preview?format=table&principal=user:someone"
```

This is the fastest way to answer "what can this person actually do". It applies the attached
policies exactly as the engine does and reports, per action, whether it is allowed and which
resource patterns qualify it.

## 3. Work through the likely causes

In rough order of how often they occur:

**Nothing grants it.** The policy set does not cover the action. Add it —
{doc}`grant-a-team-access`.

**The policy is not attached.** A policy grants nothing until a subject holds it:

```bash
curl -sk "https://localhost:5056/api/policies/subjects/user:someone"
```

**The action is newer than the policy.** After an upgrade that adds actions, the
`administrators` policy can be narrower than the product — which is why a brand-new feature can
return `403` for an administrator who should obviously be allowed:

```bash
python backend/cli.py reconcile-admin-policy
```

**The resource does not match.** A grant on `catalog.tool:nvloom` does not cover
`catalog.tool:_all`, which is what a `:list` resolves to — so a table will not load. Grant
`catalog.tool:*`.

**The rack id case is wrong.** Rack resources are folded to lower case. A deny written as
`rack.power:P17-R01` will not match.

**The policy set will not parse**, so *everything* is refused. Check the logs at startup and on
evaluation for `policy set unusable`.

## 4. If every request is refused, including reads

Two deployment-level causes, both logged at ERROR:

- **No authorization engine registered.** `app.py` did not call `authz.set_engine()`. A
  deployment bug.
- **An unparseable policy file.** Deliberate: an unusable policy set is not a reason to fall back
  to permitting.

Confirm what the deployment thinks it is doing:

```bash
python backend/cli.py check
```

This reports what a `serve` would create and exits `78` on a fault, without creating an
administrator, credential or policy.

## Reference

- {doc}`../reference/identity/evaluation` — the decision procedure and every failure mode
- {doc}`../reference/identity/actions-and-resources` — why `_all` and case-folding matter
