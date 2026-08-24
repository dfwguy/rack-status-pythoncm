# PythonCM/direct-execution migration

Rack Status uses PythonCM for BCM-managed inventory and direct execution when
`RACK_SCHEDULER=pythoncm`. Slurm remains an explicit compatibility fallback; it is
not selected implicitly by a PythonCM failure. Existing `/api/slurm-*` routes and
Slurm-shaped JSON fields are preserved so browser bookmarks, integrations, and
stored validation activities continue to work across the cutover.

## Why PythonCM runs on the BCM head

PythonCM is version-coupled to the site's CMDaemon and is not a normal application
dependency. Rack Status launches `bin/scripts/nvis_bcm_query.py` with the
BCM-matched interpreter on the head node, reads JSON, and keeps the PythonCM module,
TLS client key, and cluster CA out of the application image.

The usual settings are:

```bash
RACK_PLATFORM=bcm
RACK_SCHEDULER=pythoncm
NVIS_BCM_PYTHON=/cm/local/apps/python312/bin/python3
NVIS_PYTHONCM_PATH=/cm/local/apps/cmd/pythoncm
NVIS_BCM_HOST=127.0.0.1
NVIS_BCM_PORT=8081
NVIS_BCM_CERT=~/.cm/cert.pem
NVIS_BCM_KEY=~/.cm/cert.key
NVIS_BCM_CA=~/.cm/cluster-ca.pem
NVIS_BCM_CA_OUT=~/.cm/cluster-ca.pem
NVIS_BCM_CA_BOOTSTRAP=0
```

Paths are resolved on the BCM head, not in the Rack Status container. Never put the
client key in git, a tarball, or a frontend setting.

## Preflight

Run these from the same user and connection profile used by Rack Status:

```bash
"${NVIS_BCM_PYTHON:-/cm/local/apps/python312/bin/python3}" \
  bin/scripts/nvis_bcm_query.py --query version
"${NVIS_BCM_PYTHON:-/cm/local/apps/python312/bin/python3}" \
  bin/scripts/nvis_bcm_query.py --query nodes
```

Both commands must return one JSON object with `"ok": true`. Confirm that the
reported PythonCM path and BCM version match the target cluster and that the node
list contains real compute hostnames. Do not continue if the query silently targets
another CMDaemon, the CA is unverified, or rack ids were substituted for compute
hostnames.

CA bootstrap is disabled by default. If the stock CA is unreadable, use the
one-shot bootstrap path to pin the chain, record its
SHA-256 fingerprints, and have the BCM administrator verify them before acceptance:

```bash
bin/scripts/nvis_bcm_query.py --bootstrap-ca --query version
```

## Acceptance sequence

Use a reserved test rack and an exact node list. Capture the returned task/run ids
and output paths at each step.

1. Load the cluster overview and compare rack/node counts with the PythonCM query.
2. Open **Compute → Scheduler** and verify Active jobs and Node availability load.
3. Launch a harmless one-node command and verify start, live output, terminal
   state, exit code, and durable activity history.
4. Launch a longer harmless command, cancel it from Active jobs, and verify the
   remote process tree is gone and the activity is terminal.
5. Drain one clean test node, verify it becomes unavailable for new work, resume it,
   and verify the reason/state clears.
6. Run one hardware or network workflow and verify exact-node fan-out, per-node
   output, evaluation, and report persistence after an application restart.
7. Run the validation family required at the site (NCCL, NeMo, ClusterKit,
   MNUberGEMM, nvloom, and multi-rack where enabled). Verify exact target placement,
   environment propagation, artifact names, parser ingestion, log tail/download,
   cancellation, and cleanup.
8. Run the full backend and frontend suites, build the SPA, and run
   `bin/verify-deploy.sh` against the candidate deployment.

A page returning HTTP 200 is not acceptance. Every launched process must be
accounted for and no command may remain on a compute node after cancel or failure.

## Compatibility and rollback

- Keep `/compute/scheduler`, `/api/slurm-*`, `sinfo`, `squeue`, `slurm_state`, and
  `slurm_reason` names stable. They are wire/storage compatibility names.
- Do not infer the provider from those names. Use `RACK_SCHEDULER` and the provider's
  command display/metadata.
- Existing workflow steps with `scheduler.kind: slurm` continue to select Slurm
  explicitly. Change them to `pythoncm` only after the corresponding script has a
  validated direct-execution form.
- To roll back, set `RACK_SCHEDULER=slurm`, restore the site's `RACK_SLURM_*` values,
  recreate/restart the application as required by the deployment, and repeat the
  active-job, cancel, drain/resume, log, and workflow smoke tests.
- A PythonCM error must remain visible. Do not silently fall back inside a run;
  provider switching mid-run destroys task ownership and auditability.

## Packaging check

Before producing the handoff tarball, ensure no key/certificate, `.env`, generated
activity data, logs, node output, or Python cache is included. The package should
contain the example configuration and this runbook, while site secrets stay on the
target deployment.
