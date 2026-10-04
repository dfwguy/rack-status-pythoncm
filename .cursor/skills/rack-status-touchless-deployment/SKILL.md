---
name: rack-status-touchless-deployment
description: Install, upgrade, verify, and roll back NVIS Rack Status on a BCM/PythonCM cluster from a pre-built, checksum-pinned container image tarball, without interactive prompts. Use when an operator asks Codex or Claude to deploy or upgrade Rack Status, swap the app-server container to a new image, sync the head-node validation scripts, or run a controlled, approved validation smoke test.
---

# Rack Status touchless deployment

Deploy only from the supplied, CI-built Rack Status **image tarball**. Execute without interactive
prompts, but never guess a value, credential, host, node, rack, image, or authorization. A missing
prerequisite is a hard stop with a precise remediation message, not an assumption.

**This repository's deployment model (and how it differs from a source deploy).** Rack Status is
shipped air-gapped as a container image built and fully tested in CI off-box. The app server
`docker load`s that image and recreates the container over its config and state bind mounts. There
is **no on-target source build and no on-target source test** — the image is the artifact. The
deployment gate proves the running product works via `bin/verify-deploy.sh`, which is extracted
from the image so the check matches what shipped.

**Authentication is SRP identity, not a shared web token.** The identity store bootstraps a single
administrator on first boot (one-time password, must-change-at-first-login); there is no shared
`RACK_WEB_AUTH_TOKEN` to mount and no cookie login to perform. Verification is therefore
unauthenticated — `/api/status` answers the real health with no credential — and the skill never
reads, synthesizes, mounts, or logs a password or token.

## Safety contract

- Run Rack Status on the app/OVA server. Do not install application components on the BCM head node.
- Reach the head node only through the configured non-root service identity with a **pinned
  known-hosts file** (`StrictHostKeyChecking=yes`), and only for documented PythonCM/BCM reads and
  the checksum-verified sync of `bin/scripts`.
- Never request or create full sudo. Never run `sudo docker`; the deployment identity must have
  direct Docker socket access. Never weaken `sudoers`, SSH host-key checking, TLS verification, PAM,
  or web authentication.
- Keep all credentials outside Git and the image. Read them from existing files or mounts. Never
  print, copy into command lines, or synthesize a credential.
- Default to PythonCM/direct execution; Slurm is a feature-specific fallback.
- Fail closed on live container drift, unknown SSH host keys, TLS trust errors, a cross-architecture
  image, ineligible smoke targets, or failed verification.
- Do not use `--force` in ways that skip a gate, `StrictHostKeyChecking=no`, or any
  authentication-disable flag.
- A smoke launch is authorized ONLY when `NVIS_SMOKE_APPROVED=YES` with an exact preset, scope, and
  node list in the sealed environment file.

## Required input

One `mode-0600` (or `0400`) environment file, its absolute path in `NVIS_DEPLOY_ENV`, owned by the
deployment identity. It lists an absolute **image tarball** path and its published SHA-256, the
image `repo:tag`, the app-server hostname, the container name, the config and state directories, the
web port and verification URL, an evidence root, the non-root head host/user/key/**pinned
known-hosts**, the remote script base, the BCM credential directory, and `RACK_SCHEDULER`. A smoke
is optional and defaults to denied. Values may name credential *files* but must never contain a
plaintext password or token. See [references/PREREQUISITES.md](references/PREREQUISITES.md) for the
complete list and [references/OPERATIONS.md](references/OPERATIONS.md) for the gate order and
evidence.

## Procedure

1. Read `references/PREREQUISITES.md` and `references/OPERATIONS.md`.
2. Run `scripts/preflight.sh` and stop on any `BLOCKED` line, quoting its remediation.
3. Run `scripts/deploy.sh`. It is a checkpointed state machine keyed by the image digest:
   preflight → load (verify digest + architecture) → drift → snapshot → sync-scripts → deploy →
   verify → activate → optional smoke. Every phase writes a durable checkpoint and a redacted
   report line; a repeat run with the same sealed environment resumes rather than repeating.
4. On any failure after the first mutation, the state machine runs `scripts/rollback.sh`
   automatically, restoring the exact prior image and the head-node script snapshot. Report the
   `BLOCKED` gate and the report path; do not improvise a recovery.
5. Report the `DEPLOYED` line (image ref, container, report path). Never claim success without the
   `VERIFY_OK` line in the report.

The implementation never shell-sources the operator file; it parses a strict `UPPER_CASE=value`
allowlist and evaluates nothing.
