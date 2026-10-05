---
diataxis: explanation
description: Why Rack Status is a single-writer, stateful service, what that means for a highly-available active/passive deployment on shared storage, and the concrete gaps between here and there.
---

# Active/passive and blue/green for Rack Status

*Explanation — why Rack Status is a single-writer, stateful service, what that means for a
highly-available active/passive deployment on shared storage, and the concrete gaps between here
and there.*

> **ROADMAP.** This page is forward-looking. Rack Status ships today as a **single container per
> cluster**; nothing below is implemented yet. It records the design so the HA work, when it is
> scheduled, starts from a shared understanding rather than a rediscovery. The **Roadmap** section
> at the end lists the concrete tasks.

## The core tension

Rack Status runs as **one gunicorn worker, by design**. It keeps meaningful state *in the process*:
the SRP challenge table (which must be single-process — `srp.assert_single_worker_safe`), async
command contexts, the rack-status cache and the SSH-reachability cache, WebSocket terminal PTYs, and
SSH ControlMaster sockets. That single-writer shape is what makes the app simple and correct.

It is also the whole difficulty of HA. **Active/passive is feasible; two instances *both active* —
the overlap window a naive blue/green cutover creates — is what breaks**, because two processes
would split the challenge table and race every shared write.

## What survives a swap, and what is lost

| State | Where it lives | Survives a cutover? |
|---|---|---|
| JWT signing key, TLS certificate | `var/secrets/` (files) | **Yes, if shared** — the decisive win: sessions stay valid and the browser cert exception persists, so **no forced re-login** and no new warning |
| Catalog, identity | TinyDB / `json_db` (`var/`) | Yes if shared (whole-file atomic writes) |
| Workflow runs | JSON under `var/workflows/runs` | State yes; the *advancing thread* no (see below) |
| results / validation / snapshots | **SQLite** (`var/`) | Yes if shared — but SQLite-on-NFS is the hazard |
| SRP challenge (mid-login) | in-memory | No — the operator retries |
| async command tracking, caches, terminals | in-memory / per-process | No — orphaned / reconnect / rebuild |

## The shared-folder hazards

Sharing `var/` across the active and passive is the obvious lever — "put it on `/cluster` and it
doesn't matter which node is active." It half-works, and the failure modes are specific:

1. **SQLite over NFS is unsafe.** `results_history`, `validation_activities`, `cluster_snapshot`
   and `cluster_overview_live` are SQLite, whose POSIX/fcntl locking is unreliable over NFS —
   corruption and "database is locked" happen **even with a single writer**, and reliably with two.
   This is the first thing that bites, and the main reason `var/` cannot simply be a shared mount.
2. **TinyDB is not multi-writer.** Whole-file atomic writes tolerate NFS for a *single* writer, but
   an overlapping blue+green racing the same file is last-writer-wins clobbering. Fine for strict
   active/passive; wrong for overlap.
3. **In-flight workflow runs stall on cutover.** A run's state is durable, but what *advances* it is
   a background thread in the active process. The new active sees the persisted state with no thread
   moving it, so the run hangs at its last step. This matters directly for P2P provisioning and for
   validation campaigns.
4. **Mid-flight logins and in-memory long-running ops are lost.** An SRP `finish` that lands on the
   new active fails (the challenge was in the old one's memory); any long operation tracked
   in-memory rather than in the workflow engine is orphaned.
5. **Caches, terminals and ControlMaster are per-process** — the new active rebuilds them; first
   loads are slow and terminals reconnect. Transient, not correctness.

## What it would take

- **Target active/passive with a shared secrets+config folder — not zero-downtime overlap.** A brief
  cutover (stop active, then start passive) sidesteps concurrent writers, and because the signing key
  and certificate are shared, sessions and the cert survive it.
- **Split storage by NFS-safety.** Documents (catalog, identity) and secrets on shared NFS are fine
  (whole-file atomic, single-writer). The **relational stores move off SQLite-on-NFS** — either a
  shared **PostgreSQL** (the `Repo` / storage-engine abstraction, citizen 15, already anticipates
  swappable backends) or kept node-local, accepting per-node reporting history since those stores are
  not HA-critical.
- **Enforce single-writer with a leader lease.** A lock/lease on the shared filesystem so exactly one
  instance mutates state; the passive stands by, health-checkable, and takes the lease on failover.
- **Accept the transient losses** — mid-login retries, terminal reconnects, cache warmup.

**NC is already halfway there:** `/cluster` is shared NFS and the deployment already puts durable
state on it — but it is not yet *exercised* by two instances, and the SQLite-on-NFS caveat is the
first thing that would fail under a second writer.

## Roadmap

Concrete tasks, roughly in dependency order:

1. **Workflow engine resumes in-flight runs on boot** — turns a cutover from "provisioning stalls"
   into "provisioning continues." Smallest change with the largest HA payoff.
2. **Move remaining in-memory long-running tracking onto the workflow engine** (citizen 4) so nothing
   important lives only in one process's memory.
3. **Relational stores on a shared backend** — a PostgreSQL `Repo` backend (or a documented
   node-local policy) so the shared-storage deployment is not SQLite-on-NFS.
4. **Leader lease + passive stand-by** — election on the shared FS, a passive that serves reads or
   idles until it holds the lease, and a cutover procedure.
5. **Shared `var/secrets`** as the supported HA layout, so sessions and the certificate survive a
   swap by construction.
