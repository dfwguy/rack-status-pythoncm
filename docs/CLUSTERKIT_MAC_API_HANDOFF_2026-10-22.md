# ClusterKit Mac API Verification and AppServer Handoff

Date: 2026-10-22. Branch: `feature/clusterkit-backend`.

## Source and Scope

Applied the cumulative patch in `NC-api-alignment-20261022T174851Z-handoff.tar.gz`
to the clean `9a450a6` backend branch after `git apply --check` passed. The input
archive SHA-256 is
`7c24ea1300a25023ba8b5a796e28e23e128db025086210d40c0cd7c854c44bc1`.

The imported changes align job IDs and phases with shared Rack Status polling,
register the blueprint through the API package loader, bind local execution
context to confirmations, retain durable submission/recovery evidence, validate
normalized results and keep previews out of writable request hooks.

Mac work covered local integration, tests, fixes and transfer preparation. No
AppServer connection, live Slurm job, deployment or frontend change was made.
The established static sbatch/rank scripts are unchanged from `9a450a6`.

## Local Fixes

- Fixed test-helper imports for both normal pytest collection and AppServer's
  existing standalone unittest discovery command.
- Fixed concurrent first creation of the submission lock on macOS. Atomic
  exclusive creation or opening the existing lock precedes the nonblocking flock;
  simultaneous confirmation still makes at most one scheduler submission.
- Reject non-ASCII confirmation tokens as invalid input before HMAC comparison,
  avoiding an unhandled request exception.
- Expanded HTTP tests to use the real Flask application, session auth, Settings
  readers, transport/provider command rendering and isolated durable registry.
  Only host execution is simulated; processes/network calls are blocked during
  requests. Synthetic NC-style names and fabric settings are test fixtures.
- Applied repository licensing headers and scoped lint fixes to imported modules.

## Verification

Python 3.14 on macOS, using the existing disposable development environment at
`/tmp/rack-status-clusterkit-test-venv`. The return bundle includes test logs.

- Combined feature and affected compatibility suite: **424 passed, 208 subtests
  passed**, including **37 HTTP tests**. Includes real Flask HTTP requests for 1/2/4/8-node lifecycles, auth,
  read-only previews, confirmed submission, polling, artifacts, failures, recovery,
  scheduler/transport, Settings precedence, catalog and legacy route parity.
- AppServer-compatible offline unittest command: **128 tests passed**. Its logged
  tracking-unavailable traceback is an intentional fault-injection case.
- ClusterKit Ruff checks, changed-module Python compilation, static shell syntax
  and `git diff --check`: passed.

Reproduce from the repository root in a dependency-complete environment:

```bash
python -m pytest -q backend/tests/test_clusterkit*.py \
  backend/tests/test_host_exec.py backend/tests/test_transport.py \
  backend/tests/test_schedulers.py backend/tests/test_scheduler_runtime.py \
  backend/tests/test_pythoncm_scheduler.py backend/tests/test_scheduler_jobid_parsing.py \
  backend/tests/test_config_precedence.py backend/tests/test_config_materialize_hook.py \
  backend/tests/test_job_env_missing_file.py backend/tests/test_validation_activities.py \
  backend/tests/test_lake_capture.py backend/tests/test_auth_token_file.py \
  backend/tests/test_catalog.py backend/tests/test_catalog_api.py \
  backend/tests/test_route_parity.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend:backend/tests python -m unittest \
  test_clusterkit_execution test_clusterkit_modular_plan test_clusterkit_suite_plan \
  test_clusterkit_service test_clusterkit_regressions -q
```

This is a targeted compatibility run, not a claim that the entire repository's
test suite or a live backend-to-Slurm connection has passed.

## AppServer Agent's Next Work

1. Review the return bundle's source revision and manifests. Its incremental
   patch is based on the exact changed-source files in the latest NC handoff,
   not on the older `9a450a6` export. Hash-check before applying; preserve any
   newer AppServer changes. A complete source archive is included for a fresh
   isolated workspace. Keep the running Rack Status installation separate.
2. Prepare a separate dependency-complete backend environment as `cmods`, using
   available offline packages/images and the site's established setup. The Mac
   virtual environment is not a Linux dependency bundle.
3. Re-run the HTTP/offline tests there. Verify the existing NC site profile,
   execution account, canonical shared results paths, script visibility and
   HPC-X/Slurm selections. Recheck current node availability and read-only API
   previews. Preserve the validated NC fabric configuration.
4. Report the isolated backend startup and exact proposed live preflight request.
   Obtain the user's live-test approval, then run one single-node preflight
   through preview -> confirmation -> submission -> status -> artifacts.
5. After reviewing that result, obtain approval for one two-node API lifecycle
   test. Keep 4/8-node tests and deployment as later steps.

The current execution recipe requires the backend to run locally as the intended
unprivileged validation account. SSH staging and collection remain unimplemented.
Functional completion and pair coverage do not establish fabric performance
acceptance: performance remains `UNVERIFIED` pending agreed thresholds.

Frontend implementation and deployment follow after the AppServer API lifecycle
is demonstrated; they are not part of this Mac verification handoff.
