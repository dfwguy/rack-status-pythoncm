# ClusterKit SU-Suite Integration

Rack Status includes two ClusterKit paths:

- `bin/scripts/run_clusterkit.sh`: existing single-rack ClusterKit sweep.
- `bin/scripts/run_clusterkit_suite.sh`: SU-scale IB validation suite adapted from the IB team's ClusterKit runner.

The SU suite keeps Slurm execution simple by submitting five explicit sbatch tiers:

- `clusterkit_suite_per_rack.sbatch`
- `clusterkit_suite_2_node.sbatch`
- `clusterkit_suite_2_node_cross_rack.sbatch`
- `clusterkit_suite_2_rack.sbatch`
- `clusterkit_suite_entire_su.sbatch`

`clusterkit_suite_common.inc.sh` contains the shared ClusterKit launch body. It reuses the existing Rack Status HPC-X, HCA profile, UCX, and mapper logic from the single-rack ClusterKit path.

## Usage

### NC: explicit two-node CPU/RoCE test

The NC cluster uses `sys1-gso1-p-phy-gpu<N>-su<M>` hostnames. Do not pass
Nscale `dh01`/`dh02` domains or rack prefixes for NC. `su<N>` is a naming
group; physical rack membership is not inferred from it.

From `/home/cmods/workspace/rack-status-clusterkit-nc`, print one sbatch plan:

```bash
bash bin/scripts/run_clusterkit_suite.sh plan \
  --nodelist sys1-gso1-p-phy-gpu1-su19,sys1-gso1-p-phy-gpu2-su19 \
  --hpcx-init /home/cmods/workspace/hpcx-v2.25.1-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64/hpcx-init.sh \
  --results-root /home/cmods/workspace/rack-status-clusterkit-nc/results \
  --partition defq --time 00:05:00 --label NC-su19 --hca roce_vf_r1:1
```

`run --dry-run --nodelist ...` accepts the same options. The `plan` mode is
always print-only. Explicit-node `run` requires `--dry-run` (or `DRY_RUN=1`)
or a deliberate `--execute`. This is an offline plan: it neither checks current node availability
nor submits jobs, loads Modules/HPC-X, invokes ClusterKit, creates output
directories, or archives results. It exits before legacy runner initialization.
The node list must contain exactly two distinct literal hostnames; bracket
expressions, empty entries, and whitespace are rejected. No additional nodes
are discovered or added.

Defaults: partition `defq`, time `00:05:00`, label `explicit-nodes`, results
root `<repo>/results`, `--gid-policy global`, `--log-level info`.
`--path-check` adds a short single-RC-path comparison inside the same allocation;
`--log-level debug` is available for failure investigation. The existing `NVIS_CLUSTERKIT_HPCX_INIT` and
`RACK_CLUSTERKIT_SUITE_RESULTS_ROOT` environment overrides are supported;
command-line options take precedence. The HPC-X init file is checked for
readability but never sourced; its parent package's parent is the planned
workspace root. Output would be under `<results-root>/<label>/2_node_test/`.

The command uses `clusterkit_suite_explicit_2_node.sbatch` and pins the exact
node list and `--nodes=2`. It exports the selected HPC-X init path. It requests
one CPU and 1 GiB per node, no GPU resources, no exclusive allocation, and no
requeue. A live execution requires an explicit HCA/port and a maximum five-minute
allocation. Both `plan` and `--dry-run` take precedence over `--execute`.

Only after operator authorization, replace `plan` with `run --execute` and use
a **new label**. The runner creates the output directory before submitting one
job and saves its ID to `jobid.txt`; it refuses an existing output directory,
does not archive or overwrite results, does not submit additional suite tiers,
and does not cancel jobs. It does not wait for completion; use read-only Slurm
queries for the returned job ID.

Read-only inspection of both NC nodes on 2026-10-13 found `roce_vf_r1:1` active
at 200 Gb/s, on NUMA node 0. HPC-X UCX exposed that device with RC transports
on node 1; broad UCX enumeration on node 2 exceeded a 20-second read timeout.
The job rechecks the exact allocated hosts, architecture, selected port's active
Ethernet state, HPC-X access and UCX inventory on both hosts before testing.
Slurm core binding is requested. Since this cluster's `task/cgroup` configuration
did not enforce that request, a rank helper verifies the current job's CPU
assignment and pins only that assigned CPU. It first verifies NC's current
identity mapping between Slurm abstract CPU IDs and Linux CPU IDs, with no SMT
or alternate `SlurmdParameters`; unfamiliar mappings fail before ClusterKit.
MPI preserves the helper's affinity. Placement records log the assigned CPU,
actual process mask, GID and one MPI task per host. CPU/memory affinity and NIC
NUMA are logged without using the Nscale GPU mapper. The first test is a single CPU bandwidth/latency
pass on that rail: 8 MiB messages, 8 bandwidth iterations, 256 latency iterations,
no retests, no GPU tests, and no stress mode. Direct Slurm PMIx-v3 launching
(matching the package's PMIx 3.2.5a1) avoids
SSH-launched MPI ranks. Preflight has a 60-second timeout, the primary MPI stage
90 seconds, and ClusterKit's test time limit is 30 seconds. Bounded failure
diagnostics and the optional path comparison are described below.

This is a functional integration check, not a calibrated performance acceptance
test or full fabric validation. The existing API and result-checking path still
use Nscale SU/rack assumptions; this change does not adapt them.

#### First approved attempt: 2026-10-13

Job `196693` ran on exactly `gpu1-su19` and `gpu2-su19`, nonexclusively, with two
CPUs and no GPU allocation. It ended FAILED after 47 seconds. Both node
preflights passed, and ClusterKit reported MPI initialization with two processes.
UCX then aborted in MPI communication (`UD endpoint ... unhandled timeout
error`, backtrace in `MPI_Bcast`). No bandwidth/latency results were produced.
Port state and UCX device enumeration establish local readiness only; the
end-to-end RoCE failure's cause is unresolved. Both nodes returned to idle.

The attempt also exposed a launcher output-path bug: specifying both absolute
`CK_OUTPUT_SUBDIR` and `--outputdir` caused the path to be duplicated. The
launcher now unsets `CK_OUTPUT_SUBDIR` and uses only `--outputdir`; this
correction was subsequently exercised successfully in job 196736.
Logged CPU affinity was `0-143` despite the binding request; do not claim
verified one-core affinity or calibrated NUMA placement. The original submitted
script, scheduler accounting, and log are preserved under
`results/NC-su19-20261013T204416Z/2_node_test/`.

Read-only follow-up found that the global IPv6 RoCE-v2 GID is at index **3**
on node 1 and **5** on node 2; other entries are link-local. Both interfaces
have IPv6 routes in table 100. This is a lead for reviewing VF/GID selection,
not proof of the timeout's cause: the attempt did not log UCX's selected GID.
The raw observations and outcome are saved in `summary.json` alongside the log.

#### Diagnostic rerun

The two-node batch now captures selected-port GIDs, interface addresses/routes,
CPU affinity, and RDMA/NIC counters before and after. UCX logs are kept
per stage/host/process, with a 16 MiB limit and one backup per log. The corrected
baseline has a 90-second execution limit. If it fails, the same allocation runs
two bounded diagnostics: a 60-second CPU/RoCE pass using each host's global
RoCE-v2 GID matching its configured interface IP, then a 45-second, 16-iteration
CPU latency check using Open MPI TCP on the Slurm hostname's IPv4 interface.
The after-snapshot also sends exactly two ICMPv6 echo requests per host to the
other approved host's RoCE IPv6 address and records the selected route. These
are process settings and narrowly targeted probes, not network reconfiguration.
All phases remain under the same five-minute, nonexclusive, two-node allocation.

Baseline failure remains the job exit status even if a diagnostic passes;
`status.txt` records all three results. A successful global-GID diagnostic is a
result for that configuration, not a successful unchanged baseline. No further
Slurm jobs are submitted by the batch. The legacy full-suite logic is not used.

Job `196736` completed this diagnostic sequence in 50 seconds. UCX debug logs
confirmed that auto selected GID **1** on node 1 and **3** on node 2: both were
the identical link-local address `fe80::22c:c9ff:fe00:0`. That baseline aborted
with a UD timeout before measuring latency/bandwidth. Selecting global RoCE-v2
GIDs **3** and **5** on the respective nodes succeeded on the same device/port
and produced both result files. The TCP control also passed. This comparison
identifies unsuitable automatic GID selection as the immediate failure
mechanism for this pair; it does not validate other nodes or rails.

The explicit-node runner now defaults to `--gid-policy global`, which resolves
the correct GID from each host's configured interface IP instead of hardcoding
one index for all hosts. `--gid-policy auto` preserves the failure-reproduction
path. The global selection was exercised successfully as a diagnostic in job
196736 and then as the primary configuration in job 197016.

Raw debug-run output reported 56,332.653 MB/s and 38.842 usec for the global-GID
pass. These are **not performance acceptance values**: the short debug test,
unverified CPU binding, result metadata reporting `PPN=144` despite one MPI rank
per host, and a bandwidth value above the VF's reported 200 Gb/s require a
separate calibrated interpretation. RDMA counters increased on the selected
port, and UCX confirmed inter-node `rc_mlx5/roce_vf_r1:1` transport. The optional
ICMP checks were unavailable (`Operation not permitted`); no privilege
escalation was attempted. Both nodes returned to idle after the job.

#### Clean primary run and path comparison

Job **197016** completed successfully in **16 seconds**, with the same approved
pair, one CPU per host, no GPUs and no exclusive allocation. Both primary and
single-path stages passed with global GID indices 3 and 5. Rank records confirm
CPU 0 was assigned to each host and the native process inherited affinity `[0]`.
Normal UCX info logging confirms four inter-node RC paths for `auto` and one for
`UCX_RC_MLX5_NUM_PATHS=1`, on the same selected `roce_vf_r1:1` port. Counters
increased on that port in each stage. Both nodes returned to idle.

| Configuration | Raw bandwidth (MB/s) | Raw latency (usec) |
| --- | ---: | ---: |
| Default / four RC paths | 56192.278 | 49.890 |
| Single RC path | 47496.308 | 3.081 |

These remain functional checks, **not performance acceptance**. Even the
single-path measurement exceeds the reported 200 Gb/s; both the VF and its
parent PF report that rate, while UCX advertises a bandwidth model near 90,489
MB/s. Multiple RC paths alone therefore do not explain the discrepancy. The
actual adapter capacity, UCX model, vendor calculation and short-sample timing
need reconciliation before treating these numbers as validated throughput.
No network, driver, firmware or Slurm configuration was changed.

The preceding job **196957** failed before ClusterKit started because the new
CPU wrapper consumed the command separator. The parser was corrected and an
exact command-line regression added before job 197016. All attempts and their
submitted scripts are preserved separately.

Artifacts for the successful run:
`results/NC-su19-clean-20261013T215740Z/2_node_test/`. Raw vendor JSON retains
`PPN=144` and `Technology=Unknown`. `HCA_Tag` is now set to the actual selected
device/port. `summary.json` separately records one actual MPI task per node and
observed RoCE-v2/Ethernet technology; original vendor files are never rewritten.
The summary mode is offline and refuses to overwrite an existing summary:

```bash
python3 bin/scripts/clusterkit_suite_node_diag.py summarize --outdir /path/to/attempt
```

The batch invokes it after completion for future attempts. The successful job's
summary was generated afterward using the same offline mode. Twenty local
stdlib unit tests pass, including no-write dry-runs, fail-closed selection,
single submission, result preservation, GID choice, CPU mapping, wrapper CLI,
optional comparison failure propagation and accurate metadata. No backend/API
deployment or legacy SU/rack selector changes were made.

### Legacy Nscale SU suite

Preview an SU run:

```bash
DRY_RUN=1 ./bin/scripts/run_clusterkit_suite.sh run dh02 j SU10
```

Run the suite:

```bash
./bin/scripts/run_clusterkit_suite.sh run dh02 j SU10
```

Check existing results:

```bash
./bin/scripts/run_clusterkit_suite.sh check SU10
```

The backend API endpoint is:

```text
POST /api/run-clusterkit-suite-async
GET  /api/run-clusterkit-suite-status?job_id=<id>
```

POST body:

```json
{
  "domain": "dh02",
  "prefix": "j",
  "su_label": "SU10",
  "log_dir": "/path/to/rack-status/logs"
}
```

## Output

By default, results are stored under:

```text
<log_dir>/clusterkit-suite/<SU_LABEL>/
```

with one subdirectory per tier:

```text
per_rack/
2_node_test/
2_node_cross_rack_test/
2_rack_test/
entire_SU/
```

Each sbatch tier writes one ClusterKit result directory per HCA.

## Thresholds

The default checks match the IB runner policy:

- Bandwidth: `170000` to `200000` MB/s
- Latency: `1.0` to `7.0` usec
- Implausible readings are reported as glitches and excluded from pass/fail:
  - bandwidth above `1000000` MB/s
  - latency above `10000` usec

These can be overridden with:

```text
RACK_CLUSTERKIT_SUITE_BW_MIN
RACK_CLUSTERKIT_SUITE_BW_MAX
RACK_CLUSTERKIT_SUITE_LAT_MIN
RACK_CLUSTERKIT_SUITE_LAT_MAX
RACK_CLUSTERKIT_SUITE_EXPECTED_HCAS
RACK_CLUSTERKIT_SUITE_RESULTS_ROOT
```

