# NCCL dual-plane RoCE on SR-IOV: cross-plane QP pairing in fused vNICs

A self-contained technical write-up of a multi-plane RoCE scale-out problem on B300 + ConnectX-8,
the root cause, the fix, and the exact runtime configuration to reproduce a healthy dual-plane run.
This is deliberately log-free — just the logic.

> **Keywords (for search/correlation):** NCCL, RoCE, RoCEv2, SR-IOV, virtual function, VF,
> Kubernetes, k8s, NVIDIA Run:ai, runai, Multus, NetworkAttachmentDefinition, NVIDIA Network
> Operator, SR-IOV Network Operator, sriov-network-operator, NV-IPAM, ConnectX-8, CX8, dual-port,
> B300, Blackwell, GB300, dual-plane, multi-plane, two-plane, rail-optimized, `NCCL_NET_FORCE_MERGE`,
> `NCCL_IB_MERGE_NICS`, `NCCL_IB_HCA`, `NCCL_NET_PLUGIN`, fused vNIC, merged NIC,
> `ibv_modify_qp failed with 110`, Connection timed out, cross-plane, GID subnet mismatch, busbw,
> `all_reduce_perf`, GPUDirect RDMA, HPC-X, nccl_rdma_sharp_plugin, Spectrum-X.

**Where this was found:** an 8× **NVIDIA B300** cluster managed by **NVIDIA Run:ai** on Kubernetes,
with RoCE provided as **SR-IOV VFs** attached to pods via Multus. If you are debugging "multi-plane
RoCE is slow / times out" on a k8s + Run:ai GPU cluster and are tempted to fix it purely in the
NVIDIA operator / cluster config, read the "Cluster / SR-IOV configuration context" section — the
operator config matters, but the actual defect is in NCCL's fused-vNIC pairing logic.

## Environment

- 8× NVIDIA B300 nodes, 8 GPU/node. Each GPU has a **dual-port ConnectX-8** (2 × 400 GbE).
- Ethernet (RoCEv2) fabric, **two planes**: each CX-8's two ports land on plane 0 and plane 1
  (separate IP subnets / leaves). Rail-optimized: GPU *i* talks to GPU *i* on a peer node.
- Kubernetes (NVIDIA Run:ai) pods get RoCE interfaces as **SR-IOV virtual functions (VFs)** attached
  via Multus, one VF per (rail × plane) — e.g. `roce_p0_r0 … roce_p0_r7` and `roce_p1_r0 … roce_p1_r7`.
- NCCL ≥ 2.30 (tested on `v2.30.7-1`).

## Cluster / SR-IOV configuration context (where this shows up)

This was reproduced on a B300 + Run:ai (Kubernetes) cluster configured as follows. The point of
this section: people hitting the same symptom often try to fix it by **changing the NVIDIA operator
or cluster networking config**. Those settings are necessary to *expose* both planes, but they do
**not** fix the NCCL pairing logic described below — they only change whether/how the VFs appear.

- **GPU/NIC:** B300, one dual-port ConnectX-8 per GPU; each port on a different RoCE plane/leaf.
- **SR-IOV provisioning:** RoCE VFs are created and advertised by the **NVIDIA Network Operator**
  (SR-IOV Network Operator + `SriovNetworkNodePolicy`, RDMA enabled, NICs in **switchdev** mode),
  with **NV-IPAM** assigning per-rail `/31` addresses. Pods receive **one VF per (rail × plane)**.
- **Pod attach:** **Multus** `NetworkAttachmentDefinition`s, one per rail×plane; the pod requests
  them via the `k8s.v1.cni.cncf.io/networks` annotation plus extended resources
  (`nvidia.com/<rail-plane>: "1"`). The dataplane is OVS-DPDK on the host.
- **Scheduler:** Run:ai (`runai/queue`, `runai-scheduler`); GPU pods request `nvidia.com/gpu`.
- **Inside the pod:** the 16 VFs appear as `roce_p0_r0..7` (plane 0) and `roce_p1_r0..7` (plane 1)
  under `/sys/class/infiniband/`, each `PORT_ACTIVE`. GIDs are IPv4-mapped, plane 0 and plane 1 on
  **different subnets** (e.g. `::ffff:172.16/17.x` vs `::ffff:172.20/21.x`).

**What operator-level changes do and don't do here.** Adjusting `SriovNetworkNodePolicy`
(`numVfs`, `resourceName`, `isRdma`, link type), the RDMA subsystem mode (shared vs exclusive), VF
naming templates, or Multus wiring affects **which VFs exist and how they're named/numbered**. The
one cluster-side change that *can* sidestep the NCCL bug is making the **VF→plane PCI/BDF order
deterministic and identical across nodes** (so stock NCCL's PCI-order sub-device list is already
plane-consistent). But on a stock SR-IOV setup VF BDF ordering is not guaranteed stable across
nodes, which is exactly what triggers the failure. The robust fix is in NCCL (below); the
operator-side deterministic-ordering approach is a platform mitigation, not a different root cause.

## Symptom

- **Single-plane (8 rails, plane 0 only):** correct, but 4-node all-reduce `busbw` plateaus at
  ~382 GB/s — roughly half the fabric is idle.
- **Dual-plane (16 rails, p0 + p1):** either hard failure with
  `ibv_modify_qp failed with 110 Connection timed out`, or severely degraded bandwidth
  (we saw ~89 GB/s; others on the same hardware report ~200 GB/s). Debug shows QPs forming between
  **mismatched subnets** — a local plane-0 device connected to a remote plane-1 device.

## Root cause (the logic)

To use both ports of a CX-8 on a given rail, NCCL **fuses** the two per-plane devices
(`roce_p0_rN` + `roce_p1_rN`) into a single virtual NIC (vNIC) with `ndevs=2`
(via auto-merge or `NCCL_NET_FORCE_MERGE`). Two facts then combine into a bug:

1. **Sub-device order inside a fused vNIC follows PCI/BDF enumeration order.**
   When NCCL builds the merged device, it stores the constituent sub-devices in the order it
   discovered them. With `NCCL_IB_DEVICE_PCI_ORDER=1` (the default), discovery order is PCI-path
   order. **For SR-IOV VFs the relative BDF ordering of the plane-0 VF vs the plane-1 VF is not
   stable across nodes** — node A may end up with `[p0_rN, p1_rN]` while node B has `[p1_rN, p0_rN]`
   for the *same* rail.

2. **QP-to-sub-device pairing inside a fused vNIC is purely positional.**
   For a connection, NCCL assigns each queue pair to a local sub-device by index
   (`qpIndex % ndevs`) and to the remote sub-device by the remote's advertised index. There is **no
   plane/subnet check** when pairing sub-devices *within* a fused vNIC.

Put together: if the two endpoints ordered their sub-devices differently (fact 1), then
slot-0↔slot-0 pairing (fact 2) connects **plane 0 on one side to plane 1 on the other**. Those VFs
live on different IP subnets, so the QP transition to RTR cannot resolve a path → `110` timeout, or
(when a route does exist) heavily imbalanced/cross-plane traffic and collapsed bandwidth.

Knobs that look relevant but do **not** fix this:
- `NCCL_IB_HCA="name:port:rail:plane"` tags fix cross-**rail** grouping (each fused vNIC contains
  the right rail), but do **not** reorder sub-devices *within* the vNIC.
- `NCCL_IB_MERGE_NICS=1` alone won't auto-fuse p0+p1 (their PCI distance exceeds the default merge
  level); `NCCL_NET_FORCE_MERGE` does fuse them but inherits the non-deterministic order above.
- `NCCL_IB_SUBNET_AWARE_ROUTING` affects which *merged device* is selected to match a remote
  subnet — it does not reorder or subnet-match the *sub-devices inside* a fused vNIC.

### The thing that hides all of the above: an external net plugin

On stacks built from NVIDIA HPC-X (very common in NGC/PyTorch images), NCCL loads an **external
network plugin** instead of its in-tree IB transport. You will see, at startup:

```
NCCL INFO NET/Plugin: Loaded net plugin NCCL RDMA Plugin v11 (v11)
NCCL INFO Initialized NET plugin NCCL RDMA Plugin v11
```

That plugin (`/opt/hpcx/nccl_rdma_sharp_plugin/lib/libnccl-net.so`) ships its **own compiled copy**
of the net_ib code. Consequently, **any fix or rebuild of the in-tree `libnccl` has no effect on the
data path** unless you either disable the plugin or patch the plugin itself. This is the single most
important detection step before debugging multi-plane behavior.

## The fix (the logic)

Make the fused vNIC's sub-device order **deterministic and identical on every node**. Then the
positional pairing (`qpIndex % ndevs`) automatically lines up plane↔plane. We sort the sub-devices
of each fused vNIC by **device name** (`roce_p0_*` sorts before `roce_p1_*`) when the vNIC is built.
Device names are stable and identical across nodes, so the order is too — independent of PCI/BDF
enumeration and independent of whether plane tags were supplied.

(One could equally sort by the GID subnet; name-sort is simplest and avoids the
`NCCL_NET_ID_UNDEF` edge case where an untagged plane id of `0` is indistinguishable from "unset".)

This is a ~6-line change in `ncclIbMakeVDeviceInternal` (`src/transport/net_ib/init.cc`): sort
`props->devs[]` by `ncclIbDevs[].devName` before constructing the merged device. Because the same
net_ib source compiles into the HPC-X plugin, the identical fix applies there.

### Result (4 nodes × 8 GPU = 32 GPU, p0 + p1)

| Configuration | busbw @ 16 GiB | QP timeouts |
|---|---|---|
| Single-plane p0 | ~382 GB/s | 0 (half the fabric idle) |
| Dual-plane, unsorted (stock) | fail / ~89 GB/s | many (110 cross-plane) |
| **Dual-plane, deterministic sub-device order (fix)** | **763.9 GB/s** | **0** |

~2× single-plane, exceeding the 2-node single-plane number — i.e. both planes fully utilized.

---

## How to detect which network backend you're on (do this first)

```bash
# Look for the plugin banner at NCCL startup:
NCCL_DEBUG=INFO ... 2>&1 | grep -E "NET/Plugin|Initialized NET plugin|Using network"
```

- `Loaded net plugin NCCL RDMA Plugin vXX` / `Initialized NET plugin NCCL RDMA Plugin` →
  **external HPC-X plugin** is on the data path (in-tree libnccl changes are inert).
- `Initialized NET plugin IB` → NCCL's **internal IB** transport (in-tree libnccl is what runs).

Find the plugin files in your image:
```bash
find / -name "libnccl-net*.so*" 2>/dev/null
ldconfig -p | grep -i nccl
```

## How to force the internal transport (so an in-tree fix / rebuild actually runs)

```bash
export NCCL_NET_PLUGIN=none      # disable external net plugin; use NCCL's built-in IB transport
```

(Set it for every rank, e.g. `mpirun -x NCCL_NET_PLUGIN=none …`. Note this bypasses HPC-X
SHARP/Spectrum-X plugin optimizations — prefer fixing/replacing the plugin for production; use this
to validate the fix or as an interim measure.)

## Config to run a healthy dual-plane (all-planes) job

Assuming in-pod device names `roce_pP_rR_vf0` (P = plane 0/1, R = rail 0-7), 8 GPU/node:

### 1. Kubernetes: attach **both** planes' interfaces to the pod (Multus)

The pod is blind to any plane whose NetworkAttachmentDefinition isn't attached. Reference **all 16**
(rail × plane) networks and request the matching extended resources:

```yaml
metadata:
  annotations:
    k8s.v1.cni.cncf.io/networks: >-
      default/roce-p0-r0, default/roce-p0-r1, ... default/roce-p0-r7,
      default/roce-p1-r0, default/roce-p1-r1, ... default/roce-p1-r7
spec:
  containers:
  - resources:
      limits:
        nvidia.com/gpu: "8"
        nvidia.com/roce_p0_r0: "1"   # ... all 8 plane-0 rails
        nvidia.com/roce_p1_r0: "1"   # ... all 8 plane-1 rails
    securityContext:
      capabilities: { add: ["IPC_LOCK"] }
```
Verify inside the pod: `ls /sys/class/infiniband/` should list **16** devices (p0_r0..7, p1_r0..7),
all `PORT_ACTIVE`.

### 2. NCCL environment variables

```bash
# --- device selection: list all 16 VFs, tagged name:port:rail:plane ---
export NCCL_IB_HCA="\
roce_p0_r0_vf0:1:0:0,roce_p1_r0_vf0:1:0:1,\
roce_p0_r1_vf0:1:1:0,roce_p1_r1_vf0:1:1:1,\
roce_p0_r2_vf0:1:2:0,roce_p1_r2_vf0:1:2:1,\
roce_p0_r3_vf0:1:3:0,roce_p1_r3_vf0:1:3:1,\
roce_p0_r4_vf0:1:4:0,roce_p1_r4_vf0:1:4:1,\
roce_p0_r5_vf0:1:5:0,roce_p1_r5_vf0:1:5:1,\
roce_p0_r6_vf0:1:6:0,roce_p1_r6_vf0:1:6:1,\
roce_p0_r7_vf0:1:7:0,roce_p1_r7_vf0:1:7:1"

# --- fuse each rail's two planes into one ndevs=2 vNIC (semicolon = groups, comma = members) ---
export NCCL_IB_MERGE_NICS=1
export NCCL_NET_FORCE_MERGE="\
roce_p0_r0_vf0,roce_p1_r0_vf0;roce_p0_r1_vf0,roce_p1_r1_vf0;\
roce_p0_r2_vf0,roce_p1_r2_vf0;roce_p0_r3_vf0,roce_p1_r3_vf0;\
roce_p0_r4_vf0,roce_p1_r4_vf0;roce_p0_r5_vf0,roce_p1_r5_vf0;\
roce_p0_r6_vf0,roce_p1_r6_vf0;roce_p0_r7_vf0,roce_p1_r7_vf0"

# --- RoCEv2 correctness ---
export NCCL_IB_GID_INDEX=3        # RoCEv2 GID
export NCCL_IB_TC=106             # match your fabric's lossless/DSCP class
export NCCL_SOCKET_IFNAME=eth0    # OOB bootstrap on the pod management NIC (not a rail)

# --- make sure the in-tree (fixed) transport runs, not the unpatched plugin ---
export NCCL_NET_PLUGIN=none
```

### 3. Verify a healthy run

In `NCCL_DEBUG=INFO`, every node should report the **same** fused order, e.g.
`Made virtual device … name=roce_p0_rN_vf0+roce_p1_rN_vf0 ndevs=2` (plane-0 member first on all
nodes), and there should be **zero** `ibv_modify_qp … 110 Connection timed out` lines. `busbw` at
4 nodes should land near the 2-node single-plane figure (≈2× the single-plane 4-node number).

> If you cannot set `NCCL_NET_PLUGIN=none` (you need SHARP/Spectrum-X), the same deterministic
> sub-device ordering must be applied in the **plugin** you are loading (HPC-X
> `nccl_rdma_sharp_plugin`, or evaluate the Spectrum-X plugin `libnccl-net-spcx.so`), or the
> platform must assign **deterministic SR-IOV VF→plane BDFs** so stock ordering is already
> plane-consistent.
