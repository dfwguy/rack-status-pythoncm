---
description: The Diataxis compass as a per-page authoring test for docs/
globs: docs/**/*.md
alwaysApply: false
---

# Writing docs: apply the compass, not the folder names

Docs follow [Diátaxis](https://diataxis.fr/). Four kinds, four different needs, four different
ways of writing. Putting a page in the right directory is not the same as writing it in the right
mode, and the second one is what readers feel.

## The compass — run this test on every page

Two questions:

1. Does the content inform **action** (doing) or **cognition** (thinking)?
2. Does it serve the reader's **acquisition** of skill (study) or **application** of skill (work)?

- action + acquisition → **tutorial**
- action + application → **how-to guide**
- cognition + application → **reference**
- cognition + acquisition → **explanation**

Every page declares its answer in MyST frontmatter:

```yaml
---
diataxis: how-to
---
```

That declaration is load-bearing in two places. `conf.py` renders it as the banner under the page
title, so a reader arriving from a search engine — who never saw the sidebar — can tell what kind
of page they landed on. And `bin/check-docs-diataxis.py`, which CI runs, fails the build if a page
declares nothing, declares an unknown mode, or declares a mode that disagrees with the directory it
is filed under.

The checker deliberately does not try to judge whether a page is *really* the mode it claims. That
stays a review question. What it catches is the mechanical drift that dissolves a Diátaxis
structure over a year: an unclassified page, a how-to whose title stopped naming a task, a
numbered procedure that grew inside an explanation.

If a page needs two of these, it is two pages. Blurring the boundaries is the single largest source
of documentation problems, and the compass exists precisely for the moments when intuition says one
thing and the writing is doing another.

## Do not create empty categories

From the Diátaxis work guide, verbatim: *"It certainly does not mean that you should create empty
structures for tutorials/howto guides/reference/explanation with nothing in them. Don't do that.
It's horrible."*

Structure emerges from content. Add a page because a reader needs it, and let the category fill up.
Never add a stub so a directory looks complete.

## Per-category constraints

**Tutorials** (`docs/tutorials/`) — a lesson. The reader learns by doing, and the instructor is
absent, so you must anticipate where they get stuck. Must work end-to-end: a tutorial that fails
halfway has failed completely. Minimal explanation inline; link out for the why.

**How-to guides** (`docs/how-to/`) — a recipe for an already-competent reader at work.

- Title begins with "How to " and names the goal. CI enforces this.
  Good: "How to map your export to the NVIS Resource Model".
  Bad: "Mapping exports" — that might be about *whether* to, not *how* to.
- Addressed to a **user goal**, not a tool operation. "How to configure the deploy options and
  press Deploy" is not a how-to guide; it is the machine's motions written down.
- Action and only action. No digression, no teaching, no completeness-for-its-own-sake. If
  background matters, link to the explanation page.
- Does not need to be complete; it needs to be usable. Start and end somewhere meaningful and let
  the reader join it to their own work.

**Reference** (`docs/reference/`) — austere, neutral, factual. Describe and only describe.

- Structure mirrors the structure of the machinery, so the docs and the product can be navigated
  together.
- **Most reference here is generated** by `bin/gen-action-vocabulary.py` — from `contracts/` schemas,
  the live OpenAPI document, `standards/`, and `config_registry.py`. Generated pages carry
  `nvis_generated: <source>` in their frontmatter and a banner naming the source file.
- Generated pages are **committed**, and CI runs `bin/gen-action-vocabulary.py --check`. Do not
  hand-edit them: the edit is lost on the next run and the check fails first. Change the source
  and regenerate. Committing them is what lets a schema change show up as a docs diff in review.
- Examples are welcome; instruction and opinion are not. Link to how-to and explanation instead.

**Explanation** (`docs/explanation/`) — context, background, and the answer to *why*. This is the
one place opinion belongs. Circle the subject, take a position, admit the trade-offs. This is where
"why Nautobot rather than our own model" and "the reboot that lost the data" live.

## Working style

Small steps, published immediately. Do not batch a documentation rewrite: pick one page, ask what
user need it serves and how well it serves it, make one improvement, commit. The structure will
follow.


## This repo has a migration boundary

Ported from nvis-software-factory, where the whole docs tree was Diátaxis from the start. Here it
is not: ~44 hand-written `SCREAMING_CASE.md` files at `docs/` root predate the site and are
**excluded** from the build by pattern, along with `docs/superpowers/` (agent plan/spec
artifacts).

So there are two kinds of document in `docs/` and the rule only governs one of them:

- `index.md` and anything under `how-to/`, `reference/`, `explanation/` (and `tutorials/` when it
  exists) is **the site**. It obeys everything above, is listed in `_toc.yml`, and is built with
  `-W`.
- A root-level uppercase file is **not yet**. Editing one is fine and needs no frontmatter.

Do not add a new page as a root-level uppercase file. `docs/README.md` describes how to migrate an
existing one, and the real work is fixing the cross-references `-W` then finds — which is why it
is done a page at a time rather than in one sweep.
