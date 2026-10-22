---
diataxis: reference
title: ClusterKit GreenLight UI Mac Verification, 2026-10-22
---

# ClusterKit GreenLight UI Mac Verification

## Scope and Provenance

- UI branch: `feature/clusterkit-ui`, based on canonical GreenLight main `a260a17`.
- Modular backend imported from the tested `feature/clusterkit-backend` lineage,
  including the NC UCX-discovery timeout fix (15 seconds).
- The earlier live NC report proved the modular API recipe on 1/2/4/8 nodes. It
  does not prove this newer GreenLight port or its Docker-to-host integration.
- All work recorded here was local to the Mac. No NC job, deployment, Docker,
  permission, node-state or tunnel change was made.

## Features Implemented

`#/tools/clusterkit` is a lazy-loaded KUI page under Tools, using the existing
active-cluster, authenticated fetch/download, theme, help and shared-command
contexts. It provides configured readiness, exact nodes or configured groups,
1-node preflight and allowed 2/4/8-node pairwise previews, expiring signed
confirmation, durable monitoring, results, diagnostics, artifact downloads and
context-scoped paginated history.

The recovery UUID is placed in the URL before dispatching confirmation. A token
is consumed once in the UI; the backend independently protects against duplicate
submission. Monitoring persists when the page unmounts and is aborted on a
cluster/catalog version change. No physical rack key is invented for node/group
work. Performance remains UNVERIFIED, independently of functional PASS.

The backend is integrated with current GreenLight identity/action declarations,
catalog storage, Settings and activity hydration. History uses the existing
durable archive plus live working set and exact attempt directories. Generic
history's exact `log_dir` filter is unchanged.

Project-layer script selection uses the existing script-tree precedence. Hashing
and staging use the selected layer's bytes rather than the shared default copy.
The reviewed launcher still requires script and backend colocation beneath the
deployment root; external script trees fail readiness explicitly. The static
SBATCH scripts and compute test flags are unchanged. This path-resolution change
must be included in isolated AppServer validation.

Legacy scheduler callers retain their existing accounting/PENDING behavior;
ClusterKit uses the explicit evidence-backed accounting/queue observation method.
The shared argv renderer uses equivalent long-form node-count arguments. Transport
calling conventions are selected before executing a command: a TypeError after
scheduler acceptance cannot trigger a second submission.

## Verification Results

| Check | Result |
|---|---|
| ClusterKit, script-tree, scheduler and configuration regression set | 242 passed, plus 176 subtests |
| Shared action/authz/catalog/routes/activity regression set | 175 passed |
| Portable backend rerun | 5,985 passed; 94 skipped; 56 known pre-existing cases deselected; 3 existing non-strict XPASS markers; 176 subtests passed |
| Full frontend Vitest suite | 962 passed across 102 files |
| Focused UI and shared-monitoring tests | 24 passed (included in the full frontend total) |
| TypeScript strict checking | Passed |
| Frontend ESLint | No errors; 66 existing warnings outside the new feature |
| Backend Ruff | Passed |
| Generated action vocabulary, route snapshot, Settings reference and TOML example | Checked against source |
| Production SPA and tracker build | Built using `start.sh` |
| Chrome browser checks | Desktop/mobile, light/dark; 16 screenshots; no page errors or horizontal page overflow |

Browser tests used real local SRP login and **synthetic ClusterKit HTTP fixtures**,
not cluster connectivity or real performance measurements. They exercised exact
selection, preview/confirmation, status polling, separate PASS/UNVERIFIED labels,
per-mode pair results, diagnostics, authenticated download and reopening a failed
attempt. The request interception also checked that the recovery UUID was already
in the page URL before submission.

## Broader Mac Suite Limitations

A broad backend run initially reported 5,977 passing tests, with 56 failures and
six setup errors. Seven integration-related failures were fixed and rerun in the
passing targeted sets above. **All remaining 49 failures and six setup errors
were reproduced by running the same cases on an untouched `a260a17` archive.**
They are pre-existing Mac/upstream limitations, chiefly Apple Bash 3.2 versus
scripts needing Bash 4+, missing GNU `timeout`, and existing log-path tests. One
additional Bash-version-dependent launcher case had already been deselected
after reproducing that limitation in the first broad run.

These are not represented as a green full Linux suite. The handoff contains an
offline Linux x86_64/CPython 3.12 wheelhouse, but installing those wheels and
validating the matched package on Linux remains the AppServer agent's job.

## Remaining AppServer Work

Follow [the isolated validation guide](../how-to/clusterkit-ui-isolated-validation.md).
Use a new `cmods` workspace, separate host process, loopback port and fresh state;
do not modify the root-owned live Docker application on 5056. Verify site policy
and print-only previews first. Obtain separate approval before each live test.
Fabric-team acceptance thresholds and interpretation of the earlier reported
`duplicate_request` counter delta remain unresolved; do not assign a cause or
claim performance acceptance from functional success.
