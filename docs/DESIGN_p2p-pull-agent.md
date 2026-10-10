# Design note — a pull agent for NVIS-P2P image distribution

**Status:** proposed; one-node prototype on Nscale NC (see [Prototype](#prototype-one-node-on-nscale-nc)).
**Author:** Rafael M. Koike (with Claude). **Date:** 2026-10-09.
**Scope:** `deploy/p2p/scripts/` (node agent, coordinator), `backend/p2p_operations.py`,
`backend/api/p2p_fleet.py`, the Artifact Distribution views. Dragonfly itself is unchanged.

## Problem

Dragonfly moves the bytes well. Everything around it is driven over SSH, and that is where campaigns
get stuck. Evidence from Nscale NC, 2026-10-06 → 10-09 (33 operations, 290 waves, ~8 800 node
installs):

- **The control plane is SSH.** For every node, every 60 s, `rollout-catalog-image-fleet.sh` opens an
  SSH session to check, launch, stop or refresh. 1 572 of ~1 860 node deferrals were
  `launch_limit:not_started` — SSH sessions that never got a download going (connection resets,
  `kex_exchange_identification`, an `xargs` batch abort on one exit 255). On NC this was made worse by
  routing every session through the BCM head (`ProxyJump`, `MaxStartups 10:30:60`), removed
  2026-10-09; the session count is the root problem either way.
- **Operations cannot be steered.** No cancel, stop, retry-node or retry-wave exists; every "failed"
  operation in the history was an operator hand-editing files on the appserver. A crash leaves a
  per-op lock file behind and the operation reads `running` forever, which disables the console.
- **One serial queue per cluster.** A `sync` waits behind hours of distribute; each wave waits for its
  slowest node.
- **Status is late.** The UI sees a 15-minute SSH/pdsh snapshot of 1 447 of 2 142 nodes; an operation's
  log is written when a wave ends.
- **Six or seven copies of the catalog,** reconciled only before a distribute. The node script verifies
  against the node's own catalog copy, so a stale copy fails a correct download
  (`CATALOG_IMAGE_UNKNOWN`), and a re-pin needs every node's copy refreshed over SSH first.
- **The node side is not supervised.** Downloads are `nohup` children of an SSH session; seven peer
  re-deploys in two days followed reimages and onboarding.

The Dragonfly control plane (manager/scheduler v2.5.1, two seeds) logged no errors in the same window.

## Goals

1. No SSH in the steady state of a campaign. SSH remains a manual bootstrap/repair tool.
2. Live per-node status in seconds, for the whole inventory, without polling nodes.
3. Cancel, retry one node, retry a wave, change pacing — as edits, not as file surgery.
4. One pin per image per campaign: the node verifies against what it was told to hold.
5. A node that reboots, is reimaged, or misses a campaign converges on its own.

**Non-goals (v1):** replacing Dragonfly's transfer, deleting images from nodes, a new UI. The agent
never removes an installed image; retiring an image is a separate, deliberate operation.

## Architecture

```text
 Rack Status (coordinator)                appserver                         compute node
 ─────────────────────────                ─────────                         ────────────
 campaign = desired state  ──writes──▶  origin nginx :12130                nvis-p2p-agent (systemd)
 waves / cancel / retry                  /.nvis-p2p/desired/<node>.json ◀─GET─┤ every 30 s, ETag
                                                                              │ reconcile:
                                         Dragonfly seeds / scheduler ◀─gRPC──┤  dfget via local peer
                                                                              │  verify sha256, place,
                                                                              │  receipt
 reads status  ◀──query── VictoriaMetrics :8428 ◀─remote_write─ Alloy ◀─ node_exporter textfile
```

Four roles, each built on something already running:

| Role | Built on | New |
|---|---|---|
| Coordinator | Rack Status run registry + P2P catalog | desired-state writer, wave/cancel logic |
| Node-facing endpoint | origin nginx on :12130 (nodes already fetch the catalog from it) | `/.nvis-p2p/desired/` files |
| Transfer | Dragonfly peer on the node (`dfget` over the unix socket) | — |
| Status | node_exporter textfile → Alloy → VictoriaMetrics (already carries node metrics) | `nvis_p2p_*` series |

### Why pull over HTTP, not push over gRPC

Measured on NC: nodes accept inbound connections and the peer's gRPC upload port (12200) is open, so
an appserver→node gRPC push is possible. It is still the wrong shape. A push coordinator must keep 2 000
calls in flight, chase nodes that are down, and expose a new authenticated listener on every node.
A pull agent needs no inbound listener, puts no fan-out on the appserver (2 000 nodes × one GET per
30 s ≈ 70 req/s, mostly `304 Not Modified`), and converges a node that was offline when the campaign
ran. The payloads are a small manifest and a few metrics, so HTTP/JSON and Prometheus text are enough;
gRPC would add generated stubs and a runtime on air-gapped aarch64 nodes for no gain. Dragonfly keeps
its own gRPC for the data plane, which is where it belongs.

## Desired-state contract

One JSON document per node, served read-only:
`http://<appserver>:12130/.nvis-p2p/desired/<hostname>.json`
(file: `/cluster/containers/.nvis-p2p/desired/<hostname>.json`). Absent (`404`) means "nothing
desired"; the agent then does nothing and reports idle.

```json
{
  "schema_version": 1,
  "generation": 7,
  "node": "sys1-gso1-p-phy-gpu50-su25",
  "origin": "http://10.10.20.22:12130",
  "images": [
    {"filename": "nvbandwidth-v0.9-arm.sqsh",
     "sha256": "e097a325…91e9", "bytes": 853815296, "priority": 0}
  ]
}
```

- **`generation`** is monotonic per node. Bumping it with the same image list is a *retry*: the agent
  clears that node's backoff and failure counters.
- **`images`** is the set to hold, in priority order. Removing an image that is mid-download is a
  *cancel*: the agent stops `dfget` and discards the partial. Removing an installed image does nothing
  (non-goal above).
- **The pin travels with the request.** The agent verifies against this `sha256`, never against the
  node's catalog copy, so a re-pin takes effect on the next poll.
- **Waves are the coordinator's business.** A node absent from the current wave simply has no entry
  yet; pacing, rack staging and cancellation are all edits to these files.

Writes are atomic (`tmp` + `rename`), so a node never reads a half-written document.

## Node agent

`deploy/p2p/scripts/nvis-p2p-agent.py` — Python 3 standard library only (nodes ship 3.12), one
systemd unit, runs as `nvis-p2p`, `Restart=always`.

Per poll (default 30 s, ±20 % jitter):

1. `GET` the desired document with `If-None-Match`. On error keep the last good document and count
   the error; never act on a malformed document.
2. For each desired image, classify: **ready** (file present at the pinned size with a matching
   receipt, or matching sha256 — then a receipt is written), **downloading**, **backoff**, or
   **queued**.
3. At most one download at a time (configurable). Start the highest-priority queued image:
   `dfget --digest sha256:<pin> --disable-back-to-source` into `staging/<file>.partial`, under an
   outer `timeout`. On exit: check size and sha256, move into place, write the receipt — the same
   install rules as `download-catalog-image.sh`.
4. A failure sets an exponential backoff (60 s → 15 min cap) and is reported; it never wedges the loop.
5. Cancel: an in-flight image that left the document is stopped (TERM, KILL after 30 s) and its
   partial removed.
6. Write `nvis_p2p_agent.prom` atomically into the textfile directory.

The agent is the only thing on the node that starts downloads, so "is a download running?" has one
answer and the pidfile/ownership races of the SSH launcher go away. A restart (crash, reboot) loses
nothing: the next poll re-derives everything from the document and the files on disk, and Dragonfly
resumes from the pieces it already holds.

### Metrics

| Series | Meaning |
|---|---|
| `nvis_p2p_agent_up{generation}` | agent loop alive; label is the document generation it acted on |
| `nvis_p2p_agent_last_poll_timestamp_seconds` | last completed poll — staleness = agent down |
| `nvis_p2p_agent_fetch_errors_total` | failed desired-state fetches |
| `nvis_p2p_image_state{filename,state}` | 1 for the image's current state (`ready`, `downloading`, `queued`, `backoff`, `failed`) |
| `nvis_p2p_image_bytes{filename}` | pinned size once installed, 0 before |
| `nvis_p2p_image_expected_bytes{filename}` | pinned size |
| `nvis_p2p_image_download_started_timestamp_seconds{filename}` | when the running download started (age = time in flight) |
| `nvis_p2p_image_failures_total{filename}` | failed attempts since the last generation bump |

A campaign's live view is one query, e.g.
`sum by (state) (nvis_p2p_image_state{filename="nvidia+nemo+26.04.01-arm.sqsh"})`.

## Coordinator

The coordinator replaces `rollout-catalog-image-fleet.sh` and the cron `operations-worker.py`
distribute path. It is a Rack Status run (citizen 4, the durable run registry), not a cron job, so it
has real states, an owner, and survives a restart by re-reading its own record.

- **Start:** resolve the image's pin from the authored catalog, compute waves (the existing
  rack-staged plan from `topology.tsv`), write desired documents for wave 0.
- **Advance:** each tick, query VictoriaMetrics; when a wave's nodes are `ready` (or have exhausted
  their budget), write the next wave. No wave waits for a single slow node beyond its budget.
- **Cancel / retry / pace:** rewrite the affected documents. These are cheap, idempotent, and do not
  need the run to be "between waves".
- **Separate lanes:** catalog sync and verification no longer queue behind a distribute; they are
  independent runs.

**Transfer acceleration (phase 4):** the v2.5.1 manager supports preheat jobs scoped to
`all_peers` and `get_task` (who holds a task). Preheating a wave's peers before writing their
documents makes the agent's `dfget` a local cache hit. This needs a manager personal access token,
which an operator must issue from the manager console.

## Security

- The desired documents are read-only and unauthenticated on the cluster network — the same exposure
  as the catalog nodes already fetch from :12130. They contain filenames, sizes and hashes only.
- Nodes expose nothing new; the agent makes outbound HTTP only.
- Status rides the existing metrics pipeline. A spoofed metric could lie about progress but cannot
  install anything: installation is decided on the node by the pinned sha256.

## Migration

| Phase | Scope | Exit criterion |
|---|---|---|
| P0 | Prototype on one node: agent as a transient unit, hand-written document | download, live metrics, cancel and restart behave as above, no SSH during the run |
| P1 | One rack, coordinator as a script on the appserver | a rack converges with no SSH; metrics agree with on-disk receipts |
| P2 | Coordinator inside Rack Status (run registry, API, Artifact Distribution views) | cancel/retry/pace from the UI |
| P3 | Agent shipped like the observability agents (installed at boot from persistent `/opt/containers`) on the whole fleet; SSH rollout retired | a fleet campaign with zero SSH sessions |
| P4 | Manager preheat per wave | cold-start time per wave measured against P3 |

## Open questions

1. Whether a preheat job in v2.5.1 can target an explicit list of peer IPs (needed for rack-staged
   preheat), or only `all_peers` — confirmed so far: `all_peers`, `all_seed_peers`,
   `single_seed_peer`, `get_task`, `delete_task`.
2. Manager-side throttling of a preheat across ~2 000 peers.
3. Whether `dfget` from the local cache into `/opt/containers` is a hard link (same filesystem) or a
   42 GB copy on NC.
4. Retiring an image from nodes (disk pressure) — explicitly out of v1.
5. In-flight progress. dfget preallocates its output, so file size and allocated blocks both read
   full from the first second, and the peer's metrics (:12204) count concurrent downloads per tag,
   not bytes per task. Real progress needs the peer's own task state (`StatTask` over the dfdaemon
   socket) — until then the agent reports only when a download started.

## Prototype: one node on Nscale NC

**Run 2026-10-09** on `sys1-gso1-p-phy-gpu50-su25`. The agent ran as a transient unit
(`systemd-run --unit=nvis-p2p-agent-proto --uid=nvis-p2p -p Restart=always … --interval 10`) from
`/opt/containers/.nvis-p2p/agent/`. Documents were written to
`/cluster/containers/.nvis-p2p/desired/` on the appserver. Every observation in the control loop
was an HTTP query to VictoriaMetrics; SSH was used only to install the agent, to inject the crash,
and to audit the results afterwards.

| Test | What happened | Result |
|---|---|---|
| Install | `nvbandwidth` (853 MB) published at generation 1. The agent adopted it within one poll, installed it 10 s later, and metrics showed `ready` 55 s after publish | pass |
| Install, large | `mnubergemm` (13.9 GB) installed 50 s after its start (~280 MB/s through the peer mesh) | pass |
| Cancel | `nemo-arm-nccl2.32.3` (42.5 GB) published; 37 s into the download it was removed (generation 5). The agent cancelled it in the same poll; `dfget` was gone and the partial deleted 10 s later; nothing was installed | pass |
| Crash and resume | The same image republished (generation 6), then SIGKILL on the agent 50 s into the download. systemd restarted it 6 s later (`NRestarts=1`); the new process rebuilt its state from the document and the disk and restarted the download. Installed 2 min 19 s after the restart, sha256 equal to the catalog pin (`737ac1a6…`), receipt written | pass |
| Status path | textfile → Alloy → VictoriaMetrics carried the agent's series with `cluster`/`node` labels, about 30 s after each write | pass |

**What the run taught us:**

- **Dragonfly keeps pieces across a cancel and a crash.** The 42.5 GB file installed after the
  restart carries the inode and mtime of the first, cancelled attempt (link count 2: the peer's
  storage and `/opt/containers`). The retry reused what was already on the node instead of
  fetching it again, so neither a cancel nor a restart costs a full transfer.
- **Metrics lag is the slowest part of the loop.** The agent reacts within one poll (10 s here), but
  scraping and remote write add about 30 s. That is fine for campaign pacing. A cancel or retry that
  needs sub-second acknowledgement would need a direct status POST, which is not in v1.
- **In-flight progress cannot come from the file** (open question 5). The first run reported a
  13.95 GB "progress" for a download that had only just started, because the partial is
  preallocated. The agent now reports only when a download started and the full size once
  installed.
- **The textfile directory is root's and the nodes have no `setfacl`.** The installer hands the
  agent one pre-created file (`nvis_p2p_agent.prom`, owned by `nvis-p2p`), and the agent rewrites it
  in place when it cannot rename into the directory.
- **Two bugs were caught by the tests before they reached the node:** the `logs/` directory was
  never created, and an exception in the download thread was recorded as a failure with no reason.

**Left in place after the run:** the transient unit (it disappears on reboot; stop it with
`sudo systemctl stop nvis-p2p-agent-proto`), the agent file under `/opt/containers/.nvis-p2p/agent/`,
the handed-over metrics file, and the node's desired document at generation 6. Every image it names
is now installed on that node, so the agent sits idle.

## P1: one rack on Nscale NC

**Run 2026-10-10** on rack **Q5** (SU22, 18 nodes). The image was `nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh`
(42.5 GB, pin `2acf607c…`), which all 18 nodes were missing. Two scripts were added:

- `deploy/p2p/scripts/install-nvis-p2p-agent-node.sh` is the SSH bootstrap: it installs the agent on
  the 18 nodes as a transient unit in **10 s**, with one result line per node and no batch abort on
  a bad node.
- `deploy/p2p/scripts/nvis-p2p-coordinator.py` is the coordinator: rack-staged rungs (`1, 50%, 100%`
  → 1, 9, 18 nodes), desired-document writes, and reads from VictoriaMetrics. It ran as a plain
  process on the appserver and opened no SSH session.

| Rung | Nodes | Time to ready per node (from release) |
|---|---|---|
| 0 (seed) | 1 | 3 min 45 s |
| 1 (50 %) | 8 | 3 min 15 s – 5 min 30 s |
| 2 (100 %) | 9 | 3 min 15 s – 5 min 30 s |

**18 of 18 ready, 0 deferred, 14 min 46 s for 765 GB.** A read-only audit afterwards found every
node holding the file at the pinned size with a receipt for the pinned sha256, so the metrics agreed
with the disks — the P1 exit criterion.

Found on the way:

- **PromQL escaping.** `re.escape` produces `\-`, which is not a legal escape in a PromQL string;
  VictoriaMetrics answered 422. Node names are now validated and only `.` is escaped.
- **A failed metrics read must stop the tick, not decide on it.** An empty observation would have
  read as "no live agent" and deferred the whole rack. The coordinator now logs the error and skips
  the tick; per-node budgets keep running on the clock.
- **The rack's Slurm state changed between plan and run.** Three Q5 nodes picked up jobs in that
  window. Downloads run at `ionice -c2 -n7 nice 10`, onto the `/opt/containers` NVMe, over the
  management NIC rather than the RoCE rails, so they ran beside the jobs. A production coordinator
  should still read node allocation and hold or pace allocated nodes as a policy choice.

**Left in place:** transient `nvis-p2p-agent` units on Q5 (gone on reboot), their desired documents
at generation 1, and the run record under `~/p2p-coordinator-runs/` on the appserver.

## P2: the coordinator inside Rack Status

The P1 script became part of the product. A rollout is now a **workflow-engine run** (citizen 4),
not a process somebody started on the appserver.

- **`backend/p2p_rollout.py`** registers a built-in `nvis-p2p-rollout` workflow. It has one native
  step, `p2p_rollout`, which is the coordinator. The run record holds three things:
  - the fixed plan (`params.plan`): the image pin from the cluster's catalog, and each rack's members
    in tray order from the server's node inventory
  - the steerable inputs: `params.pacing` and `params.commands`
  - the coordinator's own state, `run.rollout`, saved every tick
- **`backend/api/p2p_rollouts.py`** serves `/api/p2p/rollouts`:
  - start, list, preview (`/plan`) and detail
  - cancel, retry and pacing on a live run
  - each control is its own action: `p2p:rollout_start`, `_cancel`, `_retry` and `_pace`
- **Artifact Distribution → Rollouts** is the UI: start with a preview of each rack's members, live
  agents and current holders; a list with progress; and per rack its rung, its nodes and a retry for
  each deferred node. Cancel is confirm-gated.
- **Per-cluster settings** (`P2pSettings`): `desired_dir`, `origin_url`, `metrics_url`,
  `rollout_node_budget_seconds` and `rollout_max_active_racks`, editable in Dragonfly Settings.
  `rollout_blockers()` reports what is missing before a rollout can start.

The workflow engine gained what a run lasting hours needs. Each addition went into the engine, not
beside it:

- **Built-in definitions.** A deployment mounts its state directory over `var/`, which hides any YAML
  baked into the image.
- **Idempotency keys.** A second start of the same image on the same racks joins the live run.
- **Live parameter updates and `persist_run`.** Pacing and retry reach a running step on its next tick.
- **Cancel is honoured mid-step.** Before, a step's result overwrote a cancel that arrived while it ran.
- **Resumable native steps and a resume sweep** at startup. Before, a run interrupted by a restart
  stayed `running` on disk forever.
- **A per-run executor `flock`.** One run is never driven twice: not by a resume, not by a second
  process, not by an `advance` racing either of them.
- **`list_runs(workflow_id=…)`.**

Two rules were applied deliberately and not followed literally:

- **No rack claim (citizen 8).** A rollout runs no jobs on a rack and takes nothing from it, so it is
  not an exclusive operation. In any case `rack_locks` accepts only `pN-rMM` rack ids, and NC's are
  `Q5`. The exclusivity that matters, one live rollout per image and set of racks, is the run's
  idempotency key.
- **Not registered as a validation activity.** That registry is the audit trail of what ran on a rack.
  A rollout's own run record is its audit trail.
