# FIRMUS (MEL01) — Project & Connection Guide

This folder (`nvis-mel01`) is the NVIS Rack Status deployment for the **Firmus**
engagement. The cluster lives in Melbourne and is still referred to as **MEL01**
in hardware/Slurm hostnames, but the dashboard's cluster display name has been
renamed to **FIRMUS** for readability.

> Heads-up on naming: the dashboard **display name** is `FIRMUS`, but the real
> BCM/Slurm rack and node hostnames still start with `MEL01-...` (e.g.
> `MEL01-HC07-SVR-CN0103`). The rack-ID / CT naming templates intentionally keep
> the `MEL01` prefix so `pdsh`/`sinfo`/rack-status probes target real hosts. Do
> **not** rename those templates to `FIRMUS-...`.

---

## Project at a glance

| Field | Value |
|---|---|
| Customer | **Firmus** |
| Project (catalog `name`) | **Firmus BK2** |
| Cluster display name | **FIRMUS** (was `MEL01`) |
| Cluster id (catalog) | `default` |
| Platform | `gb300-nvl72` — **GB300 NVL72** |
| Cluster manager | BCM (NVIDIA Base Command Manager) |
| Operator home | `cmsupport` (Boston uses `cmods`) |
| Rack-ID template | `MEL01-HC[\d+]-SVR-CN[01-99][01-99]` |
| CT (node) template | `MEL01-HC[\d+]-SVR-CN[01-99][01-99][01-99]` |
| Example real rack/node | `MEL01-HC07-SVR-CN0103` |

The project/cluster definition is stored in the catalog (`var/store/catalog.json`)
on the running container, project id `default`, cluster id `default`.

---

## How to connect

### 1. Be on the VPN
All access goes through the corporate VPN. Without it, none of the hosts below
are reachable.

### 2. Network path

```
your laptop ──VPN──▶ jump host (mel01-jump / meljump, 10.130.18.62, user nvis)
                         └─▶ head node mbcm01 (10.136.128.9)
                                  └─▶ Docker container "rack-status"  (publishes :5000)
```

The head node `mbcm01` (`10.136.128.9`) is **only reachable via the jump host** —
direct SSH from the laptop times out. The jump host is already configured in
`~/.ssh/config`:

```sshconfig
Host mel01-jump meljump
  HostName 10.130.18.62
  User nvis
  ForwardAgent yes
```

### 3. Web UI (the running service)

The production dashboard is a Docker container on the head node, published on
port **5000**:

- **URL (on VPN):** http://10.136.128.9:5000/spa/
- **Auth:** token mode — username `root`, password = `RACK_WEB_AUTH_TOKEN`
  (the shared secret stored in the container's environment; see
  [Secrets](#secrets)).

### 4. SSH to the head node

```bash
# Hop straight to the head node through the jump host
ssh -J meljump root@10.136.128.9

# One-shot remote command
ssh -J meljump root@10.136.128.9 'docker ps --filter name=rack-status'
```

---

## Operating the service

The dashboard runs as a Docker container named **`rack-status`** on `mbcm01`.

```bash
# Health / status
ssh -J meljump root@10.136.128.9 \
  "docker ps --filter name=rack-status --format '{{.Names}} {{.Status}} {{.Ports}}'"

# Is the web app responding?
ssh -J meljump root@10.136.128.9 \
  "curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:5000/spa/"

# Logs
ssh -J meljump root@10.136.128.9 "docker logs --tail 100 rack-status"

# Restart
ssh -J meljump root@10.136.128.9 "docker restart rack-status"
```

### Key container environment

| Var | Value |
|---|---|
| `PORT` | `5000` |
| `RACK_WEB_AUTH_MODE` | `token` |
| `RACK_WEB_AUTH_USER` | `root` |
| `RACK_WEB_PROJECT_NAME` | `Firmus BK2` (sidebar brand / page title) |
| `RACK_REMOTE_BASE_PATH` | `/home/cmsupport/nvis-rack-status` |
| `RACK_SSH_PORT` | `22` |

In SSH mode the container connects to the head node over **loopback**
(`ssh_host = 127.0.0.1`) — it SSHes back into `mbcm01` itself to run cmsh / Slurm
/ pdsh and the `bin/scripts` tooling under `RACK_REMOTE_BASE_PATH`.

---

## Local dev instance (optional)

To run a local dev server against this project from the `nvis-mel01` folder, use
port **5051** so it doesn't collide with the Boston instance on `5050`:

```bash
cd /Users/rmarcelinoko/code/nvis-mel01
PORT=5051 BUILD_FRONTEND=0 ./start.sh > /tmp/nvis-mel01.log 2>&1 &
# then open http://localhost:5051/
```

`BUILD_FRONTEND=0` skips the frontend rebuild for backend-only iteration. The
running production service does **not** depend on this local instance.

---

## Secrets

The web login password is the container's `RACK_WEB_AUTH_TOKEN`. It is **not**
committed to this repo. Retrieve it from the running container when needed:

```bash
ssh -J meljump root@10.136.128.9 \
  "docker exec rack-status env | grep RACK_WEB_AUTH_TOKEN"
```

Use that value as the password (username `root`) on the web UI and for any
authenticated `/api/*` calls (POST `/api/login` with `{"password": "<token>"}`
to obtain a session cookie).

---

## Renaming the cluster display name (how it was done)

The display rename (`MEL01` → `FIRMUS`) was applied live via the catalog API,
**without** touching the naming templates:

1. `POST /api/login` with the token → session cookie.
2. `GET /api/catalog/projects/default` → edit `clusters[].name` only.
3. `PUT /api/catalog/projects/default` with the full project doc.
4. Verify: `GET /api/catalog/active` → `cluster.name == "FIRMUS"`.

To change it again, repeat those steps. Never alter
`naming.rack_id_template` / `naming.ct_id_template` away from the real `MEL01-...`
hostnames.

---

## Cross-rack NCCL / RoCE validation — diagnosis & fix (playbook)

> **TL;DR** — Cross-rack `llmb-run microbenchmark_nccl` was collapsing to
> **~3.7 GB/s** because the Spectrum-X NCCL plugin failed to load (wrong path) and
> the RoCEv2 GID was never pinned, so NCCL silently fell back to **TCP over
> `bond0`**. Two one-line config fixes in the dgxc-benchmarking `systems.toml`
> took it to **826 GB/s @2-rack** and **643 GB/s @4-rack** with **0 timeouts**.
> No hardware changes.

### Problem statement

GB300 NVL72 racks talk NVLink **within** a rack, but multi-rack jobs (scale 144 /
288) must use the **RoCE / Spectrum-X (ConnectX-8)** fabric for the inter-rack
all-reduce. Cross-rack runs were producing near-zero bandwidth and the operator
log only showed a low `busbw`, with no obvious error — the job "passed" but at
TCP speed.

### Symptoms (log snippets that pinpoint it)

Baseline 2-rack all-reduce — bandwidth never ramps (a healthy run reaches
hundreds of GB/s):

```text
# Avg bus bandwidth    : 5.65289
 17179868160   ... float sum -1  4556006  3.77  3.74  0   ...   3.76  3.74  0
```

The decisive signatures are in the per-rank `nccl_debug_file.*.txt`:

```text
# (1) the Spectrum-X plugin is requested but NOT found -> fallback
NCCL INFO NCCL_NET_PLUGIN set by environment to spcx
NCCL INFO NET/Plugin: Could not find: spcx libnccl-net-spcx.so

# (2) with the plugin path fixed, plugin loads BUT RoCE never comes up ->
#     NCCL drops to the TCP socket transport (this is the ~3 GB/s)
NCCL INFO NET/IB : No device found.
NCCL INFO Using network Socket
NCCL INFO Channel 00/0 : 0[0] -> 72[0] [send] via NET/Socket/0
```

Container in-tree NCCL is **`2.29.3+cuda13.1`** — the buggy generation for
multi-plane SR-IOV ordering (see Mayo Clinic write-ups), so falling back to
in-tree is *not* a viable workaround here; the fixed **SPCX plugin** must run.

### Root cause (two independent config bugs)

1. **Plugin path wrong.** The recipe runs in the `nvidia+nemo+26.04.00` container
   and bind-mounts HPCX 2.26 at **`/opt/hpcx`**, but `systems.toml` set
   `LD_LIBRARY_PATH=/nvcomms-perf-workspace/hpcx/nccl_spectrum-x_plugin/lib` — a
   path that does not exist in the container → `Could not find: spcx` → fallback.
2. **RoCEv2 GID not pinned.** Without `NCCL_IB_GID_INDEX=3` (+ `NCCL_IB_TC=106`)
   the rails only expose a link-local IPv6 GID, so RoCEv2 QP setup has no usable
   entry → NCCL falls back to `NET/Socket` (TCP over `bond0`).

The fixed Spectrum-X plugin with the cross-plane ordering fix ships in
**HPCX ≥ 2.26 / DLFW ≥ 26.03** (`SPCX Ver. 1.3-0`, `Loaded net plugin SPCX (v11)`)
— confirmed by the NCCL service team. It is present on the head node at
`/home/cmsupport/workspace/hpcx-v2.26-.../nccl_spectrum-x_plugin/lib`, mounted to
`/opt/hpcx` inside the container.

### The fix (dgxc-benchmarking `systems.toml`, `[gb300.run_params].nccl`)

```toml
nccl = [
    "MELLANOX_VISIBLE_DEVICES=all",
    "NCCL_DEBUG=INFO",
    "NCCL_IB_HCA=roce_vf_r0,roce_vf_r1,roce_vf_r2,roce_vf_r3",
    "NCCL_P2P_NET_CHUNKSIZE=262144",
    "NCCL_NET_PLUGIN=spcx",
    "NCCL_IB_GID_INDEX=3",                                              # FIX: pin RoCEv2 GID
    "NCCL_IB_TC=106",                                                   # FIX: RoCEv2 traffic class
    "NCCL_IB_QPS_PER_CONNECTION=2",
    "NCCL_SOCKET_IFNAME=bond0",
    "LD_LIBRARY_PATH=/opt/hpcx/nccl_spectrum-x_plugin/lib:$LD_LIBRARY_PATH",  # FIX: was /nvcomms-perf-workspace/hpcx/...
    "UCX_NET_DEVICES=bond0",
    "NCCL_MIN_CTAS=16",
]
```

A correct run shows the data path on RoCE GPUDirect RDMA (not Socket):

```text
NCCL INFO NET/Plugin: Loaded net plugin SPCX (v11)
NCCL INFO Plugin Path : /opt/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so (SPCX Ver. 1.3-0)
NCCL INFO Using network SPCX
NCCL INFO Channel 00/0 : 0[0] -> 72[0] [send] via NET/SPCX/1/GDRDMA(PCI)
```

### Validation evidence (A/B)

| Job | Scale | Config | Transport | busbw @16 GiB | timeouts |
|---|---|---|---|---|---|
| 2152 | 2-rack (144) | broken plugin path | in-tree fallback | **3.74 GB/s** | 0 |
| 2180 | 2-rack (144) | plugin loads, no GID | `NET/Socket` (TCP) | ~3 GB/s | 0 |
| **2183** | **2-rack (144)** | **spcx + GID3 + TC106** | **`NET/SPCX GDRDMA`** | **826 GB/s** | 0 |
| **2187** | **4-rack (288)** | **spcx + GID3 + TC106** | **`NET/SPCX GDRDMA`** | **643 GB/s** | 0 |

### Where the fix lives / how durable it is

The fix is in the **recipe config**, not in `run_llmb_nccl.sh` — so **anyone**
running `llmb-run microbenchmark_nccl` (team or NVIS wrapper) gets it. Copies:

- `…/microbenchmark_nccl/Nvidia-Comms-Perf-Suite/config/systems.toml` — active at runtime ✅
- `dgxc-benchmarking/nccl/config/systems.toml` — source that `setup_nccl_launcher.sh` copies ✅

The whole Firmus RoCE block was added by the team into `nccl/config` (upstream's
`llmb_repo` template is a bare stub), so the fix is as durable as the existing
cluster tuning and survives `setup_nccl_launcher.sh` re-runs. A full from-scratch
LLMB reinstall would require re-applying the customization. `.bak-*` backups of
each edited file were left next to the originals.

**Caveats:** applies to the **`gb300`** profile + **`microbenchmark_nccl`**
workload only. Other LLMB workloads (e.g. nemo pretrain) carry their own env and
need the same `GID_INDEX=3` / `TC=106` / `/opt/hpcx` path for cross-rack RoCE.
This is a live head-node edit — get it into the team's git/IaC for the next
cluster (e.g. NSCALE USA).

### Unrelated blocker seen during scale-out: BCM prolog / cgroup drift

4-rack launches intermittently failed with the job held and **no log dir created**
("path does not exist"). Cause is **node health, not NCCL**:

```text
slurmd: error: cgroup namespace 'cpuset' not mounted. aborting
slurmd: error: prolog failed: rc:1 ... [job 2185] prolog failed status=1:0
sinfo -R:  MEL01-HC07-SVR-CN021211 | drain | Prolog error
```

Same OS image / kernel (`6.17.0-1014-nvidia-64k`) and `unified_cgroup_hierarchy=0`
on all nodes, but legacy **cgroup v1** controllers are mounted on-demand and
**nondeterministically at boot**, so some nodes end up missing `cpuset`/`memory`
→ Slurm cgroup plugin aborts → prolog drains the node. Durable fix (cluster/BCM
team): move the compute category to **cgroup v2 unified** (or pin the v1
controller mounts). Workaround: launch on racks whose nodes pass a pre-flight
check; drained nodes so far: `021211`, `021213`, `021411`.
