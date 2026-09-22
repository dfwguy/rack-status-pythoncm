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

## Connection

The simple form is four fields, and it is all a single-host deployment needs:

| Field | Purpose |
|---|---|
| `exec_mode` | `ssh` for a real cluster, `local` when the app runs on the target host itself. |
| `ssh_host` | The head node. |
| `ssh_user` | The account. |
| `ssh_key` | **A path to a private key, never key material.** |
| `remote_base_path` | Where scripts and logs live on that host. Everything else derives from it. |

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
