# NCCL-tests on Firmus/MEL01 — bare-metal runbook (NCCL 2.30.7 + SHARP toggle)

A quick way to validate the fabric with **nccl-tests directly** (no container, no
`llmb-run`), and to play with **SHARP / CollNet**. Everything is pre-built and staged
on the shared home — you only allocate nodes and run.

What's already staged (no build needed):

| Thing | Path |
|---|---|
| NCCL **2.30.7** lib | `/home/cmsupport/workspace/nccl-2307/build/lib/libnccl.so.2.30.7` |
| nccl-tests binaries | `/home/cmsupport/workspace/nccl-2307/nccl-tests/build/*_perf` |
| Spectrum-X plugin | `/home/cmsupport/workspace/hpcx-v2.26-.../nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so` |
| **Run wrapper** | `/home/cmsupport/workspace/nccl_allreduce.sh` |

The wrapper sets the golden RoCE/Spectrum-X env (`NCCL_NET_PLUGIN=spcx`,
`NCCL_IB_HCA=roce_vf_r0..r3`, `NCCL_SOCKET_IFNAME=bond0`, …) and runs
`all_reduce_perf`. Knobs via env vars: `SHARP=0|1`, `BEG=8`, `END=16G`, `DBG=WARN|INFO`.

> GB300 = **4 GPU/node**, **18 nodes/rack** (NVL72). A valid cross-rack number needs
> **full racks** — always allocate in multiples of 18 nodes, `--gres=gpu:4`,
> `--ntasks-per-node=4`.

---

## Option A — interactive: lock racks once, run as many variants as you want

```bash
# 1) Grab 4 FULL racks (72 nodes = 288 GPUs) and hold them. --no-shell returns
#    immediately and keeps the allocation alive (no shell to lose on SSH drop).
#    Example uses 4 full HC08 racks from your 'rion' reservation (RK0103/05/11/13):
NODES="MEL01-HC08-SVR-CN[010311-010318,010328-010337,010511-010518,010528-010537,011111-011118,011128-011137,011311-011318,011328-011337]"
salloc --no-shell -t 02:00:00 -J rion-nccl --exclusive --gres=gpu:4 --ntasks-per-node=4 -w "$NODES"
JOB=$(squeue -u "$USER" -n rion-nccl -h -o %i | head -1); echo "alloc=$JOB"
#   (to see/pick your reserved nodes:  scontrol show res rion )

# 2) all-reduce, SHARP OFF (baseline), full 8B->16GiB sweep
SHARP=0 srun --jobid=$JOB --ntasks-per-node=4 --mpi=pmix --cpu-bind=none \
  bash /home/cmsupport/workspace/nccl_allreduce.sh | tee ~/nccl_off.log

# 3) all-reduce, SHARP ON (CollNet). DBG=INFO so you can see if it engages.
SHARP=1 DBG=INFO srun --jobid=$JOB --ntasks-per-node=4 --mpi=pmix --cpu-bind=none \
  bash /home/cmsupport/workspace/nccl_allreduce.sh | tee ~/nccl_on.log

# 4) Read the numbers (busbw is the last two columns; peak is at 16 GiB)
echo "== SHARP off =="; grep -E '^\s+(4294967296|17179869184) ' ~/nccl_off.log
echo "== SHARP on  =="; grep -E '^\s+(4294967296|17179869184) ' ~/nccl_on.log
# Did SHARP actually engage? Look for CollNet device count (0 == not provisioned on the switches):
grep -iE 'CollNet device|Loaded collnet|NCCL version' ~/nccl_on.log | sort -u | head

# 5) Release the racks when done
scancel $JOB
```

Tweak message size for an "apples-to-apples @ 4 GiB" run: add `BEG=4G END=4G` before `srun`.

---

## Option B — fire-and-forget: one sbatch

```bash
cat > ~/nccl_ar.sbatch <<'SB'
#!/bin/bash
#SBATCH --job-name=rion-nccl
#SBATCH --nodes=72
#SBATCH --ntasks-per-node=4
#SBATCH --gres=gpu:4
#SBATCH --exclusive
#SBATCH --time=00:30:00
#SBATCH --output=%x.%j.log
# pick full racks (edit to your reservation):
#SBATCH --nodelist=MEL01-HC08-SVR-CN[010311-010318,010328-010337,010511-010518,010528-010537,011111-011118,011128-011137,011311-011318,011328-011337]
srun --mpi=pmix --cpu-bind=none bash /home/cmsupport/workspace/nccl_allreduce.sh
SB

# baseline (SHARP off):
sbatch ~/nccl_ar.sbatch
# SHARP on:
SHARP=1 sbatch --export=ALL,SHARP=1 ~/nccl_ar.sbatch
# results land in ~/rion-nccl.<jobid>.log
```

---

## Reading busbw (cheat-sheet)

- Columns: `size  count  type  redop  root | time algbw busbw #wrong | time algbw busbw #wrong`
  (first triple = out-of-place, second = in-place). **busbw** is the right-most pair.
- Sweep is 8 B → 16 GiB; small messages are latency-bound, so **ignore "Avg bus
  bandwidth"** — use the **peak at 16 GiB** (or compare at a fixed size like 4 GiB).
- Reference points (4-rack / 288 GPU, NCCL 2.30.7): **576 GB/s @4 GiB, 669 @16 GiB**.
- SHARP check: if `nccl_on.log` shows `Rank 0: 0 CollNet devices`, switch-side SHARP
  isn't provisioned and NCCL falls back to NVLS_TREE (so the numbers won't change).

---

## Appendix — rebuild from source (only if you want a different NCCL version)

```bash
# On ONE compute node (aarch64), e.g. inside an salloc:
W=/home/cmsupport/workspace; D=$W/nccl-2307
source $W/hpcx-v2.26-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64/hpcx-mt-init-ompi.sh; hpcx_load
source $W/cuda/env.sh
cd $D && git fetch --tags && git checkout v2.30.7-1 && make -j$(nproc) src.build
cd $D/nccl-tests && make -j$(nproc) MPI=1 MPI_HOME=$OMPI_HOME NCCL_HOME=$D/build
```

The wrapper (`nccl_allreduce.sh`) for reference:

```bash
export LD_LIBRARY_PATH=$D/build/lib:$HPCX/nccl_spectrum-x_plugin/lib:$LD_LIBRARY_PATH
export NCCL_NET_PLUGIN=spcx               NCCL_IB_HCA=roce_vf_r0,roce_vf_r1,roce_vf_r2,roce_vf_r3
export NCCL_SOCKET_IFNAME=bond0           UCX_NET_DEVICES=bond0
export NCCL_P2P_NET_CHUNKSIZE=262144      NCCL_IB_QPS_PER_CONNECTION=2
export NCCL_MIN_CTAS=16                   MELLANOX_VISIBLE_DEVICES=all
export NCCL_COLLNET_ENABLE=${SHARP:-0}    # <-- SHARP/CollNet toggle
all_reduce_perf -t1 -g1 -f2 -n20 -w5 -b ${BEG:-8} -e ${END:-16G} -d float -R0
```

Swap `all_reduce_perf` for `all_gather_perf`, `reduce_scatter_perf`, `alltoall_perf`,
`sendrecv_perf` to test other collectives.
