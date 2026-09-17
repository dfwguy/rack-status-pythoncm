---
description: Which storage engine to use — the join test, the four paradigms we already run, and why "put it in a SQL table" is usually the wrong default
alwaysApply: true
---

# Choosing a storage engine

**This rule outranks a request to "add a table for it".** Reaching for SQL by default is the
failure mode here, not an oversight — it is what most of us learned first, so it is what gets
proposed for data that has no relations in it at all. If a prompt asks for a table and the data
fails the test below, say so and use the document store instead.

We already run **four** persistence paradigms. Adding a fifth, or adding another bespoke file to
one of them, needs an argument in the commit message — not a default.

## The test: does anything *join* it?

Not "is it tabular". Almost everything looks tabular when you draw it. The question is whether any
code path needs the engine to relate one row to another.

**It is relational — use SQLite — if any of these is true:**

- a real one-to-many with a parent, especially with `ON DELETE CASCADE`
- a query that joins two tables, or aggregates across rows (`GROUP BY`, percentiles, fleet stats)
- range scans over an ordered column that must stay fast as the table grows

**It is a document — use the `Repo` interface — if all of these are true:**

- the record is read and written **whole**, by its own id
- the only other access is a filter over a handful of denormalised fields
- nothing joins it to anything else

A record whose entire body is a JSON blob beside a primary key is a document. Storing it in a
relational engine is not *wrong* in the sense of broken — it is a choice that costs you the
swappable backend and the cloud path for no benefit you are using.

## The four we run

| Paradigm | Where | What it is good for | Measured today |
|---|---|---|---|
| **Document store behind `Repo`** | `backend/catalog/store.py`, TinyDB over `var/store/catalog.json` | Small, bounded, operator-edited config | 240 KB, rewritten **whole** on every write |
| **SQLite** | `results.db`, `validation_activities.sqlite3`, `cluster_snapshots.sqlite3`, `cluster_overview_live.sqlite3` | Genuine relations, aggregates, unbounded append | 6.1 MB / 2.5 MB / **57 MB** / 84 KB |
| **Loose JSON files** | `var/cache/*.json` via `backend/json_db.py` | Single small mutable documents (locks, overrides) | 6 files, 493 B – 529 KB |
| **Filesystem as database** | `var/reports/` | Nothing. This is a mistake we have not yet paid for | **140 files**, 83 reports plus sidecar meta |

`backend/catalog/store.py` says the important part in its own docstring: the `Repo` interface exists
so "a DynamoDB / cloud backend can implement the same interface later **without touching
callers**". Every document you put behind it inherits that. Every document you put in a bespoke
SQLite file does not.

**The `Repo` protocol is eight methods** — `list`, `get`, `upsert`, `delete`, `query`, `get_meta`,
`set_meta`, `bulk_upsert`. If your data fits those, it is a document, and the backend is an
implementation detail you should not be choosing at the call site.

## The worked example, both ways

Two archives in this repo, same era, opposite answers. Read both before arguing for a new table.

**`results_history` is genuinely relational.** `run_metrics` hangs off `runs` with
`REFERENCES runs(run_id) ON DELETE CASCADE` and a composite key `(run_id, metric_name, seq)` where
`seq = 0` is the run's scalar summary and `seq >= 1` are in-run series points. Three real joins
(`results_history.py` ~842, ~864, ~914) plus `GROUP BY` and nine indexes serve the trend and
fleet-percentile queries. A document model forces either client-side joins or documents with an
unbounded embedded series — an NCCL busbw curve has no natural size limit. **Keep it in SQL.** It is
also the one place where DynamoDB would be a poor fit, because range aggregates are exactly what it
is bad at.

**The validation-activity archive is a document store wearing SQL clothes.** `activity_id` primary
key, six denormalised index columns, and the whole record in `payload TEXT`. Zero joins, zero
foreign keys. It maps to a partition key plus two secondary indexes without reshaping. It is in
SQLite because SQLite was familiar, and the cost is that it is the fourth `.sqlite3` file and it
cannot follow the catalog to a cloud backend.

## Anti-patterns, each of which already happened here

- **A fifth bespoke `.sqlite3` file.** We have four. They have different lifecycles — `results.db`
  is durable history, `cluster_snapshots` is a 57 MB cache — but nothing forced them into separate
  files. New document tables go behind `Repo`; new relational tables go in an existing database.
- **A whole-file JSON store used for unbounded append.** `json_db.AtomicJSONStorage.read()` is
  `json.loads` of the entire file and `write()` dumps the entire document. That is correct for a
  catalog of a few dozen platforms and tools. It is wrong for anything that grows per launch:
  `validation_activities` caps its live JSON at `MAX_ACTIVITIES = 500` precisely because the file
  is rewritten whole, and its archive is deliberately *unpruned* — which is why the archive cannot
  be TinyDB.
- **A read path that writes, defeating its own cache.** `revocation_snapshot()` pruned expired rows
  while reading, and TinyDB's `remove()` rewrites the file even when nothing matches. Every read
  performed a write, the write moved the mtime, the mtime was part of the cache key, and the cache
  **never hit once** while every functional test passed. Prune on write; filter on read. And assert
  the cache actually hits — a cache that never hits cannot go stale, so staleness tests prove
  nothing. Full account in `identity-auth-traps.mdc` §2.
- **The filesystem as the index.** `var/reports/` is 140 files with no way to ask "which reports
  mention this rack" other than opening all of them. Do not add another directory like it.
- **A new engine.** No Postgres, no Redis, no embedded server. The deployment is one Flask process
  with `--workers 1` on an appliance that is often air-gapped; a service to install is a service the
  operator has to keep alive. `backend/requirements.txt` lists exactly one storage dependency
  (`tinydb`) and that is a feature.

## Adding persistent state

1. Run the join test above. Write the answer in the commit message.
2. **Document** → put it behind `Repo` (`backend/catalog/store.py`). Do not open a database.
3. **Relational** → add tables to the database that already owns that concern. Declare the schema
   in one place, index what you query, and say in a comment which query each index serves.
4. **Atomic writes and file modes** are already solved: use `json_db`'s `AtomicJSONStorage` /
   `exclusive_lock`, and create the temp file with the mode you want rather than re-applying it
   after `os.replace` (`identity-auth-traps.mdc` §3).
5. Anything under `/app/var` in a container is a **named volume**, seeded from the image only while
   empty. A file written there at build time is invisible on any deployment that has ever run
   (`local-dev.mdc`).

Reasoning and the trade-offs, for a reader rather than a checklist:
`docs/explanation/which-storage-engine.md`.
