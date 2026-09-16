# Unattended deployment prerequisites

The operator creates a mode-0600 shell environment file and sets `NVIS_DEPLOY_ENV` to its absolute path. The file is configuration, not a secret store: secret values remain in separately protected files.

Required values:

```bash
NVIS_RELEASE_TARBALL=/absolute/path/NVIS_Rack_Status_16K_PythonCM_YYYYMMDD.tar.gz
NVIS_RELEASE_SHA256=<published-64-hex-digest>
NVIS_DEPLOY_PATH=/opt/observability/nvis-rack-status-v3
NVIS_DEPLOY_HOST=<exact-hostname-of-this-app-server>
NVIS_COMPOSE_PROJECT=nvis-rack-status
NVIS_CONTAINER=nvis-rack-status-v3
NVIS_COMPOSE_SERVICE=rack-status
NVIS_VERIFY_URL=http://127.0.0.1:5056
NVIS_SITE_PROFILE=nscale-tx
NVIS_EVIDENCE_ROOT=/var/lib/nvis-rack-status-deploy/evidence

RACK_SSH_HOST=<bcm-head-host-or-ip>
RACK_SSH_USER=<non-root-service-account>
RACK_SSH_KEY_PATH=/absolute/path/to/existing-private-key
RACK_KNOWN_HOSTS_PATH=/absolute/path/to/pinned-known-hosts
RACK_REMOTE_BASE_PATH=/home/<service-account>/nvis-rack-status

RACK_WEB_AUTH_TOKEN_FILE=/absolute/path/to/existing-web-token-file
RACK_BCM_CREDENTIALS_PATH=/absolute/path/to/existing-bcm-credential-directory
RACK_SCHEDULER=pythoncm
RACK_VALIDATION_CONTAINER_ROOT=/absolute/shared/container/cache

NVIS_SMOKE_APPROVED=NO
NVIS_STAGE_APPROVED=NO
NVIS_STAGE_ONLY=false
# The following are required only when NVIS_SMOKE_APPROVED=YES:
NVIS_SMOKE_PRESET=exemplar-system-info
NVIS_SMOKE_SCOPE=node
NVIS_SMOKE_NODES=<comma-separated-exact-node-list>
```

Optional values include `RACK_SSH_PORT`, `NVIS_CA_CERT_FILE`, `NVIS_COMPOSE_OVERRIDE`, `NVIS_STATE_ROOT`, archive expansion limits, explicit Python/npm binaries, smoke timeout/poll interval, and a site-approved immutable validation artifact/digest consumed by the selected preset. For a first installation where the remote script directory is absent, seal `NVIS_ALLOW_INITIAL_SCRIPT_INSTALL=YES`; it is never inferred. `NVIS_STAGE_APPROVED=YES` plus `NVIS_STAGE_ONLY=true` is valid only for the reviewed MNUberGEMM stage-only API plan. The pinned known-hosts file must identify the exact configured head endpoint.

The environment syntax is deliberately narrower than shell syntax: one allowlisted `UPPER_CASE=value` assignment per line, optional whole-value single/double quotes, and comments only on their own lines. Do not use `export`, interpolation, command substitution, multiline values, or inline shell commands. The parser never evaluates the values.

The app-server identity must already have:

- direct permission to `docker info` and `docker compose`; do not use sudo;
- noninteractive `BatchMode=yes` SSH access to the configured head-node service identity;
- read access to the existing SSH, BCM, web-token, CA, and container-source files;
- write access to the deployment tree and deployment-owned validation/evidence locations;
- network access to the approved image source, or a complete air-gapped image set with published digests;
- `bash`, `git`, `sha256sum`, `tar`, `curl`, `jq`, `rg`, Python, and the pinned Node/npm toolchain required by the release.

The head-node identity needs only the least privileges documented by the site for PythonCM/BCM read operations, validation task submission, and its own `RACK_REMOTE_BASE_PATH`. Full sudo is neither required nor allowed by this skill.

Before invoking an agent, the operator approves the exact smoke preset and nodes in the environment file. The agent does not ask questions mid-run. Any missing, ambiguous, or inconsistent prerequisite produces `BLOCKED` before mutation.
