---
diataxis: reference
description: The decision procedure, its failure modes, and what a refusal tells the caller.
---

# How a decision is made

Implemented by `PolicyEngine.evaluate` in `backend/authz/policy.py`. Every request passes through
it once, in the request gate, before the handler runs.

## The procedure

1. **Collect the policies in scope.** The principal's own attachments, plus one lookup per group
   it belongs to. A policy attached both directly and through a group is evaluated once.
2. **Every `deny`, first.** A matching deny returns immediately.
3. **Then every `allow`.** The first match permits.
4. **Otherwise refuse.** Default deny.

Steps 2 and 3 are separate passes over all attached documents, not one pass. A deny appearing
*after* an allow — in the same document or a different one — still wins, so file order cannot
decide a security outcome.

A statement matches when **both** its action patterns and its resource patterns match, by glob.

## What the caller is told

| Outcome | Status | Body |
| --- | --- | --- |
| Allowed | the handler's own | the handler's own |
| Refused by policy | `403` | `{"ok": false, "error": "Access Denied. Contact administrator to request access to this resource", "reason": "policy"}` |
| Target not identifiable | `400` | `{"ok": false, "reason": "unresolvable_target"}` |

`403`, not `401`: the caller *is* authenticated and simply may not do this. Answering `401` would
tell a client to re-authenticate, which cannot help and produces a credential-prompt loop. See
`docs/API_RESPONSE_CODES.md`.

```{note}
A refusal names the action but **never the resource**. A message confirming that a resource exists
is an existence oracle for anyone probing. The full detail — principal, action, resource, engine
and deciding rule — goes to the audit trail, which only an administrator reads.
```

## Failure modes

Every way this machinery can break resolves to a denial.

| Situation | Result |
| --- | --- |
| No policy matches | **deny** — `no policy grants <action>` |
| No engine registered | **deny**, logged at ERROR. A deployment bug; nothing else would notice |
| The policy file will not parse | **deny everything**, logged at ERROR |
| The evaluator raises | **deny** — an evaluator that cannot decide has not permitted anything |
| An attachment names a policy that no longer exists | skipped with a warning; the request proceeds on the rest |
| A group lookup fails | treated as no groups; direct attachments still apply |

The last two are softened deliberately: deleting a document should not take down every request
that mentions it, and a failing group lookup should not revoke a grant held directly.

## Which engine is running

`authz.install_default()` chooses at startup, and the audit trail records the answer on every
event as `policy_engine`:

`rbac-v1`
: A policy file exists. Default-deny, as described above.

`allow-all`
: No policy file. Every authenticated request is permitted — and every audit event says so, which
  is the point. This is the honest way to run permissively.

```{warning}
Since the startup gate landed, a brand-new deployment creates an `administrators` policy during
first boot, so `allow-all` is no longer what a fresh install gets. A deployment reporting
`allow-all` has had its policy file removed.
```

## Cost

Bounded by what the *caller* holds, not by the size of the policy corpus: one attachment lookup
for the principal plus one per group, then a walk of the matched documents' statements. Adding
policies for other teams does not slow anybody else's requests. Measured figures are in
`docs/PERFORMANCE_BASELINE.md`.
