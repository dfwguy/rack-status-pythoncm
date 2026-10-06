---
diataxis: tutorial
description: A guided, end-to-end first run of NVIS-P2P — distribute one image (example.img) to 100 nodes as two staged waves of 50, watch the seeds stay flat, then try the same run as four waves of 25.
---

# Distribute your first image with NVIS-P2P

*Tutorial — by the end you will have pushed a single image to 100 nodes with the campaign tooling,
seen the peer mesh carry the bytes instead of the origin, and understood each thing you typed well
enough to change it. You need appserver access as a `cmods`-group member and a 100-node roster you
are allowed to write to.*

We will distribute one image — call it `example.img.sqsh` — to 100 nodes, deliberately in **two
waves of 50** rather than all at once, and watch the second wave cost the seeds almost nothing. Then
we will redo it as **four waves of 25** to see the knob that controls staging. Nothing here changes
production state beyond placing an image you already intend to ship.

## Set up

On the appserver:

```bash
export NVIS_P2P_ROOT=/opt/nvis-p2p/current
cd "$NVIS_P2P_ROOT/scripts"
```

Put your 100 hostnames, one per line, in `~/roster100.txt`. (If you do not have a file yet, the
nodes you are bringing up are usually listed by whatever inventory produced them; any 100 reachable
peers will do for this lesson.)

## Step 1 — make sure the image exists and is pinned

The origin serves only what is in `/cluster/containers`, and every node will check what it downloads
against the catalog's recorded hash. Confirm all three agree:

```bash
curl -sI "http://<appserver>:12130/example.img.sqsh" | head -1    # HTTP/1.1 200 OK
awk -F'\t' '$3=="example.img.sqsh"{print $4, $5}' \
  /cluster/containers/.nvis-p2p/catalog/images.tsv                # bytes and sha
sha256sum /cluster/containers/example.img.sqsh | cut -c1-64       # same sha
```

If the `sha` column is empty, `example.img.sqsh` is not in the catalog yet — add a row to
`images.tsv` and `images.json` first. If the `sha` is present but different from `sha256sum`, re-pin
it (see the [how-to](../how-to/distribute-a-catalog-image.md#1-confirm-the-image-is-on-the-origin-and-pinned)).
Getting this right now is what saves you from a confusing "digest mismatch" later.

## Step 2 — give every node the current catalog

This is the step first-timers skip, and it is the one that bites. Each node decides what to pull and
what to verify from *its own* copy of `images.tsv`. Refresh all 100:

```bash
xargs -P50 -I{} ssh -o ConnectTimeout=10 -o BatchMode=yes {} \
  'curl -s -m10 http://<appserver>:12130/.nvis-p2p/catalog/images.tsv \
     -o /opt/containers/.nvis-p2p/catalog/images.tsv' < ~/roster100.txt
```

Check that it actually landed on all 100 before continuing:

```bash
xargs -P50 -I{} ssh {} grep -lc "$(awk -F'\t' '$3=="example.img.sqsh"{print $5}' \
  /cluster/containers/.nvis-p2p/catalog/images.tsv)" \
  /opt/containers/.nvis-p2p/catalog/images.tsv < ~/roster100.txt | grep -c 1
```

You want `100`. If it is less, re-run the refresh — some ssh connections time out under load, and a
node left stale will fail verification in step 4.

## Step 3 — warm the seeds

Tell both seeds to fetch the image from the origin once, so the first wave pulls from a warm mesh:

```bash
bash preheat-catalog-image.sh example.img.sqsh
```

You are looking for two lines:

```
PREHEAT_READY seed=seed   example.img.sqsh verification=sha256
PREHEAT_READY seed=seed2  example.img.sqsh verification=sha256
```

If instead you see a download error mentioning an unroutable address, the deployed `preheat` script
has a placeholder origin — fix the origin IP in `scripts/preheat-catalog-image.sh` and retry. If one
seed reports a permission error, run with `umask 002` so the daemon can write the shared preheat
directory.

## Step 4 — run it as two waves of 50

Record where the seeds start, split the roster in half, and run the campaign once per half:

```bash
seed0=$(for p in 12114 12124; do \
  curl -s http://127.0.0.1:$p/metrics | awk '/^dragonfly_client_upload_traffic /{print $2}'; \
  done | paste -sd+ | bc)

split -l 50 -d ~/roster100.txt wave_

for f in wave_*; do
  NVIS_P2P_ROLLOUT_PARALLELISM=50 NVIS_P2P_ROLLOUT_POLL_SECONDS=20 \
    bash rollout-catalog-image-fleet.sh example.img.sqsh "$f" "$(basename "$f")"
done
```

Each wave prints `ROLLOUT_BEGIN`, then `ROLLOUT_PROGRESS iteration=… ready=… pending=…` every 20
seconds, then `ROLLOUT_COMPLETE total=50 ready=50 deferred=0`. The first wave takes the longest — it
is pulling from the two seeds. The second wave is faster, because its nodes are pulling largely from
the first wave's nodes.

## Step 5 — see what the mesh saved you

Read how many bytes the seeds served for the whole run:

```bash
seed1=$(for p in 12114 12124; do \
  curl -s http://127.0.0.1:$p/metrics | awk '/^dragonfly_client_upload_traffic /{print $2}'; \
  done | paste -sd+ | bc)
echo "seeds served $(( (seed1 - seed0) / 1000000000 )) GB to deliver 100 copies"
```

If `example.img` is, say, 10 GB, you delivered ~1 TB to 100 nodes. The seeds will show only a few
tens of GB — most of it from the first, cold wave; the second wave adds almost nothing. That gap is
the whole point: the origin served the image roughly twice (once per seed), and the fleet carried
the rest.

Confirm every node actually has it:

```bash
for f in wave_*; do
  RD=$(ls -dt "$NVIS_P2P_ROOT"/state/rollout-"$(basename "$f")"-* | head -1)
  tail -n +2 "$RD/ready.tsv"
done | wc -l        # expect 100
```

## Step 6 — redo it as four waves of 25

Nothing about the campaign changed between two-waves and four-waves except how you sliced the
roster. That slicing *is* the staging control. Re-run — and because the image is already on the
nodes, the campaign is a near-instant no-op that proves idempotency:

```bash
split -l 25 -d ~/roster100.txt q_
for f in q_*; do
  NVIS_P2P_ROLLOUT_PARALLELISM=25 NVIS_P2P_ROLLOUT_POLL_SECONDS=20 \
    bash rollout-catalog-image-fleet.sh example.img.sqsh "$f" "$(basename "$f")"
done
```

Every wave returns almost immediately with `ready=25`, and the seed upload total barely moves —
each node hits its receipt and exits without pulling. You have now seen both halves of the system:
a cold distribution that the mesh offloads, and a warm re-run that costs nothing.

## Where to go next

- To do this for real against your own fleet, follow
  [How to distribute a catalog image to the fleet](../how-to/distribute-a-catalog-image.md).
- To understand *why* the second wave was free and why the seeds are still necessary, read
  [How catalog images reach every node](../explanation/p2p-catalog-distribution.md).
- For every parameter you touched, see
  [the reference](../reference/p2p-catalog-distribution.md).
