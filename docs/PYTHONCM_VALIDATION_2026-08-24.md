# PythonCM validation record — 2026-08-24

Target: Nscale TX 16k BCM application server (`appserver`), PythonCM/CMDaemon 11.0.

## Passed

- PythonCM/CMDaemon API compatibility: 11.0 / 11.0.
- Certificate-authenticated inventory: 220 racks and 3,943 PhysicalNode entities.
- Backend regression: 1,409 passed, 1 skipped.
- PythonCM combined focused regression: 89 passed across the provider, scheduler,
  compatibility runtime, workflow, and factory suites (including the 61-test
  provider sub-suite).
- Frontend TypeScript build and production SPA build completed.
- Flask/SPA smoke test: `/`, scheduler compatibility routes, validation presets,
  and workflows returned HTTP 200 under `RACK_SCHEDULER=pythoncm`.
- Python lint, byte compilation, shell syntax, and `git diff --check` passed.

## Live acceptance gate

The service account has a valid client certificate, but BCM 11 requires an
authenticated login cookie for `cmproc`/`cmjob`. The live readiness probe therefore
correctly returns:

```text
PythonCM execution needs --cookie-file/NVIS_BCM_COOKIE or a username plus
--password-file/NVIS_BCM_PASSWORD
```

Provision `NVIS_BCM_COOKIE_FILE`, or `NVIS_BCM_USERNAME` plus
`NVIS_BCM_PASSWORD_FILE`, on the BCM head. Then reserve an approved test node and
complete the mutation sequence in `PYTHONCM_MIGRATION.md`: harmless execution,
status/output, cancellation, drain/resume, power status, and the enabled validation
families. No power or drain mutation was attempted without authorization.

Slurm-native workload scripts remain explicit compatibility workloads until each
site-specific script has a validated PythonCM/direct form. PythonCM mode never
silently falls back to Slurm for scheduler/provider operations.
