---
diataxis: explanation
description: How NVIS-P2P moves container images to a whole GPU fleet from a couple of seeds — the task/roster/campaign model, the seed-and-peer mesh, why staged waves scale, and the trade-offs that follow from content-addressing.
---

# How catalog images reach every node

*Explanation — the model behind NVIS-P2P catalog distribution: what a task, a roster and a
campaign are, why there is a fixed seed as well as a peer mesh, and why the thing that makes it
scale is staging, not raw fan-out.*

NVIS-P2P exists to answer one question: how do you put a 10–40 GB container image on a few hundred
GPU nodes without the origin server — a single appserver — melting under a few hundred simultaneous
downloads. The answer is [Dragonfly](https://d7y.io/): a BitTorrent-shaped peer-to-peer fabric
where nodes fetch an image in *pieces* from a small set of seeds **and from each other**, so the
origin serves the bytes roughly once and the fleet carries the rest.

Most of the vocabulary that accretes around this is orthogonal, not hierarchical. It is worth
pinning down before anything else.

## Three independent things: catalog, task, roster — joined by a campaign

There is no tree here. Four nouns, and the first three are independent inputs:

| Term | What it is | The question it answers |
|---|---|---|
| **Catalog** | The manifest of which images exist and their pinned `sha256`/size (`images.tsv`/`images.json`). | *What* can be distributed |
| **Task** | One image's transfer. Its identity is a task-id derived from the image's content hash. | *The transfer itself* |
| **Roster** | A flat list of target hostnames. | *Which* nodes |
| **Campaign** | Drive one catalog image onto one roster, tracking each node to `ready` or `deferred`. | *Make it so, and account for it* |

A campaign is the **binding** of a task to a roster:

```
campaign  =  task (one image, id = sha)   ×   roster (which nodes)
```

Same task, different roster → a different campaign. Same roster, different task → a different
campaign. The catalog is the shared registry both sides read from — the task gets its `sha` there,
the roster is just names. This is a two-dimensional composition, not a cascade, and most of the
confusion people have with the system comes from expecting a hierarchy that is not there.

### A task is content-addressed, and that has consequences

A task's identity is literally `nvis-p2p-sha256:<sha256-of-the-image>`. That single fact drives most
of the system's good behaviour and all of its sharp edges:

- **It unifies the swarm.** Every node that pulls the same image computes the *same* task-id, so
  they all join the *same* task. That shared task *is* the swarm — it is what lets peers trade
  pieces. Without content-addressing, 500 downloads would be 500 unrelated transfers.
- **It is stable forever.** The same image always produces the same id. "Re-running" a distribution
  is not a new task; it rejoins the existing one. A *new* id appears only when the *content* changes.
- **It deduplicates rather than collides.** Two requests for identical bytes are not a hash
  collision to be feared; they are the same task, by design. A real collision — different content,
  same id — would require breaking SHA-256.
- **The id carries no intent.** Because identity is purely content, there is no way to model "two
  independent transfers of the same bytes at different priorities." You almost never want that, so
  it is the right trade — but it is why `--tag`/`--application` exist to namespace *campaign* intent
  without touching *task* identity.

## Seeds and peers are different roles

The most common misconception is that an early-finishing node "becomes a seed." It does not, and the
distinction is load-bearing.

- A **seed** is one of a small, fixed set of daemons (two, on the appserver) that are **registered
  with the manager** and configured with `seedPeer.enable: true` and `enableBackToSource: true`.
  Only a seed may fetch an image from the origin. It holds the whole image and serves it to peers.
- A **peer** is a compute node. It runs the same daemon but with `seedPeer.enable: false` and —
  critically — **`enableBackToSource: false`**: a peer is *forbidden from contacting the origin at
  all*. It pulls pieces from seeds and other peers, and (because `upload.disableShared: false`)
  serves the pieces it has to other peers.

A peer feeding pieces to another peer is **real, and it is how the system scales out** — but it is a
transient piece-source for one task, not a seed. The seeds are irreducible: when a brand-new image
appears, *no peer has a single byte*, and peers are not allowed to ask the origin. Something must
make the first pull from the origin, and that something is a seed. Remove the fixed seeds and the
first pull has no source — the swarm never ignites. **Seeds cold-start each image from the origin;
peers scale it out across the fleet.**

## What actually makes it scale: staging, not fan-out

A campaign does not move bytes — the Dragonfly scheduler does. What a campaign adds is **convergence
and accounting**: a ledger (`tracking.tsv`) that starts every roster node at `NEW`, a loop that
polls each node's real state, re-launches stragglers up to a limit, defers the unreachable, and ends
with a partition of the roster into `ready.tsv` and `deferred.tsv`.

The campaign also paces the launch — `xargs -P <parallelism>` throttles how many nodes it *starts*
per poll cycle. The lesson learned the hard way on NC: firing all ~500 downloads at once with a raw
`pdsh` fan-out **corrupts the scheduler state and stalls**, because a few hundred nodes announcing
the same task simultaneously is more than two seeds and one scheduler absorb gracefully. Staging the
launch in bounded waves lets the first wave become a piece-source for the next, so the mesh warms
itself before the whole fleet piles on.

The effect is measurable. A real staged run — 50 nodes per wave of a 9.8 GB image — produced this:

| Wave | Ready | Seed bytes served | Delivered | Offload |
|---|---|---|---|---|
| 1 (cold) | 48/50 | 29 GB | 471 GB | 16× |
| 2 | 48/50 | **0 GB** | 471 GB | 100% peer |
| 3 | 50/50 | **0 GB** | 490 GB | 100% peer |
| 4 | 50/50 | **0 GB** | 490 GB | 100% peer |
| 5 | 50/50 | **0 GB** | 490 GB | 100% peer |
| total | 293 | **52 GB** | 2.88 TB | ~55× |

Even the cold first wave offloaded 16×, because the 50 nodes in it shared pieces among themselves.
Once the mesh was warm, waves 2–5 delivered ~1.9 TB with the seeds serving **zero bytes**. The seeds
carried under 2% of the total; the peer mesh carried the rest. That is the entire thesis, measured.

### Staging by rack, not by name

Bounded waves warm the mesh, but *which* nodes share a wave decides where the pieces come from.
Every peer is labelled with its SU and rack, and the scheduler ranks a parent in the same rack
first, then the same SU, then the seeds. A roster sliced in hostname order ignores that: on NC,
where the rack is not in the hostname, alphabetical waves of 25 touched only 7–14 racks each, and
the last of 93 racks received its first copy in wave 53 of 58. Until then, every node in a rack
without a holder pulled across racks or from the seeds.

The `rack-staged` wave plan follows the scheduler instead. It first puts one copy in every rack —
the only waves that need the seeds — and then grows every rack in steps. The steps matter as much
as the seeding: if a rack's single first holder fed its 16 rack-mates at once, that one node would
become the rack's bottleneck. The default ladder fills every rack to half first, so the first holder
feeds about as many nodes as remain, and the last half pulls from half a rack of holders. A finer
ladder (`1, 2, 4, 8, 100%`) doubles the holders at each step, so no holder ever feeds more than one
new node per step, at the cost of more waves. Because later stages pull inside racks, they can run
wider waves than the seed stage without adding seed load — which is why the two are paced
separately.

On a fleet that already holds the image in most racks, seeding every rack again is wasted work —
the first run of the plan spent its seed waves on racks that had copies, and picked a few freshly
reimaged nodes as seeds. So the plan reads the fleet snapshot when it is fresh: holders count
toward each rack's rungs, a rack with a copy gets no seed, and a rack is seeded by its most proven
peer. The gain from staging is therefore largest on a cold fleet or a set of new racks; on a warm
fleet most of what remains is fewer waves, each with less fixed overhead.

## The sharp edges, and where they come from

None of these are bugs; they follow from the design, and knowing them is the difference between the
system working and mysteriously not.

- **The catalog must be synced to the nodes first, and completely.** A node resolves the expected
  `sha` from its *local* catalog, and that `sha` is both the task-id (which swarm to join) and the
  verification target. Re-pin an image on the server but leave a node's catalog stale and that node
  will pull the correct bytes and then *reject them* as a digest mismatch. An incomplete catalog push
  shows up later as a block of nodes failing verification — not as an obvious error at push time.
- **The peer-sharing window is time-boxed.** A peer serves a task's pieces only while the task lives
  in its storage (`gc.taskTTL`, about a day). After that it still has the assembled image on disk
  (so it correctly *skips* a re-run) but has GC'd the servable pieces — so a late repair of a few
  stragglers falls back to the seeds. The swarm for a given image fades after a day, which is exactly
  why the seeds must be permanent.
- **A stuck download used to hold a wave open to its timeout.** The campaign polls until every node
  is `ready` or `deferred`, and a download that neither finished nor failed counted as active — on NC
  one sat on 40 of 42.5 GB, idle, for a quarter of an hour. The rollout now watches the bytes each
  download has actually written and treats one that has not moved for the stall window as stuck:
  it is stopped and relaunched (the peer keeps its pieces and the scheduler picks new parents), and
  deferred once it has used its launches. What remains is the stall window itself, which must be
  long enough to cover a slow transfer that is still moving. Re-run with `deferred.tsv` as the next
  roster to pick up what was deferred.
- **Giving up on a node has to stop the node.** The same NC download outlived its wave by ten hours:
  the rollout recorded the node as deferred and moved on, dfget ignored its own six-hour timeout, and
  every later launch found the process alive and re-adopted it. Each layer now enforces its own end —
  the node kills a transfer at its download limit (an hour by default), a launch replaces a download
  older than that limit, and a wave that times out stops what it is giving up on — so no single
  layer's failure leaves a node stuck.

## Idempotency: why re-running is cheap and correct

Because the task-id is stable and the node-side download guards on a receipt, a campaign is a
**converge-to-desired-state** operation, not a one-shot push. `download-catalog-image.sh` checks for
the image at the expected size with a matching receipt (or matching on-disk `sha`) and exits
immediately if it is already there — no transfer, not even a re-hash in the receipt case. So
re-running a campaign over a roster where most nodes are done is nearly free: the satisfied nodes
short-circuit in milliseconds and only the missing or corrupt ones actually pull. The
`nvis-p2p-catalog-sync` timer on each node does exactly this on a schedule — it *is* the "re-run the
campaign forever" loop, made autonomous.

## See also

- Reference — [P2P catalog distribution: components, parameters, and scripts](../reference/p2p-catalog-distribution.md)
- How-to — [How to distribute a catalog image to the fleet](../how-to/distribute-a-catalog-image.md)
- Tutorial — [Distribute your first image with NVIS-P2P](../tutorials/distribute-your-first-image.md)
- [NVIS-P2P as a per-cluster capability](p2p-module-integration.md)
