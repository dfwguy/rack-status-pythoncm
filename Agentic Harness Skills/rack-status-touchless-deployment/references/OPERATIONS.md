# Deployment gates and evidence

## Gate order

1. Release digest and archive safety.
2. Local tools, permissions, pinned SSH identity/host keys, Docker-without-sudo, BCM/PythonCM reachability.
3. Source tests, type checks, shell/static checks, and secret scan.
4. Live application and head-script drift.
5. Rollback snapshot.
6. SPA/image build and configuration validation.
7. Canonical script sync and checksum verification.
8. Direct Compose recreate and repository deployment verification, with exact container/image/build attestation.
9. Read-only API checks.
10. Exact-target container stage and validation smoke launch.
11. Terminal activity, evidence, and parser verification.

Never reorder the mutation steps ahead of gates 1–5. Never call the repository's legacy
`bin/deploy-scripts.sh`; the skill state machine performs its own pinned-host-key, non-root,
checksum-verified synchronization without deleting head-only files.

## State machine and restart behavior

`scripts/deploy.sh` keys its mode-0700 run directory by release SHA-256. Every completed phase has a
durable checkpoint and the redacted report records pass/skip/failure. Preflight, extraction, tests,
drift, and rollback capture precede the first production mutation. A repeat invocation with the same
sealed environment resumes checkpoints rather than inventing commands or duplicating work.

The application is installed under `NVIS_DEPLOY_PATH/releases/<release-sha256>` and selected through
the atomic `current` symlink. The prior image ID and exact Compose rendering are preserved until
the new release verifies. This provides deterministic recreation without retaining a conflicting
Compose-labeled container. If script synchronization,
Compose recreation, verification, activation, or the authorized smoke fails, rollback restores the
prior scripts and container and removes only paths proven to have been introduced by this release.

## Read-only health expectations

The service health URL must respond without transport errors. Authenticated checks should cover `/api/status`, `/api/validation-activities`, `/api/post-run-node-report`, `/api/reports/generated`, and `/api/metrics/health`. A metrics dependency may report a documented degraded state, but Rack Status itself, its durable activity registry, PythonCM inventory, mounted modules, and matching SPA assets must be healthy.

Do not diagnose health by polling `journalctl`. Use container stdout only for bounded deployment verification, plus the product APIs and the site's Alloy/Prometheus/NHC evidence.

## Validation acceptance

The smoke plan and launch must use the same server-generated plan hash. Record the requested nodes and the resolved nodes and require exact set equality. Require every target to be eligible, every validation artifact to match its immutable provenance, and every expected GPU to be visible. Track both the application job and durable validation activity. A provider submission without terminal evidence is not success.

## Rollback record

Record, without secrets:

- UTC start/end, operator identity, hostname, site profile;
- release filename and SHA-256, Git commit/build ID;
- previous and new container image IDs/digests;
- redacted normalized Compose configuration hash;
- deployment-owned file backup path and checksum;
- previous and new `bin/scripts/` tree checksums;
- output of both verification gates;
- smoke preset, exact nodes, plan hash, activity/job IDs, terminal state;
- rollback action and post-rollback verification when applicable.

Backups must exclude `deploy-secrets/`, private keys, token contents, `.env`, runtime `var/`, and validation evidence. Those are preserved in place, not copied into an artifact.

## Hard-stop conditions

- wrong/missing SHA-256, unsafe archive members, or multiple ambiguous releases;
- writable-by-group/world environment or credential files;
- Docker access works only through sudo;
- unknown SSH host key, interactive authentication, root target, or TLS failure;
- head node is selected as the application deployment host;
- dirty/drifted live deployment or remote script tree;
- test, build, secret, import, route, mount, or SPA parity failure;
- missing/ineligible/extra validation node, stale plan hash, missing image/digest, partial stage;
- request to disable security notifications, authentication, verification, or observability controls.

Report the exact gate and remediation. Do not prompt, retry with weaker controls, or continue partially.
