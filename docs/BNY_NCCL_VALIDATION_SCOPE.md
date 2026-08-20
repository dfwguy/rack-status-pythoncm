# BNY — DGX SuperPOD (H100 / B200) Cluster Validation Scope

**Prepared for:** Ryan Vaudry (NVIDIA AM) → Jyothsna "Jo" Padmakumar Bindu (BNY)
**Prepared by:** Rafael M. Koike, Solutions Architect, NVIS OVA (Observability, Validation, Automation)
**Date:** 2026-08-19
**Cluster under discussion:** BNY on-prem DGX SuperPOD, H100 and B200, scheduled with **NVIDIA Run:ai** on Kubernetes

---

## 0. Read this first — the one-paragraph answer

NCCL testing is a **fabric and collective-communication test**. It is the fastest, cheapest way to
prove that the GPUs, NVLink/NVSwitch, InfiniBand fabric, driver/library stack and the scheduler's
placement all work *together* at production message sizes — and it is very good at that. It is
**not** a whole-cluster health assessment on its own: it will not find a thermally throttling node,
a degrading HBM row, or a slow-compute outlier. Those come from DCGM and per-node compute tests.

So the honest answer to Jo's real question — *"will this surface the right gaps?"* — is: **NCCL
testing surfaces the communication-path gaps, which are the gaps that most often silently cost
20–50% of training throughput and are the hardest to find any other way.** To make it a
*deployment-readiness* verdict rather than a single data point, it should be run as layer 4 of the
7-layer stack in §3. Layers 0, 1, 3 and 5 are passive or near-passive and can run against the
existing deployment with no disruption; layers 2, 4 and 6 need the nodes under test to be idle for
the duration of the test.

**No clean-slate re-deployment is required.** What is required is *exclusive use of the nodes under
test for the test window* — which is a scheduling action in Run:ai, not a rebuild.

---

## 1. Direct answers to Jo's questions

### 1.1 "Can this be done with our existing deployments on-prem, or does it require a clean-slate setup?"

**Existing deployment. No rebuild, no re-imaging, no cluster downtime.** The NCCL tests run as an
ordinary Run:ai *distributed training workload* (MPI framework) in a container, alongside everything
else on the cluster.

There is one important distinction that the phrase "clean slate" usually conflates:

| | Needed? | Why |
|---|---|---|
| Clean-slate **cluster deployment** (rebuild / re-image / reinstall Run:ai) | **No** | Tests are a normal containerized workload |
| Clean-slate **nodes for the test window** (the specific nodes under test drained of other GPU work) | **Yes** | A bandwidth number measured while another job shares the same GPUs, NVLink or fabric rails is **not comparable to any baseline**. This is the single most common reason validation results get argued about |

Practically: reserve a Run:ai node pool (or use a dedicated project with a quota that covers the
target nodes) for a 2–4 hour window per topology tier. Passive checks (layers 0/1/3/5) need no
window at all and can be collected today.

### 1.2 "How frequently can/should this testing be performed?"

| Cadence | Scope | Rationale |
|---|---|---|
| **Once, as a baseline** | Every node, every topology tier | This is the number everything later is compared against. Without it, a future regression is unprovable |
| **Continuously** | Layers 0/1/3 via DCGM-exporter → Prometheus/Grafana | Telemetry, not testing. Zero disruption, catches thermals/ECC/XID/link flaps as they happen |
| **Monthly or quarterly** | Sampled regression: single-node NCCL on ~10% of nodes + one multi-node run per SU | Cheap (a few GPU-hours) and catches drift |
| **Before a major training campaign** | The tier the campaign will use | A gate. Much cheaper than discovering it 200 GPU-hours in |
| **Event-driven (see triggers below)** | The affected scope | Most real regressions are change-induced, not spontaneous |

**Retest triggers** — any of these should force a re-run of at least the affected scope:

- GPU driver, CUDA, NCCL, or container base image change
- MLNX_OFED / DOCA / NIC firmware change; GPU VBIOS or system BIOS change
- NVSwitch / fabric manager version change
- Any InfiniBand change: switch firmware, cable or transceiver replacement, UFM routing or SHARP
  configuration change, subnet manager change
- Node hardware event: GPU or baseboard RMA, HBM row remap, NVLink cable/cartridge replacement
- **Run:ai upgrade, node-pool change, or network-topology label change** (this silently changes
  *placement*, which silently changes measured bandwidth)
- Any unexplained training throughput regression, Xid storm, or job-completion-time outlier
- After a datacenter power or cooling event affecting the halls in question

### 1.3 "What are the expected outputs from a deployment-readiness and performance standpoint?"

Per test run, the raw artifact is the `nccl-tests` table plus the NCCL debug log. Exact fields are
in §5.3. The *deliverables* built from them:

1. **Per-node pass/fail matrix** for the passive and node-local layers (which node, which metric,
   which threshold).
2. **Bandwidth/latency curves** — busbw vs message size, one curve per topology tier
   (intra-node, intra-SU, cross-SU), overlaid on the expected reference curve.
3. **Scaling efficiency table** — measured busbw at 1, 2, 4, 8, 16, 32 nodes vs the ideal, which is
   what exposes congestion, routing and rail-imbalance problems that a 2-node test cannot see.
4. **Outlier list** — nodes or links more than 5%/10% off the cohort median, ranked. In practice this
   is the highest-value output: on a healthy cluster it is short and specific.
5. **Root-cause attribution** for each finding, using the decision table in §7 (GPU vs network vs
   software vs topology vs scheduling).
6. **Remediation list** with owner (BNY infra / NVIDIA / vendor RMA) and a retest criterion.
7. **The baseline record itself** — versioned, so the next run is a comparison and not a debate.

### 1.4 "Explicitly list the parameters and metrics that will be measured."

§3 and §4. §3 is the full table in the format Jo asked for; §4 shows precisely which of those come
from NCCL testing specifically.

### 1.5 "Will this effort help us identify the right gaps and drive meaningful improvements?"

Where it will: fabric misconfiguration, rail/plane mapping errors, missing RDMA in pods,
topology-unaware placement, degraded links, congestion behaviour under scale, driver/library
inconsistency across the fleet, and slow-outlier nodes on the communication path. These are exactly
the problems that show up as "training is slower than it should be" and have no obvious owner.

Where it will not, and what covers it instead: see §8 (Limitations) and layers 1, 2 and 6 of §3.
Being explicit about this up front is the difference between a validation engagement that builds
trust and one that gets characterised as "testing for the sake of testing".

---

## 2. Two corrections before the table — these change the test plan

### 2.1 The Run:ai NCCL tutorial is written for GB200. BNY has DGX H100 / B200. Do not copy the GPU counts.

The published Run:ai tutorial uses **4 GPUs per worker** (single node 4 GPUs; same rack 2×4 = 8;
multi-rack 3×4 = 12). That is correct for **GB200 NVL72**, where a compute tray carries 4 Blackwell
GPUs. **DGX H100 and DGX B200 are 8-GPU systems.**

Running `-g 4` on a DGX means you are testing **half a node**: you never exercise all 8 NVLink
endpoints and you only use half the network rails. The resulting number is not comparable to any
8-GPU baseline, and it under-reports. The correct scope for BNY is 8 GPUs per node.

| Tier | Run:ai doc (GB200) | **Correct for BNY (DGX H100 / B200)** |
|---|---|---|
| Single node | 1 × 4 = 4 GPUs | **1 × 8 = 8 GPUs** |
| Node pair | 2 × 4 = 8 GPUs | **2 × 8 = 16 GPUs** |
| Scale-out | 3 × 4 = 12 GPUs | **4 / 8 / 16 / 32 × 8 = 32 … 256 GPUs** |

### 2.2 On a DGX SuperPOD, "same rack vs cross rack" is the wrong axis. Use intra-node / intra-SU / cross-SU.

This matters for interpreting results, and it is worth getting right before the customer call.

On GB200/GB300 NVL72, "same rack" means *inside one NVLink domain* — a genuinely different and much
faster transport than leaving the rack. **On a DGX H100/B200 SuperPOD there is no NVLink between
nodes.** A DGX rack holds only a handful of systems, and every node-to-node byte leaves the chassis
over InfiniBand regardless of whether the peer is in the same rack. Worse, the SuperPOD compute
fabric is **rail-optimized**: each of a node's 8 compute ports goes to a *different* leaf switch, so
two nodes in the same rack talk to each other through the same leaf switch as two nodes in different
racks of the same Scalable Unit.

**Consequence:** "same rack" and "cross rack within an SU" should measure **the same** bandwidth. If
they don't, that is a finding — a cabling, rail-mapping or routing defect — not an expected topology
effect. The tiers that actually differ in hop count are:

| Tier | Transport | What a shortfall here means |
|---|---|---|
| **Intra-node** (8 GPUs, 1 node) | NVLink 4 / NVLink 5 via NVSwitch | GPU, NVLink cartridge, NVSwitch, or fabric-manager problem |
| **Intra-SU** (2 … 32 nodes) | InfiniBand, GPU→rail leaf, 1 hop | Node NIC/rail, leaf port, GPUDirect RDMA, or pod RDMA attachment |
| **Cross-SU** (>1 SU) | InfiniBand via spine, 3 hops | Spine capacity, routing, adaptive routing, congestion control |

*To confirm before the call:* BNY's actual SU count, nodes per rack, whether the fabric is
rail-optimized as per the RA, and whether UFM/SHARP (in-network reduction) is deployed — SHARP
materially changes the expected all_reduce number on H100 and must be a known-constant, not a
surprise.

---

## 3. The full parameter / metric table

Format follows Jo's example, with two columns added because she asked *how* each data point is
collected and evaluated. Grouped into 7 layers so that each metric maps visibly back to an intended
outcome — and so it is clear which layers are non-disruptive.

**"Runs alongside production?"** — *Yes* = safe on a live, busy cluster. *Idle GPUs* = the GPUs
under test must have no other work. *Idle nodes* = the whole node, and for fabric tests the peer
nodes too.

### Layer 0 — Fleet consistency and configuration

| Parameter / Metric | Gap it surfaces | How measured | Test type | Runs alongside production? | Pass / evaluation | Category |
|---|---|---|---|---|---|---|
| GPU driver version | Fleet drift; NCCL feature availability | `nvidia-smi --query-gpu=driver_version` | Config check | Yes | Identical across fleet; ≥ minimum for the GPU generation | Fleet consistency |
| CUDA runtime / toolkit version | Container-to-driver incompatibility | `nvcc --version`, container labels | Config check | Yes | Compatible with driver per CUDA compatibility matrix | Fleet consistency |
| NCCL version | Missing NVLS/SHARP/algorithm fixes; version skew across ranks | `python -c "import torch; print(torch.cuda.nccl.version())"` | Config check | Yes | Identical for all ranks in a job; ≥ site-approved minimum | Fleet consistency |
| MLNX_OFED / DOCA version | RDMA stack drift | `ofed_info -s` | Config check | Yes | Identical across fleet; matches NIC firmware support matrix | Fleet consistency |
| NIC firmware | Known link-training / RoCE / IB defects | `ibstat`, `mlxfwmanager --query` | Config check | Yes | Identical; on vendor-approved release | Fleet consistency |
| GPU VBIOS / system BIOS | Power/clock behaviour differences between "identical" nodes | `nvidia-smi -q \| grep VBIOS`, `dmidecode -s bios-version` | Config check | Yes | Identical per node model | Fleet consistency |
| NVSwitch / fabric-manager version and state | Intra-node NVLink fabric not fully initialized | `systemctl status nvidia-fabricmanager`, `nvidia-smi -q -d FABRIC` | Config check | Yes | Service active; fabric state `Completed` / status `Success` on all GPUs | Performance at spec |
| GPU persistence mode | Latency spikes and inconsistent first-iteration timing | `nvidia-smi -q \| grep Persistence` | Config check | Yes | Enabled on all compute nodes | Fleet consistency |
| MIG configuration | Scheduler-unsafe or inconsistent GPU slicing | `nvidia-smi -L`, `nvidia-smi mig -lgi` | Config check | Yes | Uniform and intentional; MIG **off** for distributed training nodes | Fleet consistency |
| Container image / NCCL test binary provenance | Measuring a different stack than production runs | Image digest, `ls /usr/local/bin/*_perf_mpi` | Config check | Yes | Pinned digest, recorded with every result | Fleet consistency |

### Layer 1 — Node health and telemetry (passive; the DCGM L1 equivalent)

| Parameter / Metric | Gap it surfaces | How measured | Test type | Runs alongside production? | Pass / evaluation | Category |
|---|---|---|---|---|---|---|
| GPU core temperature | Overheating; airflow or CDU problems | `nvidia-smi -q -d TEMPERATURE`, DCGM field 150 | DCGM r1 / passive | Yes | Below slowdown threshold with margin; no node > ~5 °C above cohort median | Thermal / power safety |
| HBM (memory junction) temperature | Memory overheating ahead of GPU-core symptoms | `nvidia-smi -q -d TEMPERATURE`, DCGM memory temp | DCGM r1 / passive | Yes | Below vendor limit; flag cohort outliers | Thermal / power safety |
| Power draw vs TDP / enforced power limit | Under-provisioned power, mis-set caps, PSU faults | `nvidia-smi -q -d POWER`, DCGM field 155 | DCGM r1 / passive | Yes | Enforced limit == intended SKU limit on every GPU; no unexplained cap | Thermal / power safety |
| Clock throttle reasons | **Silent slowdowns** — thermal, power, or HW slowdown | `nvidia-smi -q -d PERFORMANCE` | DCGM r1 / passive | Yes | No `HW Slowdown`, `SW Thermal Slowdown` or `HW Power Brake` during steady state | Thermal / power safety |
| SM / memory clocks under load | Nodes running slower than peers for the same work | `nvidia-smi --query-gpu=clocks.sm,clocks.mem` | DCGM r1 / passive | Yes | At expected boost clocks; consistent across cohort | Performance at spec |
| ECC errors — correctable (single-bit) | Correctable memory faults; early degradation signal | `nvidia-smi -q -d ECC`, DCGM fields 310–312 | DCGM r1 / passive | Yes | Zero preferred; rising rate on one GPU = investigate | Correctness of compute |
| ECC errors — uncorrectable (double-bit) | Uncorrectable faults — job-killing and silently corrupting | `nvidia-smi -q -d ECC`, DCGM | DCGM r1 / passive | Yes | **Zero. Any non-zero is a service event** | Correctness of compute |
| Xid errors | Driver / hardware faults, in the kernel log where they are usually missed | `dmesg -T \| grep -i xid`, `journalctl -k` | Passive | Yes | Zero critical Xids (48, 63, 64, 74, 79, 92, 94, 95, 119, 120…); catalogue and attribute any found | Early failure detection |
| Row-remap / retired-page counts (pending + failure) | Degrading HBM before it becomes an uncorrectable error | `nvidia-smi --query-gpu=remapped_rows.pending,remapped_rows.failure` | DCGM r1 / passive | Yes | `failure` == 0; `pending` == 0 (pending requires a GPU reset to apply) | Early failure detection |
| GPU utilization / SM occupancy | GPU allocated but idle; ghost allocations; data-starved jobs | `nvidia-smi --query-gpu=utilization.gpu`, DCGM DCP fields | DCGM r1 / passive | Yes | Consistent with the workload's expected profile | Early failure detection |
| PCIe link speed and width | Downgraded links (Gen5 x16 negotiated as Gen1/x8) | `nvidia-smi -q \| grep -A2 'Link Width'`, `lspci -vv` | DCGM r1 / passive | Yes | Max == current == Gen5 x16 on every GPU and NIC | Performance at spec |
| PCIe replay / correctable-error counters | Marginal riser, seating or signal-integrity problems | `nvidia-smi -q -d PCI`, DCGM PCIe counters | DCGM r1 / passive | Yes | No sustained growth | Early failure detection |
| NVLink link state and count (up/down) | A GPU running with fewer NVLink lanes than spec | `nvidia-smi nvlink -s` (count links at expected rate) | Passive | Yes | Exactly the expected link count per GPU for the platform; **any** missing link is a finding | Performance at spec |
| NVLink CRC / replay / recovery error counters | Marginal NVLink cartridge or connector | `nvidia-smi nvlink -e` | Passive | Yes | No growth during a run; zero recovery errors | Early failure detection |
| BMC sensors — fan, inlet, chassis temp, PSU | Node-level hardware faults, out of band, no host impact | Redfish `/redfish/v1/Chassis/.../Thermal`, `ipmitool sdr` | Passive (out-of-band) | Yes | All sensors nominal; no failed fan or PSU redundancy loss | Thermal / power safety |
| DCGM health watches | Continuous aggregate health verdict | `dcgmi health -c` | DCGM r1 | Yes | All health groups `Healthy` | Early failure detection |

### Layer 2 — Node-local active performance (requires idle GPUs)

| Parameter / Metric | Gap it surfaces | How measured | Test type | Runs alongside production? | Pass / evaluation | Category |
|---|---|---|---|---|---|---|
| DCGM deployment/software diagnostic | Driver, NVML, persistence, page-retirement, permissions problems | `dcgmi diag -r 1` | DCGM r1 | Yes | All checks Pass | Early failure detection |
| PCIe + NVLink bandwidth (active) | Actual achieved PCIe/NVLink throughput vs spec | `dcgmi diag -r 2` | DCGM r2 | **Idle GPUs** | All checks Pass; bandwidth within plugin thresholds | Performance at spec |
| GPU memory test + memory bandwidth | HBM faults and below-spec memory bandwidth | `dcgmi diag -r 3` | DCGM r3 | **Idle GPUs** | All checks Pass | Correctness of compute / Performance at spec |
| SM stress / targeted stress | Compute instability and thermal behaviour under real load | `dcgmi diag -r 3` | DCGM r3 | **Idle GPUs** | Pass, no Xid, no throttling beyond expected | Thermal / power safety |
| Targeted power, EUD, memtest (long) | Production-grade GPU qualification; marginal parts | `dcgmi diag -r 4` | DCGM r4 | **Idle GPUs** | Pass; ~30 min/node — schedule accordingly | Thermal / power safety |
| Host↔device and P2P bandwidth matrix | Asymmetric or degraded individual GPU-pair paths inside a node | `nvbandwidth` (all test cases) | Active micro-bench | **Idle GPUs** | Every pair within tolerance of the cohort; no asymmetry between directions | Performance at spec |
| Per-GPU dense GEMM throughput | **Slow-compute outlier nodes** — the classic straggler that slows a whole job | `cublasMatmulBench` / GEMM harness, per GPU | Active micro-bench | **Idle GPUs** | Within 5% of cohort median; >10% below = CRITICAL | Performance at spec |

### Layer 3 — Fabric health and topology (mostly passive)

| Parameter / Metric | Gap it surfaces | How measured | Test type | Runs alongside production? | Pass / evaluation | Category |
|---|---|---|---|---|---|---|
| IB port state, rate, and width | Ports down or trained below rate (e.g. NDR400 link at 200G) | `ibstat`, `ibstatus`, `ibdev2netdev -v` | Passive | Yes | All compute ports `Active`/`LinkUp` at full designed rate; expected port count per node | Performance at spec |
| IB error counters | Marginal cables, transceivers, connectors | `perfquery`, `/sys/class/infiniband/*/ports/1/counters/*` — `symbol_error`, `link_downed`, `port_rcv_errors`, `local_link_integrity_errors` | Passive | Yes | Zero, or zero *growth* from a cleared baseline. Growth under load localises the bad link | Early failure detection |
| IB congestion counters | Fabric congestion and unfair routing under collective load | `port_xmit_wait`, `port_xmit_discards` | Passive | Yes | Low and evenly distributed; concentration on one port is a routing/imbalance finding | Performance at spec |
| Fabric-wide topology and routing audit | Miscabling, wrong rail mapping, missing links, credit loops | `ibdiagnet` (fabric-wide), UFM topology compare | Passive | Yes | Topology matches the as-designed RA; zero errors, zero duplicate GUIDs | Performance at spec |
| Subnet manager / UFM health | SM failover state, routing engine, unmanaged switches | UFM dashboard / `sminfo` | Passive | Yes | One active SM with standby, expected routing engine | Performance at spec |
| SHARP / in-network reduction availability | All_reduce running without in-network reduction (large, silent perf loss on H100) | UFM aggregation manager status; NCCL debug shows CollNet/SHARP | Passive + config check | Yes | Present and used if the design includes it — and consistent between runs | Performance at spec |
| GPUDirect RDMA path | GPU traffic taking a host-memory detour | `nvidia-smi topo -m`, NCCL debug (`GDRDMA`) | Config check | Yes | GDR enabled on all rails | Performance at spec |
| Rail / HCA-to-GPU affinity map | Rail-optimized design not actually honoured; cross-rail traffic | `nvidia-smi topo -m`, `NCCL_IB_HCA` resolution in debug log | Config check | Yes | Each GPU pairs with its designed HCA on every node, identically | Performance at spec |

### Layer 4 — Collective communication: **this is the NCCL test layer**

| Parameter / Metric | Gap it surfaces | How measured | Test type | Runs alongside production? | Pass / evaluation | Category |
|---|---|---|---|---|---|---|
| CUDA / NCCL / MPI initialization | Software-stack readiness end to end, in the real container | `all_reduce_perf_mpi` starts and completes | NCCL tests | Idle nodes | Completes with no init error; every rank reaches `Init COMPLETE` | Software-stack readiness |
| GPU visibility and allocation per pod | Scheduler gave the pod the GPUs it asked for | `nvidia-smi` in each pod; Run:ai allocated-GPU column | NCCL tests / config | Yes | 8 GPUs visible per worker pod; rank count == workers × 8 | Deployment configuration |
| MPI connectivity across workers | Distributed-workload readiness (SSH/PMIx wiring between pods) | `mpirun … hostname` across all ranks | NCCL tests | Idle nodes | Every rank reports its hostname; host list matches intended nodes | Distributed-workload readiness |
| **Intra-node all_reduce bus bandwidth** | NVLink / NVSwitch performance inside a node | `all_reduce_perf_mpi -b 8 -e 1G -f 2 -g 1 -w 2 --iters 10 -c 10`, 8 GPUs, 1 node | NCCL tests | Idle node | Peak busbw ≥ platform target (§6); ≥95% of target = ACCEPTABLE | Performance at spec |
| **Intra-SU (inter-node) all_reduce bus bandwidth** | InfiniBand scale-out performance, rails, GPUDirect RDMA | Same sweep, 2 → 32 nodes × 8 GPUs | NCCL tests | Idle nodes | Peak busbw ≥ scale-out target (§6); flat across node counts | Performance at spec |
| **Cross-SU all_reduce bus bandwidth** | Spine capacity, routing and congestion control across SUs | Same sweep, nodes drawn from ≥2 SUs | NCCL tests | Idle nodes | Within a defined tolerance of the intra-SU number | Performance at spec |
| Latency at small message sizes | Fixed per-collective overhead — dominates small-tensor and inference paths | `time(us)` at 8 B … 64 KB from the same sweep | NCCL tests | Idle nodes | Consistent with the reference curve; no node pair an outlier | Performance at spec |
| Bandwidth vs message size curve | Where the fabric transitions from latency- to bandwidth-bound; protocol switch points | busbw across the full 8 B → 1 GB sweep | NCCL tests | Idle nodes | Monotonic, smooth, no plateau or dip at an unexpected size | Performance at spec |
| **Scaling efficiency across node counts** | Congestion, rail imbalance, one bad link in a large job | busbw at 1/2/4/8/16/32 nodes vs ideal | NCCL tests | Idle nodes | Near-flat busbw as nodes scale; a knee localises the defect | Performance at spec |
| **Correctness — `#wrong` / out-of-bounds values** | **Data corruption on the communication path** | `-c 10` validation checks in every run | NCCL tests | Idle nodes | **Exactly 0. Non-zero is a hard stop**, regardless of bandwidth | Correctness / data integrity |
| Collective coverage (other patterns) | Workload-specific paths: TP, ZeRO, MoE, pipeline | `all_gather_`, `reduce_scatter_`, `alltoall_`, `sendrecv_`, `broadcast_perf_mpi` | NCCL tests | Idle nodes | Each within its own reference; `alltoall` is the sensitive fabric test | Performance at spec |
| Transport, algorithm and protocol actually selected | Silent fallback: TCP instead of RDMA, Ring instead of NVLS, SHARP off | `NCCL_DEBUG=INFO` — `NET/IB` vs `NET/Socket`, algo/proto, `NCCL_ALGO`, plugin banner | NCCL tests | Idle nodes | Uses IB/RDMA + expected algorithm on every run. **See §5.4 — this is the #1 false alarm** | Diagnosis of transport / topology |
| Run-to-run variance | Flapping links and marginal hardware that a single run hides | Repeat the sweep N≥5 times (we use 5 rounds) | NCCL tests | Idle nodes | Peak busbw variance within a few percent; zero `#wrong` in every round | Communication reliability |

### Layer 5 — Scheduler and placement (Run:ai)

| Parameter / Metric | Gap it surfaces | How measured | Test type | Runs alongside production? | Pass / evaluation | Category |
|---|---|---|---|---|---|---|
| Pod → node placement | Scheduler spread a tightly-coupled job across the fabric | `kubectl get pods -o wide`, `runai training mpi describe` | Config check | Yes | Pods landed on the intended nodes for the tier under test | Scheduler / topology alignment |
| Network-topology labels and node pools | Topology-aware scheduling not actually configured — the most common cause of "training got slower after an upgrade" | `kubectl get nodes --show-labels \| grep topology`; Run:ai network topology on the node pool | Config check | Yes | Rack/block/zone labels present, correct, and attached to the node pool | Scheduler / topology alignment |
| Topology placement constraint honoured | Gang placement escalating to a wider level than intended | `kai.scheduler/topology-*-placement` annotations vs actual placement | Config check | Yes | Placement at the requested level, or an explicit, understood escalation | Scheduler / topology alignment |
| Gang scheduling / all-or-nothing | Partial placement causing hangs or idle GPU burn | Run:ai workload status; pod readiness | Config check | Yes | All pods scheduled atomically | Deployment configuration |
| **RDMA / SR-IOV devices present in the pod** | Pod has GPUs but no fabric — NCCL silently falls back to TCP | `ls /sys/class/infiniband/` inside the pod; requested extended resources | Config check | Yes | Expected device count, all `PORT_ACTIVE`. **Verify before every fabric measurement** | Deployment configuration |
| GPU quota vs test requirement | Test cannot be placed at the intended scale | Run:ai project quota | Config check | Yes | Quota ≥ the largest planned run | Deployment configuration |

### Layer 6 — Sustained reliability and real-workload proxy

| Parameter / Metric | Gap it surfaces | How measured | Test type | Runs alongside production? | Pass / evaluation | Category |
|---|---|---|---|---|---|---|
| Sustained multi-GPU stress (thermal/power under load) | Stability at full load; thermal drift; power delivery under sustained draw | GEMM/stress soak, 1–24 h | DCGM r4 / soak | **Idle nodes** | No Xid, no uncorrectable ECC, no throttling, thermals in range for the full duration | Thermal / power safety |
| Long-duration collective soak | Link flaps, marginal transceivers, slow memory leaks — invisible in a 5-minute run | Repeated NCCL sweep or continuous collective, 4–24 h | NCCL soak | **Idle nodes** | Zero `#wrong`, bandwidth stable within a few percent end to end | Communication reliability |
| Real training proxy throughput | **The number the business actually cares about**: tokens/sec, TFLOP/s per GPU, step time | NeMo / Megatron reference recipe at a fixed scale | Workload benchmark | **Idle nodes** | Within ~5% of the NVIDIA reference architecture baseline for that model and scale | Performance at spec |
| Step-time variance / straggler detection | One slow node dragging a whole synchronous job | Per-step timing distribution from the training proxy | Workload benchmark | **Idle nodes** | Low variance; no persistent per-rank outlier | Early failure detection |
| Checkpoint / storage throughput | Storage, not GPUs, being the real bottleneck | Storage benchmark or checkpoint timing in the proxy run | Workload benchmark | Depends | Meets the design target for the model size and cadence | Performance at spec |

---

## 4. Ryan's draft table — validated, with corrections

Ryan asked whether his summary correctly reflects what NCCL testing provides. Mostly yes. Four rows
need adjusting, and two are worth adding.

| Ryan's measurement | Intended outcome | Verdict |
|---|---|---|
| GPU visibility and allocation | Deployment configuration and scheduling readiness | ✅ Correct |
| Successful CUDA/NCCL/MPI initialization | Software-stack readiness | ✅ Correct |
| MPI connectivity | Distributed-workload readiness | ✅ Correct |
| Intra-node bandwidth and latency | NVLink/NVSwitch **and PCIe** performance | ⚠️ **Drop "PCIe".** An intra-node all_reduce on a DGX runs over NVLink/NVSwitch and will not isolate a PCIe problem. PCIe belongs to `nvbandwidth` / `dcgmi diag -r 2` (layer 2) |
| Same-rack bandwidth and latency | Local inter-node network performance | ⚠️ **Re-frame as "intra-SU".** See §2.2 — on a DGX SuperPOD there is no NVLink between nodes, so same-rack ≡ cross-rack-within-SU. Keeping the "rack" framing invites the customer to expect a difference that shouldn't exist |
| Cross-rack bandwidth and latency | Fabric and topology performance across racks | ⚠️ **Re-frame as "cross-SU"** — that is where the extra spine hops and the real risk are |
| Correctness or validation errors | Communication reliability and data integrity | ✅ Correct — and worth naming the field: `#wrong` / "out of bounds values", which must be **0** |
| NCCL debug output | Diagnosis of transport, interface, topology, or configuration issues | ✅ Correct, and under-sold. This is also how you prove RDMA was used at all (§5.4) |
| Workload placement | Scheduler and topology alignment | ⚠️ **Qualify.** NCCL tests only reveal placement *indirectly*, via a bandwidth number that looks wrong. Placement must be verified **explicitly** (layer 5) or you will misattribute a scheduling problem to the fabric |
| Results across message sizes | Performance behavior for different communication payloads | ✅ Correct |
| Results across different collectives | Alignment with expected training or inference patterns | ✅ Correct. Sreev's note applies: pick the collectives that match BNY's actual workloads |
| **Add: scaling efficiency across node counts** | Congestion, rail imbalance, and single-link defects that only appear at scale | ➕ The most diagnostic measurement in the set, and absent from the draft |
| **Add: run-to-run variance** | Marginal/flapping hardware that a single passing run conceals | ➕ Cheap to add — repeat the sweep |

On Sreev's point about **workload placement**: he is right that it depends on the workload. For BNY,
the placement question only matters for *distributed, GPU-based, synchronous* training — which is
precisely the workload class NCCL testing is a proxy for. For single-node inference serving,
placement is a utilization question, not a fabric one.

---

## 5. Runnable commands

Everything below is copy-pasteable. §5.1 is safe on the live cluster today. §5.2 onward needs the
target nodes idle.

### 5.1 Passive collection — run this first, no window needed

Per node (SSH or a privileged DaemonSet). Layers 0/1/3.

```bash
# ---- identity & versions (Layer 0) -------------------------------------------
hostname
nvidia-smi --query-gpu=index,name,serial,uuid,vbios_version,driver_version --format=csv
nvcc --version 2>/dev/null | tail -2
ofed_info -s 2>/dev/null
systemctl is-active nvidia-fabricmanager
nvidia-smi -L
nvidia-smi mig -lgi 2>/dev/null || echo "MIG not enabled"

# ---- health & telemetry (Layer 1) --------------------------------------------
nvidia-smi --query-gpu=index,temperature.gpu,temperature.memory,power.draw,\
enforced.power.limit,clocks.sm,clocks.mem,utilization.gpu,ecc.errors.corrected.volatile.total,\
ecc.errors.uncorrected.volatile.total,retired_pages.pending,remapped_rows.pending,\
remapped_rows.failure,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,\
pcie.link.width.max --format=csv

nvidia-smi -q -d PERFORMANCE | grep -A12 'Clocks Event Reasons'   # throttle reasons
nvidia-smi -q -d FABRIC | grep -E 'State|Status'                   # NVSwitch fabric registration
dmesg -T | grep -i xid || echo "no Xid"
dcgmi health -c
dcgmi diag -r 1

# ---- NVLink (Layer 1/3) ------------------------------------------------------
nvidia-smi nvlink -s                      # link state + rate, per GPU
nvidia-smi nvlink -s | grep -c 'GB/s'     # count active links -> compare across fleet
nvidia-smi nvlink -e                      # CRC / replay / recovery error counters
nvidia-smi topo -m                        # GPU<->GPU<->NIC affinity matrix

# ---- InfiniBand (Layer 3) ----------------------------------------------------
ibstat | grep -E 'CA |State|Physical|Rate|Link layer'
ibdev2netdev -v
for p in /sys/class/infiniband/*/ports/1/counters; do
  echo "== $p"; grep -H . "$p"/{symbol_error,link_downed,port_rcv_errors,\
local_link_integrity_errors,port_xmit_wait,port_xmit_discards} 2>/dev/null
done
```

Fabric-wide, from the UFM / management host:

```bash
ibdiagnet -r --pc                  # full topology + routing audit, clears perf counters
# ...run load, then re-read counters to find links that error only under traffic
```

Collect into one CSV per node and diff across the fleet — the outliers are the finding. (For an
ongoing view rather than a snapshot, DCGM-exporter → Prometheus → Grafana is the right answer and
BNY may already have it.)

### 5.2 Run:ai — submit the NCCL test workloads

Corrected for **8 GPUs per DGX node**. Replace `bny-validation` with the actual project.

**Single node, 8 GPUs — intra-node NVLink/NVSwitch:**

```bash
runai training standard submit nccl-single-node \
  -p bny-validation \
  -i nvcr.io/nvidia/pytorch:26.01-py3 \
  -g 8 \
  --large-shm \
  -- bash -c 'sleep 4h'
```

**Multi-node — 2 workers × 8 GPUs (intra-SU); scale `--workers` for 4/8/16/32:**

```bash
runai training mpi submit nccl-multi-node \
  -p bny-validation \
  -i nvcr.io/nvidia/pytorch:26.01-py3 \
  -g 8 \
  --workers 2 \
  --slots-per-worker 8 \
  --large-shm \
  --master-command bash \
  --master-args "-c 'sleep 4h'" \
  -- bash -c 'apt-get update -qq && apt-get install -y --no-install-recommends openssh-server \
     && mkdir -p /var/run/sshd && /usr/sbin/sshd -p 2222 -o StrictModes=no && sleep 4h'
```

The worker command installs and starts `sshd` on port 2222 because the NGC PyTorch image ships no
SSH server and the MPI launcher reaches workers over SSH. A site image that already bundles `sshd`
(or PMIx) can skip it — and for repeated validation, building that image once is worth it.

Confirm placement and state before exec'ing in:

```bash
runai training mpi describe nccl-multi-node -p bny-validation
kubectl get pods -o wide | grep nccl-multi-node        # which node did each pod land on?
kubectl get nodes --show-labels | grep -i topology     # are topology labels even set?
```

For the tier tests, pin placement deliberately — either a node pool whose nodes are all in the
target SU, or Run:ai network-topology annotations
(`kai.scheduler/topology-required-placement`) so "intra-SU" really is intra-SU.

### 5.3 In-pod — verify, then measure

**Verify before you measure.** These four checks take 30 seconds and prevent the majority of wasted
runs and wrong conclusions:

```bash
nvidia-smi -L                                   # 1. 8 GPUs visible?
ls /sys/class/infiniband/                       # 2. RDMA devices in the pod?  If empty -> STOP
ibstat | grep -E 'CA |State|Rate'               # 3. all PORT_ACTIVE at full rate?
mpirun -np 16 --hostfile /etc/mpi/hostfile --map-by ppr:8:node --allow-run-as-root \
  -mca plm_rsh_args "-p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
  hostname                                      # 4. MPI reaches every rank?
```

**Intra-node, 8 GPUs:**

```bash
mpirun -np 8 --allow-run-as-root \
  -x NCCL_DEBUG=INFO \
  /usr/local/bin/all_reduce_perf_mpi -b 8 -e 1G -f 2 -g 1 -w 2 --iters 10 -c 10
```

**Multi-node — 2 nodes × 8 GPUs = 16 ranks (raise `-np` and keep `ppr:8:node` as you scale):**

```bash
mpirun -np 16 \
  --hostfile /etc/mpi/hostfile \
  --map-by ppr:8:node \
  --allow-run-as-root \
  -mca plm_rsh_args "-p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
  -x NCCL_DEBUG=INFO \
  /usr/local/bin/all_reduce_perf_mpi -b 8 -e 1G -f 2 -g 1 -w 2 --iters 10 -c 10
```

**Sweep parameters explained** (Jo asked for exact parameters):

| Flag | Meaning |
|---|---|
| `-b 8 -e 1G -f 2` | Message sizes from 8 B to 1 GB, doubling each step |
| `-g 1` | One GPU per MPI rank (rank count == GPU count) |
| `-w 2` | 2 warm-up iterations, discarded |
| `--iters 10` | 10 measured iterations per size |
| `-c 10` | 10 correctness/validation checks — this is what produces `#wrong` |
| `-d float` | (optional) datatype; add `-d bfloat16` to match training precision |

For a **deeper** baseline I would raise the ceiling and the iteration count — `-e 16G … -n 100` —
because the peak-bandwidth plateau on modern GPUs is above 1 GB, and 1 GB can under-report the true
ceiling. The 1 GB sweep is the right *smoke test*; a 16 GB sweep is the right *baseline*.

**Output fields** — every row of the `nccl-tests` table reports:

```
size(B)  count  type  redop  root  |  time(us)  algbw(GB/s)  busbw(GB/s)  #wrong   <- out-of-place
                                   |  time(us)  algbw(GB/s)  busbw(GB/s)  #wrong   <- in-place
```

plus a trailing `# Avg bus bandwidth : <N>`. **`busbw`** (bus bandwidth) is the metric to compare
against baselines — it normalises for the collective's algorithmic data movement, so it is
comparable across node counts, unlike `algbw`. **`#wrong`** must be 0. `time(us)` at the smallest
sizes is your latency figure.

**Other collectives** — same `mpirun`, swap the binary:

| Binary | Workload pattern it represents |
|---|---|
| `all_reduce_perf_mpi` | Gradient synchronization (data parallel) — the default |
| `all_gather_perf_mpi` | Tensor-parallel forward path / activation gather |
| `reduce_scatter_perf_mpi` | ZeRO / sharded optimizer step |
| `alltoall_perf_mpi` | MoE dispatch, sequence parallel — **the most sensitive fabric test** |
| `sendrecv_perf_mpi` | Pipeline-parallel hop |
| `broadcast_perf_mpi` | Weight broadcast from rank 0 |

### 5.4 The check that prevents the most common false alarm

If the pod does not have RDMA devices attached, **NCCL silently falls back to TCP over the
management network and still completes successfully** — it just reports a number 50–200× lower. We
have measured exactly this failure mode on a comparable cluster: ~3.7 GB/s on the TCP fallback path
versus ~690 GB/s on the intended fabric. It reads like a catastrophic fabric problem and it is a pod
configuration problem.

```bash
# In the NCCL_DEBUG=INFO output, confirm the transport:
#   "NET/IB"        -> good, RDMA in use
#   "NET/Socket"    -> TCP FALLBACK. The bandwidth number is meaningless. Fix the pod.
grep -E 'NET/IB|NET/Socket|NET/Plugin|Using network' nccl-run.log
```

On a Run:ai/Kubernetes cluster the pod must request the RDMA devices explicitly (SR-IOV VFs or an
RDMA shared device, plus the Multus network annotations) — GPUs alone are not enough. Worth
validating with a 2-node run **before** scheduling any large window.

Two more transport-level things to confirm in the debug log while you are there, both of which move
the expected number substantially: whether an **external net plugin** (HPC-X
`nccl_rdma_sharp_plugin`) is on the data path rather than NCCL's in-tree IB transport, and whether
**SHARP/CollNet** is active. Both must be constant between a baseline and a comparison, or the
comparison is meaningless.

---

## 6. Acceptance criteria — what "good" looks like

This is Sreev's point, and it is the right one: a report without a reference is a number, not a
verdict. Two independent criteria, both required.

### 6.1 Correctness — absolute, non-negotiable

`#wrong` / out-of-bounds values **== 0** in every collective, at every message size, in every round.
A bandwidth number from a run with `#wrong != 0` should not even be reported — data corruption on
the communication path is a hard stop and gets escalated regardless of performance.

### 6.2 Performance — relative to a platform reference

The MARS/MLOps scale-test framework (Stormbreaker) uses a **delta-to-target classification** that
BNY can adopt directly, and which I would propose as the report's verdict scheme:

| Classification | Delta vs target | Meaning |
|---|---|---|
| **ACCEPTABLE** | ≥ −5% | Within normal variance. Pass |
| **MINOR** | −10% … −5% | Investigate; usually configuration or a soft outlier |
| **CRITICAL** | < −10% | Real defect. Localise and remediate before production |
| **FAILED** | — | Did not complete, or `#wrong != 0` |

Reference `all_reduce` peak busbw targets from that framework (`all_reduce_perf_mpi -b 8 -e 32G
-f 2 -g 1 -c 1 -n 100`), single-node / scale-out, GB/s:

| Platform | Intra-node | Scale-out |
|---|---|---|
| **H100** | **480** | **358** |
| B300 | 840 | 760 |
| GB200 | 680 | 900 |
| GB300 | 690 | 930 |
| **B200** | *not in the table — see below* | *see below* |

Two things to say honestly about these numbers rather than presenting them as gospel:

- **B200 is not in that table.** DGX B200 has NVLink 5 like B300 (so intra-node should be in the
  800+ range) and the same 8 × 400 Gb/s compute fabric as DGX H100 (so scale-out should be close to
  H100's 358, since scale-out all_reduce is bounded by node network injection and the framework's own
  rule of thumb is ~90% of NIC line rate: 8 × 400 Gb/s ≈ 400 GB/s per direction × 90% ≈ 360). The
  rule checks out against the two platforms that *are* in the table — B300's 760 is ~90% of the
  800 GB/s a dual-port ConnectX-8 node can inject, and we independently measured 763.9 GB/s on a
  4-node B300 dual-plane RoCE run. So the derivation is defensible, but it is still a derivation and
  **not** a validated baseline. **Action: get the measured B200 figures from the NCCL / MARS-MLOps
  team before we publish a threshold to BNY.** This is exactly what Sreev flagged, and it is the
  single most important open item.
- These targets assume a specific NCCL version, container, message-size ceiling and SHARP
  configuration. A baseline is only a baseline if the stack is pinned alongside it. Any comparison
  across different NCCL versions or with SHARP toggled is invalid.

Also note the structural expectation from §2.2: on a rail-optimized SuperPOD, **intra-SU bandwidth
should not degrade as node count grows** from 2 to 32. A downward slope is the finding — that is
congestion, routing or a single degraded rail, and the shape of the curve tells you which.

---

## 7. How results isolate the root cause

This is the table that makes the testing actionable rather than informational. Read it top to bottom;
the first matching row is usually the answer.

| Observation | Most likely domain | Next step |
|---|---|---|
| Bandwidth ~1–10 GB/s on multi-node, run otherwise clean | **Pod configuration** — TCP fallback, no RDMA | `ls /sys/class/infiniband/` in the pod; check `NET/Socket` in debug log (§5.4) |
| Intra-node low on **every** node, uniformly | **Software stack** — container, driver, NCCL version, algorithm selection | Compare NCCL version/algo and driver against a known-good image; check `NCCL_ALGO`/NVLS in debug |
| Intra-node low on **one** node only | **That node** — NVLink, NVSwitch, fabric manager, or a GPU | `nvidia-smi nvlink -s` link count and `-e` errors; fabric-manager state; `dcgmi diag -r 2` |
| Intra-node fine, 2-node low | **Network path** — NIC, rail mapping, GID/TC, GPUDirect RDMA, leaf port | `ibstat` rate, IB error counters, `nvidia-smi topo -m` affinity, `NCCL_IB_HCA` resolution |
| 2-node fine, degrades as nodes scale | **Fabric at scale** — congestion, routing, adaptive routing, SHARP, or one bad rail | `port_xmit_wait` distribution; bisect the node set to localise; `ibdiagnet` |
| Intra-SU fine, cross-SU poor | **Spine** — capacity, routing, or oversubscription | UFM routing audit; confirm the design's cross-SU oversubscription ratio |
| `#wrong != 0` | **Data integrity** — marginal link/cable, ECC, or memory fault | Stop. `nvidia-smi -q -d ECC`, NVLink CRC counters, IB `symbol_error`; bisect to the link |
| Bandwidth varies run to run on the same nodes | **Marginal hardware** or **contention** | Confirm nodes are idle; then repeat runs and correlate with IB `link_downed` / NVLink recovery counters |
| Number is fine but placement is wrong | **Scheduler** | `kubectl get pods -o wide`; check topology labels and node pool config (layer 5) |
| All collective results fine but training is still slow | **Not the fabric** — compute outlier, storage, data loader | Layer 2 per-GPU GEMM, layer 6 training proxy with per-step timing |

---

## 8. Effort, duration, and what BNY needs to provide

### 8.1 Effort

| Phase | Elapsed | NVIDIA effort | BNY effort | Disruption |
|---|---|---|---|---|
| Prerequisites and access | 0.5–1 day | Guidance | Project + quota, image pull, RDMA verification, node-pool setup | None |
| Layer 0/1/3 passive sweep | 2–4 hours | Analysis | Run the collection script fleet-wide | **None** |
| 2-node RDMA smoke test | 30 min | Joint | 2 idle nodes | 2 nodes |
| Layer 4 NCCL tiers (intra-node → intra-SU → cross-SU) | 1–2 days | Execution + analysis | Idle nodes in 2–4 h windows | Nodes under test only |
| Layer 2 DCGM r3/r4 on target nodes | ~30 min per node, parallelizable | Guidance | Idle nodes | Nodes under test only |
| Layer 6 soak + training proxy (optional, recommended) | 1 day + overnight | Execution + analysis | Idle nodes overnight | Nodes under test only |
| Analysis, baseline record, report | 1–2 days | Report authoring | Review | None |

**Realistic total: about one week elapsed** for a full SuperPOD baseline, of which BNY-visible
disruption is a handful of scheduled windows on subsets of nodes. A targeted "is the fabric healthy"
engagement — passive sweep plus the NCCL tiers — fits comfortably in **2–3 days**.

### 8.2 Prerequisites checklist for BNY

- [ ] Run:ai project with GPU quota ≥ the largest planned run (32 nodes × 8 = 256 GPUs to test a full SU)
- [ ] Distributed training with the **MPI framework** enabled on the cluster (the MPI operator)
- [ ] Container image reachable from the cluster (`nvcr.io/nvidia/pytorch:26.01-py3` is publicly
      pullable; a private registry needs a credential configured). Tag must match the driver/CUDA level
- [ ] **RDMA/SR-IOV exposed to pods**, with the network attachments and extended resources the pods
      must request — verified with a 2-node smoke test before any large window
- [ ] Ability to constrain placement: node pool per SU, or network-topology labels + annotations
- [ ] Maintenance windows where the nodes under test carry no other GPU work
- [ ] Read access to IB fabric state (UFM or `ibdiagnet` from a management host)
- [ ] Node-level access (SSH or privileged DaemonSet) for the passive layer-0/1 collection
- [ ] Confirmation of the physical facts: SU count, nodes per rack, rail-optimized or not, SHARP
      deployed or not, H100 vs B200 node inventory

---

## 9. Limitations, and what covers the gap

Stating these up front is what separates a credible validation plan from a sales pitch. Jo asked
directly whether this will surface the right gaps — so here is what it will not surface.

| NCCL testing does **not** cover | Why | What covers it |
|---|---|---|
| Per-GPU **compute** performance | It is a communication test. A GPU at 80% of expected FLOPS passes NCCL cleanly | Layer 2: per-GPU GEMM, `dcgmi diag -r 3/r4` |
| Slow-compute straggler nodes | Same reason; the collective is bounded by the fabric, not the GPU | Layer 2 + layer 6 per-step timing |
| Thermal and power behaviour over hours | A 5-minute run never reaches thermal steady state | Layer 6 soak; continuous DCGM telemetry |
| Intermittent faults, link flaps | Point-in-time measurement | Layer 6 long soak; IB counter growth over time |
| Storage and data-loader performance | Not exercised at all | Storage benchmark; checkpoint timing in a real training run |
| Real training behaviour | Real jobs overlap compute and communication, have stragglers, checkpoints, and irregular collectives. A synthetic symmetric collective is an idealisation | Layer 6 NeMo/Megatron reference recipe |
| Nodes not included in the test | A 3-node test says nothing about the other 29 | Fleet-wide passive sweep + sampled per-node NCCL |
| Control plane, Run:ai platform resilience | Different failure domain | Platform-level testing |
| Inference-serving latency SLOs | Different traffic pattern; small messages, asymmetric | Inference-specific benchmarking |

**On "is longer-duration testing needed to assess production reliability?"** — yes, and this is worth
saying plainly to Jo. Short runs answer *"is it correctly configured and performing to spec?"* Only a
soak answers *"will it stay that way for a 3-week training run?"* Marginal transceivers, thermally
drifting nodes and slowly degrading HBM all pass a 5-minute test. In our own campaigns we run a
~1-hour smoke and up to a 24-hour soak, and the soak finds things the smoke test never does. I would
recommend at minimum one overnight soak on a representative SU before BNY commits to a large
production training campaign.

---

## 10. Open items requiring the NCCL / MARS-MLOps team

Ordered by how much they block a customer-facing threshold.

1. **Measured B200 `all_reduce` busbw baselines** (intra-node and scale-out) for DGX B200 with
   8 × 400 Gb/s compute fabric — plus the NCCL version and container they were measured with. This is
   the blocking item: without it we cannot state a B200 pass/fail threshold, only an H100 one.
2. **Whether BNY's H100 fabric includes SHARP** and what the H100 480/358 targets assume about it —
   the difference is large enough to change a PASS into a CRITICAL.
3. **Cross-SU tolerance**: what bandwidth delta between intra-SU and cross-SU is expected-by-design
   at BNY's oversubscription ratio, versus a defect.
4. **Comparable results from a similar customer environment** (Sreev's request) — an H100 SuperPOD of
   comparable SU count, for side-by-side context in the report.
5. Confirmation of the recommended **regression cadence** for a production financial-services
   cluster, where change windows are scarce and audit trails matter.

---

## 11. Note on automation

The layer 0/1/3 sweep, the NCCL tier runs, result parsing and the outlier/report generation are all
things our team already automates for GB200/GB300 bring-up campaigns (single-pass log parsing,
per-node pass/fail matrices, CSV/JSON/PDF reports). Nothing here needs to be run by hand at BNY's
scale, and if this engagement proceeds it is worth deciding early whether BNY wants the one-time
baseline or a repeatable, self-service capability — the second is not much more work and is far more
valuable to them at their retest cadence.
