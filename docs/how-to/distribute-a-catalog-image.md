---
diataxis: how-to
description: Push one catalog image to a fleet with a staged campaign — refresh the node catalog, preheat the seeds, run the rollout, read the result, and repair stragglers.
---

# How to distribute a catalog image to the fleet

Get one image onto a roster of nodes using the campaign tooling, in bounded waves so the seeds are
not overwhelmed. For the model behind these steps see
[How catalog images reach every node](../explanation/p2p-catalog-distribution.md).

In Rack Status the same thing is one action: pin the image under **Artifact Distribution →
Catalog**, then launch a campaign under **Campaigns**. For a large image, set the campaign's wave
plan to **Rack-staged**: it gives every rack one copy before growing each rack, so most nodes pull
from rack-mates instead of the seeds (see
[Wave plan](../reference/p2p-catalog-distribution.md#wave-plan-operations-worker)). The operations worker runs steps 3–4 for you,
and before it preheats it checks that the cluster's manifest pins the image as the catalog does —
syncing first when it does not. Use the steps below when you need to drive a rollout by hand.

All commands run on the appserver as a member of the `cmods` group (no `sudo` needed for P2P
operations).

```bash
export NVIS_P2P_ROOT=/opt/nvis-p2p/current
cd "$NVIS_P2P_ROOT/scripts"
```

## 1. Confirm the image is on the origin and pinned

The origin only serves what is in `/cluster/containers`, and nodes verify against the catalog
`sha`. Check both:

```bash
curl -sI "http://<appserver>:12130/<image>.sqsh" | head -1          # expect 200
awk -F'\t' '$3=="<image>.sqsh"{print $5}' \
  /cluster/containers/.nvis-p2p/catalog/images.tsv                  # the pinned sha
sha256sum /cluster/containers/<image>.sqsh | cut -c1-64             # must match the pin
```

If the pin does not match the file (for example after re-importing an image — a rebuilt image never
reproduces an earlier hash), re-pin it under **Artifact Distribution → Catalog** and run **Sync**.
Do not edit `images.tsv` or `images.json` on the cluster by hand: Rack Status is the catalog's
source of truth, and the next sync rewrites both files from it.

## 2. Make sure the nodes carry the new pin

A node resolves the expected `sha` from its *own* catalog copy. If that copy is stale, the node
pulls the correct bytes and rejects them as a digest mismatch (or refuses the image outright as
`CATALOG_IMAGE_UNKNOWN`). Every peer refreshes its copy from the origin every 15 minutes
(`nvis-p2p-catalog-refresh.timer`), so after a sync you can simply wait one interval. To push it now
instead, run the same validated refresh on every node, then verify the count:

```bash
xargs -P50 -I{} ssh -o ConnectTimeout=10 -o BatchMode=yes {} \
  'sudo -n systemctl start nvis-p2p-catalog-refresh.service' < roster.txt

# verify: how many carry the new pin
xargs -P50 -I{} ssh {} grep -l <new-sha> \
  /opt/containers/.nvis-p2p/catalog/images.tsv < roster.txt | wc -l
```

Do not proceed until the count equals the roster size. A missed block here becomes a block of
deferred nodes later. A node without the timer (`systemctl is-active
nvis-p2p-catalog-refresh.timer`) never catches up on its own — re-deploy its peer.

## 3. Preheat the seeds

Make both seeds hold the image before the fleet asks for it, so the first wave pulls from a warm
mesh rather than racing the origin:

```bash
bash preheat-catalog-image.sh <image>.sqsh
# expect: PREHEAT_READY seed=seed … and PREHEAT_READY seed=seed2 …
```

## 4. Run the campaign in waves

Split the roster into waves and run the rollout per wave, so each completed wave seeds the next:

```bash
split -l 50 -d roster.txt wave_
for f in wave_*; do
  NVIS_P2P_ROLLOUT_PARALLELISM=50 \
  NVIS_P2P_ROLLOUT_POLL_SECONDS=20 \
  NVIS_P2P_ROLLOUT_TIMEOUT_SECONDS=1800 \
    bash rollout-catalog-image-fleet.sh <image>.sqsh "$f" "$(basename "$f")"
done
```

Each run prints `ROLLOUT_BEGIN`, the two `PREHEAT_READY` lines (instant once warm), `ROLLOUT_PROGRESS`
per poll, and `ROLLOUT_COMPLETE total=N ready=R deferred=D`.

## 5. Read the result and the offload

The per-wave ledger is in the run directory:

```bash
RD=$(ls -dt "$NVIS_P2P_ROOT"/state/rollout-<label>-* | head -1)
cut -f1 "$RD/ready.tsv" | tail -n +2         # nodes that got it
cut -f2 "$RD/deferred.tsv" | tail -n +2       # failures, with reasons
```

To confirm the mesh is doing the work, watch seed upload against bytes delivered:

```bash
for p in 12114 12124; do
  curl -s "http://127.0.0.1:$p/metrics" | awk '/^dragonfly_client_upload_traffic /{print $2}'
done
```

A small seed-upload delta against a large delivered volume is the win. After the first (cold) wave,
later waves should add almost nothing to the seed total.

## 6. Repair stragglers

Deferred nodes fall into two buckets: `launch_limit_N` (pulled but never verified — usually a stale
catalog, so go back to step 2 for them; `launch_limit_N:not_started` means the peer itself is not
running, see [How to bring P2P peers up on new or reimaged nodes](recover-p2p-peers-after-a-reimage.md))
and `unreachable_after_N_checks` (down or unreachable).
Collect them into a new roster and re-run — the campaign is idempotent, so nodes that already have
the image short-circuit instantly:

```bash
cut -f1 "$RD/deferred.tsv" | tail -n +2 | sort -u > repair.txt
NVIS_P2P_ROLLOUT_PARALLELISM=25 bash rollout-catalog-image-fleet.sh <image>.sqsh repair.txt repair
```
