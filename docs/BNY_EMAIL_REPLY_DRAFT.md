# Draft reply to Ryan Vaudry — BNY NCCL testing scope

Two versions below. **Version A** is the reply to Ryan (internal, with the corrections he needs to
know about before he forwards anything). **Version B** is the customer-ready text for Jo, which Ryan
can send more or less as-is with the table attached.

Attachment for both: `BNY_NCCL_VALIDATION_SCOPE.md` (convert to .docx or PDF before sending —
`pandoc BNY_NCCL_VALIDATION_SCOPE.md -o BNY_Cluster_Validation_Scope.docx`).

---

## Version A — internal reply to Ryan

**Subject:** RE: BNY-NVIDIA: NCCL Node Testing Intro — validated scope + metric table

Ryan,

Happy to help. I put together the full answer as an attachment — it is written so you can forward
the customer-facing part to Jo directly. It answers each of her questions, and the centrepiece is
the comprehensive parameter/metric table in the format she asked for, with two extra columns
covering *how* each data point is collected and *how it is evaluated*, since that was the real
substance of her ask.

Your draft table is largely correct. Four things to fix before it goes out:

1. **The GPU counts in your example scope are for GB200, not for BNY's hardware.** The Run:ai NCCL
   tutorial uses 4 GPUs per node because a GB200 NVL72 compute tray has 4 GPUs. **DGX H100 and DGX
   B200 are 8-GPU systems.** Testing with 4 GPUs on a DGX tests half a node — half the NVLink
   endpoints, half the network rails — and produces a number that is both low and not comparable to
   any baseline. The correct scope is 8 GPUs per node: 1×8 single node, 2×8 for a node pair, then
   4/8/16/32 nodes for scale-out.

2. **"Same rack vs cross rack" is the wrong axis for a DGX SuperPOD.** On GB200/GB300 NVL72, "same
   rack" means inside an NVLink domain, which is genuinely faster. On a DGX H100/B200 SuperPOD there
   is no NVLink between nodes — every node-to-node byte goes over InfiniBand, and because the fabric
   is rail-optimized, two nodes in the same rack traverse the same leaf switch as two nodes in
   different racks of the same Scalable Unit. So same-rack and cross-rack *should measure the same*.
   If we promise Jo a difference and there isn't one, we look like we don't know her architecture;
   worse, if there *is* a difference, that's a cabling or routing defect we'd be explaining away as
   normal. The tiers that actually differ are **intra-node / intra-SU / cross-SU**.

3. **Drop "PCIe" from the intra-node row.** An intra-node all_reduce on a DGX runs over
   NVLink/NVSwitch; it will not isolate a PCIe problem. PCIe belongs to `nvbandwidth` and
   `dcgmi diag -r 2`.

4. **Qualify the workload-placement row.** NCCL tests only reveal placement *indirectly* — as a
   bandwidth number that looks wrong. Placement has to be verified explicitly (pod-to-node mapping,
   Run:ai topology labels), or we will misattribute a scheduling problem to the fabric. This also
   addresses Sreev's note: placement only matters for distributed, GPU-based, synchronous training,
   which is exactly what these tests proxy.

I also added two measurements that were missing and are the most diagnostic in the set: **scaling
efficiency across node counts** (2 → 32 nodes; a downward slope localises congestion, routing, or a
single bad rail in a way a 2-node test simply cannot) and **run-to-run variance** (marginal hardware
passes a single run).

On Sreev's main point — what constitutes an acceptable outcome — the attachment proposes the
delta-to-target scheme our MARS/MLOps scale-test framework already uses: ACCEPTABLE ≥ −5%, MINOR
−10% to −5%, CRITICAL below −10%, plus an absolute correctness gate (validation errors must be zero,
irrespective of bandwidth). It includes the published `all_reduce` targets for H100 (480 GB/s
intra-node, 358 GB/s scale-out) and neighbouring platforms.

**One blocking gap, and I'd rather flag it than paper over it:** there is no validated **B200**
figure in that reference set. I can derive a defensible estimate — B200 has NVLink 5 like B300, so
intra-node should land in the 800+ GB/s range, and it has the same 8 × 400 Gb/s compute fabric as
DGX H100, so scale-out should be near H100's 358 — but that is a derivation, not a measurement, and
I don't want to hand a financial-services customer a pass/fail threshold built on it. **This is
where we do need the NCCL / MARS-MLOps team**: measured B200 baselines with the NCCL version and
container they were taken on. Section 10 of the attachment lists that plus four other items for
them, ordered by how much they block us.

Two answers you can give Jo immediately, since they're the ones gating her decision:

- **No clean-slate deployment is required.** The tests run as an ordinary Run:ai distributed
  workload on the existing cluster. What *is* required is exclusive use of the specific nodes under
  test during the window — a scheduling action, not a rebuild. Roughly half the metrics in the table
  (configuration, telemetry, fabric counters, placement) are fully passive and can be collected on
  the live cluster today with no window at all.
- **Effort:** about one week elapsed for a full SuperPOD baseline, of which BNY-visible disruption is
  a handful of 2–4 hour windows on subsets of nodes. A focused "is the fabric healthy" pass fits in
  2–3 days.

The attachment also has a prerequisites checklist for BNY, the exact `runai` and `mpirun` commands
corrected for 8-GPU DGX nodes, the exact output fields the tests produce, a root-cause attribution
table (how a given result isolates GPU vs network vs software vs topology vs scheduling), and an
explicit limitations section. That last one matters: Jo said "the goal is not to test for the sake of
testing," so I've been direct about what NCCL testing does *not* cover — per-GPU compute, thermals
over time, storage, real training behaviour — and which complementary test covers each. In my
experience that section is what earns the engagement rather than losing it.

One thing worth raising on the call: the single most common failure in Run:ai NCCL testing is the
pod not receiving its RDMA devices, in which case NCCL silently falls back to TCP and still
"succeeds" — at roughly 1–2% of the real bandwidth. We've measured that exact failure on a
comparable cluster: ~3.7 GB/s versus ~690 GB/s. It reads like a catastrophic fabric problem and it's
a pod configuration problem. I'd insist on a 30-minute 2-node smoke test to prove RDMA works before
anyone schedules a large window.

Happy to join the call with Jo and her engineers, or to do a dry run with you first if that's
useful.

Thanks,
Rafael

---

## Version B — customer-ready text for Jo

**Subject:** RE: BNY–NVIDIA: NCCL Node Testing — scope, metrics, and effort

Hi Jo,

Thanks for the clear framing — particularly "the goal is not to test for the sake of testing." That
is the right question, and it shaped how we put this together. The attached document answers each of
your questions and includes the full parameter/metric list you asked for. Summarising the key points
here:

**Can this run on your existing on-prem deployment, or does it need a clean slate?**
Your existing deployment. No rebuild, no re-imaging, no cluster downtime. The tests run as an
ordinary NVIDIA Run:ai distributed training workload in a container. The one real requirement is
that the *specific nodes under test* carry no other GPU work during the test window — a bandwidth
measurement taken while another job shares the same GPUs or fabric rails cannot be compared to any
baseline. That is a scheduling action in Run:ai, not a redeployment. Roughly half the metrics in the
attached table (configuration consistency, GPU telemetry, fabric counters, scheduler placement) are
entirely passive and can be collected on the live cluster with no window at all.

**How frequently should it be performed?**
Once as a baseline; continuously for the passive telemetry via DCGM/Prometheus; a sampled regression
monthly or quarterly; as a gate before any major training campaign; and event-driven after any
change to drivers, CUDA/NCCL, NIC or GPU firmware, fabric cabling or routing, Run:ai topology
configuration, or after any node hardware replacement. The attachment has the full trigger list. In
practice, most real regressions are change-induced rather than spontaneous, so the event-driven
triggers matter more than the calendar.

**What are the expected outputs?**
A per-node pass/fail matrix; bandwidth and latency curves per topology tier against expected
reference values; a scaling-efficiency table across node counts; a ranked outlier list; root-cause
attribution for each finding (GPU vs network vs software vs topology vs scheduling); a remediation
list with owners and retest criteria; and the versioned baseline itself, so the next run is a
comparison rather than a debate.

**What exactly is measured, and how does each metric map back to readiness?**
The attached table lists every parameter in the format you provided, organised into seven layers —
fleet configuration consistency, node health telemetry, node-local active performance, fabric health
and topology, collective communication (the NCCL tests themselves), scheduler and placement, and
sustained reliability. For each metric it gives the gap it surfaces, the exact command or tool used,
the test type, whether it can run alongside production workloads, the pass criterion, and the
category. Where thresholds exist we have cited them; where a threshold is still being confirmed for
your specific hardware, we have said so explicitly rather than guess.

**How results will be evaluated.**
Two independent criteria. Correctness is absolute: the tests run built-in data-validation checks and
any non-zero validation error is a hard stop, regardless of the bandwidth achieved. Performance is
relative to a platform reference, classified as ACCEPTABLE (within 5% of target), MINOR (5–10%
below), or CRITICAL (more than 10% below) — the same scheme NVIDIA's internal scale-test framework
uses. We are confirming the measured reference values for your B200 nodes with the NCCL engineering
team so that the thresholds we give you are validated numbers rather than estimates.

**Will it surface the right gaps?**
For the communication path — fabric misconfiguration, rail mapping, degraded links, congestion at
scale, missing RDMA in pods, topology-unaware scheduling, library version drift, and slow outlier
nodes — yes, and these are typically the problems that show up as "training is slower than expected"
with no obvious owner. The attachment is equally explicit about what NCCL testing does *not* cover:
per-GPU compute performance, thermal behaviour over hours, storage throughput, and real training
dynamics. Each of those has a named complementary test in the table, so you can decide how much of
the stack you want covered rather than assuming one test covers all of it.

**Effort and duration.**
Roughly one week elapsed for a full SuperPOD baseline, with BNY-visible disruption limited to a
handful of 2–4 hour windows on subsets of nodes. A focused fabric-health pass fits in 2–3 days. The
attachment includes a prerequisites checklist for your team — Run:ai project quota, MPI-enabled
distributed workloads, container image access, RDMA exposure to pods, and the ability to constrain
placement to a given Scalable Unit.

Two things we would want to confirm with your engineers before scheduling anything, both quick:
your fabric layout (number of Scalable Units, whether SHARP in-network reduction is deployed), and
that pods on your cluster receive their RDMA devices correctly. The second is a 30-minute two-node
test, and it is worth doing first — if pods do not get RDMA, the tests still complete successfully
but report a small fraction of the real bandwidth, which is easy to misread as a fabric fault.

Happy to walk through the attachment on a call with your engineers whenever suits.

Best regards,
Ryan
