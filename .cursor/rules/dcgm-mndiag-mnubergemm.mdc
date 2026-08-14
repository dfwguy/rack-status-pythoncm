---
description: DCGM multi-node diagnostics (mndiag / mnubergemm) setup gotchas for GB200/GB300 clusters
alwaysApply: true
---

# DCGM `mndiag` / `mnubergemm` — don't relearn this the hard way

Full runbook: `docs/DCGM_MNDIAG_MNUBERGEMM.md`. Validated on Boston (DCGM 4.5.3, driver 580):
single-node `dcgmi mndiag` = `MNUBERGEMM Test: Pass`.

- **`ubergemm` ≠ `mnubergemm`.** `ubergemm` = single-node GEMM kernel (`srun`-able; DCGM 4.6
  ships v1.31, GEMM-only CLI). `mnubergemm` = **multi-node** MPI app launched **only** via
  `dcgmi mndiag -r mnubergemm --hostList "n1;n2;..." [-p mnubergemm.time_to_run=<sec>]`.
  Never `srun` mnubergemm; only `time_to_run` is tunable. Legacy `--NET_*`/`--CE_*`/`--MM_*`
  flags → `ubergemm 1.31` exits 2. `bin/scripts/mnubergemm_stress.sbatch` is the single-node
  soak, NOT the multi-node test.

- **Plugin pkg** provides `mnubergemm`: `datacenter-gpu-manager-4-multinode` +
  `…-multinode-cuda13` (and `-cuda12`). **Version MUST match the DCGM core** (Boston 4.5.3).
  **Match the CUDA major DCGM picks** — driver 580 → it wants `plugins/cuda13/mnubergemm`
  (cuda12-only install → MPI exit 132 `could not access ... cuda13/mnubergemm`).

- **MPI: install `openmpi-bin` (Ubuntu 4.1.6 → /usr/bin/mpirun). Do NOT use HPC-X mpirun** —
  its build rejects `--oversubscribe` (which dcgmi passes) → MPI exit 1. With `openmpi-bin`
  present, the package default `DCGM_MNDIAG_MPIRUN_PATH=/usr/bin/mpirun` just works (no
  systemd drop-in needed).

- **Run `dcgmi mndiag` as the non-root invoking user (`cmods`)** — `nv-hostengine` spawns
  `mpirun`/SSH as that user; `cmods` passwordless SSH works and OpenMPI's root-guard never
  fires. If you must run as root: set `OMPI_ALLOW_RUN_AS_ROOT=1` + `_CONFIRM=1` in a `99-`
  drop-in. (SSH key must be **unencrypted on disk** — nv-hostengine, not your shell, spawns SSH.)

- **systemd drop-in gotcha** (only if overriding MPI env): every restart regenerates
  `/run/systemd/system/nvidia-dcgm.service.d/50-mndiag.conf`. systemd sorts drop-ins by
  filename across /etc,/run,/usr/lib, so your `/etc` override must sort **after `50-`** →
  `99-…`. Confirm with `systemctl cat nvidia-dcgm` (last `Environment=` wins).

- **IMEX must be ACTIVE** for multi-node NVLink (`systemctl start nvidia-imex`) or `mnubergemm`
  fails NCCL init. (Single-node smoke passes without it.)

- **Compute nodes flaky to `developer.download.nvidia.com`** → download debs on the head,
  stage on shared `/home`, `apt-get install ./*.deb` per node.

- **Multi-rack = one job PER RACK, never one job spanning racks.** `mnubergemm` is an
  NVLink-domain test: the audited sbatch disables IB and enables MNNVL, so a single Slurm
  allocation covering two physical racks joins two separate NVLink domains and **hangs**. The
  `multirack` scope of the `mnubergemm-26.03-v1.17` preset therefore groups the selected nodes by
  rack and fans out independent jobs (`--group-nodelist` per rack, see
  `bin/scripts/run-validation-preset.sh`). The dispatcher fails closed if the groups are not a
  clean partition of the reviewed target — a node in two groups, or outside the selection, is a
  refusal, not a warning. The preset also carries a 60-minute allocation ceiling
  (`max_runtime_minutes`), so a wedged run cannot hold a rack indefinitely.
