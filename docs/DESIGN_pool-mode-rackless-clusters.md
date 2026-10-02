# Design note — rendering clusters that have no placement data (`pool` mode)

**Status:** implemented on v3.1 (2026-10-01). **Author:** Rafael M. Koike (with Claude).
**Scope:** `backend/native_status.py`, `backend/catalog/models.py`, `backend/catalog/context.py`,
`frontend/src/{types,views,components}`.

## Implementation (landed)

- **`pool` mode:** `RACK_MAPPING_MODES` now `("direct","translate","pool")`
  ([catalog/models.py](../backend/catalog/models.py)); `Cluster.validate` accepts `pool` and
  rejects `pool` + `scalable_units` (a contradiction). `native_status._rack_mapping_mode` honours
  `pool`; new `_hostname_encodes_rack()` (true only for `direct`) gates the hostname rack-parse so
  `pool` never invents a rack. `rack_mapping_mode` is emitted in the status payload.
- **Shape = scheduler, one call:** `collect_sinfo_nodes_and_shapes()` reads `-o '%N %t %G'`, so GRES
  rides the existing inventory call (no second sinfo). `parse_gres_gpu` turns `gpu:nvidia:8(S:0-1)`
  into `{gpu_count,gpu_type}`. Non-Slurm clusters fall back to `sample_node_gpu_shapes_via_smi`
  (one `nvidia-smi -L` per BCM category, up nodes only, TTL-cached). `build_node_inventory` attaches
  `gpu_count`/`gpu_type`; the worker refreshes shapes via `_refresh_node_shapes`.
- **Frontend:** `NodeInventoryEntry` carries `gpu_count`/`gpu_type`; `rack_mapping_mode` adds
  `"pool"`. New `NodePoolView` renders the honest flat pool (banner + hostname-ordered tiles with GPU
  counts); `SingleRackView` branches to it in pool mode. `NodesView` gains a GPUs column;
  `ProjectsView` exposes `pool` in the rack-mapping Select and hides naming templates for it.
- **Tests:** backend `test_node_status_inventory.py` (GRES parse, shape flow, pool, nvidia-smi
  sample) + `test_catalog.py` (pool validation); frontend `NodePoolView.test.tsx`. Full suites green
  (backend 4564 passed; frontend 691 passed). Verified via unit tests + `tsc` — a live pool cluster
  (Reno) was not available locally to exercise the render in the browser.

---

**Original proposal follows.**

## The problem, from a real cluster

The **Akamai / Reno RTX 6000** cluster (`bcm-rno-01`) renders *nothing* in Rack Status —
`0 racks total`, blank Floor, empty Multirack. This is not a bug in the poll; it is the
rack-first data model meeting a cluster that has no placement data at all.

What we confirmed on it (2026-10-01):

| Source | Command | Result |
|---|---|---|
| BCM rack objects | `cmsh -c "rack; list"` | **empty** — no racks configured |
| BCM device rack attr | `foreach -t physicalnode (get hostname; get rack; get category)` | `rack` **empty** on every node; category `rtx6000` |
| Slurm GRES (node shape) | `sinfo -N -o "%n %G"` | `gpu:nvidia:8(S:0-1)` — **8 GPUs/node, known** |
| Slurm partitions | `scontrol show node` | `Partitions=defq` — single partition, no grouping |
| Slurm features | `scontrol show node` | `Features=location=local` — no rack/row |

~330 homogeneous nodes `gpu-rno-248..601` (sparse), each 8× RTX 6000, dual-socket, ~1.28 TB RAM.

**The node *shape* is fully known; the *placement* was never entered in BCM.** No command can
reveal geometry that was never recorded. This is a legitimate cluster *shape* — a flat pool —
distinct from NC GB300 (`translate`) and rack-in-hostname clusters (`direct`).

## Root cause: two things are conflated, and the render path is rack-first

Rack Status today sources both of these from the same `rack; list`:

1. **Placement** — *where* a node sits (rack / row / slot). The floor map.
2. **Node shape** — *what* a node looks like (how many GPUs to draw in the tile).

Both `direct` (hostname template) and `translate` (provider `get rack` + `scalable_units`) are
**placement** mechanisms — see `RACK_MAPPING_MODES` in
[catalog/models.py:55](../backend/catalog/models.py). Node shape has never been a first-class
input; it is effectively implied by the cluster's category (GB300 ⇒ 8-GPU trays) and hardcoded.

And the render path is rack-first: [`build_native_rack_status`](../backend/native_status.py:2239)
calls `refresh_racks_blocking()` → [`_cmsh_rack_list`](../backend/native_status.py:580). When that
returns `[]`, [`build_table_rows`](../backend/native_status.py:2259) has nothing to build, and
every downstream view (Racks, Floor, Multirack) is empty. **The rack-first assumption is itself
part of the problem**, not just the missing `pool` enum value.

The good news: the node roster is *already* decoupled.
[`build_node_inventory`](../backend/native_status.py:945) owns the roster from the scheduler /
cluster-manager provider, takes rack membership as **optional enrichment**, and already
*"leaves an unplaced node ungrouped rather than mis-grouped."* `pool` mode leans on this existing
seam — it does not invent a parallel one.

## Proposal — three distinct pieces, correctly scoped

The mistake would be a single blurry "geometry via Slurm" flag. Keep the axes separate:

### 1. Node-shape enrichment — a provider capability, not a Slurm-specific call

Per-node GPU count is "tell me each node's shape," and the *source* depends on what's managing the
cluster. Model it as one capability on `PlatformProvider`
([base.py:52](../backend/platforms/base.py), next to `sinfo_cmd` / `pdsh_cmd`) —
e.g. `node_gpu_shape() -> dict[str, GpuShape]` — with per-provider implementations resolved in
priority order (cheapest, most central first):

1. **Slurm scheduler present → `sinfo -N -h -o "%n %G"`** (parse `gpu:<type>:<N>`). One cheap
   central call on the head; covers every node including DOWN ones (GRES is config, not probed).
   This is the Reno path today.
2. **BCM / PythonCM carries GPU inventory → read it centrally** (`cmsh` category / device sysinfo,
   or the pythoncm API). Prefer this over probing when the cluster manager already knows the shape.
3. **Fallback: `nvidia-smi` fan-out from the nodes** (`nvidia-smi -L` or
   `--query-gpu=count --format=csv,noheader` via `pdsh_cmd`). Ground truth, but **per-node remote**
   — the expensive path. Use it only when neither 1 nor 2 can answer (e.g. PythonCM-only, no Slurm).

The three differ in cost and failure mode, so the `nvidia-smi` path needs guardrails the `sinfo`
path does not:

- **Sample, don't sweep.** For a homogeneous pool (Reno: all `rtx6000`, all 8 GPU) probe **one UP
  node per BCM category** and apply the result to that category, instead of fanning out to all ~330
  nodes. BCM category is the natural key; verify the homogeneity assumption and fall back to
  per-node probing only for mixed categories.
- **Tolerate DOWN / unreachable nodes.** Reno (and NC mid-install) have many non-UP nodes;
  `nvidia-smi` can't reach them. Sampling a known-UP node per category sidesteps this.
- **Cache with a TTL.** Shape changes only on hardware changes — cache aggressively (reuse the
  existing native-status cache pattern) so a per-node probe isn't repeated every poll.

Shared contract regardless of source:

- **API:** add `gpu_count` (and optionally `gpu_type`) to the node-inventory record
  (`node_inventory_snapshot`); draw N GPU cells from it in **every** mode (fixes Reno's blank tiles
  *and* stops NC hardcoding tray width).
- **No new flag** — this is inventory data that flows unconditionally.
- **Fallback:** when no source can answer, leave `gpu_count` unset and draw the current generic tile.

### 2. `pool` placement mode — the 3rd `rack_mapping_mode`

Add `"pool"` to `RACK_MAPPING_MODES` ([models.py:55](../backend/catalog/models.py)) alongside
`direct` / `translate`. Semantics: **no placement source exists; lay nodes out by hostname numeric
order, wrapped into rows of N.** The only geometry decision it makes is the *ordering key*
(hostname number), which needs no external data and degrades gracefully across the gaps
(missing 257, 261, …).

Plumbing already exists for the other two modes and must learn `pool`:

- [models.py:1040](../backend/catalog/models.py) `Cluster.rack_mapping_mode` default (unchanged;
  `pool` is opt-in per cluster).
- [context.py:196](../backend/catalog/context.py) already emits `NVIS_RACK_MAPPING_MODE` — no change.
- [`_rack_mapping_mode`](../backend/native_status.py:183) currently collapses **anything** that is
  not `translate` to `direct`. This is the critical edit: it must recognize `pool` as a third value
  rather than silently folding it into `direct`.

### 3. Node-first render path + honest empty state

Because `_cmsh_rack_list` returns `[]` for a rack-less cluster, `pool` needs a render path that
builds the view from the **node inventory** (`build_node_inventory` + the GRES shape from §1),
**bypassing** `rack; list` and `build_table_rows`. Nodes are the atom; there is no rack row.

- **UI:** render a flat grid of node tiles, ordered by hostname, wrapped into rows of N — the same
  wrapping already introduced for the node-count bar in commit `99d1cd5`. Group them under a
  container **explicitly labeled not-a-rack** ("Unplaced nodes" / "Reno pool — no rack mapping").
- **Do NOT** draw one giant "rack" card holding all nodes: it asserts a false physical fact
  (330 nodes ≠ one rack) and does not scale as a single card.
- **Rack / Floor / Multirack views** keep rendering, but when the active cluster is `pool` (or any
  cluster yields zero racks) they must show an **explicit empty state** — "No rack placement data
  in BCM for this cluster" — not silent emptiness. The current blank is itself a UX defect: an
  operator cannot tell "empty by design" from "backend down."
- **SetupWizard** ([SetupWizard.tsx](../frontend/src/components/setup/SetupWizard.tsx)) exposes the
  mode as an editable control (per the feature = API + UI rule), with `pool` selectable and its
  implication described.

### Out of scope (YAGNI)

**Slurm `topology/tree` as a 4th placement source.** It would be real geometry *if* a cluster had
`topology.conf` populated — but Reno is `topology/none`, and building this path now would be dead
code with no cluster behind it. Add it only when a cluster actually ships a switch tree. Likewise
the BCM `ethernetswitch` / IB-fabric wiring proxy: valuable if ever populated, but unconfirmed and
unbuilt until a cluster has it.

## What this preserves

- **Shape and placement stay decoupled.** The day someone sets `rack`/`rackindex` in BCM (or hands
  us a DCIM export), the *same* Reno cluster flips to `translate` with **no rework** — the tiles
  already know their shape; only the placement source changes.
- **Build on the first-class citizens.** `pool` reuses the existing scheduler provider, the
  node-inventory roster that already tolerates unplaced nodes, and the `rack_mapping_mode`
  resolution path — it is built *on* them, not beside them.
- **Honest rendering.** Reno is flat *because the placement data was never entered*, not because it
  has no racks. The UI should say exactly that, rather than fabricate a rack or show nothing.

## Open questions for review

1. Row width N for `pool` wrapping — fixed (e.g. 8/16), responsive to viewport, or a per-cluster
   geometry field?
2. Should `pool` be **auto-detected** (zero racks + non-empty node roster ⇒ fall back to pool
   rendering) or strictly **opt-in** via the wizard? Auto-fallback fixes the blank for any
   misconfigured cluster; opt-in keeps the mode explicit. Recommendation: auto-fallback *rendering*
   (never show blank) but keep `pool` an explicit stored mode for intent.
3. `gpu_type` surfacing — Slurm reports generic `gpu:nvidia`; the real model (`rtx6000`) is BCM's
   category. Which do we show on the tile?
