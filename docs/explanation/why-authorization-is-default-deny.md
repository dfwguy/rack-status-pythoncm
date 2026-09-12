---
diataxis: explanation
description: The five moving parts of Greenlight's authorization model, and why each behaves the way it does.
---

# Why authorization is default-deny

Greenlight decides every request with one question: *may this principal perform this action on
this resource?* This page is about the shape of that question and why it is shaped that way. It
contains no instructions — for those, see {doc}`../how-to/grant-a-team-access`.

## Five parts, kept separate on purpose

```{mermaid}
flowchart LR
    U["user:rkoike"] -->|belongs to| G["group:sre"]
    U -->|attachment| P["policy document"]
    G -->|attachment| P
    P --> S["statements"]
    S --> D{"allow / deny"}
```

**A principal** is the authenticated subject, spelled `user:<login>`. It comes from the verified
access token's `sub` claim — never from a header or a request field, because anything a caller
can set is not an identity.

**A group** is a flat set of users. A group contains users and never another group, so membership
is not transitive: there is no inheritance chain to reason about when you are trying to work out
why somebody has access.

**A policy document** is a named, reusable list of statements. The important part is what it does
*not* contain: **it never names who it applies to**. That is the attachment's job.

**An attachment** binds a subject — `user:x` or `group:y` — to a policy id.

**A statement** is one rule: these actions, on these resources, allowed or denied.

### Why policies do not name principals

This is the design decision everything else follows from. If a policy named its subjects, then
granting one more person access would mean editing a document that forty other people depend on,
and every grant would carry the risk of a typo in somebody else's access. Separating the two
means `read-only` is written once and attached forty times.

It also bounds the cost of a decision. Evaluation looks up the policies attached to *this*
principal and its groups — one lookup plus one per group — rather than scanning every policy in
the deployment looking for mentions. Cost grows with what the caller holds, not with the size of
the corpus.

## Why the default is refusal

An unmatched request is refused. No policy, no access.

The alternative — permit unless denied — requires enumerating everything that must not happen,
including the actions the product will add next release. That list is never finished, and every
new endpoint silently becomes public until somebody remembers to deny it. Default-deny fails the
other way: a missing grant is a 403 that an operator reports in minutes, which is a far better
failure than a permission nobody noticed they had.

## Why a deny can never be outvoted

Denies are evaluated first, in a separate pass over every attached document, and a match is
final. No number of allows at any specificity overrides one.

This is what makes the commonest policy shape trustworthy: a broad allow with a narrow deny
carved out of it. If specificity decided, an operator would have to reason about whether their
exception was *specific enough* to win against a grant somebody else wrote in a different
document — and they would get it wrong, because the answer would depend on files they have never
read.

Two passes rather than one also mean **file order cannot decide a security outcome**. A deny that
appears after an allow still wins. Ordering-dependent security is the kind of thing that works
until somebody sorts the file.

## Why a bare `*` is refused in an allow

The schema rejects `actions: ["*"]` in an allow statement. It grants every action the product
declares *and every action it will ever add*, which is a grant nobody can reason about because
half of it does not exist yet.

When a deployment genuinely wants that, there is already an honest way to say so: the
`AllowAllEngine`, which records `policy_engine: allow-all` on every audit event. An auditor
reading the trail sees the truth rather than a blanket grant buried three files deep.

A `*` in a **deny** needs no such guard. "Nobody does anything" is a legitimate and occasionally
necessary rule — see the break-glass example in {doc}`../how-to/grant-a-team-access`.

## Why failure means refusal, every time

Every way this machinery can break resolves to a denial:

- no engine registered — a deployment bug, logged at ERROR because nothing else would notice;
- the policy file will not parse — everything is refused, loudly;
- the evaluator raises — an evaluator that cannot decide has not permitted anything.

The reasoning is the same each time. A security control that fails open has not failed safely; it
has stopped being a control while continuing to look like one. A control that fails closed
produces an outage, and an outage gets fixed.

The one place this is softened is deliberate: an attachment naming a policy that no longer exists
is skipped with a warning rather than failing the request. The attachment is stale; the request is
not, and deleting a document should not take down every request that mentions it.

## Why a refusal does not say what it refused

A denial names the action but never the resource. A message like *"you may not read
`rack:p17-r01`"* confirms that `p17-r01` exists, which turns the authorization layer into an
existence oracle for anyone probing.

The audit trail records the full detail — principal, action, resource, engine, and the deciding
rule — because that is a channel only an administrator reads.

Related: `.cursor/rules/identity-auth-traps.mdc` collects the traps this area has actually
produced, including why a token can be refused nineteen different ways and which four happen by
accident.
