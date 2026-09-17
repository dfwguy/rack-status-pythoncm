---
diataxis: explanation
description: Why a rack id is not a hostname, what geometry is for, and how one example hostname becomes a naming template.
---

# Geometry and hostnames

Two things have to be true before any action can run on a cluster: Greenlight has to know **how
many of everything there is**, and it has to be able to turn a rack the operator clicked into
**hostnames that resolve**. They are separate problems with separate sources, and conflating
them is the single most common way a new deployment fails.

## A rack id is not a hostname

Every cluster has two names for compute, and they are not interchangeable.

| | Where it comes from | Example |
|---|---|---|
| **Rack id** | `cmsh rack list` — the grouping key the floor view shows | `bos01-a05` |
| **Compute hostname** | Slurm and DNS — what `ssh` and `pdsh` must target | `bos01-a05-11-s1-dgx-01-c01` |

The failure this causes is specific and misleading. A naive `<rack>-[01-NN]` derived from the
rack id produces `bos01-a01-[01-18]`, and `pdsh` answers `Name or service not known` — which
reads as a DNS or network fault, days after somebody configured the naming. The hostname is not
derivable from the rack id by pattern; it has to be described.

So the ground truth is always the cluster:

```bash
sinfo -N -h -o %N | head     # the real hostname pattern
getent hosts <one-node>      # confirm it resolves from the execution host
```

`cmsh` is frequently head-node-only, which is why the rack id and the hostname come from
different commands.

## What the templates are

A cluster record carries three templates. They are literal text with **slots** for the numeric
and letter fields:

| Token | Matches | Use it when |
|---|---|---|
| `[01-99]` | a two-digit field, zero-padded | the field is fixed width |
| `[\d+]` | one or more digits | the site spells it variably, as `cmsh` does |
| `[a-z]` / `[A-Z]` | a single letter | the field is a row id |

```
NVIS_RACK_ID   p[\d+]-r[\d+]                 the rack, as the floor view groups it
NVIS_CT_ID     p[\d+]-r[\d+]-ct[\d+]         a compute node
NVIS_NVSW_ID   p[\d+]-r[\d+]-nvsw-[\d+]      an NVSwitch tray
```

**The node template must begin with the rack template.** That is what lets one string answer
both "which rack is this node in" and "what are this rack's nodes", and the catalog refuses a
triplet where it does not hold.

**Prefer `[01-99]` for a fixed-width field.** Consecutive `[\d+]` slots with no separator between
them are genuinely ambiguous — the regex is greedy, so `CN010213` could split as `01|02|13`,
`0102|13` or `010213` and nothing in the string says which. Firmus' `SVR-CN` suffix is exactly
this shape, which is why its template pins the width.

## Why one hostname cannot always settle it

The setup wizard asks for a single example hostname and proposes templates from it. It proposes
rather than decides, and the reason is worth understanding before trusting the result.

Given `ink50-dh01-h15-15` the reading is clear: the last field is the tray, `h` is a row, and the
rack is `ink50-dh01-h15`.

Given `bos01-a05-11-s1-dgx-01-c01` nothing in the string says that `-s1-dgx-` is fixed
infrastructure text rather than data. "The rack is `bos01-a05`" and "the rack is
`bos01-a05-11-s1-dgx-01`" both parse the example perfectly. A heuristic that picked one would be
right on this site and wrong on the next.

Given `CBR01-DH01-SVR-CN010213` the tray could be `13`, `213` or `0213`.

So every candidate the wizard offers has already been compiled and run against the hostname the
operator typed, and the UI leads with what it *parsed* — rack `bos01-a05`, tray `01` — rather
than with the regex. Confirming a parse you can read is a judgement an operator can make;
approving a regex is not.

## What geometry is, and what it is not

Geometry is the count of things, and it comes from the **platform** with the cluster able to
override:

| Field | Meaning |
|---|---|
| `gpus_per_node` | GPUs in one node |
| `nodes_per_tray` | nodes in one tray |
| `trays_per_rack` | trays in a full rack |
| `gpus_per_rack` | the product, stated rather than computed, because a partly populated rack is normal |
| `nvlink_domain` | GPUs in one NVLink domain — the number that decides what "multi-rack" means for a test |

The cluster's own `geometry` adds the floor shape: `pods`, `rows_per_pod`, `racks_per_row`,
`trays_per_rack`.

**Geometry is not in a hostname, and cannot be inferred from one.** A site with trays
`11-18,28-37` — Nscale's shape — has 18 trays in two non-contiguous runs, and no hostname
implies that. It is asked for explicitly, which is why the wizard has a question for it rather
than a derivation.

`nvlink_domain` is the field with the most operational consequence. It is why `mnubergemm` runs
as one job **per rack** rather than one job spanning racks: the test disables InfiniBand and
enables MNNVL, so a single allocation covering two physical racks joins two separate NVLink
domains and hangs. The dispatcher groups the selected nodes by rack and fans out independent
jobs, and refuses if the groups are not a clean partition of the reviewed target.

## The Scalable Unit — the level above a row

A rack is not the largest unit that matters. A **Scalable Unit (SU)** is the group of racks that
one set of leaf and spine switches serves: the fabric's quantum. It is the number that decides how
a floor is drawn, how racks are grouped for acceptance, and how much of a cluster a single fabric
fault can take with it.

**Its size is a property of the platform, not of the site**, because it is the switch count that
bounds it. A GB300 NVL72 SU and a VR200 SU are different sizes, and a newer generation reaches
further — VR200 spans more racks per SU than NVL72 does. So SU sizing sits on the platform's
`fabric`, beside the scale-out fields, rather than in geometry:

| Field | Meaning |
|---|---|
| `racks_per_su` | racks one leaf/spine group serves. **`0` means this platform has no SU model** |
| `leaf_switches_per_su` | expected leaf count in one SU; `0` is unknown |
| `spine_switches_per_su` | expected spine count in one SU; `0` is unknown |

`gpus_per_su` is deliberately **not** stored — it is `racks_per_su × gpus_per_rack`, and a second
copy of a derived number is a second thing to keep in step.

The seeded NVL72 platforms carry `racks_per_su: 8`, which is 576 GPUs. That figure is
corroborated rather than assumed: Nscale TX 16K is 112 racks in 14 SUs. VR200 is deliberately left
at `0` until its reference architecture settles the number, because an invented value would draw a
floor plan that is confidently wrong, while `0` makes callers fall back to ungrouped racks.

### Which racks are in which SU

The cluster answers that, and it may answer explicitly. When `scalable_units` is empty the
mapping is **derived** — racks chunked in inventory order into groups of `racks_per_su`, keeping a
partial final group, because a half-populated SU is normal mid-bring-up.

Derivation is right for a tidy floor and wrong for a real one, which is why the explicit form
exists and why it wins. Nscale TX 16K numbers SU1–SU14 across `ink50-dh01` rows a–c *and*
`ink50-dh02` rows a–k; no chunking of one ordered list reproduces that. A site that has written
its own mapping down knows something the arithmetic cannot compute.

An explicit map must be a **clean partition**: a rack in two SUs is refused at the record, not
warned about. The reason is the same one that makes `mnubergemm` one job per rack — a rack counted
twice double-counts on the floor view, and anything that ever fans out per SU would send two jobs
to one rack.

## Where each value ends up

The active cluster's naming and geometry are materialised into the environment every request, so
scripts and launchers see them without reading the catalog:

- naming → `NVIS_RACK_ID`, `NVIS_CT_ID`, `NVIS_NVSW_ID`, `NVIS_CT_JOINER`
- geometry → the platform's low overlay, beneath anything an operator set
- SU shape → `NVIS_SU_RACKS`, `NVIS_SU_GPUS`, `NVIS_SU_LEAF_SWITCHES`, `NVIS_SU_SPINE_SWITCHES`,
  published **only** when the platform models an SU, so a script can tell "no SU model" from "an
  SU of one rack"
- identity → `NVIS_PROJECT_ID`, `NVIS_PLATFORM_ID`, `RACK_CLUSTER_NAME`

That is also why switching the active cluster changes which scripts run: the project id in that
overlay is what selects a project's own copy of the script tree.

## Related

- [Projects and clusters](../reference/catalog/projects-and-clusters.md) — the fields these live on.
- [How a script path is chosen](how-a-script-path-is-chosen.md) — what the project id selects.
- `backend/nvis_id_templates.py` — the template syntax and the four deployment families it was
  built against.
