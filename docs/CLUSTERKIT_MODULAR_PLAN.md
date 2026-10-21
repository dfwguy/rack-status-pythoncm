# ClusterKit modular profile and planner: implementation map

Scope for this change: audit, Phase 1 and read-only Phase 2. No live jobs,
deployment, service starts or system changes. The existing NC two-node CLI and
legacy Nscale paths remain intact. Source and handoff exports stay in this repo.

## Audit and module map

| Existing owner | Finding and planned change |
| --- | --- |
| `backend/config_env.py` | Register one non-secret `RACK_CLUSTERKIT_SITE_PROFILE` JSON Settings value. Keep the established high cluster > Settings > process environment > low platform > default order. Add a read-only resolution view: normal readers create a flock file and materialize environment variables, which a dry-run must avoid. |
| `backend/catalog/models.py`, `context.py`, `store.py` | Retain a cluster-owned optional `clusterkit_profile` in the catalog document and expose it through the existing high overlay. Read existing catalog storage through a snapshot repository without initializing a writable TinyDB store. No new configuration file format/store. |
| `backend/clusterkit_profile.py` (new) | Typed, validated resolved profile, readiness/errors and product capabilities. Disabled by default. No site defaults hidden in reusable code. |
| `backend/clusterkit_planner.py` (new) | Product request validation, read-only Slurm snapshot adapter, deterministic exact selection, topology attribution, expected pairs, resource plan and safe reviewed-entrypoint argv where implemented. No submit function. |
| `bin/scripts/plan_clusterkit.py` (new) | Print-only local CLI using existing local transport and configuration reader; only readiness and plan operations. No execution option. |
| `backend/tests/test_clusterkit_modular_plan.py` (new) | Offline unit tests for profile/precedence, selection, injection resistance, side effects, resources and execution blockers. Preserve the existing 20-test two-node regression suite. |
| Existing `run_clusterkit_suite.sh`, explicit batch/rank/diagnostic scripts | Preserve byte-for-byte in this turn. Generalizing rank assumptions, per-node policy and entrypoints belongs to Phase 3. |
| `backend/app.py`, `api/job_status.py`, `validation_activities.py`, `workflow_engine.py` | Existing legacy launch routes record validation activities but use the stress-job worker. Later add a new API module using the durable registry/workflow lifecycle, not another job dictionary. Keep legacy route contracts. No routes or running services change now. |
| `backend/catalog/seed.py` | Existing ClusterKit tools expose rack and legacy SU execution, not the proposed product scopes. Later add/update catalog metadata only after the execution service is ready. Do not route a new UI button into the old full-SU runner. |
| Results in `backend/app.py`, `universal_parser.py`, script summary | Existing triangular matrix decoding is reusable; the legacy positive-cell summary counts both symmetric directions and is not a coverage/acceptance model. Phase 5 must count unique unordered pairs, detect missing/failed pairs and split functional/performance status. |
| Frontend `ValidationTab`, `ValidationPresetDialog`, node selector, RackCommand/ActiveCluster contexts, `ClusterkitResultsSection` | Reuse the existing preview/plan-hash confirmation and shared command/activity contexts. The generic launch-options dialog exposes low-level knobs and must not become this product's request model. Frontend work waits for the backend contracts and lifecycle. |

## Installed vendor evidence

Read, without executing the ARM binary:
`/home/cmods/workspace/hpcx-v2.25.1-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64/clusterkit/share/clusterkit/doc/README`.
Lines 33–40 specify one process per node, an even count >=2, full all-pairs by
default, and a reduced `--quick` mode. Plan 1/6/28 unordered pairs for 2/4/8
nodes; do not use quick/fromfile/bycore/bygpu modes for the initial CPU recipe.
This establishes documented semantics, not observed 4/8-node coverage.

Open fabric/vendor questions before larger live tests: confirm direction and
aggregation conventions in raw bandwidth, reconcile the reported 200 Gb/s rate
with observed values, agree acceptance/warmup/iterations, confirm complete
unique-pair output and behavior when a bounded run stops partway, and verify
the approved rail/topology mappings. Physical racks are not inferred from SU
hostnames. Performance is UNVERIFIED by default.

## Progress

- Audit complete; baseline copies and hashes saved in the transfer export.
- Implemented profile and safe planner; retained explicit execution blockers for
  absent/generalized entrypoints rather than point 4/8-node jobs at the two-node
  batch file.
- Offline tests and print-only plans passed. The transfer package includes
  changed source, a baseline-checked patch, verification evidence and this
  handoff. No live submission is authorized.

## Administrator configuration and public contracts

`docs/examples/clusterkit-site-profile.example.json` documents the complete
profile shape and is deliberately **disabled** with fictitious values. It is
documentation, not an independently loaded configuration file. Supply its JSON
through the existing `RACK_CLUSTERKIT_SITE_PROFILE` Settings/environment/platform
default key, or the active catalog cluster's `clusterkit_profile` object.
The catalog object becomes the same Settings key in the high overlay.

Precedence remains cluster high > Settings overrides > process environment >
platform low > defaults. Profiles are atomic: do not merge one site's paths
with another site's limits. Catalog `null` inherits; `{}` explicitly disables
the active cluster. The existing Settings 8192-character limit applies. Inline
topology mappings are bounded to 512 members per group; large external topology
providers and hostname grouping rules are future work, not a second config
store. Simple node regexes filter names only; they never establish topology.
Rack/SU-kind groups require an explicit authoritative flag. Logical example
groups with unverified physical meaning must not become authoritative rack/SU
selectors in a future UI.

Readiness is available as `resolve_profile().public()` and the print-only CLI's
`readiness` operation. It reports **configuration** validity, enabled state and
available profile choices, not verified paths/fabric/compute health. Paths are
validated syntactically; read-only compute preflight must later verify actual
access/HCA/GIDs/architecture on allocated nodes. `discover` HCA policy, non-RoCE
policies and different functional parameters are represented/validated but
blocked from the old two-node entrypoint if it cannot honor them.

`parse_request()` accepts only `scope`, `node_count`, `nodes`, `group`, `label`,
`test_mode` and optional `path_comparison`. Paths, walltime, HCA, UCX, profile,
raw arguments and output-root overrides are rejected as unknown request fields.
All counts and names are checked before inventory discovery. Preflight uses
one node; initial pairwise capability sizes are 2/4/8, intersected with the
administrator's choices and maximum. Empty pairwise choices permit a
preflight-only profile. A higher administrator ceiling does not enable larger
product capabilities.

Explicit selection preserves exactly the requested nodes/order. Healthy
allocated/mixed nodes remain eligible-but-busy and receive a scheduling warning;
drain/down/reservation/future/unknown states and state flags are rejected. Group
selection takes exactly N currently idle allowed members in deterministic
natural order, never fills an insufficient group from busy or foreign nodes,
and never promises nodes remain idle. The group mapping is configured data;
physical topology is not inferred. An explicit selection without a mapping
records unknown topology.

`build_plan()` is pure and `plan_request()` adds the injected read-only inventory
adapter. The adapter uses the existing control connection and platform provider;
no scheduler submission object is constructed. The diagnostic CLI explicitly
selects the existing local transport with `--local-inventory` and must be run
on the intended cluster helper/submission host with its Slurm environment
loaded. It is not a remote multi-cluster HTTP service.

A plan contains schema version, generated attempt ID, exact nodes, resolved
non-secret profile, timestamped availability, configured/unknown topology,
resource argv, expected unique unordered pairs, warnings and blockers. It
reports `functional_status=NOT_RUN`, `performance_status=UNVERIFIED`,
`submitted=false`, `execution_ready=false`, `submission_enabled=false`.
The hash fingerprints this exact preview; it is not an authorization token,
and a new preview gets a new attempt ID. Phase 4 must persist/confirm the plan
and revalidate effective policy/inventory at submission.

Only a compatible two-node CPU/RoCE profile gets an actual `sbatch_argv` pointing
at the existing reviewed static entrypoint. That remains a proposal, never
executed by these modules. It has two exact nodes/tasks, one task and CPU per
node, requested 1 GiB/node, no GPUs/exclusive/requeue and the configured bounded
walltime. The existing entrypoint's functional parameters and three transports
must match; unsupported policies get blockers, not ignored settings.

For one-node and 4/8-node plans, `sbatch_resource_argv` is available but
`sbatch_argv`/`entrypoint` are null. This is intentional: no nonexistent/unreviewed
script is presented as runnable, and no 4/8-node job is sent to a two-node-only
script. Every scope remains submission-disabled in this phase. Attempt paths
are planned with generated IDs; Phase 4 must exclusively create them and save
profile/plan/source/job metadata before submission. Planning creates none.

Performance threshold values, when supplied, are explicitly in megabytes/sec
and microseconds. Enabling evaluation requires both a verified flag and an
acceptance reference; it still cannot accept an unrun test. Evaluation and
functional PASS/WARN/FAIL normalization belong to Phase 5. Raw vendor PPN and
technology labels must continue to be preserved and corrected in sidecars,
not overwritten in original output.

## Verification observations (2026-10-19)

Thirty-four new stdlib tests and the existing twenty two-node tests passed. Tests
cover settings precedence and disabled/invalid profiles, real catalog snapshot
code, limits and malformed/untrusted fields, 1/2/4/8-node selection, group
selection, busy versus unavailable nodes, exact resources/quoting, no-write
planning, unsupported policy blockers and performance remaining UNVERIFIED.
Discovery timeouts are returned as structured planning errors without retries.
The copied helper host lacks Flask, TinyDB and pytest; no packages were
installed and the Flask application was never imported/started. Catalog tests
load the real snapshot/models/context modules under an isolated test package
with a TinyDB constructor/touch operation that aborts if invoked. This validates
the read-only path but is not a real TinyDB/Flask integration test. Existing
catalog policy cannot be silently ignored if catalog dependencies are missing;
readiness fails closed when such a store exists.

Read-only Slurm discovery found the previously approved SU19 naming group no
longer usable: 19 `drained*` and 89 `down*` in that bounded query. No node/service
state was changed and no cause was inferred. Do not use historical idle or
successful-run evidence as current availability.

Five real read-only CLI previews passed using an export-local temporary
profile and idle `sys1-gso1-p-phy-gpu1-su20` through `gpu8-su20`: one-node,
explicit 2/4/8 nodes and a configured logical four-node group. They predicted
0/1/6/28 pairs as appropriate. A process audit hook rejected filesystem writes,
mkdir/remove/rename/chmod and non-discovery process launches. Each preview made
only a read-only `sinfo` call, created no attempt directory and left source and
runtime directory signatures unchanged. The old SU19 pair was correctly
rejected. Previews are snapshots, not allocations or reservations.

The temporary profile declares the prior prototype's HCA/HPC-X expectations;
these were **not checked on SU20 compute nodes**. The logical preview group is
explicit fixture membership, not a fabric-team-approved SU/rack map. No profile
was saved to active Settings/catalog storage, no HPC-X binary was invoked and
no live Slurm job was submitted. Evidence is in the dated export bundle.

## What remains before building the frontend

1. **Phase 3:** reviewed static one-node and generalized pairwise entrypoints;
   consume all effective policies, remove rank-0/1 assumptions, preserve
   per-node GID/affinity/evidence for N ranks, enforce exact allocation and
   record output/primary status without automatic retries. Validate documented
   4/8-node behavior with separately approved live tests, in sequence.
2. **Phase 4:** new `backend/api/clusterkit.py` readiness/inventory/plan/submit/
   status/results routes bound to the active project's transport, scheduler and
   durable workflow/activity registry. No new job dictionary. Confirm plan
   freshness and limits server-side, exclusively create attempts, persist the
   effective profile/roster/source hash/job ID and preserve failed attempts.
   Reconcile completion after restarts and provide artifact/status URLs. Keep
   all legacy route contracts intact.
3. **Authorization boundary:** the audited Settings PUT and catalog handlers
   use the dashboard authentication gate; no separate administrator role check
   was found there. “Administrator only” in a settings label is not access
   control. Define and enforce who may edit deployment policy before exposing
   ordinary-user launch controls; add auth/limit tests using the real Flask
   environment. Do not introduce an auth bypass. Also account for the current
   `before_request` bootstrap calling the write-capable settings loader: a pure
   planner alone does not make a new HTTP plan route globally write-free.
   The new profile currently gates only the new planner. It does not disable or
   impose new limits on unchanged legacy launch routes. Decide the authorized
   legacy/migration policy before offering a server-wide safety guarantee;
   preserve legacy contracts without creating an ordinary-user policy bypass.
4. **Phase 5:** stable attempt/result schema, unique-pair coverage and missing/
   failed pair detection, raw-log attribution and functional/performance
   separation through the existing results pipeline. A process exit of zero
   is insufficient to prove complete pair coverage or performance acceptance.
5. **Catalog/frontend:** advertise only implemented capabilities in the catalog;
   add typed contracts and services, then a focused KUI validation surface using
   ActiveCluster/RackCommand contexts and the existing preview-confirm pattern.
   No low-level run fields. Only authoritative mappings become physical rack/SU
   choices. Add confirmation, lifecycle and detailed pair/artifact views.
6. **Rollout:** full dependency-backed configuration/catalog/API/auth and legacy
   route tests on the development machine, then print-only plans; each live
   preflight/2/4/8 test needs separate explicit approval. Broader scale and
   acceptance thresholds need fabric-team agreement. No deployment is included.

## Reproduce the offline review

Run in an isolated development checkout, without importing `app.py` or starting
services:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s backend/tests -p test_clusterkit_modular_plan.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s backend/tests -p test_clusterkit_suite_plan.py -v
python3 bin/scripts/plan_clusterkit.py readiness
```

The last command returns a disabled/incomplete readiness result unless an
administrator profile has been supplied through the existing configuration
ladder. It does not manufacture an NC profile or save Settings. On the intended
local submission host with its Slurm environment loaded, a profile-backed plan
uses product-level JSON, for example:

```bash
python3 bin/scripts/plan_clusterkit.py plan --local-inventory \
  --request '{"scope":"explicit","nodes":["node1","node2"],"node_count":2}'
```

Replace those example names with nodes allowed by the resolved profile. Do not
add paths/HCA/environment overrides to the request. No execution/submit mode
exists in this CLI. The old two-node runner still exists separately and must
not be invoked live without a new explicit approval.

The export patch is against the four captured AppServer source files at the
start of this task, not a verified GitLab commit (this transferred checkout has
no Git metadata or network access to GitLab). The development reviewer must
run `git apply --check` on the intended integration branch before applying it.
Original successful two-node scripts/tests and legacy app/routes/catalog seed
are hash-verified unchanged. The new modules will also need the deployment's
normal module packaging/bind mounts later; no deployment file was changed here.
