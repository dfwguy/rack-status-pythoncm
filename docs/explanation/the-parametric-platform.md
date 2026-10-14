---
diataxis: explanation
description: The architecture that lets one Rack Status serve many different clusters -- the foundation records and resolvers every feature is built from, how a site's facts reach the UI and the scripts, and the rules for adding to it.
---

# The parametric platform

Rack Status runs the same code and the same scripts against clusters that share almost nothing:
GB200 and GB300, InfiniBand and Spectrum-X RoCE over IPv4 or IPv6, BCM-placed racks and fleets with
no placement at all, Slurm and pythoncm, hostnames that encode the rack and hostnames that do not.
Nothing in the product is allowed to know which of those it is talking to. Every difference is a
**parameter**, stated once on a record and carried from there to the page that draws it and the
script that acts on it.

This page is the map of that design: the small set of foundation components everything else is
built from, how a site's facts travel through them, and what to do when a new difference appears.
The detail of each piece lives in its own page; this one shows how they fit.

## Why it is built this way

Each site that adopted Rack Status arrived with a fact the code had quietly assumed was universal,
and each assumption failed the same way: correct on the cluster it was written on, silently wrong on
the next one.

| The assumption | Where it broke | What it did |
|---|---|---|
| A rack is the hostname minus its last `-` part | Nscale NC (`…-gpu100-su20`) | Grouped the same node number across 37 SUs as one "rack" |
| Compute rails are the fastest RDMA devices | Nscale NC | Picked the 400G management NICs instead of the 200G `roce_vf_r*` rails |
| Every rail runs at 800 Gb/s | Nscale NC | Failed every healthy node's network audit and drained one |
| The fabric is InfiniBand | Nscale NC | Found no rails; NCCL opened 26 devices and failed with `GID index -1` |
| Logs, toolchains and images live at fixed paths | Every new site | A bring-up drained on `mkdir: cannot create directory '/cluster'` |
| Every node has a rack | Reno (RTX 6000 pool) | Drew empty rack rows for a cluster with no placement data |

The fix in every case was not a better default. It was moving the fact out of the code and onto the
record of the cluster it belongs to, so the next site states its own answer instead of inheriting
the last one's.

## The foundation

Eight components are the foundation. They are not features; they are what features are made of.
A feature that needs a site fact, a host, a hostname, a script or a scheduler asks one of these,
never works it out for itself.

```{mermaid}
flowchart TB
    subgraph REC["Records — what is true about a site"]
        direction LR
        PLAT["Platform<br/><i>what the hardware is</i><br/>geometry · fabric shape · SU size<br/>tool_param_defaults"]
        PROJ["Project<br/><i>a customer engagement</i>"]
        CLU["Cluster<br/><i>one deployment</i><br/>connection & paths · naming templates<br/>rack_mapping_mode · scalable_units<br/>fabric · nccl_env · scheduler · campaigns"]
        PROJ --> CLU
        PLAT -. "defaults a cluster may override" .-> CLU
    end

    subgraph RES["Resolution — one answer per question"]
        direction LR
        CFG["Configuration resolution<br/>high overlay › Settings › env › low overlay<br/><code>_eget</code> · <code>_subprocess_env</code>"]
        NAME["Naming<br/>rack id ≠ hostname<br/><code>nvis_id_templates</code>"]
        TREE["Script tree<br/>project › custom › platform › default"]
    end

    subgraph RUN["Reach — how anything touches a cluster"]
        direction LR
        TRANS["Transport<br/><code>host_exec</code> · hops · fan-out<br/>env forwarded by prefix"]
        PROV["Providers<br/>cluster manager: BCM · MAAS · pythoncm<br/>scheduler: Slurm · pythoncm · local"]
    end

    REC --> RES
    RES --> RUN
```

**Records.** Three catalog documents, edited in the UI and over the API
([projects and clusters](../reference/catalog/projects-and-clusters.md)):

- A **Platform** says what the hardware *is*: GPUs per node, trays per rack, the NVLink domain, the
  scale-out fabric it was designed for (protocol, rails per GPU, rail rate), how many racks make an
  SU, and `tool_param_defaults` for its tests. It describes a *family* of sites.
- A **Project** is a customer engagement; it groups clusters.
- A **Cluster** is one deployment, and holds everything that is true of that site alone: how to
  reach it and where its files live, its naming templates, how its nodes map to racks
  (`direct`, `translate` or `pool`), its Scalable Units, its fabric where it differs from the
  platform, the NCCL variables its launches need, its scheduler, and its acceptance campaigns.

**Resolution.** Three components turn records into one answer:

- **Configuration resolution** merges the active cluster (the *high overlay*), the Settings page,
  the process environment and the platform (the *low overlay*), in that order of precedence.
  Every knob is read through `_eget` and its siblings, never `os.environ`, or it silently stops
  being per-cluster.
- **Naming** turns a rack the operator clicked into hostnames that resolve, from the cluster's
  templates and the provider's placement, never by string surgery
  ([geometry and hostnames](geometry-and-hostnames.md)).
- **The script tree** decides which copy of a script runs, so a project can carry its own edits
  without touching the shipped set ([how a script path is chosen](how-a-script-path-is-chosen.md)).

**Reach.** Two components touch the cluster:

- **Transport** runs every command, locally or over SSH, through the head or directly to a node,
  with bounded fan-out, and forwards `NVIS_*` / `RACK_*` variables so the resolved configuration
  arrives on the far side (`docs/EXECUTION_ARCHITECTURE.md` has the detail).
- **Providers** hide which cluster manager and which scheduler a site runs, behind one interface
  each. A new one is a provider, never an `if` in a caller.

## What is built on it

Everything a user sees is a composition of the foundation. None of these components holds a site
fact of its own.

```{mermaid}
flowchart TB
    subgraph F["Foundation"]
        direction LR
        R["Records<br/>Platform · Project · Cluster"]
        C["Configuration<br/>resolution"]
        N["Naming"]
        S["Script tree"]
        T["Transport"]
        P["Providers"]
    end

    INV["Inventory & placement<br/><i>which nodes, in which rack or pool</i>"]
    SNAP["Cluster snapshot<br/><i>one cached answer, shared by every worker</i>"]
    LAUNCH["Launchers<br/><code>bin/scripts</code> + <code>lib/*.inc.sh</code><br/><i>read NVIS_* — never a literal</i>"]
    WF["Workflows & fan-out<br/><i>durable runs, audits, sweeps</i>"]
    PIPE["Results pipeline<br/><i>log scan → verdicts → metrics</i>"]
    STAGE["Stages<br/><i>node & rack Stage 1 / Stage 2</i>"]

    UI["Pages<br/>Racks · Nodes · Post-run report<br/>Analytics · Deployment tracker"]
    API["API & CLI<br/><code>/api/*</code> · <code>nvrs</code>"]
    ROUT["Fleet routines<br/><i>hourly validation, doctors</i>"]

    P --> INV
    N --> INV
    R --> INV
    INV --> SNAP
    C --> LAUNCH
    S --> LAUNCH
    T --> LAUNCH
    LAUNCH --> WF
    WF --> PIPE
    PIPE --> STAGE
    SNAP --> STAGE
    SNAP --> UI
    STAGE --> UI
    R --> UI
    STAGE --> API
    SNAP --> API
    API --> ROUT
```

Two things to notice. The **pages** read placement from the snapshot and geometry from the records
rather than parsing hostnames, so the Post-run report groups nodes exactly as the Racks page does,
and a pool cluster gets one pool card in both. And the **launchers** never name a site: a
launcher that needs the fabric, a path or an image directory reads a variable the configuration
resolution put in its environment, through a shared shell library.

## How a site's fact reaches a script

One value, end to end. Nscale NC runs Spectrum-X RoCE on GB300, while its platform record describes
the InfiniBand family.

```{mermaid}
flowchart LR
    A["Cluster record<br/>fabric.scaleout = roce<br/>fabric.rail_rate_gbps = 0"] --> B["High overlay<br/>NVIS_FABRIC_NETWORK=RoCE"]
    P2["Platform record<br/>fabric.scaleout = ib<br/>rail_rate_gbps = 800"] --> L["Low overlay<br/>platform rate dropped:<br/>cluster runs another protocol"]
    B --> E["Launch environment<br/><code>_subprocess_env</code>"]
    L --> E
    E --> X["Transport<br/>NVIS_* forwarded over SSH"]
    X --> SH["<code>lib/fabric.inc.sh</code><br/>nvis_fabric_network → RoCE<br/>scan: rails = roce_vf_r1..4"]
    SH --> J["NCCL_IB_HCA=roce_vf_r<br/>NCCL_IB_ADDR_FAMILY=AF_INET6<br/>audit expects 200 Gb/s"]
```

The same path carries every other site fact: `connection.log_dir` becomes `NVIS_LOG_DIR`,
`workspace_dir` becomes `NVIS_WORKSPACE_DIR`, the image directories become
`RACK_VALIDATION_CONTAINER_ROOT` and `NVIS_NODE_CONTAINER_DIR`, `rack_mapping_mode` becomes
`NVIS_RACK_MAPPING_MODE`, and a launch receives the cluster's `nccl_env` list for the span it runs
over (node, rack or multirack) with its own options winning. The detail of each root is in
[how cluster paths reach scripts](how-cluster-paths-reach-scripts.md).

### A platform default is an expectation, not a fact

A platform describes a family of sites, so its values are what a site *should* have, and the node
gets the last word on what it *does* have. Two rules follow, and both were learned on NC:

- When a cluster states a different fabric protocol from its platform, the platform's rail count
  and rail rate are not projected: an 800 Gb/s InfiniBand expectation means nothing to 200 Gb/s
  RoCE rails. The cluster states its own with `fabric.rail_rate_gbps`.
- When the stated protocol and what the node exposes disagree, the scripts use what the node has
  and say so, rather than failing to find any rail.

## Adding to it

When a new site exposes a difference, the question is never "how do I handle this site" but "which
foundation component owns this fact".

| The new thing | Where it goes | Not |
|---|---|---|
| A fact about one site (a path, a template, a fabric override, a roster) | A **Cluster** field, wired through the API and an editable control in the UI | An environment variable, a branch on the cluster name |
| A fact about a hardware family (GPUs per node, SU size, a test's default) | A **Platform** field or `tool_param_defaults` | A constant in a module |
| A deployment-wide knob (a timeout, a cache TTL) | The **Settings registry**, read through `_eget` | `os.environ.get` |
| A new cluster manager or scheduler | A **provider** | An `if` in a caller |
| A path a script needs | Derived from a cluster root by the shared shell library | A literal in the script |
| A new page or report | Composed from the **snapshot**, the **records** and the **stages** | Re-deriving placement or hostnames in the browser |

A feature built this way works on the next cluster with no code change, because the next cluster
brings its own record. A feature that reaches past the foundation works on exactly one.

## Related

- [Projects and clusters](../reference/catalog/projects-and-clusters.md) — every field on the records.
- [How cluster paths reach scripts](how-cluster-paths-reach-scripts.md) — the path roots and their journey.
- [The cluster home directory](the-cluster-home-directory.md) — consolidating the roots under one home.
- [Geometry and hostnames](geometry-and-hostnames.md) — naming templates, geometry and the SU.
- [How a script path is chosen](how-a-script-path-is-chosen.md) — the layered script tree.
- `docs/EXECUTION_ARCHITECTURE.md` (in the repository) — transport, hops and environment forwarding.
- [Settings reference](../reference/configuration/settings.md) — every deployment-wide knob.
