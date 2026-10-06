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
| `NVIS_P2P_ROOT` | `/opt/nvis-p2p` | Install root; set to `/opt/nvis-p2p/current` for the live release. |

Invocation: `rollout-catalog-image-fleet.sh <image-filename> <roster-file> [<label>]`.

Run-directory artifacts (under `$NVIS_P2P_ROOT/state/rollout-<label>-<image>-<stamp>/`):

| File | Contents |
|---|---|
| `tracking.tsv` | `node  launches  unreachable  last_state  last_detail` — the live ledger. |
| `ready.tsv` | Nodes confirmed present + verified, with timestamp. |
| `deferred.tsv` | Nodes given up on, with reason (`launch_limit_N`, `unreachable_after_N_checks`). |
| `rollout.log` | `ROLLOUT_BEGIN` / `PREHEAT_READY` / `ROLLOUT_PROGRESS` / `ROLLOUT_COMPLETE`. |

## Catalog manifest (`images.tsv`)

Tab-separated, one row per image:

```
order	name	filename	bytes	sha256
2	HPC Benchmarks	nvidia+hpc-benchmarks+26.02-arm.sqsh	9818501120	8f2e92ba…
```

`images.json` carries the same records plus a `required_by` list. Both live at
`NVIS_CATALOG_PATH` (e.g. `/cluster/containers/.nvis-p2p/catalog/`) and are served by the origin at
`http://<appserver>:12130/.nvis-p2p/catalog/images.tsv`.

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
