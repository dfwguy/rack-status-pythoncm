---
diataxis: reference
description: What node, rack and multirack scope mean, what each one guards against, and how to choose.
---

# Actions by scope

Every action declares the scope it runs at — `node`, `rack`, `multirack` or `cluster` — and the
scope is not a label. It decides what the operator selects before launching, which nodes the
command reaches, what locks are taken, and whether one Slurm job or several are submitted.

The live list is on the Tools screen and on each rack's action boards, filtered to what the
active platform can run. It is not reproduced here: tools are catalog documents an operator can
add, so a list in a document would be wrong on any deployment that used the feature. As seeded
today there are 3 node-scoped, 23 rack-scoped and 4 multirack-scoped tools.

For *who* may run each one, see the [Action vocabulary](identity/action-vocabulary.md).

## Node scope

One host at a time. The operator picks a node; the command runs there and nowhere else.

Use it for anything whose answer is per-host — a NIC classification, a single-node
microbenchmark, reading one tray's inventory. A node-scoped action takes no rack lock, because
it is not exclusive: two operators probing two nodes in the same rack do not conflict.

The hostname it targets is the real Slurm/DNS name, resolved through the cluster's node
template. If a node-scoped action fails with `Name or service not known`, the naming is wrong
rather than the network — see [Geometry and hostnames](../explanation/geometry-and-hostnames.md).

**Reaching a node is a hop, and on some deployments a direct one.** By default the app relays
through the head node, which spends one of that host's SSH session slots per node; where the app
can reach trays directly, `RACK_DIRECT_NODE_SSH=1` skips the relay and measured about 20%
faster on a large sweep.

## Rack scope

The default, and the shape most validation takes. The operator picks a rack; the action expands
it to that rack's nodes and runs across them.

Three things a rack-scoped action gets that a node-scoped one does not:

- **An advisory rack lock.** Two operators cannot run a rack action on the same rack at once.
  Locks come from two places, merged into one view: *implicit* locks derived from in-flight
  operations, and *explicit* claims persisted with `fcntl` and a refreshable 30-minute TTL. Both
  expire — a stuck job never wedges a rack permanently, and a closed browser tab does not leave a
  ghost claim.
- **A durable activity record.** The launch is registered before it starts, so "what ran on this
  rack, when, and what came of it" survives the process that started it. That is the question
  every handover asks.
- **Bounded fan-out.** Per-node work runs with a concurrency ceiling, because the head node's
  `sshd MaxSessions` — unset means OpenSSH's default of 10 — bounds it whether you respect it or
  not. Exceeding it fails nodes *silently* with "Session open refused by peer"; one fleet sweep
  lost 655 of 2,013 nodes that way.

**Validate one rack at a time, and require every node healthy before starting.** A rack with a
drained tray produces a run that either refuses or reports a partial result, and the second is
worse.

## Multirack scope

The operator selects nodes across several racks. This is not "rack scope with a longer list",
and the difference is the whole reason the scope exists.

**A multirack launch is grouped by rack and dispatched as one job per rack.** For an NVLink-domain
test that is not an optimisation, it is correctness: `mnubergemm` disables InfiniBand and enables
MNNVL, so a single Slurm allocation spanning two physical racks joins two separate NVLink
domains and **hangs** — no error, no output, ranks spinning at 99% CPU with the GPUs idle.

The dispatcher **fails closed** if the groups are not a clean partition of the reviewed target: a
node appearing in two groups, or outside the selection, is a refusal rather than a warning. The
seeded multirack preset also carries a 60-minute allocation ceiling, so a wedged run cannot hold
a rack indefinitely.

Two failures only ever seen at multirack size, both worth recognising before debugging the
fabric:

- **Dies in `MPI_Init` with rc=134.** HCOLL opens its own UCX endpoints and straddles the RoCE
  bond and the InfiniBand rails. `OMPI_MCA_coll_hcoll_enable=0` fixes it. The InfiniBand
  retry-exceeded error it reports is a *symptom* — all link counters were zero.
- **Hangs forever in `ncclCommInitRank`, silently.** Every rack advertising `CliqueId 32766`
  means NCCL tries to bind one multicast team across separate NVSwitch fabrics.
  `NCCL_NVLS_ENABLE=0` works around it; the real fix is a unique fabric clique id per NVLink
  domain in the site's config. Compare `Init START` against `Init COMPLETE` counts first — that
  alone separates "hung in init" from "hung in a collective".

A tool can bound its own selection with `launch.min_nodes` and `launch.max_nodes`, which is what
refuses a two-node run of a test that needs a full rack.

## Cluster scope

Fleet-wide reads: the status dataset, the cluster overview, the exports. These are renderings of
one canonical dataset rather than separate scans, which is why they cannot disagree with each
other. No seeded tool declares `cluster` as its execution target today; the scope belongs to the
built-in status and reporting endpoints.

## Choosing a scope

| The answer you want | Scope |
|---|---|
| about one host | `node` |
| about a rack, or a test that fills one | `rack` |
| a test spanning racks, where each rack must run independently | `multirack` |
| about the fleet | `cluster` |

The question that settles most cases is whether the work is *exclusive*. If two operators
running it simultaneously on the same hardware would interfere, it is at least rack scope and
wants the lock.

## Related

- [Tool documents](catalog/tools.md) — `execution.target` is where the scope is declared.
- [Action vocabulary](identity/action-vocabulary.md) — the permission each action needs.
- [Geometry and hostnames](../explanation/geometry-and-hostnames.md) — how a rack becomes hostnames.
