---
description: Implementation effort is not a design input — choose for quality, simplicity, robustness, scalability and long-term maintainability, and never trade the right structure for a smaller diff
alwaysApply: true
---

# "That's a bigger change" is not an argument

**When you are choosing between two designs, the effort to write them is not one of the inputs.**
Drop it from the comparison entirely. An agent writes the larger change in the same sitting as the
smaller one, so the hours a human would have spent are not being saved by anyone — while the cost
of the wrong structure is paid on every future change, by a person, forever.

Decide on: **quality, simplicity, robustness, scalability, composability, long-term
maintainability.** When those are genuinely tied, prefer the smaller change. They are rarely tied.

## The sentences that mean you are doing it

Each of these is effort reasoning in disguise, and each one produced a real defect in this repo:

| What gets said | What it actually proposes |
|---|---|
| "To keep the change minimal…" | Ship the shape you already know is wrong |
| "For now, let's just…" | There is no later; `# TODO` is not a plan |
| "That would mean touching the engine" | Touch the engine — that is where the concern lives |
| "A quick dict/table/flag here is enough" | A parallel structure beside a first-class citizen |
| "Refactoring that is out of scope" | The scope is the correct change, not the visible one |

This is the mechanism behind almost every entry in `core-designs.mdc`. Nobody set out to build
beside the architecture; they each judged that extending it was *more work than this task warranted*.
That judgement produced **four in-memory job dicts** instead of one durable runner, **four bespoke
`.sqlite3` files** for a product with one storage dependency, and an `app.py` that reached **20,000
lines** holding 135 routes, at which point every feature collided with every other feature and a
whole feature ended up uncommitted on a customer deployment. Every one of those was cheap once and
expensive continuously.

## Cheap is not the same as simple

Simplicity is a property of the **result** — fewer moving parts, one place to change a thing, a
reader who can predict behaviour without running it. Effort is a property of the **act of writing**.
They are unrelated, and the larger change is frequently the simpler system: the layered script tree
is more code than an `if` on the cluster name, and it is the reason one server can serve five
customers without a conditional anywhere.

So do not defend a shortcut as "simpler". Ask which one a stranger can extend correctly in six
months.

## What still counts as a cost — and it is never build time

Effort is out. These are real and belong in the decision:

- **Runtime and operator cost.** A fan-out that exhausts the head's `sshd MaxSessions` silently
  loses nodes; a read path that writes defeats its own cache. Both are quality problems.
- **Blast radius.** On a live customer cluster, a change that can be rolled back beats an
  equivalent one that cannot. That is robustness, not caution about work.
- **Reviewer cost.** A large *incoherent* diff is worse than two coherent ones. The fix is to
  sequence the work, never to shrink it.
- **A new mechanism's ongoing cost.** A fifth paradigm is worse than extending the fourth — which
  is the same rule pointing the other way, and why `storage-engines.mdc` overrules a request for a
  SQL table when the data has no joins in it.

## When the right change genuinely cannot land in one step

Do the right change. If it truly cannot be one commit — it crosses a deploy boundary, it needs a
migration, it would leave `main` broken — then **say so, propose the sequence, and do step one
properly.** What is not acceptable is silently substituting the shortcut and reporting it as done.

**And when you do take the smaller path, name it in the commit message and in your summary**: which
citizen you did not extend, and what it will cost to unwind. `core-designs.mdc` already requires an
argument in the commit message for a new structure; this is the same obligation for a knowingly
incomplete one. An undeclared shortcut is discovered by a customer, which is exactly what all of
this exists to prevent.
