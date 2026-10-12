---
diataxis: reference
description: The fields that define a customer's cluster, how the app reaches it, and where credentials live.
---

# Projects and clusters

A **Project** is a customer engagement. A **Cluster** is one deployment within it. One running
instance serves several of both, which is what lets a single container answer for Mayo, Firmus,
Boston and Nscale — and why anything cluster-specific belongs on these records rather than in an
environment variable or a branch on cluster name.

## Project

| Field | Type | Purpose |
|---|---|---|
| `id` | slug | Stable key. Also names the project's script directory, `scripts/project-<id>/`. |
| `name` | string | Shown in the cluster switcher. |
| `customer` | string | The organisation. Reaches reports and the AI context envelope. |
| `description` | string | Free text. |
| `clusters` | list | One or more Cluster records, below. |
| `source` | string | `seed` for the project seeding invents from the environment on first boot; anything else for one an operator created. |

`source` is load-bearing rather than descriptive: the setup wizard treats any project whose
source is not `seed` as proof that setup happened. That is what stops the wizard reopening on
every login, and what keeps it away from a deployment that upgrades.

## Cluster

| Field | Type | Purpose |
|---|---|---|
| `id` | slug | Stable key within the project. |
| `name` | string | Shown in the switcher beside the project. |
| `platform_id` | string | Which Platform this is — supplies geometry and per-tool defaults, and selects the platform layer of the script tree. |
| `cluster_manager` | enum | `bcm` or `maas`. Decides which provider answers node inventory: `cmsh` or `maas-cli`. |
| `scheduler` | enum | Where work runs: `slurm` (default), `pythoncm`, `local`, `runai`, `slinky`, `maas`. Separate from `cluster_manager` — see below. |
| `site_prefix` | string | The leading token in this site's rack ids. Used to group a multirack launch. |
| `naming` | object | The three templates — see [Geometry and hostnames](../../explanation/geometry-and-hostnames.md). |
| `geometry` | object | `pods`, `rows_per_pod`, `racks_per_row`, `trays_per_rack`. |
| `fabric` | object | How this site's compute fabric is wired and addressed, where it differs from the platform: `scaleout`, `addr_family`, `compute_hca`, `rail_rate_gbps`. All blank/0 = detect on each node; see below. |
| `nccl_env` | object | Variables this cluster adds to every NCCL-using launch, one list per span: `node`, `rack`, `multirack`. All empty = the scripts' defaults; see below. |
| `clusterkit` | object | How ClusterKit runs on this cluster: `tests`, `gpudirect` and `multirack_tests`. Empty = the launcher's defaults; see below. |
| `scalable_units` | list | This cluster's SU-to-rack mapping. Empty derives it from the platform's `fabric.racks_per_su`; see below. |
| `connection` | object | How the app reaches the cluster, below. |

### scheduler

`cluster_manager` and `scheduler` answer different questions, and a BCM cluster is where the
difference becomes visible: BCM tells you what the nodes *are*, Slurm decides what runs on them,
and plenty of BCM clusters have no Slurm at all. On those, `pythoncm` submits through CMDaemon's
own ProgramRunner instead.

The default is `slurm`, and the asymmetry with `cluster_manager` is deliberate. Guessing the
inventory tool wrong produces an immediate, obvious failure; guessing the *scheduler* wrong
produces a launch that goes somewhere unintended. So a native scheduler is opted into per
cluster, never inferred from the cluster manager.

Every value here must be a kind `schedulers.factory` can build — a cluster naming one it cannot
would fail at launch, long after the record was saved, so the model refuses it at write time.

`pythoncm` additionally needs `bin/scripts/nvis_pythoncm_exec.py` on the head — the setup
wizard's Scripts step sends it, as does "Deploy scripts to head" on this screen and
`bin/deploy-scripts.sh`. `GET /api/scheduler-probe` answers whether it is reachable before a
launch depends on it.

### naming

Naming is optional for node discovery. In setup, both rack and compute templates may be
left blank to inherit existing settings. When supplied, both are required and the compute
template must begin with the rack template. Rack geometry describes grouping and expected
capacity; it does not create inventory nodes.

| Field | Purpose |
|---|---|
| `rack_id_template` | The rack as the floor view groups it. |
| `ct_id_template` | A compute node. Must begin with the rack template. |
| `nvsw_id_template` | An NVSwitch tray. |
| `ct_joiner` | The literal between a rack id and a node index, derived from the templates. |
| `slurm_hostlist` | An explicit host list, when a site's naming cannot be expressed as a template at all. |

Precedence when resolving a rack to real hostnames is: an explicit comma-list, then the
template, then the live Slurm inventory. The last is the ground truth and the reason a template
that is slightly wrong shows up as unresolvable targets rather than as silence.

### Compute node inventory

The Nodes view and the dashboard node count consume the same `nodes` roster in the
`/api/status` snapshot. With the Slurm scheduler, the roster contains actual `sinfo` NodeNames,
deduplicated across partitions. Other schedulers use the cluster-manager provider: BCM's
structured compute-device query or MAAS's machines inventory. Management addresses are
optional, so a missing RF0/BMC address does not remove a node.

Each record has `id`, nullable `rack`, `status`, `scheduler_state`, and `ip`. A known rack
placement or hostname association can attach a node to an existing rack; otherwise the Nodes
view labels it **Unassigned**. The behavior is shared by RTX PRO 6000, B200, B300, and NVL
platforms. Platform geometry never supplies synthetic nodes to this roster.

`nodes: []` means discovery succeeded with no compute nodes. `nodes: null` means no successful
inventory is available. A failed refresh retains the last successful roster and sets
`node_inventory_error`, which the Nodes view displays. Legacy snapshots without `nodes` retain
the earlier rack-based rendering until refreshed.

Unassigned nodes support node terminals and bring-up result inspection. The existing rack
action tabs require a known rack and an exact, representable node target; they are unavailable
when those conditions are absent. This prevents a node selection from falling back to a
whole-rack operation.

### evidence

Some sites run their own collector — a pipeline that walks the fleet's logs and writes a
snapshot of what passed. That snapshot is *better* evidence than anything Greenlight can
re-derive from the same logs, because the collector has already applied the site's acceptance
policy. So Greenlight ingests it and does not reopen the artifacts behind it; forming a second
opinion would give two disagreeing answers to "is this rack accepted".

| Field | Purpose |
|---|---|
| `snapshot_path` | The collector's JSON snapshot, read through the `scripts` role. |
| `healthcheck_root` | Root of per-node health-check markers, one directory per node. |

Both empty is the common case, and means this cluster's evidence is the logs Greenlight reads
itself. They live on the cluster record because they are paths on *this* cluster's hosts and
one instance serves several clusters — the implementation this replaced hard-coded
`/cluster/OVA/logs/nscaletx-16k-validation/validated-racks.json`, which is one customer's
filesystem compiled into the product.

**A snapshot feeds two streams, and the distinction is the point.** Acceptance is
**monotonic**: a rack that passed stays passed, because an acceptance record is a historical
claim that a later unrelated failure does not retract. Current state is **not**: a rack that
is broken now must read broken, or a pass from last week paints it green forever. They are
stored under different `source` values and read by different queries —
`latest_successful_runs_by_rack` for acceptance, `latest_runs_by_rack(source=…)` for now — and
the fleet floor applies the second over the first.

A partial snapshot is refused whole. The collector sets `complete` last, and a rack missing
from a half-written file is indistinguishable from a rack with no evidence, so the gap would
read as "not yet validated" rather than "we did not finish looking".

Rack identity comes from the snapshot's `rack` field, or from `su` plus `rack_number` resolved
through this cluster's [`scalable_units`](#scalable_units). Nothing computes a rack id from a
site's floor plan in code.

Ingest with `bin/scripts/nvis_ingest_evidence.py --cluster-default`, on whatever schedule the
collector runs. It is idempotent: an unchanged snapshot is recognised by content and skipped.

### campaigns

An acceptance **campaign** is a pinned roster of exact compute hostnames, so a bring-up can be
reproduced. Acceptance is a claim about specific hardware: "we validated rack h18" only means
something if the eighteen trays are the same eighteen every time, which a rack name cannot
guarantee once inventory refreshes.

| Field | Purpose |
|---|---|
| `id` | Slug, unique within the cluster, e.g. `wave1`. |
| `label` | Display name. Falls back to the id. |
| `nodes` | Exact compute hostnames. This is the roster. |
| `nodes_per_rack` | Enforced count per rack, or `0` to not check. A wave that reviewed 16 trays wants the 17th to be a refusal. |
| `created_at`, `notes` | Provenance for whoever reads the record later. |

**There is no `version` field, and that is deliberate.** The version is a digest of `nodes`
that the backend derives, so it cannot disagree with the roster it describes — edit the nodes
and it changes by itself. A version an operator maintains by hand is a value that goes stale
silently: change the roster, forget the bump, and every consumer believes it is looking at the
previous list.

A launch sends both `campaign` and `campaign_version`. The id alone was never enough, because
it is a label a human chose: a roster edited under a stable id used to pass the staleness check
and run different hardware than was reviewed, recorded in the activity trail under the same
name. A caller holding a stale roster cannot echo the current digest, so it is refused with a
409 naming both values. Sending no version is still allowed, so a script or an older bundle
keeps working — the check constrains what a caller *claims*, and one that claims nothing runs
the current roster either way.

A cluster may keep several campaigns, one per wave. `RACK_MULTIRACK_CAMPAIGN_ID` selects
between them; with exactly one, it is used without being named.

**The legacy manifest still resolves, second.** `RACK_MULTIRACK_CAMPAIGN_ID` plus
`RACK_MULTIRACK_CAMPAIGN_MANIFEST` (a path to a newline-separated host list on the scripts
host) is what Nscale TX runs on today and keeps working, but the cluster record wins when both
exist. The record is the better home because it travels with the project: visible to the
cluster switcher, editable from the Projects screen, and reproducible on a second deployment,
none of which is true of a path on one host. The resolved snapshot reports which source
answered in its `source` field.

### scalable_units

A Scalable Unit is the group of racks one set of leaf and spine switches serves. Its **size** is a
platform field (`fabric.racks_per_su`, with `leaf_switches_per_su` and `spine_switches_per_su`);
its **membership** is this list.

| Field | Purpose |
|---|---|
| `id` | Slug, unique within the cluster, e.g. `su01`. |
| `label` | Display name, e.g. `SU1`. Falls back to the id. |
| `racks` | Rack ids in this SU. |

Leave the list **empty** and the mapping is derived: racks chunked in inventory order into groups
of `racks_per_su`, keeping a partial final group. State it explicitly when the floor does not
follow that order — Nscale TX 16K numbers SU1–SU14 across two `dh` rows, which no chunking of one
ordered list reproduces.

An explicit list must be a clean partition. A rack appearing in two SUs, a duplicate id, or an SU
with no racks is a validation error on the record, not a warning.


### fabric

Overrides for the on-node fabric scan (`bin/scripts/lib/fabric.inc.sh`). The **platform** says
what the scale-out hardware is (`fabric.scaleout`, rails per GPU, rate); the **site** decides how it
is wired and addressed, and two sites with the same platform can disagree. Every field is blank by
default, which means "detect", and detection is right on every cluster we run today — set a field
only where it is not.

| Field | Values | Exported as | Purpose |
|---|---|---|---|
| `scaleout` | blank, `ib`, `roce` | `NVIS_FABRIC_NETWORK` (`IB`, `RoCE`) | The cluster's fabric network: the protocol its compute fabric runs, which every script uses to decide how to find the interfaces. Shown as **Fabric network** in the cluster form. Blank inherits the platform's `fabric.scaleout`. When a node has no port of the stated protocol but does have the other, the scripts use what the node has and warn. |
| `addr_family` | blank, `ipv4`, `ipv6` | `NVIS_FABRIC_ADDR_FAMILY` | RoCE address family; becomes `NCCL_IB_ADDR_FAMILY`. Blank reads it off the compute GIDs. |
| `compute_hca` | an `NCCL_IB_HCA` value | `NVIS_FABRIC_HCA` | The devices (or name prefix) that are the compute plane, e.g. `roce_vf_r`. Blank detects. Device names or prefixes, comma-separated, optionally led by `^` or `=`; nothing a shell would interpret. |
| `rail_rate_gbps` | `0`–`3200` | `NVIS_FABRIC_EXPECT_RATE` | The line rate each compute rail should train at, in Gb/s (Nscale NC's Spectrum-X VF rails: `200`). `0` = not stated. A platform's `fabric.rail_rate_gbps` describes a family of sites, so it is projected only where this cluster runs the platform's own protocol: an 800G InfiniBand platform's rate is not applied to a RoCE cluster. Where nothing is expected, the network audit holds each RoCE rail to the fastest rail on the node. |

What detection does, so you can tell when it is wrong. A RoCE rail is an ACTIVE port with a
**routable** GID (not zero, not `fe80::`) whose netdev does **not** carry a default route; bonds and
the NVSwitch-attached HCA are never rails, and rate is not used. That is what finds Nscale NC's
compute plane — SR-IOV VFs `roce_vf_r1..4` at 200G with IPv6-only GIDs — and rejects the 400G
`mlx5_12` (the default-route management NIC) and the OVS uplink PFs `roce_r1..4` (no routable GID).
Because an unpinned NCCL would also open those, the scan pins `NCCL_IB_HCA` to the rails' common name
prefix (`roce_vf_r`, the same string on every node) and sets `NCCL_IB_ADDR_FAMILY=AF_INET6`.
InfiniBand is unchanged: IB GIDs sit on `fe80::/64` by design, so IB rails are still chosen by rate
and left to per-rank auto-detect. All three values are projected per cluster in the high overlay, so
`scaleout` beats the platform's default, and they forward over SSH by their `NVIS_` prefix.

### clusterkit

How a ClusterKit launch on this cluster runs, so the choice is made once on the record instead of in
Settings or the launcher. Shown as **ClusterKit** in the cluster form.

| Field | Values | Exported as | Purpose |
|---|---|---|---|
| `tests` | ClusterKit test cases (`bw`, `lat`, `gpu_gpu_bw`, `gpu_gpu_lat`, `alltoall`, `bisection_bw`, ...) | `RACK_CLUSTERKIT_TESTS` | Which tests run on every compute rail. Empty = `bw lat` from host memory. Names are checked against ClusterKit's own list (served to the form by `GET /api/catalog/vocab`, `clusterkit_tests`). |
| `multirack_tests` | ClusterKit test cases | `RACK_CLUSTERKIT_MULTIRACK_TESTS` | Tests for a run across racks, where each rack is a ClusterKit scope. Empty = `bisection_bw` (rack-to-rack bandwidth on every rail). |
| `gpudirect` | true / false | `RACK_CLUSTERKIT_GPUDIRECT=1` | The `gpu_*` tests use GPU-Direct (`clusterkit.sh -G`). On GB300 each rail then runs from its own GPU through Data Direct, ~700 Gb/s, instead of through host memory (~455 Gb/s via Grace). |

Both are in the high overlay, so the cluster's choice beats a global Settings value. For GPU tests the
launcher puts the workspace CUDA runtime on the library path, binds each rank's memory to its rail's
NUMA node without pinning the CPU (a CPU pin on the second Grace socket makes ClusterKit skip its GPU
tests), and fails any rail that asked for GPU tests and wrote no GPU results.

**Across racks.** The Multirack view's **ClusterKit Rack-to-Rack** button (catalog tool
`clusterkit-multirack`, the same `/api/run-clusterkit-async` with `racks: [...]`, two or more) runs on
every placed node of the selected racks -- or an active exact-node campaign's nodes -- and passes each
rack's hosts as `RACK_CLUSTERKIT_SCOPES` (`RACK:h1,h2;RACK:h3,...`). `clusterkit.sbatch` writes them as
ClusterKit's scope file and runs `multirack_tests` on every rail, so `bisection_bw` reports bandwidth rack
against rack under concurrent flows. A rail where a node cannot connect (a silent dead plane breaks
UCX's connection setup for every rank) fails by name and the other rails still run.

### nccl_env

Variables a site adds to every launch that runs NCCL — the NCCL tests (node Stage 1, rack Stage 1,
cross-rack), NeMo (single-rack and cross-rack) and LLMB — chosen by the span the launch covers. It
is where a cluster's own fabric tuning goes, instead of an edit to the shared launchers that every
other cluster also runs. Shown as **NCCL environment** in the cluster form, one `KEY=VALUE` per line.

| List | A launch receives it when it covers | Example |
|---|---|---|
| `node` | exactly one node | `NCCL_DEBUG=WARN` |
| `rack` | one rack's NVLink domain, including part of a rack | `NCCL_MIN_NCHANNELS=16` |
| `multirack` | more than one rack, over the scale-out fabric | `NCCL_NET_PLUGIN=<hpcx>/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so` |

A launch gets **only its own span's list**; the lists do not inherit from one another, so a variable
every span needs is written in each. A partial rack counts as `rack`, not `node`: it is recorded as
node evidence, but NCCL still runs it inside one NVLink domain.

Precedence, lowest first: the cluster's list, then what the operator sets for that one launch (the
NCCL debug level, the step count, the dialog's **Extra environment variables**). Every name from
either source is listed in `NVIS_EXTRA_ENV_KEYS`, which multi-node launchers use to forward the
variables to every rank, not only those on the `mpirun` host.

The same bounds apply as in the launch dialog, because both end in the same job environment: names
match `[A-Za-z_][A-Za-z0-9_]*`, values are a single line of at most 2048 characters, and a list holds
at most 32 variables. A record that breaks them is refused when saved, with the reason. The
validated presets (`/api/validation-presets`) do not take these variables: their recipes are
code-owned and refuse operator environment by design.

## Connection

The simple form is four fields, and it is all a single-host deployment needs:

| Field | Purpose |
|---|---|
| `exec_mode` | **Optional, deprecated — leave unset.** SSH is the default; a stored value is projected to the deprecated `RACK_EXEC_MODE` and logs a warning. Set `local` only when the app runs on the target host itself. |
| `ssh_host` | The head node. |
| `ssh_user` | The account. |
| `ssh_key` | **A path to a private key, never key material.** |
| `remote_base_path` | Where scripts and logs live on that host. Everything else derives from it. |
| `log_dir` | Where this cluster's nodes write the logs Rack Status ingests. Per-cluster on purpose: a multi-tenant deployment reads each project's logs from its own cluster, not from one global directory. Blank falls back to `remote_base_path/var/logs`. Supersedes the deprecated global `RACK_STATUS_LOG_DIR`. |
| `workspace_dir` | The cluster's workspace: toolchains and the LLMB install built or installed on the cluster. Exported as `NVIS_WORKSPACE_DIR`; scripts derive `LLMB_INSTALL` (`<ws>/llmb`), `LLMB_VENV` (`<ws>/llmb_venv`), HPC-X, CUDA and NCCL paths from it via `workspace-dir-common.sh`, and the workspace installers write under it. No built-in default: unset is an error naming this field. Nscale NC: `/home/cmods/workspace`. See [how cluster paths reach scripts](../../explanation/how-cluster-paths-reach-scripts.md). |
| `shared_container_dir` | Where this cluster keeps its shared container images, as the launch host mounts them. Exported as `RACK_VALIDATION_CONTAINER_ROOT`; blank falls back to that setting, then `/cluster/containers`. Only the directory is configurable: preset image basenames and digests stay code-owned. |
| `node_container_dir` | Where each compute node caches container images: staging copies into it (skipping a node that already holds a same-size copy) and jobs run from it. Exported as `NVIS_NODE_CONTAINER_DIR`; blank falls back to that setting, then `/tmp/containers`. Nscale NC uses `/opt/containers` (its nodes have no `/raid`). Must be an absolute path of plain path characters. |

**With P2P enabled** these two fields are the source of truth for NVIS-P2P as well: its `container_path` and `peer_root` (`<node_container_dir>/.nvis-p2p`) derive from them, and the P2P Settings view shows them read-only. Left blank, a P2P cluster inherits its P2P instance's own paths. Because P2P's node scripts publish to `/opt/containers`, a P2P cluster's `node_container_dir` must be blank or `/opt/containers`. Launchers on a P2P cluster never rsync an image from the head: they check each node's copy against the P2P catalog, which Rack Status publishes into the `config/` of the scripts the project runs (`<scripts>/config/image-catalog.{json,tsv}`): `project-<id>/config` once the project is bootstrapped, otherwise the platform layer's or `multi-platform/config` (Nscale NC today). `config/` ships with the scripts holding only its README, both deploy paths ship only that README, and the catalog is published on every catalog save, project script bootstrap and script deploy (or *P2P → Catalog → Publish to scripts*). Size, then SHA256 of the file itself. A missing or mismatched image means running a P2P campaign, and preset/LLMB `--rsync` is refused. This is script execution's own check: the P2P control plane keeps its own manifest copies and receipts to know whether *it* is in sync, and scripts never read those.

### Keys are paths

`ssh_key` holds a filename because that is what `ssh -i` takes. The setup wizard accepts pasted
or uploaded key *material* and writes it to `var/secrets/ssh/<project>-<cluster>.key`, created
owner-only at `0o600` by `os.open` rather than chmod-ed afterwards — an atomic replace installs
the temp file's mode, so correcting it later leaves a window where the key is readable. Only the
path is stored on the record; the material is never logged, returned or served.

### Endpoints and roles, for a split deployment

Some clusters do not have one host that can do everything. Boston is the example: `cmsh` and
Slurm answer on the head node as `cmods`, while `pdsh`, the root-owned `/raid` staging and
`sbatch` run from a different host as `root`.

An **Endpoint** is a place the app can execute:

| Field | Purpose |
|---|---|
| `id` | Referenced by roles. |
| `type` | `local` or `ssh`. |
| `host`, `user`, `key`, `port` | The target. |
| `via` | Another endpoint to hop *through* — a jumphost, expressed as composition rather than as SSH config. |
| `sudo` | Escalate on this endpoint. |
| `opts` | Extra SSH options. |

A **ConnectionRole** is what a call site asks for, decoupled from where that lives:

| Role | What asks for it |
|---|---|
| `control` | `cmsh` and Slurm queries |
| `scripts` | the validation and diagnostic scripts |
| `logs` | reading result logs |
| `bcm` | BCM-specific commands where they are split out |

A call site says "the scripts role", not "that host". That indirection is what lets Boston
declare two endpoints and keep every other deployment on one, and it is why
`role_root("scripts")` — not `RACK_REMOTE_BASE_PATH` — is the authority on where scripts live.

## Authentication, and what the cluster record does not hold

The cluster record holds **how the app reaches the cluster**. It holds nothing about who may use
the app:

- **Operators** authenticate over SRP against the identity store, and every request carries a
  signed token. See [Why authorization is default-deny](../../explanation/why-authorization-is-default-deny.md).
- **Permissions** come from policies attached to users and groups, naming actions from the
  [Action vocabulary](../identity/action-vocabulary.md). A cluster is a *resource* in those
  policies, not a credential store.
- **The SSH key** is the app's credential for the cluster, shared by every operator the app
  authorises. An operator never presents their own SSH key; they present a Greenlight
  credential, and the app uses its own.

That separation is worth being explicit about, because it sets the blast radius: authorising
somebody to run a rack action means authorising them to run it *as the app's SSH identity*, so
per-rack and per-action policy is the only granularity there is.

## Switching the active cluster

One cluster is active at a time, and the switch re-scopes the whole app. It materialises the
record into the environment (naming, geometry, connection, project and platform ids) and drops
the caches that depend on it — the naming cache, the status cache, the job-env cache, the
connection, and the script-layout probe.

That last set matters: the connection and the script layout are cached, and a switch that left
them stale would resolve scripts against the *previous* customer's paths. Everything
cluster-specific must therefore be reachable from these records, which is the same reason a
branch on cluster name inside a shared script is the wrong shape.

## Related

- [Geometry and hostnames](../../explanation/geometry-and-hostnames.md)
- [How a script path is chosen](../../explanation/how-a-script-path-is-chosen.md)
- [Tool documents](tools.md)
