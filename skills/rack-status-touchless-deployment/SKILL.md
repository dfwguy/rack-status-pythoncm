---
name: rack-status-touchless-deployment
description: Install, configure, deploy, verify, smoke-test, and roll back NVIS Rack Status on a BCM/PythonCM cluster from the release tarball. Use when an operator asks Codex or Claude to deploy or upgrade Rack Status, configure its app-server-to-BCM connection, deploy validation scripts, stage validation containers, start the service, or launch a controlled validation smoke test without interactive prompts.
---

# Rack Status touchless deployment

Deploy only from the supplied Rack Status release tarball. Treat its extracted repository as the source of truth. Execute without interactive prompts, but never guess a value, credential, host, node, rack, container source, or authorization. A missing prerequisite is a hard stop with a precise remediation message.

## Safety contract

- Run Rack Status on the app/OVA server. Do not install application components on the BCM head node.
- Access the head node only through the configured non-root service identity and only for the documented PythonCM/BCM operations and checksum-based deployment of `bin/scripts/`.
- Never request or create full sudo access. Never run `sudo docker`; the app-server deployment identity must have direct Docker socket access. Never weaken `sudoers`, `mail_badpass`, root-mail forwarding, SSH host-key checking, TLS verification, PAM, or web authentication.
- Never query or poll `journalctl`, `systemd-journald`, or head-node logs for recurring observability. Rack Status uses PythonCM/BCM, Slurm feature endpoints, NHC/site health evidence, and Alloy/Prometheus data configured by the site.
- Keep all credentials outside Git and the tarball. Read them from existing files or mounted secrets. Do not print, copy into command lines, or synthesize them.
- Default to PythonCM/direct execution. Slurm is a feature-specific fallback, not the source for all cluster actions.
- Fail closed on live drift, unknown SSH host keys, TLS trust errors, stale validation plans, ineligible targets, missing images, partial GPU visibility, or failed verification.
- Do not use `--force`, `--delete`, `StrictHostKeyChecking=no`, `NVIS_SKIP_VERIFY`, or authentication-disable flags.
- A smoke launch is authorized only when `NVIS_SMOKE_APPROVED=YES` and exact target nodes and a preset are supplied in the sealed environment file.

## Required input

Find exactly one downloaded `NVIS_Rack_Status_*.tar.gz`, unless the operator supplied an explicit path. Verify its published SHA-256 before extraction. Keep this file with its supplied `scripts/` and `references/` directories: they are required deployment implementation, not optional examples. Never synthesize an alternative deployment command sequence.

Require a mode-0600 environment file named by `NVIS_DEPLOY_ENV`. Require absolute release archive, release checksum, app-server deployment root and hostname, fixed Compose project/container/service names, evidence root, verification URL, non-root head-node host/user/key/pinned-known-hosts, remote script base, web-token file, BCM credential directory, `RACK_SCHEDULER=pythoncm`, and validation-container root. A smoke is optional and defaults to denied; exact preset/scope/nodes are required only with `NVIS_SMOKE_APPROVED=YES`. Values may name credential files, but must not contain plaintext passwords or API tokens. The implementation parses a strict allowlist and never shell-sources the operator file.

## Procedure

1. Read `references/PREREQUISITES.md` and `references/OPERATIONS.md` completely. Confirm the operator already created the sealed file; do not ask for values interactively.
2. Run exactly:

   ```bash
   NVIS_DEPLOY_ENV=/absolute/path/deploy.env scripts/deploy.sh
   ```

3. Return the script's terminal status (`DEPLOYED`, `ROLLED_BACK`, or `BLOCKED`) and report path. Do not claim success from an intermediate message.

`deploy.sh` is the idempotent state machine. It strictly parses configuration; checks the artifact; safely extracts only regular files/directories into mode-0700 staging; runs the secret, backend, frontend, type/build, and shell gates; hard-gates attributable app and remote-script drift; records rollback evidence; installs into a content-addressed release directory; renders and hashes the exact Compose configuration; uses a token file; builds the SPA/image; synchronizes scripts through pinned non-root SSH without calling `bin/deploy-scripts.sh`; verifies every released script checksum while preserving head-only files; recreates through direct Docker Compose; attests container/image/build/API state; atomically selects the release; and optionally launches and tracks an exact-node smoke to terminal evidence.

Checkpoints under the protected state root make a failed pre-mutation run restartable. Once remote scripts or the service are mutated, any later failure automatically calls `scripts/rollback.sh`. Do not delete or edit checkpoints by hand.

## Failure and rollback

Stop immediately when a gate fails. Do not improvise a repair on production. `deploy.sh` automatically invokes `scripts/rollback.sh --state-dir <run-state>` after mutation: it restores the checksum-protected prior remote script tree, removes only release-introduced paths, recreates the prior content-addressed image/configuration, selects the prior release, and verifies it. It does not roll back durable `var/` evidence, secret files, operator data, or unrelated jobs.

Return a concise final status: `DEPLOYED`, `ROLLED_BACK`, or `BLOCKED`, the exact failed gate if any, and paths to the redacted evidence. Never include secret contents.
