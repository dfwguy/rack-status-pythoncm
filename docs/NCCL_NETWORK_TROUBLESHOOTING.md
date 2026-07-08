# NCCL Network Troubleshooting — Decision Tree & Command Reference

Adapted for NVIS/OVA GB200/GB300 NVL72 validation from the internal ESP guide
[NCCL Network troubleshooting Decision Tree](https://nvidia.atlassian.net/wiki/spaces/ESP/pages/2264968542/NCCL+Network+troubleshooting+Decision+Tree).
Use it to triage a NCCL job that **fails** vs one that **runs but is slow**. Boston-specific
cross-references (things we actually hit) are called out in **> NVIS note** blocks.

## Fault domains at a glance

| Domain | Typical symptom | First action |
|---|---|---|
| Network | high latency, dropped packets, timeouts | validate routes, MTU, PFC, RoCE settings |
| NCCL config | wrong interface, silent fallback to TCP | set `NCCL_SOCKET_IFNAME` / `NCCL_IB_HCA`, enable IB/GDR |
| GPU/compute | PCIe bottleneck, NVLink mismatch | fix NUMA placement, check GPU util/topology |
| Scheduler | premature kills, timeouts | raise job time, validate launcher |
| MPI/SSH | fails **only** under MPI | validate hostfile, SSH, MPI version |
| System | CPU/mem overload, thermal | monitor load, cooling, hugepages |

## Decision tree

```
START — Is the NCCL job FAILING or just SLOW?
│
├─ FAILING
│  ├─ Error in NCCL logs (connection error / timeout)?
│  │   ├─ Check node-to-node reachability (ping / ssh)
│  │   ├─ Is NCCL on the right interface?  $NCCL_SOCKET_IFNAME  /  $NCCL_IB_HCA
│  │   ├─ Repro on 2 nodes: nccl-tests with NCCL_DEBUG=INFO
│  │   └─ RDMA (RoCE/IB) setup:  ibv_devinfo · ib_write_bw · mlxconfig · dcbtool
│  └─ No error in logs
│      ├─ Scheduler timeout / MPI config issue
│      └─ Validate GPUs:  nvidia-smi · nvidia-smi dmon · nvidia-smi topo -m · nvidia imex-ctl -N
│
└─ RUNS BUT SLOW
   ├─ Optimal path (GDR / NVLink / RoCE)?  NCCL_DEBUG=INFO + NCCL_TOPO_DUMP_FILE
   │   └─ GPUs & NICs on same NUMA node?  numactl --hardware · lstopo
   ├─ Network congestion (dropped pkts / PFC)?  ethtool -S · dmesg · nvidia-smi dmon
   └─ Compute bottleneck?  thermal throttle (nvidia-smi) · CPU sat (htop/mpstat) · PCIe (nvidia-smi topo -m)
```

> **NVIS note (interface/link-layer):** "Is NCCL on the right interface?" is exactly the class of
> fault behind the Boston a06/a08 `c14`/`c15` failures — a **mixed InfiniBand/RoCE link layer** across
> the rack made NCCL error `all_reduce.cu:507 'internal error … Remote RoCE device is incompatible with
> the local …/IB. Try selecting NICs of only one link type using NCCL_IB_HCA`. Enforce a uniform link
> layer (all IB) and/or pin `NCCL_IB_HCA` to the compute NICs. For single-rack NVL72 the data path is
> **NVLink (MNNVL)**, so `NCCL_IB_DISABLE=1` sidesteps the IB plane entirely.

> **NVIS note (GPU/IMEX branch):** on GB200 always add `nvidia imex-ctl -N` and `nvidia-smi -q | grep
> Fabric` to the "validate GPUs" step — multi-node NVLink (MNNVL) needs IMEX ACTIVE + fabric
> `State=Completed / Status=Success`. Our `bin/scripts/nvlink_preflight.sh` already gates all of this
> (GPU enumerate, 18 NVLinks, P2P matrix, fabric register, IMEX domain, dmesg Xid) per node.

## Command reference (grouped)

**Reachability & interface**
```bash
ping / ssh <peer>                      # basic node-to-node reachability
echo $NCCL_SOCKET_IFNAME $NCCL_IB_HCA  # what NCCL will use
```

**2-node NCCL repro (the single most useful test)**
```bash
NCCL_DEBUG=INFO NCCL_TOPO_DUMP_FILE=/tmp/topo.xml all_reduce_perf -b 8 -e 16G -f 2 -g 1
```
> NVIS: use `bin/scripts/nccl_portable.sh` (NFS OpenMPI, no OS deps) as the launcher — survives BCM
> image-sync. `nccl_fullrack_rounds.sh` wraps it for the Stage-1 acceptance sweep.

**RDMA / RoCE / IB**
```bash
ibv_devinfo            # HCA + port state (IB vs Ethernet link_layer, ACTIVE/DOWN)
ibstat                 # port GUID, rate, state
rdma stat -p           # per-port RDMA counters
mlnx_qos -i <netdev> --pfc 0,0,0,1,0,0,0,0    # set PFC (RoCE lossless, e.g. prio 3)
# RoCE bandwidth with congestion class (Class 3 / tclass 96):
ib_write_bw -d mlx5_1 -p 18513 -F -s 8192 --tclass=96                 # server
ib_write_bw -d mlx5_1 -i 1 <server_ip> -p 18514 -D 60 -F -s 8192 --tclass=96   # client
mlxconfig -d <dev> q | grep -iE 'link|power|thermal|status'          # NIC config/health
dcbtool / lldptool     # DCB/PFC state on the switch-facing port
```

**GPU / topology / NUMA**
```bash
nvidia-smi ; nvidia-smi dmon              # live GPU util / power / SM / mem / throttle
nvidia-smi topo -m                        # GPU<->GPU<->NIC affinity (NV#, PIX, SYS, PXB)
nvidia-smi -q | grep -iE 'Fabric|Xid'     # MNNVL fabric state + Xid
nvidia imex-ctl -N                        # IMEX nodes/domain status (MNNVL prerequisite)
numactl --hardware ; lstopo               # NUMA layout; are GPU + its NIC on the same node?
nvbandwidth                               # host<->device / device<->device bandwidth matrix
```

**NIC counters & firmware/health**
```bash
lspci | grep -i mel                       # enumerate Mellanox/NVIDIA NICs (count = sanity check)
# per-interface error/drop counters:
for dev in $(ls /sys/class/net/ | grep -E 'eth|ens|enp'); do
  echo "== $dev =="; ethtool -S "$dev" | grep -iE 'err|drop|miss|overflow'
done
mlxfwmanager | grep -E 'PSID|FW Version' # firmware versions across NICs
```

## When still stuck — full diag bundle to attach to an escalation
- NCCL logs at `NCCL_DEBUG=INFO` (and `TRACE` if needed) + the `NCCL_TOPO_DUMP_FILE`.
- `nccl-tests` output with **min / avg / max** busbw; also `nvbandwidth`.
- NIC stats: `ethtool -S`, `mlxconfig`, `ibstat`, `rdma stat -p`.
- GPU stats: `nvidia-smi`, NVLink status, `nvidia-smi -q` (Fabric/Xid).
- CPU & NUMA: `numactl --hardware`, `lscpu`, `top`/`htop`, `perfspect`.
- On GB200 also: `nvidia imex-ctl -N`, `nvidia-bug-report.sh -o /tmp/nvbr_<node>` for hardware RMA.

> **NVIS note (container NCCL):** if the failure is a **segfault right after NCCL init** inside the
> NeMo container (`NCCL INFO MNNVL busId … healthMask …` → SIGSEGV) rather than a network error, it's
> the container's NCCL build, not the fabric — see the NCCL 2.28.3 MNNVL bug + `LD_PRELOAD` 2.28.9 /
> vetted-image workaround in `VALIDATION_TRACKER.md`. Confirm the fabric is fine first with the
> portable launcher (host NCCL) and `nvlink_preflight.sh`.

_Source: internal ESP wiki — [NCCL Network troubleshooting Decision Tree](https://nvidia.atlassian.net/wiki/spaces/ESP/pages/2264968542/NCCL+Network+troubleshooting+Decision+Tree). Adapted with NVIS/OVA Boston GB200 findings._
