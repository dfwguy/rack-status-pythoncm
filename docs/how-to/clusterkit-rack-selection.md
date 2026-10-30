---
diataxis: how-to
---

# How to choose a rack for ClusterKit validation

## 1. Select a rack

Open **Tools → ClusterKit**. Search the rack list or use **Floor** to select a rack.

Check mapped-node counts, availability and the inventory update time. A rack with
   **No placement** cannot be tested. **Not enabled** means the site profile does not
   permit testing there.
## 2. Choose a subset or whole rack

Use **Selected size** for preflight or a configured subset. Choose **1** for preflight
or an enabled pairwise size, normally **2**, **4** or **8**. Use **Rack** to choose
that many eligible idle members, or **Exact nodes** to select them yourself.

Choose **Coverage → Whole rack** to test every node in the selected authoritative
rack mapping. All members must be permitted, idle and in the configured partition.
A busy, unavailable or missing member blocks preview and confirmation; no smaller
subset or replacement node is chosen. Odd-sized racks are unsupported by the current
ClusterKit pairwise mode and are blocked.

For an 18-node rack, preview must show exactly 18 nodes and 153 expected pairs.
Review the wall-time bound before confirming. The CPU/RoCE recipe does not allocate
GPUs, and functional success does not certify performance thresholds.
## 3. Preview and confirm

Select **Preview plan**. Review the exact nodes, resource limits, warnings and pair count.
   Preview reserves no nodes and creates no result attempt.

Confirm the displayed plan to submit once. Follow progress and review the existing
   results, diagnostics, artifacts and history. Functional status and performance acceptance
   remain separate; unverified thresholds produce **UNVERIFIED** performance.

Select **Refresh** to reload inventory and current selected-rack Slurm availability.
Changing rack, size, selection or refreshing discards an old preview. Availability can
change before scheduling.

## Enable the shared rack directory

Add the following field to the existing administrator ClusterKit profile in the selected
cluster's catalog overlay. Retain the existing HPC-X, HCA, results and Slurm settings.

    {
      "rack_inventory": {
        "snapshot_db": "/absolute/path/to/cluster_snapshots.sqlite3",
        "cluster_id": "catalog:PROJECT/CLUSTER",
        "log_root": "/absolute/selected/source/log/root",
        "checkpoint_only": true,
        "max_age_seconds": 300
      }
    }

Use the exact source identity and log root of Rack Status's canonical inventory.
Keep the node allowlist or regex scoped to nodes approved for testing. Directory
visibility does not grant execution permission. Do not point this application's state,
identity or catalog roots at another deployment's state.

The reader uses only an existing immutable SQLite checkpoint, never initializes a
database or starts a collector, and may lag newer WAL records. An unavailable, changing
or stale checkpoint blocks new previews and confirmations. Refresh or repair the
source through its normal owner; do not loosen its file permissions.

Physical membership comes from canonical translate/direct mappings. Hostnames do
not establish physical rack membership. Rack rows without mapped members remain visible.
Existing manually configured topology groups and legacy ClusterKit routes remain supported
when the rack inventory field is omitted.

## Enable whole-rack validation

Set **whole_rack_enabled** to true in the existing administrator site profile and
raise **max_nodes** only to the approved rack size. Keep the existing node allowlist,
partition, HPC-X/HCA settings and time bounds. The flag defaults to false, preserving
existing deployments. Authoritative manual rack groups and the canonical inventory
are both supported.

The backend supports bounded even pairwise counts from 2 through 64. This is a
software bound, not a live qualification of every size. **allowed_node_counts** still
controls subset requests; raising max_nodes or enabling Whole rack does not enable
additional subset sizes automatically. Ordinary requests cannot override site settings.

The whole-rack request contract is scope **rack**, the configured rack/group name, and
node_count equal to every mapped member. Existing preflight/explicit/group requests
and static execution entrypoints are retained.
