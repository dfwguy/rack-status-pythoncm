# Nscale TX PythonCM-primary deployment

This runbook applies only to Nscale TX. The authoritative feature/provider map is
[`../README_NSCALE_PYTHONCM_HYBRID.md`](../README_NSCALE_PYTHONCM_HYBRID.md),
with independent evidence in
[`NSCALE_PYTHONCM_HYBRID_AUDIT_2026-08-25.md`](NSCALE_PYTHONCM_HYBRID_AUDIT_2026-08-25.md).

## Required configuration

```bash
RACK_CLUSTER_ID=nscale-tx
RACK_CLUSTER_NAME=Nscale-TX
RACK_CLUSTER_CUSTOMER_NAME=Nscale
RACK_CLUSTER_PRODUCT_LINE=gb300
RACK_PLATFORM=bcm
RACK_SCHEDULER=pythoncm
```

The primary Nscale provider must be PythonCM. Explicit Slurm construction is
retained only inside the feature-specific fallback paths described in the map.

## Deployment requirement

This change adds `backend/pythoncm_validation.py`, which `backend/app.py` imports
at startup. The Nscale OVA's historical compose override mounts backend files
individually, so a backend-only file sync is not sufficient unless that new file
is also added to the override. Prefer rebuilding the image and recreating the
container (`docker compose up -d`), or first replace the per-file backend mounts
with the reviewed backend-directory mount. A plain `docker restart` cannot add a
missing mount. Run the repository deployment verifier after recreation.

PythonCM must run with the BCM-matched interpreter on the BCM head. Keep the
client key, cookie, and password files out of source packages and frontend
settings. Configure the relevant paths from:

```bash
NVIS_BCM_PYTHON=/cm/local/apps/python312/bin/python3
NVIS_PYTHONCM_PATH=/cm/local/apps/cmd/pythoncm
NVIS_BCM_HOST=127.0.0.1
NVIS_BCM_PORT=8081
NVIS_BCM_CERT=~/.cm/cert.pem
NVIS_BCM_KEY=~/.cm/cert.key
NVIS_BCM_CA=~/.cm/cluster-ca.pem
NVIS_BCM_COOKIE_FILE=~/.cm/cookie
# or NVIS_BCM_USERNAME plus NVIS_BCM_PASSWORD_FILE
```

## Persistent connection owner

Production Rack Status must not construct a new PythonCM `Cluster` for every
inventory refresh or ProgramRunner poll. Install and enable the supplied unit,
then expose its socket read-only to the container:

```bash
sudo install -m 0644 deploy/nvis-pythoncm-broker.service \
  /etc/systemd/system/nvis-pythoncm-broker.service
sudo systemctl daemon-reload
sudo systemctl enable --now nvis-pythoncm-broker.service
```

Set `NVIS_PYTHONCM_SOCKET=/run/nvis-pythoncm/query.sock`. The broker owns one
BCM-matched `Cluster`, serializes entity and ProgramRunner operations, sends a
60-second keepalive, reconnects failed reads, and is restarted indefinitely by
systemd. It is independent of the Rack Status container, so a web deployment
does not close the BCM session.

Certificate authentication is sufficient for entity/device reads on Nscale TX.
ProgramRunner, power, drain, and resume require a BCM login cookie. Put those
settings in the service user's mode-600
`~/.cm/pythoncm-broker.env` (for example `NVIS_BCM_COOKIE_FILE=...`) and restart
the broker; never put the cookie itself in the environment file or repository.

## Read-only preflight

Run from the same user/connection profile used by Rack Status:

```bash
"${NVIS_BCM_PYTHON:-/cm/local/apps/python312/bin/python3}" \
  bin/scripts/nvis_bcm_query.py --query version
"${NVIS_BCM_PYTHON:-/cm/local/apps/python312/bin/python3}" \
  bin/scripts/nvis_bcm_query.py --query racks
"${NVIS_BCM_PYTHON:-/cm/local/apps/python312/bin/python3}" \
  bin/scripts/nvis_bcm_query.py --query nodes
```

All commands must return one JSON object with `"ok": true`, the expected BCM
version, Nscale rack IDs, and real compute hostnames. Do not continue if a query
targets another CMDaemon or the CA is not verified.

## Reserved-node acceptance

After an administrator supplies ProgramRunner credentials and approves one
reserved node:

1. Submit a harmless exact-node ProgramRunner command.
2. Record its native UUID before waiting.
3. Verify running state, terminal state, exit code, stdout/stderr, and durable
   history across an application restart.
4. Verify `RACK_PYTHONCM_JOBS_DIR` has the required visibility from the BCM head
   and compute node; stop if it does not.
5. Submit a longer harmless command, cancel it, and prove the remote process is
   gone.
6. Drain and resume only the approved node, verifying state and reason through
   PythonCM.
7. Exercise power only with explicit authorization.
8. Exercise enabled read-only hardware/NVLink/fabric commands and verify exact
   placement and per-node output.

After PythonCM acceptance, validate one approved instance of each retained Slurm
recipe (NCCL, NeMo, Llama, GPT-OSS, Exemplar/system-info, ClusterKit, NVLoom,
and MNUberGEMM). Confirm numeric job-ID status, log discovery, cancellation, and
durable activity recovery. These are on-demand fallbacks and must not change the
provider used by `/api/status`.

A successful HTTP response is not acceptance. Every native UUID and process must
be accounted for, and no process may survive cancellation or failure.
