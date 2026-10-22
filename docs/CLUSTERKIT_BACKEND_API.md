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

## AppServer review fixes (2026-10-21)

The modular API is discovered through `backend/api/clusterkit.py`. The old
`clusterkit_api` import remains an alias; legacy single-rack and suite URLs keep
working. Shared Rack Status authentication remains the only access gate.

Preview confirmations now use version 2. They bind the effective profile, exact
nodes, stable execution digest, reviewed source/HPC-X init fingerprint and local
execution identity. Volatile inventory timestamps and busy/idle observations are
revalidated rather than signed as execution settings. A fresh preview is required
when source, policy, target, execution user or arguments change. Earlier tokens
are rejected. Preview reports both the native `sbatch_argv` and the complete
`submission_command_display` including the pinned platform wrapper.

The planner and scheduler share one argv renderer. Each attempt has a unique job
name and `rack-clusterkit:<attempt_id>` comment for bounded recovery. Inherited
`SBATCH_*` overrides are removed from the modular execution transport.

### Local execution boundary

This release supports only an unprivileged local control/scripts/logs context.
Remote staging and result collection are not implemented; SSH or sudo-decorated
roles are rejected before submission. Existing connection factories, read-only
catalog snapshots, configuration precedence and role roots are reused. The API
must run as the site's intended validation account; it does not switch user or
escalate privileges. No new role or permission model is introduced.

Execution identity and Slurm command/environment selection are stored in each
attempt. Access after changing active cluster, host, user, script location or
Slurm selection fails closed until the original context is selected. Status uses
pinned provider commands, not the current cluster's numeric job namespace.

### Submission evidence and uncertain acceptance

An existing result directory is never reused. A nonblocking filesystem lock
serializes submission decisions across processes; contention returns a conflict
without contacting Slurm. The attempt records:

- `submission-intent.json`: attempt/activity/task identities, context, plan and
  recipe digests, exact command and unique recovery tag.
- `submission-started.json`: fsynced before the one scheduler submission call.
- `submission-receipt.json`: accepted job reference, fsynced before mutable
  activity tracking updates.
- `submission-error.json`: an ambiguous submission error, when available.

A tracking failure never cancels an accepted job. A lost/error response returns
`submission_state: SUBMISSION_UNKNOWN`, an activity ID and warnings; it does not
retry. A fresh token cannot submit overlapping nodes while prior acceptance is
unknown. Nonzero scheduler errors are conservatively treated as unknown rather
than proof that no job was accepted.

Status can recover a missing receipt by a bounded read-only lookup of the exact
job name/comment/user/start window in `squeue` and `sacct`. No match remains
unknown; multiple matches or corrupt evidence require operator review. No retry
or cancellation occurs. Missing activity state can be rebuilt from a unique
existing durable intent in the configured result root, with a 512-entry search
bound. Partial/corrupt existing records are preserved and fail closed rather than
being overwritten. A prepared attempt with no submission-start marker has not
been automatically submitted during recovery.

### Results and artifact boundaries

COMPLETED alone is insufficient. Passing summaries must match the attempt, plan,
job, exact nodes and effective configuration, and contain successful primary and
preflight exits, unique per-node GID/placement evidence, required stages, and all
expected bandwidth/latency pairs. Performance remains UNVERIFIED. The ordinary
activity refresher delegates modular attempts to this same validator; its legacy
Slurm/log-only completion rule cannot promote them to success.

Artifact access uses descriptor-relative opens with no symlink traversal,
including root ancestors. Only regular files are downloaded, with size checked
before reading and a limit+1 bounded read for concurrent growth. Directory
replacement cannot redirect an already-open descriptor. Configure canonical
local output roots; symlink aliases are deliberately rejected.

### Preview request lifecycle and deferred verification

Readiness, inventory and preview are explicitly classified as print-only by the
shared bootstrap and lake-capture hooks. They retain authentication while skipping
request-time settings materialization and action capture (including capture-all
mode). Read-only connection discovery does not construct a writable catalog store
or run the cached environment bootstrap. Starting a complete Rack Status process
still has its existing startup responsibilities; print-only is not a promise that
application startup itself is side-effect free.

AppServer checks cover shell/Python static checks, offline unit/regression tests,
real durable-registry reload using isolated paths, historical result validation
and audit-guarded NC service/CLI previews. Flask/HTTP/auth/route-parity and pytest
compatibility checks were subsequently completed on the Mac development machine;
see [the Mac verification handoff](CLUSTERKIT_MAC_API_HANDOFF_2026-10-22.md).
No live job or deployment change is authorized by these code changes.

Preview already includes the deterministic `activity_id` and status/result URLs.
Clients should retain these before submitting: if the HTTP submission reply is
lost, poll that activity rather than requesting a new attempt. Recovery does not
require an expired preview token.

## Shared frontend job contract (2026-10-22)

Rack Status's existing `RackCommandsStoreProvider` accepts a successful launch with
`ok: true` and a top-level `job_id`, then polls for top-level `phase` until
`done`, `error` or `timeout`. The modular API now exposes these compatible fields
without removing the detailed durable `activity` or normalized `summary`.

- `job_id` and `activity_id` are the same Rack Status activity UUID.
- `scheduler_job_id` is the separate numeric Slurm reference, or null when unknown.
- Status also exposes `work_phase`, `returncode`, `error`, `tasks`, `target`,
  command display and output-tail fields used by the existing job-output formatter.
- `phase: done` requires the same validated normalized success as before; Slurm
  COMPLETED without a summary remains `phase: running`, `work_phase: collecting`.
- Ambiguous acceptance remains nonterminal (`running` / `submission_unknown`),
  never a signal to automatically resubmit or cancel.
- HTTP `ok` describes request success. Functional and performance acceptance stay
  in the detailed normalized summary and must be displayed separately.

The future UI should use the existing `apiFetch` wrapper for the current session
cookie and operator/client headers, a typed feature service alongside the other
`frontend/src/services/` modules, and shared activity/result views. It must retain
the preview's activity ID and confirmation token, clear stale preview state on an
`ActiveClusterContext` change, and submit only after the user confirms the plan.
A client-side timeout is not permission to retry the scheduler submission.

This compatibility envelope is not a completed frontend. The node/group picker,
confirmation panel, separate functional/performance display and profile-scoped
history still need to be wired. The shared rack-command store requires a rack
key; explicit nodes must not be assigned an invented physical rack merely to use
that UI. Use configured topology where available and the durable activity ID for
node-only runs. Existing activity history filters must include the configured
ClusterKit results root rather than accidentally using another tool's log root.

The legacy `/api/run-clusterkit-async` and suite routes retain their old behavior.
Do not point the new explicit-node UI at those routes: their legacy selection and
request contracts differ from the modular preview/confirmed-run contract.

## Development HTTP verification

`backend/tests/test_clusterkit_api.py` exercises the complete Flask application
with authentication enabled, its real configuration readers, static command
renderer, services and durable activity registry. Only transport execution is
simulated; external processes and network connections are blocked during requests.
It covers 1/2/4/8-node previews and lifecycles, confirmation changes/expiry,
duplicate prevention, unknown acceptance recovery, missing/invalid results,
artifact access and the existing top-level job polling contract.

```bash
python -m pytest -q backend/tests/test_clusterkit_api.py
```

Run this with the development dependencies installed, from the repository root.
Passing these tests establishes the local HTTP contract, not live NC execution.
