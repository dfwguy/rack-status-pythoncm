---
diataxis: explanation
description: Why Greenlight keeps a document store and a relational one, how to tell which a new piece of data belongs in, and why "put it in a table" is the wrong default.
---

# Which storage engine, and why

Greenlight persists four different kinds of thing in four different ways, and that is deliberate
rather than accumulated. This page is the reasoning. The rule you apply while writing code is
`storage-engines.mdc`; this is the argument behind it, including the parts where we got it wrong.

## The default is the problem

Almost everyone reaches for a SQL table first. It is what most of us learned first, it is what the
tutorials use, and a table is genuinely a good general-purpose answer — which is exactly why it
gets proposed for data that has no relations in it at all.

The cost is not that SQL breaks. It is that a table quietly buys nothing and gives something up.
A record that nothing joins, stored relationally, gets none of the benefit of the relational engine
and loses the one property the document interface exists to provide: the ability to change where it
is stored without changing any code that reads it.

So the question is never "could this be a table". It is "does anything join it".

## The test

Two shapes, and the boundary between them is sharper than it looks.

**Relational**, if any of these is true: there is a real one-to-many with a parent; a query joins
two tables or aggregates across rows; or a range scan over an ordered column has to stay fast as
the table grows.

**A document**, if all of these are true: the record is read and written whole, by its own id; the
only other access is a filter over a handful of denormalised fields; and nothing joins it to
anything.

"Is it tabular?" is the wrong question, because almost everything looks tabular once you draw it
with columns. Rows and columns are a rendering. Relations are a property of the data.

## Two archives, same era, opposite answers

The clearest way to see the boundary is that this repo contains one of each, written within weeks
of each other.

`results_history` is genuinely relational, and not marginally. Its `run_metrics` table hangs off
`runs` with `REFERENCES runs(run_id) ON DELETE CASCADE`, keyed on `(run_id, metric_name, seq)`
where sequence 0 is the run's scalar summary and everything above it is a point on an in-run curve.
Three joins, a `GROUP BY` and nine indexes serve the trend charts and the fleet percentiles. Try to
model that as documents and you get one of two bad outcomes: joins reimplemented in Python, or run
documents with an NCCL bandwidth curve embedded in them and no natural size limit. It stays in SQL,
and it is the one dataset here that a key-value cloud store would serve badly, because range
aggregates are precisely what those are bad at.

The validation-activity archive is the opposite, and it is instructive because it *looks* like a
table. It has a primary key, six columns, and indexes. But six of those columns are denormalised
copies of fields for filtering, the seventh is the entire record as a JSON blob, and there is not a
single join or foreign key anywhere. It is a key-value document store, implemented in a relational
engine because the relational engine was the familiar tool. It would map onto a partition key and
two secondary indexes without reshaping a thing.

Neither of these is a bug. The first is right. The second works, and costs us the swappable
backend for no gain we are using.

## Why the document interface is worth protecting

`backend/catalog/store.py` defines a `Repo` protocol of eight methods — `list`, `get`, `upsert`,
`delete`, `query`, `get_meta`, `set_meta`, `bulk_upsert` — and its docstring states the intent
plainly: a cloud backend "can implement the same interface later without touching callers."

That is the whole value. These deployments are appliances, frequently air-gapped, running one Flask
process. Today a JSON file on disk is the right backend. On a hosted deployment it would not be,
and the difference should be one class rather than a migration of every call site. Every document
put behind the interface inherits that. Every document put in its own database file does not.

It is also why "just add a database" is a bigger decision than it sounds. The product has exactly
one storage dependency, `tinydb`, and an operator with an air-gapped appliance has no service to
keep alive. That is a feature worth defending.

## Where we got it wrong, and what it cost

Three mistakes, all still visible in the tree.

**A whole-file store used for growth.** `json_db.AtomicJSONStorage` reads with `json.loads` on the
entire file and writes by dumping the entire document. For a catalog of a few dozen platforms and
tools that is correct and simple. For anything that grows once per launch it is not — which is why
the live activity table caps itself at 500 entries, and why its archive, which must never be
pruned, could not use the same storage.

**A read path that wrote, and defeated its own cache.** A snapshot function pruned expired rows
while reading. TinyDB rewrites the file on `remove()` even when nothing matches, the write moved
the file's mtime, and the mtime was part of the cache key — so the cache was invalidated by its own
reads and never hit once, while every functional test passed. The lesson generalises past storage:
prune on write, filter on read, and assert that a cache actually hits, because a cache that never
hits cannot go stale and staleness tests will not notice.

**The filesystem used as an index.** `var/reports/` holds 140 files. There is no way to ask which
of them mention a given rack except to open all of them. Nothing has broken yet, which is the only
reason it is still there.

## What this means for a new feature

Apply the test, and write the answer down in the commit message so the next person inherits the
reasoning rather than the outcome. A document goes behind `Repo` without opening a database. A
genuine relation goes into the database that already owns that concern, with each index commented
by the query it serves.

And if a request arrives asking for a table, for data that fails the test, the right response is to
use the document store and explain why. That is not pedantry about storage. It is the difference
between a product that can move and one that has four database files and cannot.
