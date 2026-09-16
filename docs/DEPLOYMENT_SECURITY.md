# Deployment security controls and residual exceptions

## Release controls

Production script synchronization is fail-closed. `bin/deploy-scripts.sh`
requires an explicit non-root SSH identity and a caller-supplied pinned
`known_hosts` file. It sets `BatchMode=yes`, `StrictHostKeyChecking=yes`, and
`UserKnownHostsFile=<approved file>` for the rsync transport. It rejects root,
implicit SSH-config users, missing host-key files, and symlinked identity or
host-key files.

Example:

```bash
RACK_SSH_HOST=192.168.10.2 \
RACK_SSH_USER=cmods \
RACK_SSH_KEY="$HOME/.ssh/id_bcm" \
RACK_KNOWN_HOSTS_PATH="$HOME/.ssh/nscale-tx-known-hosts" \
bin/deploy-scripts.sh --dry-run
```

Verify the host-key file fingerprint through an approved out-of-band channel
before deployment. Never populate it with an unattended `ssh-keyscan` whose
result is trusted without verification.

Dashboard token authentication is file-first. Compose mounts the host path in
`RACK_WEB_AUTH_TOKEN_PATH` as `/run/secrets/rack-web-auth-token`, and the app
reads it through `RACK_WEB_AUTH_TOKEN_FILE`. The legacy `RACK_WEB_AUTH_TOKEN`
environment value is supported only for rolling upgrades. After migration:

1. Verify login with the file-backed token.
2. Remove `RACK_WEB_AUTH_TOKEN` from Compose overrides, environment files, and
   service-manager configuration.
3. Recreate the container and confirm `docker inspect` contains no token value.
4. Rotate the old token because it was previously inspectable.

Never log, echo, render, checksum, or copy the token into deployment evidence.
It is sufficient to record the file path, owner, mode, and whether it is
non-empty.

## Residual interactive-terminal exception

This hardening does **not** claim that every runtime SSH path is pinned yet.
The legacy interactive web-terminal transport in `backend/host_exec.py` still
uses permissive host-key options to preserve the browser terminal's established
multi-hop behavior. That exception is not used by `bin/deploy-scripts.sh`, and
it must not be used for unattended deployment or validation orchestration.

Until the terminal transport is refactored and tested with per-endpoint pinned
host keys, operators should disable or access-restrict the interactive terminal
in production and treat use of that feature as an explicitly accepted residual
risk. Do not generalize this exception to deployment, background jobs, or new
transport code.
