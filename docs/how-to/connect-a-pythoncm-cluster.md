---
diataxis: how-to
description: Everything a BCM head needs before Rack Status can drive it through PythonCM — the API-capable cert, a readable CA, the deployed helper scripts, and the NVIS_BCM_* knobs — plus the probe that proves it.
---

# How to connect a PythonCM (BCM) cluster

On a Bright Cluster Manager cluster, Rack Status talks to CMDaemon through **PythonCM** for the
node roster, per-node rack/slot placement, device details, and job lifecycle. PythonCM is version-
locked to the head's `cmdaemon`, so the app never imports it in-process: it ships two helper scripts
(`nvis_pythoncm_exec.py`, `nvis_bcm_query.py`) that run **on the head over SSH**. Getting this
working the first time meant discovering several prerequisites that nothing checks for you. Prepare
them *before* you connect and the bring-up is uneventful.

Set `RACK_SCHEDULER = "pythoncm"` on the cluster (the field is on the Projects & Clusters record;
see [Projects and clusters](../reference/catalog/projects-and-clusters.md)). Everything below is
what that scheduler needs on the head.

## Prerequisites on the head (ask your BCM admin)

These require root on the head, and the SSH user Rack Status connects as (e.g. `rkoike`) usually
has **no passwordless sudo on the head** — only on the app server — so a cluster admin has to do
them. Do all three before connecting.

### 1. An API-capable certificate for the SSH user

The SSH user's BCM certificate needs a profile that permits the CMDaemon **API session** methods.
A `breakfix` (or `readonly`) cert can drive `cmsh`'s read-only views but **cannot open a PythonCM
session** — the probe fails with `No valid cookie provided, did your session end?`. Check the
profile and, if needed, issue an `admin`-profile cert:

```bash
# On the head, as an admin:
cmsh -c "main; showprofile"          # 'breakfix'/'readonly' => the API will be refused
cmsh -c "cert; createcertificate 4096 nvis-rack-status NVIDIA NVIS SantaClara CA US admin <ssh-user> 3650 /home/<ssh-user>/.cm/app-cert.key /home/<ssh-user>/.cm/app-cert.pem"
chown <ssh-user>:<group> /home/<ssh-user>/.cm/app-cert.key /home/<ssh-user>/.cm/app-cert.pem
chmod 600 /home/<ssh-user>/.cm/app-cert.key /home/<ssh-user>/.cm/app-cert.pem
```

The `createcertificate` argument order is fixed by your BCM version — confirm with
`cmsh -c "cert; help createcertificate"`. The app submits and cancels jobs (CMJob write), so a
read-only profile is not enough; `admin` is the reliable choice.

Alternatively, elevate the user's existing `~/.cm/cert.pem` profile to `admin` and skip the extra
files — then the helper's defaults (`~/.cm/cert.pem` + `~/.cm/cert.key`) already work.

### 2. A CA the SSH user can read

The helper verifies CMDaemon's (self-signed) certificate against a CA. Its default,
`/cm/local/apps/cmd/etc/cacert.pem`, is **`root`-only (0600)**, so the SSH user can't read it and
the probe fails with `PermissionError: [Errno 13] Permission denied`. Place a readable copy:

```bash
# On the head, as an admin:
install -o <ssh-user> -g <group> -m 0600 /cm/local/apps/cmd/etc/cacert.pem /home/<ssh-user>/cacert.pem
```

Point the app at it with `NVIS_BCM_CA` (below). Do **not** substitute the user's own `cert.pem` for
the CA — that fails with `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`.

### 3. PythonCM importable on the head

The helpers run under BCM's bundled interpreter, `/cm/local/apps/python312/bin/python3` (the distro
Python usually lacks PythonCM's deps). If PythonCM lives somewhere non-standard, note the path for
`NVIS_PYTHONCM_PATH`; otherwise the helper auto-discovers it.

## Steps

### 1. Deploy the helper scripts to the head

This is a **separate step from `docker compose up`** — the container ships the scripts but does not
push them. They land in the layered script tree at `RACK_REMOTE_BASE_PATH/scripts/multi-platform/`
(not the legacy `.../bin/scripts`). Push them from the Projects & Clusters page (**Deploy scripts to
head**), the first-run wizard, or `POST /api/scripts/deploy`; from a workstation, `bin/deploy-scripts.sh`.
See [How a script path is chosen](../explanation/how-a-script-path-is-chosen.md).

If the head's scripts directory is empty, every probe fails with
`can't open file '.../nvis_pythoncm_exec.py'`.

### 2. Configure the connection and the BCM knobs

On the cluster record (or `config/rackstatus.toml`):

```toml
[connection]
RACK_SSH_HOST = "<head-node-ip>"     # BCM head; CMDaemon listens on localhost there
RACK_SSH_USER = "<ssh-user>"
RACK_REMOTE_BASE_PATH = "/home/<ssh-user>/nvis-rack-status"

[bcm]
RACK_MODULES_INIT = "/etc/profile.d/modules.sh"   # a non-login shell has an empty MODULEPATH
RACK_CMSH = "/cm/local/apps/cmd/bin/cmsh"         # cmsh is not on the non-login PATH
NVIS_BCM_CA = "/home/<ssh-user>/cacert.pem"       # the readable CA from prereq 2
# Only if not using the ~/.cm defaults (prereq 1):
# NVIS_BCM_CERT = "/home/<ssh-user>/.cm/app-cert.pem"
# NVIS_BCM_KEY  = "/home/<ssh-user>/.cm/app-cert.key"
```

The helper connects to `localhost:8081` by default; override with `NVIS_BCM_HOST` / `NVIS_BCM_PORT`
only if your CMDaemon differs. `NVIS_BCM_PYTHON` overrides the head interpreter;
`NVIS_PYTHONCM_PATH` the module path. These are forwarded to the helper as `--ca`/`--cert`/`--key`/
`--host`/`--port`/`--module-path` — the app passes only *paths* on the argv, never a secret value.

If the head's hostnames don't embed the rack (e.g. `sys1-gso1-p-phy-gpu1-su19`), also set the
cluster's **rack mapping** to *translate* and supply the SU list — see
[Geometry and hostnames](../explanation/geometry-and-hostnames.md).

### 3. Verify

Run the probe on the head as the SSH user — it should print `{"ok": true, "ready": true}`:

```bash
/cm/local/apps/python312/bin/python3 \
  /home/<ssh-user>/nvis-rack-status/scripts/multi-platform/nvis_pythoncm_exec.py probe \
  --ca /home/<ssh-user>/cacert.pem
# add --cert/--key if you created a dedicated app cert
```

Then restart the container so the app re-reads the config, and confirm the fleet populates (racks
with their node totals). In the app, `scheduler_watch` stops logging `pythoncm not ready`.

## Troubleshooting — the error tells you the layer

| Probe / log message | Cause | Fix |
|---|---|---|
| `can't open file '.../nvis_pythoncm_exec.py'` | helper scripts not on the head | deploy scripts (step 1) |
| `PermissionError: [Errno 13] Permission denied` | default CA is `root`-only | readable CA + `NVIS_BCM_CA` (prereq 2) |
| `CERTIFICATE_VERIFY_FAILED: self-signed … in chain` | `--ca` points at the client cert, not the CA | point `NVIS_BCM_CA` at the real CA copy |
| `No valid cookie provided, did your session end?` | cert profile can't use the API (`breakfix`/`readonly`) | `admin`-profile cert (prereq 1) |
| `pythoncm is not importable on this host` | wrong interpreter / module path | `NVIS_BCM_PYTHON`, `NVIS_PYTHONCM_PATH` |

Each of these is the same identity presented through the wrong file or the wrong permission, so read
the message literally — it names which of the four prerequisites is missing.
