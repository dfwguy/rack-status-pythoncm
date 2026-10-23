---
diataxis: how-to
title: How to Validate the ClusterKit UI in an Isolated Instance
---

# How to Validate the ClusterKit UI in an Isolated Instance

The **Tools > ClusterKit** page (`#/tools/clusterkit`) uses the modular validation
API in the same application process. The reviewed execution recipe is host-local
CPU/RoCE preflight and all-pairs bandwidth/latency, with optional baseline versus
single-path comparison. It does **not** enable GPU tests, remote SSH staging, or
performance acceptance. Existing rack-based ClusterKit tools remain unchanged.

## 1. Keep the Live Deployment Separate

On NC, the observed deployment is the root-owned Docker container
`nvis-rack-status-nc`, image `nvis-rack-status:0.3.1-nscale-nc-app.69f742f`,
published on host loopback port **5056**. Its persistent state lives under
`/cluster/nvis-rack-status-app/state`. The image's source revision was not available
in the fetched repository, so this branch is **not** claimed to be a drop-in patch
for that image. It is based on canonical GreenLight main `a260a17`.

Do not rebuild/restart that container, replace `/app`, reuse its state or identity
store, alter its tunnel, grant Docker access, or submit as root. The proven NC
ClusterKit execution arrangement is a separate host process running as `cmods`.
Preserve that arrangement for this review. Docker-to-host execution remains a
separate deployment decision, not something the frontend enables automatically.

## 2. Prepare the Isolated Host Workspace

The Mac handoff includes matched source and built `static/spa` assets, plus an
optional wheelhouse for **Linux x86_64 / CPython 3.12**. Check the actual interpreter,
architecture and wheel compatibility before installing anything.

1. Transfer the archive through the approved jump host and verify its SHA-256.
2. Extract into a **new**, `cmods`-owned workspace, not the live deployment tree.
3. Confirm effective identity with `id` and confirm both the intended Python and
   Slurm clients are usable as `cmods`. Load the approved Slurm module into the
   parent service environment before launch: `sbatch`, `srun`, `sinfo`, `squeue`,
   `scontrol` and `sacct` must resolve there. Absolute provider paths alone do
   not satisfy the compute recipe's PATH requirements. Do not skip module
   initialization unless that environment is already complete. No root
   workaround is required or intended.
4. Create a new Linux virtual environment there. Do not copy the Mac virtual
   environment. With a compatible wheelhouse, install without network access:

   ```bash
   python3.12 -m venv .venv
   .venv/bin/python -m pip install --no-index --require-hashes --find-links wheelhouse-linux-x86_64-cp312 -r requirements.offline.lock
   ```

5. Choose an unused loopback port other than **5056**, and fresh configuration,
   catalog, identity, policy, signing-key, TLS and activity paths. Use a fresh
   workspace `var/` and inspect `backend/cli.py check --help` for path overrides.
   Never point these paths at production stores or reuse the Mac test stores.
6. Use `backend/cli.py check --json` before `backend/cli.py serve`. Serve only on
   `127.0.0.1` with TLS, using the repository's existing startup gate. Keep the
   generated administrator credential private and change it at first login.
7. Access the isolated port through an independently approved tunnel. Do not
   modify `rack-status-tunnel.service` or the live endpoint.

The prebuilt SPA requires no Node/npm on the AppServer. Do not run `start.sh` there
if its dependency/bootstrap downloads would require internet access.

## 3. Configure the Site Policy

Use the existing Settings entry **ClusterKit node validation profile (JSON)**
(`RACK_CLUSTERKIT_SITE_PROFILE`) or the active cluster catalog's atomic
`clusterkit_profile` object. The catalog profile takes precedence; an explicitly
disabled catalog profile does not inherit an enabled global profile. No new
roles, credentials or separate configuration database are introduced.

Start with the already reviewed NC profile, but use a **new writable results
root** and verify every site-specific value. Required values include:

- Slurm partition and approved exact node allowlist and/or hostname regex.
- The HPC-X initialization script visible on allocated nodes.
- Expected architecture and link layer, HCA selection policy and device/port.
- Allowed node counts, maximum nodes and bounded wall time.
- Optional configured topology groups with explicit membership and provenance.
- Path comparison and bounded functional-test parameters.

See `backend/clusterkit_profile.py` for the authoritative schema. No NC hostnames,
SU sizes or HCA defaults are supplied by the UI. Group labels do not imply physical
rack membership. A regex-only policy can use the additional exact-hostname input;
a finite allowlist/group inventory supplies the checkbox table.

The script resolver respects GreenLight's project/custom/platform/shared layers.
The reviewed compute launcher still requires that selected layer and `backend/`
share the deployment root (flat `bin/scripts/` or standard `scripts/<layer>/`).
An external custom script tree is rejected during readiness rather than importing
an unrelated backend. Source hashing covers the selected layer's script bytes,
not a different shared default copy.

The reviewed recipe rejects unsupported fabrics/policies rather than silently
running an approximation. Leave performance evaluation disabled until the fabric
team provides and validates acceptance criteria. Preview TTL is configurable via
`RACK_CLUSTERKIT_PREVIEW_TTL_SEC` (30-900 seconds, default 300); collection grace via
`RACK_CLUSTERKIT_COLLECT_GRACE_SEC` (30-1800 seconds, default 300).

## 4. Validate Without Launching Jobs

1. Confirm login, active project/cluster, readiness and the execution user.
2. Preview one-node preflight and 2/4/8-node explicit/group selections permitted
   by the profile. Confirm exact nodes, HCA, resources and new attempt directory.
3. Verify previews create no allocation or result directory. Unavailable/drained
   nodes must not become idle by dropping state flags.
4. Change a selection or refresh readiness: the previous confirmation disappears.
   An expired or policy-changed preview must be rejected.
5. Confirm disabled/unsupported configuration produces an actionable error and
   cannot submit a job. Verify an unauthorized session cannot access the API.

## 5. Separately Approve Live Tests

Only after explicit cluster-owner approval, confirm one preflight, review it, then
approve a small pairwise test. Larger tests require their own approval. This
handoff does not authorize a cluster reservation or a live job.

Verify the complete UI-to-API-to-Slurm lifecycle, not just the script:

- Exactly one submission per confirmation, including rapid repeated clicks.
- A lost response is recovered by the preview's activity UUID, never resubmitted.
- Navigation does not stop shared monitoring; reload can reopen `?run=<uuid>`.
- Cluster/catalog changes invalidate previews and detach old-context monitoring.
- Scheduler completion alone is insufficient for functional PASS.
- Baseline/single-path coverage reports bandwidth and latency separately: 1 pair
  for two nodes, 6 for four, 28 for eight. Units remain MB/s and usec.
- Node diagnostics expose actual HCA, GID index, placement and CPU affinity.
- Artifacts download using the existing authenticated request wrapper.
- History reopens exact attempt directories, including failures, without using
  the parent directory as an exact `log_dir` query.
- Functional **PASS** remains distinct from performance **UNVERIFIED**.

`GET /api/clusterkit/runs?limit=25&before=<cursor>` is a context-scoped paginated
view of the shared live/archive activity store. Generic validation history retains
its existing exact `log_dir` semantics. Neither API rewrites the immutable summary
just to change its scheduler-state observation.

## 6. Run Local Verification and Review

Use `./start.sh` to build/run locally. Unit tests use Flask's in-process client,
simulated Slurm, and synthetic UI fixtures; they require no cluster connectivity.
Browser screenshots likewise exercise fixture responses, **not real measurements**.
The [October 23 V19 evidence](../reference/clusterkit-v19-validation-2026-10-23.md)
now proves the matched GreenLight API-to-Slurm lifecycle with the compute import
fix in an isolated `cmods` host process. It does not prove the production
container's execution arrangement. Browser fixture validation on the Mac and
real HTTP validation on the AppServer are distinct checks. Review new releases
with the isolation procedure above; do not repeat live jobs without a reason
and explicit approval.

Do not deploy into the live Docker application until the matched frontend/backend
package has passed that review and the
[deployment plan](clusterkit-deployment-plan.md) is approved.
