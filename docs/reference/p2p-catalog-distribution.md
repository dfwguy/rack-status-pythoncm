---
diataxis: reference
description: The NVIS-P2P catalog-distribution machinery described — server and node components, ports, processes and services, the script inventory, the peer/seed configuration keys, the campaign parameters, and a glossary.
---

# P2P catalog distribution — components, parameters, and scripts

*Reference — the components, ports, configuration keys, scripts and parameters of NVIS-P2P catalog
distribution. For the model behind it see
[How catalog images reach every node](../explanation/p2p-catalog-distribution.md).*

## Glossary

| Term | Definition |
|---|---|
| Catalog | The manifest of distributable images and their pinned `sha256`/size, in `images.tsv` and `images.json`. |
| Task | One image's content-addressed transfer. Task-id is `nvis-p2p-sha256:<sha256>`. |
| Roster | A file of target hostnames, one per line, consumed by a campaign. |
| Campaign | One image driven onto one roster with per-node tracking (`ready`/`deferred`). |
| Seed | A registered Dragonfly daemon permitted to fetch from the origin and serve peers. Two on the appserver. |
| Peer | A compute-node daemon that pulls from seeds/peers and shares pieces; never contacts the origin. |
| Origin | The nginx service that serves the image files and catalog over HTTP. |
| Offload | Bytes delivered to nodes ÷ bytes served by the seeds. Higher is better. |
| Back-to-source | A seed fetching an image from the origin (the only path that reads the origin). |

## Server components (appserver)

| Component | Runs as | Role | Ports |
|---|---|---|---|
| Origin (`nvis-p2p-seed-origin`) | Docker container (nginx) | Serves `/cluster/containers` — image files + catalog manifest | 12130 (HTTP) |
| Manager (`nvis-p2p-manager`) | host process, `User=nvis-p2p` | Seed-peer registry, auth, dynamic config | 12003 gRPC, 12004 REST (localhost), 12005 |
| Scheduler (`nvis-p2p-scheduler`) | Docker container | Builds the per-task piece-distribution plan | internal |
| Seed 1 (`nvis-p2p-seed`) | host `dfdaemon`, `User=nvis-p2p` | Holds full images, back-to-sources from origin, serves peers | 12110 upload, 12114 metrics |
| Seed 2 (`nvis-p2p-seed2`) | host `dfdaemon`, `User=nvis-p2p` | Second seed | 12120 upload, 12124 metrics |
| Redis (`nvis-p2p-redis`) | Docker container | Manager backing store | 15379 (localhost) |
| Postgres (`nvis-p2p-postgres`) | Docker container | Manager backing store | 15432 (localhost) |
| Object store (`nvis-p2p-object-store`) | Docker container (MinIO) | Optional object storage; not on the catalog peer path | 19000/19001 |

Seeds are host `dfdaemon` processes, not containers. They are distinguished from peers by their
config (`seedPeer.enable: true`, `type: super`) **and** by registration in the manager
(`POST /api/v1/seed-peers`); both are required.

## Node components (compute node)

| Component | Role |
|---|---|
| `nvis-p2p-peer.service` | The `dfdaemon` peer. Config at `/opt/containers/.nvis-p2p/peer.yaml`. |
| `nvis-p2p-catalog-sync.service` (timer-driven) | Periodically reconciles the node against the catalog by running `dfstore-sync-catalog.sh`. |

Node-local paths:

| Path | Contents |
|---|---|
| `/opt/containers/*.sqsh` | Delivered image files (what jobs read). |
| `/opt/containers/.nvis-p2p/catalog/images.tsv` | The node's local copy of the catalog manifest. |
| `/opt/containers/.nvis-p2p/receipts/<image>.sha256` | Per-image completion receipts. |
| `/opt/containers/.nvis-p2p/bin/` | Node-side scripts (`download-catalog-image.sh`, …). |
| `/opt/containers/.nvis-p2p/storage/` | Dragonfly piece storage (GC'd per `gc.taskTTL`). |

## Peer configuration keys (`peer.yaml`)

| Key | Example | Meaning |
|---|---|---|
| `manager.addr` | `http://<appserver>:12003` | Where the peer finds the scheduler/seeds. |
| `scheduler.enableBackToSource` | `false` | A peer never pulls from the origin — seeds/peers only. |
| `scheduler.announceInterval` | `10s` | How often the peer announces to the scheduler. |
| `scheduler.maxScheduleCount` | `50` | Max scheduling attempts per task. |
| `download.concurrentPieceCount` | `16` | Pieces pulled in parallel. |
| `download.pieceTimeout` | `90s` | Per-piece timeout. |
| `download.bandwidthLimit` | `50GB` | Download rate cap. |
| `upload.disableShared` | `false` | The peer serves its pieces to other peers (the mesh). |
| `upload.bandwidthLimit` | `50GB` | Upload rate cap. |
| `seedPeer.enable` | `false` | This daemon is a plain peer, not a seed. |
| `storage.dir` | `/opt/containers/.nvis-p2p/storage` | Piece storage. |
| `gc.policy.taskTTL` | `86400s` | How long a completed task's pieces remain servable. |

## Seed configuration keys (`seed.yaml`)

Same schema as a peer, with the distinguishing values:

| Key | Value |
|---|---|
| `seedPeer.enable` | `true` |
| `seedPeer.type` | `super` |
| `scheduler.enableBackToSource` | `true` (seeds may read the origin) |
| `upload.server.port` | `12110` (seed) / `12120` (seed2) |
| `storage.dir` | `/cluster/OVA/NVIS-P2P/seed-cache/seed1` (and `…/seed2`) |

## Campaign parameters

Environment variables read by `rollout-catalog-image-fleet.sh`:

| Variable | Default | Meaning |
|---|---|---|
| `NVIS_P2P_ROLLOUT_PARALLELISM` | `48` (max 128) | Concurrent ssh launches/checks per wave. |
| `NVIS_P2P_ROLLOUT_POLL_SECONDS` | `60` | Pause between poll iterations. |
| `NVIS_P2P_ROLLOUT_MAX_LAUNCHES` | `3` | Re-launch attempts before a node is deferred. |
| `NVIS_P2P_ROLLOUT_MAX_UNREACHABLE` | `3` | Failed reachability checks before a node is deferred. |
| `NVIS_P2P_ROLLOUT_TIMEOUT_SECONDS` | — | Overall campaign timeout. |
| `NVIS_P2P_ROLLOUT_STALL_SECONDS` | `600` | Stop and relaunch a download whose written bytes have not moved for this long; `0` disables. |
| `NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS` | `3600` | Hard limit on one node's download of one image, passed to every launch and enforced on the node. |
| `NVIS_P2P_ROOT` | `/opt/nvis-p2p` | Install root; set to `/opt/nvis-p2p/current` for the live release. |

Invocation: `rollout-catalog-image-fleet.sh <image-filename> <roster-file> [<label>]`.

### Wave plan (operations worker)

A distribute launched from Rack Status is split into waves by the operations worker
(`deploy/p2p/scripts/operations-worker.py`), which then runs `rollout-catalog-image-fleet.sh` once
per wave, in order. Cluster defaults live in **Dragonfly Settings**; a campaign overrides them
(0 or blank inherits).

| Setting (cluster) | Campaign override | Default | Meaning |
|---|---|---|---|
| `rollout_strategy` | `strategy` | `sequential` | `sequential` slices the roster in order. `rack-staged` plans by placement — see below. |
| `rollout_wave_size` | `wave_size` | `50` | Nodes per wave; under `rack-staged`, the size of the first-stage (seed-loading) waves. |
| `rollout_fill_wave_size` | `fill_wave_size` | `0` | `rack-staged` only: nodes per wave in every later stage. `0` = the wave size. |
| `rollout_stages` | `stages` | `1, 50%, 100%` | `rack-staged` only: the stage ladder. |
| `rollout_stall_seconds` | `stall_seconds` | `600` | Becomes `NVIS_P2P_ROLLOUT_STALL_SECONDS`; `0` (cluster only) disables. |
| `rollout_verify_mb_per_second` | `verify_mb_per_second` | `100` | Becomes `NVIS_P2P_ROLLOUT_VERIFY_MBPS`. A download with every byte written is checksumming; its stall window is the larger of 2 x `stall_seconds` and the image size at this rate (a 36.8 GB image at 100 MB/s gets 368 s). `0` keeps the flat 2 x stall. |
| `rollout_download_timeout_seconds` | `download_timeout_seconds` | `3600` | Becomes `NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS`; at least `60`. |

**Stage ladder.** Each rung is a cumulative per-rack target: a node count (`1`, `2`) or a share of
the rack, rounded up (`50%`). Counts must rise, shares must rise, and the last rung must be `100%`.
Counts and shares may be mixed (`1, 2, 4, 50%, 100%`); a rack smaller than a count reaches that rung
early and skips it. For a 17-node rack the default ladder gives cuts of 1, 9 and 17 nodes.

**How `rack-staged` plans.**

1. Stage one takes one node from every rack, paced at `wave_size`. These are the only pulls with no
   holder in their rack, so the only stage that loads the seeds.
2. Each later stage takes every rack from its previous rung to its next, paced at
   `fill_wave_size`. A rack's slice of a stage is never split across two waves unless it is larger
   than a whole wave.
3. Racks are visited round-robin across SUs (natural order: `U7` before `U19`), so each wave spreads
   over the fabric.
4. Roster nodes missing from the topology go last, paced at `wave_size`.

**Using the fleet snapshot.** When the cluster's fleet snapshot
(`<state>/p2p-snapshots/<cluster>.json`, beside the operations directory; the worker's `--snapshot`
overrides it) is less than an hour old, the plan also uses what is already on the fleet:

- A rack's rungs are measured against the whole rack in the topology, and nodes that already hold
  the image count toward them. A rack with a copy gets no first-stage node; a rack already past 50%
  goes straight to its last stage.
- Within a rack, the reachable node with the most verified catalog images is taken first, so a rack
  is seeded by a peer that has finished downloads before.
- Nodes the snapshot could not reach, and roster nodes that already hold the image, go to their
  rack's last stage.

A missing, unreadable or older snapshot is logged and the plan uses placement alone.

**Topology contract.** The plan reads `$NVIS_TOPOLOGY_FILE`, default
`<install_root>/current/inventory/topology.tsv`: one row per compute node,
`<hostname>\t<su>\t<rack>`, captured from the cluster manager's placement. It is the file the peers'
scheduler labels (`idc` = SU, `location` = rack) are rendered from, so the plan and the scheduler
agree on what a rack is. If the file is missing or has no placed node, the worker logs
`falling back to sequential waves` and runs `sequential`.

The worker logs the plan before the first wave, for example:

```text
[worker] rack-staged plan from /opt/nvis-p2p/current/inventory/topology.tsv: 93 racks in 15 SUs, ladder 1 → 50% → 100%, seed waves 25, fill waves 72
[worker]   fleet snapshot (723s old): 55 racks already hold the image, 55 roster nodes hold it, 22 roster nodes were unreachable (placed last)
[worker]   stage 1: 93 nodes over 93 racks in 4 waves
[worker]   stage 50%: 647 nodes over 93 racks in 10 waves
[worker]   stage 100%: 707 nodes over 93 racks in 11 waves
[worker] wave 0 — 25 nodes (stage 1)
```

Run-directory artifacts (under `$NVIS_P2P_ROOT/state/rollout-<label>-<image>-<stamp>/`):

| File | Contents |
|---|---|
| `tracking.tsv` | `node  launches  unreachable  last_state  last_detail` — the live ledger. |
| `ready.tsv` | Nodes confirmed present + verified, with timestamp. |
| `deferred.tsv` | Nodes given up on, with reason (`launch_limit_N`, `unreachable_after_N_checks`, `stalled_<S>s_after_<N>_launches`, `timeout_after_<S>s`). |
| `progress.tsv` | `node  written  since_epoch` — each active download's written bytes and when they last changed. |
| `rollout.log` | `ROLLOUT_BEGIN` / `PREHEAT_READY` / `ROLLOUT_PROGRESS` / `ROLLOUT_STALLED` / `ROLLOUT_COMPLETE`. |

**Stalled downloads.** Each poll, `check-catalog-image-node.sh` reports an active download as
`ACTIVE  pid=<pid> written=<bytes>`, where `written` is the bytes allocated to the node's staging
partial (dfget preallocates its length, so only the allocated blocks move; `-1` until a partial
exists). A download whose `written` has not changed for `NVIS_P2P_ROLLOUT_STALL_SECONDS` is stalled,
and a download whose `written` has reached the image size (it may be checksumming) gets twice that
window. A stalled node is stopped with `stop-catalog-image-node.sh` and relaunched; once it has used
`NVIS_P2P_ROLLOUT_MAX_LAUNCHES` it is stopped and deferred as `stalled_<S>s_after_<N>_launches`. The
peer keeps the pieces it already holds, so a relaunch resumes rather than restarts. A node still
downloading when the wave reaches `NVIS_P2P_ROLLOUT_TIMEOUT_SECONDS` is deferred **and stopped**
(`ROLLOUT_TIMEOUT_STOPPED`), so a deferred node never keeps a transfer running.

**Download limit on the node.** `launch-catalog-image-node.sh NODE FILENAME [SECONDS]` passes the
limit to `download-catalog-image.sh`, which runs dfget under `timeout --kill-after=60`: dfget is told
the limit, and is killed when it passes even if it ignores it (the log ends with
`CATALOG_DOWNLOAD_TIMEOUT`, exit 124). A launch that finds a "running" download older than the limit
plus `NVIS_P2P_STALE_GRACE_SECONDS` (default `60`) replaces it (`replaced_stale_pid=` in
`CATALOG_LAUNCH_STARTED`) instead of reporting it active. The download script takes the seed origin
from the node's `catalog-refresh.env` (scheme, host and port of `NVIS_P2P_CATALOG_URL`) unless
`NVIS_P2P_ORIGIN` is set, and refuses with `CATALOG_ORIGIN_UNKNOWN` when it has neither.

## Catalog manifest (`images.tsv`)

Tab-separated, one row per image:

```
order	name	filename	bytes	sha256
2	HPC Benchmarks	nvidia+hpc-benchmarks+26.02-arm.sqsh	9818501120	8f2e92ba…
```

`images.json` carries the same records plus a `required_by` list. Both live at
`NVIS_CATALOG_PATH` (e.g. `/cluster/containers/.nvis-p2p/catalog/`) and are served by the origin at
`http://<appserver>:12130/.nvis-p2p/catalog/images.tsv`.

### Required by — which launch runs which image

`required_by` names the launches that run an image. Every launch that stages a container finds its
image here, and nowhere else: the filename, the byte size and the SHA256 that the stage gates check
are this entry's. No launcher carries an image name or digest of its own.

| Required by | Launch |
|---|---|
| `nemo-26.04.01` | NeMo 26.04.01 validation preset (rack and multi-rack) |
| `exemplar-system-info` | DGXC Exemplar system_info preset |
| `llama31-405b-fp8` | Llama 3.1 405B FP8 preset |
| `gpt-oss-cpu` | GPT-OSS CPU microbenchmark preset (from the LLMB image store) |
| `mnubergemm-26.03-v1.17` | MNUberGEMM preset |
| `nemo-xrack` | Multirack NEMO |
| `validation-singlerack` | NeMo single-rack validation |
| `llmb-nccl-container` | LLMB NCCL container sweep (24 tests) |

The Catalog Editor offers exactly these names (`GET /api/p2p/catalog` returns them as `consumers`).
A launch is refused before it reaches Slurm when no image names it, when two images name it, or when
the image it names has no bytes or SHA256 — the message says which, and what to set. The resolved
image is part of the reviewed plan, so editing the catalog between review and launch changes the
plan hash and the launch must be reviewed again. The launcher receives it as `NVIS_LAUNCH_IMAGE`,
`NVIS_LAUNCH_IMAGE_BYTES` and `NVIS_LAUNCH_IMAGE_SHA256`. A manual run may set `NVIS_LAUNCH_IMAGE`
alone; the size and digest then come from the catalog published beside the scripts
(`config/image-catalog.tsv`).

## Script inventory

Server-side (`$NVIS_P2P_ROOT/current/scripts/`):

| Script | Purpose |
|---|---|
| `install-nvis-p2p.py` | Stage and activate a release; owns the tree to `nvis-p2p:cmods`. |
| `preheat-catalog-image.sh <image>` | Make both seeds hold an image (back-to-source once). |
| `rollout-catalog-image-fleet.sh <image> <roster> [label]` | Run a campaign for one image over a roster. |
| `preflight-catalog-node.sh` / `preflight-catalog-fleet.sh` | Readiness checks. |
| `deploy-catalog-peer-node.sh` / `deploy-catalog-peer-fleet.sh` | Install the peer on nodes. |

Node-side (`/opt/containers/.nvis-p2p/bin/`):

| Script | Purpose |
|---|---|
| `download-catalog-image.sh <image>` | Pull one image via `dfget`; receipt-guarded and `sha`-verified. |
| `dfstore-sync-catalog.sh` | Reconcile the node against the whole catalog (the sync service's entry point). |
| `check-catalog-image-node.sh` / `verify-catalog-node.sh` | Report a node's state for one image. |

## Node-side download command

`download-catalog-image.sh` resolves the row from the local catalog and invokes:

```
dfget \
  --endpoint /opt/containers/.nvis-p2p/run/dfdaemon.sock \
  --digest "sha256:<expected_sha>" \
  --content-for-calculating-task-id "nvis-p2p-sha256:<expected_sha>" \
  --application NVIS-P2P-Catalog \
  --tag "catalog-v1-<order>" \
  --disable-back-to-source \
  "http://<appserver>:12130/<image>" \
  -O <staging>/<image>.partial
```

It exits early with `CATALOG_IMAGE_READY` if the image is already present with a matching receipt
or on-disk `sha`; otherwise it verifies size and `sha` after the pull and writes a receipt.

## Ownership model

The deployed release and catalog are owned `nvis-p2p:cmods`. Operator-editable directories
(`scripts`, `configs`, `catalog`, `state`) are `2775` (setgid + group-write); `dist/` binaries are
owned by the service account but read-only; systemd unit files under `/etc/systemd/system/` remain
root-owned (a systemd requirement) while the services run as `nvis-p2p` via `User=`. The installer
(`install-nvis-p2p.py`) applies this at release time.
