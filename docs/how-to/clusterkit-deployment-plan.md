---
diataxis: how-to
title: How to Prepare a Reviewed ClusterKit Rollout on NC
---

# How to Prepare a Reviewed ClusterKit Rollout on NC

Use this gated plan after the [V19 validation](../reference/clusterkit-v19-validation-2026-10-23.md).
It prepares a rollout; it does not authorize a deployment, reservation, live job,
container restart, tunnel change or privilege change.

## 1. Record the Release and Deployment Boundary

1. Select the reviewed `feature/clusterkit-ui` commit containing the V19 compute
   import fix. Record its full SHA and the transfer archive checksum. Keep source,
   `static/spa`, `static/tracker` and build metadata matched to that release.
2. Retain the V19 handoff, including all six attempts, the two failures, HTTP
   evidence and counter observations. Do not import its site profile as a product
   default or copy its test identities, keys or tokens.
3. Have the AppServer agent recheck the current deployment read-only. Record the
   image digest/tag, recoverable source revision, mounts, state/config paths,
   service command, execution user, listener and tunnel target. Historical NC
   observations were `nvis-rack-status-nc`, image
   `nvis-rack-status:0.3.1-nscale-nc-app.69f742f`, HTTPS loopback 5056 and persistent
   state under `/cluster/nvis-rack-status-app/state`. These are observations to
   verify, not defaults to install. If inspection needs privileges unavailable to
   `ykhair`/`cmods`, request the operator's read-only output; do not change access.
4. Identify the maintained NC deployment branch and review its differences from
   this main-based feature branch before merging. The old image's source was not
   present in the Mac's fetched history. Do not overlay this branch onto `/app`,
   silently replace the deployment baseline or assume an application restart
   activates new source when no source bind mount exists.

**Gate:** the release, existing deployment and rollback artifact must be identified
before any production promotion is proposed.

## 2. Preserve the Validated Host Execution Environment

1. Keep the immediate operator-review lane as a separate, unprivileged `cmods`
   host process on `appserver-nc`, with its own unused loopback port, catalog,
   identity, policies, TLS/signing keys, state and results. Keep production on
   5056 and its existing tunnel untouched. Follow the
   [isolated instance guide](clusterkit-ui-isolated-validation.md).
2. Load the approved Slurm environment in the **parent service process** before
   launch, using the same existing module initialization that passed V19. Check
   `sbatch`, `srun`, `sinfo`, `squeue`, `scontrol` and `sacct` resolve as `cmods`.
   Check the provider's configured commands, including `scancel`, resolve too;
   this check must not invoke cancellation. Verify `SLURM_CONF` and any cluster
   selection match NC. Remove accidental inherited `SBATCH_*` launch overrides.
3. Do not set `RACK_SLURM_SKIP_MODULE_LOAD=1` unless that parent environment is
   complete. Absolute provider command paths alone do not provide compute-side
   `scontrol`/`srun` on PATH. A future supervisor must reproduce this environment
   from a clean login/restart; an interactive shell export is not a service fix.
4. Verify the approved HPC-X initialization path and reviewed script/backend
   colocation. The AppServer is x86_64; allocated compute nodes are aarch64. Run
   ARM binaries only on compute nodes. Keep site paths, exact allowlists,
   HCA/GID policy, groups, counts and limits in Settings/catalog configuration.
5. Keep the tested CPU/RoCE recipe, maximum eight nodes and reviewed resource and
   wall-time bounds. Leave performance acceptance disabled. Treat V19 membership
   as user-declared topology until independently confirmed by the fabric team.

**Gate:** the intended service environment must pass clean-start checks without
root, compute-node package installation or production state access.

## 3. Complete Operator Acceptance Without Repeating Validation Jobs

1. Verify the exact patch/release and offline package hashes. Rerun the cold
   compute imports and focused software/API tests in the isolated Linux venv.
   Run `backend/cli.py check --json` only against the isolated configuration.
2. Start the matched TLS instance as `cmods` through the existing CLI/startup
   gate. Use a separate approved tunnel. Check the served build identifier and
   that every SPA/tracker asset referenced by its HTML resolves.
3. Have an operator log in and review `#/tools/clusterkit`: active cluster,
   readiness, exact-node/group selection, 1/2/4/8-node previews, configured bounds,
   functional/performance labels and negative cases. Preview only; do not confirm.
   Verify no result attempts or Slurm jobs were created by this review.
4. Review saved V19 results, diagnostics, authenticated downloads and exact-attempt
   paginated history in their original compatible execution context. Do not
   rewrite plans/manifests or reset stores to make old results appear new.
   Context includes user, host, selected cluster, script path and Slurm settings;
   moving a release or changing those values can intentionally reject old access.
   Preserve the original evidence and record any history-migration requirement.
5. Report UI or environment defects. Request approval for a targeted live test
   only when a relevant code/environment change needs validation. The already
   passed V19 1/2/4/8 sequence need not be repeated just to produce new job IDs.

**Gate:** functional PASS and performance UNVERIFIED remain separate. Neither
Slurm completion alone nor a clean UI certifies performance, GPU/GPUdirect,
all V19 nodes, a whole rack or a whole SU.

## 4. Resolve the Unified Production Architecture

Before proposing a cutover, have the product/deployment owner choose and approve
one supported arrangement:

- Retain the containerized GreenLight application and implement/review the
  missing ClusterKit remote staging, execution and result access through the
  existing connection/transport and role providers, executing as `cmods` on the
  host. The current local-only backend rejects this arrangement; enabling SSH
  for other GreenLight tools does not implement it for ClusterKit.
- Move the matched full GreenLight application to an approved host-local
  `cmods` service. Validate all required existing tools, authorization, state
  handling and operational supervision, not just ClusterKit, before replacing
  the live application. This is a broader deployment change, not a UI patch.

Keep the separate host instance a review/pilot lane, not an undeclared permanent
second production system. Do not bypass authentication with a reverse-proxy
shortcut, introduce a bespoke privileged broker, mount the Docker socket, or run
ClusterKit as container root. Continue using existing shared-access policies;
this rollout does not introduce a new role system.

**Gate:** neither production arrangement is proven by host-local V19 validation.
Any required code changes must land with tests and a separate deployment review.

## 5. Obtain Cutover Approval and Define Rollback

1. Record the chosen architecture, exact source/image, owners, maintenance window,
   authorized node pool, service environment, endpoints, startup checks and
   acceptance/rollback criteria. Keep new launches disabled until final checks.
2. Back up configuration and mutable state using the existing deployment's
   supported procedure. Preserve image artifacts and immutable attempt data.
   Do not copy live JSON/SQLite files mid-write or casually replace an identity
   store: restoring old revocations while retaining signing keys can restore
   revoked access. Review identity/secret recovery together.
3. Drain application-level submissions, not compute-node states. Reconcile any
   active/uncertain submissions and let approved jobs finish before moving code
   or state. Source hashes and context pinning make in-place edits during runs
   unsafe. Never automatically cancel, retry or resubmit as a rollback action.
4. Have the deployment owner perform the approved release and endpoint switch.
   Verify login/authorization, assets, active cluster, readiness, previews and
   existing tool compatibility. Only then enable the reviewed profile and allow
   separately approved live work. Watch for duplicate submissions or invalid
   evidence; retain failures and logs.
5. On failure, stop accepting new submissions, reconcile jobs and restore the
   approved prior service/image/endpoint and compatible state. Preserve new
   attempts separately. Do not roll an old writer onto migrated state or revert
   identity data without the reviewed recovery procedure.

Send the deployment owner the evidence and a go/no-go recommendation. Fabric-team
review of throughput/latency acceptance, VF-reported rate interpretation and the
`duplicate_request` observation can proceed in parallel; until agreed and tested,
performance stays UNVERIFIED.
