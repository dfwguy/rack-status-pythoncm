---
diataxis: how-to
description: Stage the NVIS observability node-agents to a cluster when the source repo is NVIDIA-internal — node arch, binary versions, the shared FS layout, and relay placement.
---

# How to stage the observability node-agents to a cluster

**Problem this solves.** The observability stack lives in `nvis-ova/nvis-observability-ncp` on
**`gitlab-master.nvidia.com`, which is NVIDIA-internal** — it does not resolve or route from a
customer/air-gapped cluster (confirmed on NC: `gitlab-master.nvidia.com` fails DNS from the
appserver). So you **cannot `git clone` it on the cluster**. You must *stage*: get the repo content
onto the cluster from an NVIDIA-connected host, then run the staging there against the **public**
internet (github.com + nvcr.io), which the cluster's app/obs host usually can reach.

The agents (`dcgm-exporter`, `node_exporter`, `cgroup_exporter`, `alloy`) are installed on compute
nodes by `install-node.sh`, which copies the binaries from a **cluster-shared directory** (`$SHARE`,
e.g. `/cm/shared/nvis/bin` — NFS-mounted on every node) and renders the systemd units from env vars.
Staging is the act of populating that shared directory.

---

## Prerequisites

| Role | Needs |
|---|---|
| **Connected host** (your laptop / a corp box) | access to `gitlab-master.nvidia.com` (token or SSH key) to get the repo |
| **Staging host** (a cluster host) | **public** internet to `github.com` + `nvcr.io`; `docker` (root/sudo); write access to the cluster-shared FS (`/cm/shared`). On NC the **appserver** qualifies. It does **not** need internal GitLab. |
| **Shared FS** | `/cm/shared` (or equivalent) mounted on the staging host, the head, and all compute nodes |

---

## Step A — Get the repo onto the cluster (the internal→cluster bridge)

The cluster can't reach internal GitLab, so pull the content where you *can* and carry it over.

```bash
# On an NVIDIA-connected host (laptop/corp network). Either:
git clone https://gitlab-master.nvidia.com/nvis-ova/nvis-observability-ncp.git
# …or, if you only want the agents, download just node-agents/ via the GitLab API with a token,
#   or grab the repo archive from the GitLab UI. Only the node-agents/ tree is required for staging.

# Transfer the node-agents/ tree to the staging host:
scp -r nvis-observability-ncp/node-agents <staging-host>:/home/<you>/ncp-node-agents
```

> Why not just run it on the connected host? Because `stage-binaries.sh` must write the **cluster's**
> shared FS, which the connected host usually can't mount. Run the content *on the cluster*.

---

## Step B — ⚠️ Stage for the COMPUTE-NODE architecture, not the staging host's

`stage-binaries.sh` auto-detects the arch of the **host it runs on** — which is wrong when the
staging host (appserver/head) differs from the compute nodes. **GB300 is Grace = `arm64`, but the
NC appserver/head are `x86_64`.** Staging on the appserver without override produces useless amd64
binaries.

**Confirm the compute-node arch** (no node SSH needed) from the BCM software images on the head:

```bash
ssh <head> 'ls -d /cm/images/*/'      # look for a *-aarch64 image or the node's dgxos image
# e.g. on NC: default-image-ubuntu2404-aarch64, dgxos-7.5-dell-gb300-ib  → arm64
```

Then **force `GOARCH`** when staging (the upstream script only reads `uname -m`; the patched copy in
this repo's sibling tooling accepts a `GOARCH` env override — add the same two lines if you use a
pristine clone):

```bash
# make GOARCH overridable in stage-binaries.sh:
#   if [[ -z "${GOARCH:-}" ]]; then  case "$(uname -m)" in ... esac  fi
```

Also extract `dcgm-exporter` for the node arch: pull/`create` the image with
`--platform=linux/${GOARCH}` (the patched script does this).

---

## Step C — Verify the binary versions before running (they go stale)

The upstream default versions can 404 as releases move. **Probe each release URL (HTTP 200) first**;
the release *download* URLs are a CDN and are **not** API-rate-limited (the GitHub *API* is, from the
shared NVIDIA egress IP). Known-good as of this writing:

| Binary | Version | arm64 asset (must return 200) |
|---|---|---|
| node_exporter | 1.8.2 | `node_exporter-1.8.2.linux-arm64.tar.gz` |
| alloy | 1.5.1 | `alloy-linux-arm64.zip` |
| **cgroup_exporter** | **1.3.0** | `cgroup_exporter-1.3.0.linux-arm64.tar.gz`  *(upstream default `0.10.3` 404s)* |
| dcgm-exporter | from image | `nvcr.io/nvidia/k8s/dcgm-exporter:3.3.9-3.6.1-ubuntu22.04` (multi-arch: amd64+arm64) |

Latest `cgroup_exporter` without the API: `curl -sI https://github.com/treydock/cgroup_exporter/releases/latest | grep -i location`.

---

## Step D — NGC / nvcr.io

`dcgm-exporter` ships only inside NVIDIA's container image. On NC it pulls **unauthenticated** — **no
NGC key required**. If a given cluster's nvcr.io path needs auth: `docker login nvcr.io` (user
`$oauthtoken`, password = NGC API key) once before staging.

---

## Step E — Run the staging (on the staging host)

```bash
cd ~/ncp-node-agents
sudo env GOARCH=arm64 CGROUP_EXPORTER_VER=1.3.0 SHARE=/cm/shared/nvis/bin bash stage-binaries.sh

# Verify the binaries are the NODE arch, not the staging host's:
for b in node_exporter cgroup_exporter dcgm-exporter alloy; do
  printf '%-16s ' "$b"; sudo file -b /cm/shared/nvis/bin/$b | grep -oE 'aarch64|x86-64'
done   # all must read aarch64 for GB300
```

`stage-binaries.sh` also installs `install-node.sh`, `dcgm-counters.csv`, and the textfile collectors
into `$SHARE`. Finally, stage the **full tree** so the head can run the relay + rollout from the
shared FS:

```bash
sudo cp -a ~/ncp-node-agents /cm/shared/nvis/node-agents
```

Result:
```
/cm/shared/nvis/
├── bin/            node_exporter · cgroup_exporter · dcgm-exporter · alloy (node arch)
│                   + dcgm-counters.csv · install-node.sh · textfile collectors
└── node-agents/    rollout.sh · head-node/ · systemd/ · alloy/ · install-node.sh · uninstall-node.sh
```

---

## Step F — Relay placement (where the nodes push)

The relay carries node telemetry to the obs host. Put it wherever the nodes can reach:
- **On the obs host / appserver** (NC's choice) — simplest, no head container; nodes push to the
  appserver. `RELAY_HOST = <appserver>`.
- **On the head** — only if compute nodes cannot reach the obs host directly.

```bash
sudo env METRICS_TARGET=<obs-host> RELAY_RW_PORT=19090 RELAY_LOKI_PORT=13100 \
     bash /cm/shared/nvis/node-agents/head-node/setup-relay.sh --docker   # nginx:alpine
```

Rollout (`rollout.sh`) still needs **SSH to the compute nodes**: run it from a host whose key the
nodes trust — on a BCM cluster that is the **head** (`root`→node is passwordless), or provision a
deploy key to the nodes via the BCM image. The appserver's key is typically **not** on the nodes.

---

## NC quick reference (values used)

| Setting | NC value |
|---|---|
| Staging host | appserver `10.10.20.22` (public internet ✓, `/cm/shared` writable as root) |
| Node arch | **arm64** (GB300 Grace; image `default-image-ubuntu2404-aarch64`) |
| SHARE | `/cm/shared/nvis/bin` |
| RELAY_HOST (nodes push here) | `10.10.20.22` (relay co-located on the appserver) |
| METRICS_TARGET | `10.10.20.22` (Phase-1 obs host) |
| Rollout origin | **head** `nscale-nc-hn1` (`root`→node passwordless) or a node deploy key |
| NGC key | not needed (nvcr.io pulls unauthenticated) |

---

## Checklist for a NEW cluster

1. Clone/download `nvis-observability-ncp/node-agents` on a connected host → `scp` to the staging host.
2. Confirm the **compute-node arch** from `/cm/images/` on the head.
3. Probe the binary release URLs for that arch (HTTP 200); set `GOARCH` + version overrides.
4. `docker login nvcr.io` only if that cluster requires it.
5. `sudo env GOARCH=<arch> SHARE=<share>/bin bash stage-binaries.sh`; verify `file` shows the node arch.
6. Copy the full `node-agents/` tree to the shared FS.
7. Deploy the relay (appserver or head); point `RELAY_HOST` at it.
8. Run `rollout.sh` from a host that can SSH the nodes (head or deploy key), canary first.
