# MEL01 (Firmus) — NVLS All-Reduce Root-Cause & Validation Report

**Date:** 27 Jun 2026 • **Cluster:** Firmus MEL01 (GB300 NVL72) • **Owner:** OVA / NVIS validation
**Status:** ✅ Closed — fabric healthy, root cause identified, ~915 GB/s reproduced independently.

## TL;DR

1. **The NVLink/NVLS/MNNVL fabric is healthy.** We independently reproduced **915.6 GB/s**
   single-rack all-reduce (72 GPUs, RK0216) with our own clean stack — matching the cluster
   team's **917.9 GB/s** within 0.25%.
2. **The earlier "NVLS hang" was our hand-built userspace lib mix**, not the fabric. The
   *same* `all_reduce_perf` binary that hung now runs to completion when pointed at a clean
   NCCL/CUDA userspace.
3. **NCCL 2.30.7 is ~5% slower than 2.29.2** for NVLS all-reduce on GB300 (the only variable
   between 873 and 915 GB/s). **Pin NCCL 2.29.2 + CUDA 13** for validation.
4. Only **RK0106 + RK0109** have a genuine NVLS/NVSwitch SDN fault (need nvswitch SDN factory
   reset; fenced in the `FIXME` reservation). 709 healthy nodes are in `DONT_TOUCH`.

## Test environment

- **Node under test:** `MEL01-HC08-SVR-CN021616` (single-node) and full rack RK0216
  `CN[021611-021618,021628-021637]` (18 nodes / 72 GPUs), borrowed from `DONT_TOUCH` via
  `salloc --reservation=DONT_TOUCH` (root).
- **Shared FS gotcha:** head `mbcm01` is x86_64; compute nodes are aarch64 (Grace). The
  shared path is `/cm/shared` on nodes = `/cm/shared-ubuntu2404-aarch64/...` on the head.
  `/home/cmsupport/workspace` is **head-local** (not visible to compute nodes).
- **Clean userspace built on-node:** pip `torch` venv (`python3.12-venv` installed on node),
  CUDA-13 nightly wheel → `torch 2.14dev+cu130, CUDA 13.0, NCCL 2.30.7+cuda13.3`, Blackwell
  `sm_100`+`compute_120` (forward-compatible to GB300 `sm_103`).
  - Note: PyPI **stable** torch (cu124, NCCL 2.21, max `sm_90a`) **cannot run on GB300** —
    "no kernel image" — which is why the team uses purpose-built binaries/containers.

## Hardware health (CN021616)

- **NVLink:** 18 links/GPU × **53.125 GB/s** = 956.25 GB/s/GPU (full NVL72 spec).
- **NVLink error counters:** all zero (no Rx/CRC/symbol/recovery errors).
- **IMEX:** `active`; domain all READY (driver 580.105.08).
- **NVLS:** `importing shareableHandle → Symmetric VA 280GB → Connected all rings` clean;
  multicast (MC) handles bind/unbind cleanly.
- **RoCE:** 4× 800 Gb/s CX-8 NICs per node (`roce_r0..r3`, 4×200G planes), GPUDirect on.

## Results — all_reduce busbw (out-of-place unless noted)

### Scale sweep (clean venv, NCCL 2.30.7)
| Scale | algbw/GPU | busbw | 2(n-1)/n |
|---|---|---|---|
| 4 GPU (1 node), 4 GiB | 457 | 685 | 1.50 |
| 72 GPU (rack), 4 GiB | 421 | 831 | 1.972 |
| 72 GPU (rack), 16 GiB | 443 | 868 | 1.972 |

→ busbw rises with GPU count purely via `2(n-1)/n`; per-GPU algbw is already saturated at
4 GPUs. Quote **algbw** for the true NVLink rate.

### NCCL version comparison (72 GPU, RK0216, 16 GiB, identical env + 36-core binding)
| NCCL | algbw/GPU | busbw (oop) | busbw (ip) |
|---|---|---|---|
| **2.29.2** | **464.3** | **915.6** | 913.3 |
| 2.30.7 | 442.6 | 869.4 | 872.9 |
| Team (Junli) 2.29.2 | 465.4 | 917.9 | 918.7 |

Env knobs (`NCCL_MNNVL_UUID`, `NCCL_MIN_CTAS=16`, `NCCL_P2P_LEVEL=NVL`, `NCCL_CUMEM_ENABLE=1`,
CPU binding) made **no** difference on 2.30.7 (~873 with or without). The gap is the version.

## Reproduction recipe (the ~915 GB/s single-rack run)

Binary: `/home/cmsupport/workspace/nccl/bins-2.29.2/all_reduce_perf` (NCCL 2.29.2). Stage to
shared FS; provide clean `libcudart.so.13` (venv `nvidia/cu13/lib`) and the 2.29.2 `libnccl.so.2`.

NCCL env (intra-rack, pure NVLink): `NCCL_NVLS_ENABLE=1 NCCL_MNNVL_ENABLE=1
NCCL_MNNVL_UUID=0x1969 NCCL_P2P_LEVEL=NVL NCCL_CUMEM_ENABLE=1 NCCL_MIN_CTAS=16
NCCL_IB_DISABLE=1 NCCL_SOCKET_IFNAME=bond0`.

Launch (72 ranks, 4/node, 36 cores/rank):
```bash
srun --reservation=DONT_TOUCH --mpi=pmix --ntasks=72 --ntasks-per-node=4 \
     --gpus-per-node=4 --cpus-per-task=36 --cpu-bind=cores \
     all_reduce_perf -b 8 -f 2 -g 1 -e 16G -R 0
```
(Team uses `mpirun … --map-by ppr:2:socket:PE=36` under HPC-X 2.26 OpenMPI; equivalent result.)

## Artifacts

- Our run logs + scripts: `dist/nccl-logs/ours/` (venv build/run, scale sweeps, version A/B).
- Team per-rack logs: `dist/nccl-logs/junli/` (the `root-NCCL-1Rack-*.txt` baseline set).
- On-cluster staging: `/cm/shared-ubuntu2404-aarch64/nvtest/` (venv `nv-real13`, binaries,
  `run_*.sh`, `*.log`).

## Recommendations

1. **Validation standard:** NCCL **2.29.2** + CUDA 13, clean userspace (container or venv).
   Treat a clean NVLS pass as the per-node/per-rack health gate.
2. **File the NCCL 2.30.7 NVLS regression** (~5% on GB300) with the NCCL team; re-test on the
   next 2.30.x.
3. **RK0106 / RK0109:** nvswitch SDN factory reset, then re-validate with the recipe above.
4. **Cross-rack (RoCE/SPCX):** still to validate — single-rack is NVLink-only. Use the same
   clean userspace; this is where SPCX 4-plane fusion and the 800 G NICs actually matter.
