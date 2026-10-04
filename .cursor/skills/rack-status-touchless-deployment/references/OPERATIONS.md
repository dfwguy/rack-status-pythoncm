# Deployment gates and evidence

## Gate order

1. **preflight** — image tarball digest + `repo:tag` shape; local tools; declared-host identity;
   protected-file permissions; pinned known-hosts non-empty; Docker-without-sudo; pinned-host-key
   head reachability; deployment-owned writable paths. Changes nothing.
2. **load** — `docker load` the tarball; confirm the declared image is present and its architecture
   matches the host (a cross-arch image loads cleanly and fails only at runtime). Records the exact
   image ID.
3. **drift** — refuse to deploy over a container with live edits under `/app` (`docker diff`); stage
   the image's own `bin/scripts` and compare against the head's canonical tree (resync is a
   checksum-verified operation, so head drift is recorded, not blocked).
4. **snapshot** — capture the prior container's image ref + ID and a tar of the head `bin/scripts`,
   so rollback is a deterministic recreate.
5. **sync-scripts** — checksum-verified sync of the image's `bin/scripts` to the head, staged then
   swapped, never deleting unrelated head-only files. *(first mutation)*
6. **deploy** — recreate the container from the loaded image with the config/state/ssh-key bind
   mounts, and pin it to the exact image ID (not a tag that may be repointed).
7. **verify** — the repository's own `bin/verify-deploy.sh` (extracted from the image) against the
   live container, plus container attestation and an unauthenticated `/api/status` ping.
8. **activate** — record the active image ref/ID and config/state directories as the current
   deployment marker.
9. **smoke** — optional and denied by default; only with sealed `NVIS_SMOKE_APPROVED=YES` and
   eligibility-checked nodes.

Mutations begin at gate 5. Any failure from gate 5 onward triggers `rollback.sh` automatically.

## State machine and restart behavior

`scripts/deploy.sh` keys a `mode-0700` run directory by the image SHA-256 under
`NVIS_EVIDENCE_ROOT/state/<sha>`. Every completed phase writes a durable `checkpoints/<phase>.done`,
and the redacted `deployment-report.txt` records each gate's PASS/SKIP/FAIL. Preflight, load, drift,
and snapshot precede the first mutation. A repeat invocation with the same sealed environment
resumes from checkpoints rather than repeating work or inventing commands.

## Rollback

`rollback.sh` reads the `mode-0600` `rollback.env` written by snapshot, restores the head
`bin/scripts` from the captured tar (removing only files this deployment newly introduced), and
recreates the container from the exact prior image ref on the unchanged config/state mounts — or
removes the container on a first install. It then confirms the restored service answers
`/api/status`. The config and state directories are never rewritten by a deploy, so the prior image
on the same mounts is a faithful restore.

## What it deliberately does NOT do

- No on-target source build or source test (the image is CI-built and CI-tested).
- No `sudo`, no `StrictHostKeyChecking=no`, no authentication-disable flags.
- No web-auth token: this product uses SRP identity, so verification is unauthenticated.
- No application component installed on the BCM head node — only the checksum-verified `bin/scripts`
  sync to the head's `RACK_REMOTE_BASE_PATH`.
