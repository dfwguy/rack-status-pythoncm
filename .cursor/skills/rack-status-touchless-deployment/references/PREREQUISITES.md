# Unattended deployment prerequisites

The operator creates a `mode-0600` (or `0400`) shell environment file owned by the deployment
identity and sets `NVIS_DEPLOY_ENV` to its absolute path. The file is configuration, not a secret
store: secret values stay in separately protected files named by these variables.

## Required values

```bash
# The release artifact: a CI-built, pre-tested container image tarball and its published digest.
NVIS_IMAGE_TARBALL=/absolute/path/nvis-rack-status-nc-YYYYMMDD-N.tar.gz
NVIS_IMAGE_SHA256=<published-64-hex-digest-of-the-tarball>
NVIS_IMAGE_REF=nvis-rack-status:nc-YYYYMMDD-N        # the repo:tag the tarball loads

# This app server and the container it runs.
NVIS_DEPLOY_HOST=<exact-hostname-of-this-app-server>
NVIS_CONTAINER=nvis-rack-status-nc
NVIS_CONFIG_DIR=/cluster/nvis-rack-status-app/config  # holds rackstatus.toml, mounted :ro
NVIS_STATE_DIR=/cluster/nvis-rack-status-app/state    # identity/catalog/policies, mounted rw at /app/var
NVIS_WEB_PORT=5056
NVIS_BIND_ADDR=127.0.0.1
NVIS_VERIFY_URL=http://127.0.0.1:5056
NVIS_EVIDENCE_ROOT=/var/lib/nvis-rack-status-deploy/evidence

# Head-node connection: non-root identity, PINNED known-hosts, no sudo.
RACK_SSH_HOST=<bcm-head-host-or-ip>
RACK_SSH_USER=<non-root-service-account>
RACK_SSH_KEY_PATH=/absolute/path/to/existing-private-key
RACK_KNOWN_HOSTS_PATH=/absolute/path/to/pinned-known-hosts
RACK_REMOTE_BASE_PATH=/home/<service-account>/nvis-rack-status
RACK_BCM_CREDENTIALS_PATH=/absolute/path/to/existing-bcm-credential-directory
RACK_SCHEDULER=pythoncm

# Optional container caps and TLS trust.
NVIS_CPUS=16
NVIS_MEMORY=48g
NVIS_CA_CERT_FILE=/absolute/path/to/ca.pem           # only if the verify URL is HTTPS

# Smoke: denied by default. The three below are required only with NVIS_SMOKE_APPROVED=YES.
NVIS_SMOKE_APPROVED=NO
NVIS_SMOKE_PRESET=exemplar-system-info
NVIS_SMOKE_SCOPE=node
NVIS_SMOKE_NODES=<comma-separated-exact-node-list>

# First install only, when the head script directory does not yet exist (never inferred):
# NVIS_ALLOW_INITIAL_SCRIPT_INSTALL=YES
```

No `RACK_WEB_AUTH_TOKEN*` key exists: this product authenticates with SRP identity, not a shared
token (see SKILL.md). Supplying one is rejected by the allowlist.

## The environment syntax is deliberately narrow

One allowlisted `UPPER_CASE=value` per line, optional whole-value single/double quotes, comments on
their own line. No `export`, interpolation, command substitution, multiline values, or inline shell.
The parser never evaluates a value.

## The app-server identity must already have

- direct permission to `docker info`/`docker run`/`docker exec` — **do not use sudo**;
- noninteractive `BatchMode=yes` SSH to the head service identity with the **pinned** known-hosts
  file (`StrictHostKeyChecking=yes`);
- read access to the SSH key, pinned known-hosts, BCM credential directory, and (if HTTPS) the CA
  file — none of them group/world writable;
- write access to `NVIS_STATE_DIR`, `NVIS_EVIDENCE_ROOT`, and the deployment-owned state tree;
- `bash`, `sha256sum`, `tar`, `ssh`, `docker`, `curl`, `python3`, `find`, `xargs`.

The head-node identity needs only least-privilege PythonCM/BCM reads and write access to its own
`RACK_REMOTE_BASE_PATH`. Full sudo is neither required nor allowed by this skill.
