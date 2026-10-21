# ClusterKit backend API

This is the product-facing API for the configuration-driven ClusterKit workflow.
It is separate from the legacy single-rack and Nscale suite launch routes.

## Workflow

1. An administrator enables `RACK_CLUSTERKIT_SITE_PROFILE` through the existing
   Settings/catalog hierarchy. The profile owns paths, partition, node limits,
   topology groups, HCA/rail policy, test parameters, and time bounds.
2. Any authenticated Rack Status user can inspect `GET /api/clusterkit/readiness`
   and the configured Slurm inventory at `GET /api/clusterkit/inventory`.
3. `POST /api/clusterkit/preview` accepts only product-level selection fields.
   It performs a read-only Slurm query and returns a print-only plan plus a
   short-lived signed confirmation token. Preview creates no files or jobs.
4. `POST /api/clusterkit/runs` requires `confirm: true` and the token. It resolves
   current policy and inventory again, rejects changed policy/selection, claims a
   new attempt directory exclusively, snapshots execution sources, records a
   durable validation activity, and submits one reviewed static sbatch file.
5. `GET /api/clusterkit/runs/<activity_id>` reconciles durable state with Slurm
   and returns the normalized summary. Completed jobs receive a bounded collection
   grace period for shared-filesystem visibility; missing or failing normalized
   evidence fails closed.
6. The two artifact routes list and retrieve bounded, non-symlink files beneath
   that attempt only, including raw vendor output under stage directories.

## Request shape

Preflight:

```json
{"scope":"preflight","nodes":["node1"],"node_count":1,"label":"preflight"}
```

Explicit pairwise:

```json
{"scope":"explicit","nodes":["node1","node2","node3","node4"],"node_count":4,"label":"u5-4"}
```

Configured group selection:

```json
{"scope":"group","group":"u5","node_count":8,"label":"u5-8"}
```

Deployment fields are never accepted in a run request. The planner supports one
preflight node and administrator-enabled pairwise sizes from 2, 4, and 8 nodes.

## Safety and recovery

- Rack Status keeps its existing shared-access model; this feature adds no roles.
- Confirmation tokens are HMAC-bound to the previewed request, nodes, attempt,
  plan, and complete effective site profile. They expire after five minutes by
  default (`RACK_CLUSTERKIT_PREVIEW_TTL_SEC`, bounded to 30-900 seconds).
- Exclusive creation of the attempt directory is the cross-process duplicate
  submission guard. A consumed or partially failed confirmation cannot be reused.
- Submission uses the existing Slurm scheduler abstraction with literal script
  arguments. It does not use generated shell, `--wrap`, SSH fanout, GPU resources,
  or automatic retries.
- Validation activities and scheduler references survive application restarts.
  Status polling reconstructs state from that durable record and the attempt's
  immutable `summary.json`.
- `RACK_CLUSTERKIT_COLLECT_GRACE_SEC` controls the bounded wait for artifacts after
  Slurm completion (default 300 seconds, bounded to 30-1800).
- Functional status and performance status remain separate. Performance remains
  `UNVERIFIED` until the fabric team supplies approved acceptance thresholds.
