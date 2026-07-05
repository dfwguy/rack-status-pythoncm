# Boston (bos01) — 2-Node NCCL All-Reduce on Rack b05 (NVLink/MNNVL Proof)

**Date:** 01 Jul 2026 • **Cluster:** Boston Markley `bos01` (GB200 NVL72) • **Owner:** OVA / NVIS validation
**Status:** ✅ Cross-node NVLink proven healthy on b05 (c01↔c02) — **743.9 GB/s avg busbw**, 0 errors.

> Imported from the prior "Boston NVIS rack status" working session. This captures the NCCL
> validation on rack **b05** so it's reproducible without re-reading the chat.

## TL;DR

1. **Cross-node NVLink/MNNVL on b05 is healthy.** A 2-node `all_reduce_perf` (8 ranks, 4×GB200
   per node) ran clean at **~744 GB/s avg** busbw, **0 wrong** — bandwidth that can only come
   from NVLink through the NVSwitch fabric (RoCE/IB would top out at a fraction of that).
2. **The "good old" NCCL path succeeded where `dcgmi mndiag`/`mnubergemm` hung** — because we
   launch `mpirun` **ourselves** (as `cmods`), so our `bond0` pinning (`--mca
   oob/btl_tcp_if_include bond0` + `-x NCCL_SOCKET_IFNAME=bond0`) actually propagates. The
   `nv-hostengine`-spawned `mpirun` ignored it and dialed a stray non-routable interface.
3. **Reusable launcher saved on NFS:** `/home/cmods/workspace/nccl/run_2node_nccl.sh`
   (env-tunable `HOSTS`/`NP`/`BEG`/`END`/`EXTRA_NCCL`), logs to `run_2node.log`.
4. **Root cause of the `mnubergemm` hang is confirmed and benign to the fabric** — an MPI
   wireup/interface-selection problem, not an NVLink fault. Fix pinned but the retry is still
   pending (see "MPI follow-up").

## Cluster / connection

| Field | Value |
|---|---|
| Cluster id | `bos01` (Boston Markley) |
| Platform | NVIDIA GB200 NVL72 — 2 GB200 boards / **4 GPUs per node** |
| Control plane (cmsh/slurm queries) | `cmods@10.29.136.8` (head) |
| Script/root plane (pdsh, sbatch, root) | `root@10.29.136.11` (slogin `bos01-a09-13-s1-mgmt-01`) |
| SSH key | `~/.ssh/nvis_container` |
| NFS workspace | `/home/cmods/workspace/` (visible on all compute nodes) |

**Naming: rack id ≠ node hostname.** Rack/dashboard id is `bos01-b05`; real Slurm node
hostnames are `bos01-b05-<NN>-s1-dgx-05-c<NN>` (e.g. `bos01-b05-11-s1-dgx-05-c01`).

## b05 fabric state before the test

- **IMEX + fabric:** 17/18 nodes healthy, clique unified, 72 NVLinks up.
- **Preflight static gate** (`nvlink_preflight.sh`): **17 PASS**.
- **Down node:** `bos01-b05-16-s1-dgx-05-c06` — down / UNAVAILABLE (no ping), excluded.
- **Nodes under test:** `c01` = `bos01-b05-11-s1-dgx-05-c01`, `c02` = `bos01-b05-12-s1-dgx-05-c02`
  (both PASS, free, clean, bond0 up and mutually reachable).

## Test stack (all prebuilt on NFS — nothing to compile)

- **Binary:** `/home/cmods/workspace/nccl/nccl-tests/build/all_reduce_perf`
  — MPI-enabled (links `libmpi.so.40`), CUDA-13.
- **NCCL:** `libnccl.so.2` **2.28.9+cuda13** at `/home/cmods/workspace/nccl/bins`
  (must be on `LD_LIBRARY_PATH`).
- **MPI:** system OpenMPI `/usr/bin/mpirun` (+ `orted`); `cmods` has passwordless SSH between nodes.
- **CUDA:** `/home/cmods/workspace/cuda13/targets/sbsa-linux/lib` (Grace/aarch64, sbsa).

## Why NCCL works when `dcgmi mndiag` hangs

These nodes have several IPv4 interfaces: **`bond0`** (the routable data/OOB path), a stray
`enP6p3s0f1np1` (`100.127.2.x/31`, **non-routable between nodes**), IB, and `docker0`. When
**we** run `mpirun`, we force the wireup onto `bond0` and it just works. When `dcgmi mndiag`
runs, `nv-hostengine` spawns `mpirun` with an environment that ignored our pin and dialed the
stray `100.127.2.x/31` → cross-node wireup hangs. **The fabric was never the problem.**

## Reusable launcher — `run_2node_nccl.sh`

Saved at `/home/cmods/workspace/nccl/run_2node_nccl.sh`:

```bash
#!/bin/bash
# 2-node NCCL all_reduce (Boston b05): MPI wireup pinned to bond0, NCCL bootstrap on bond0,
# NVLink/MNNVL for the data path. Env knobs: HOSTS, NP, BEG, END, NCCL_DEBUG, EXTRA_NCCL.
set -u
NCCL=/home/cmods/workspace/nccl
BIN="$NCCL/nccl-tests/build/all_reduce_perf"
export LD_LIBRARY_PATH="$NCCL/bins:/home/cmods/workspace/cuda13/targets/sbsa-linux/lib:${LD_LIBRARY_PATH:-}"
H1=bos01-b05-11-s1-dgx-05-c01
H2=bos01-b05-12-s1-dgx-05-c02
HOSTS="${HOSTS:-$H1:4,$H2:4}"
NP="${NP:-8}"; BEG="${BEG:-512M}"; END="${END:-2G}"
echo "[$(date -u +%FT%TZ)] hosts=$HOSTS np=$NP range=$BEG..$END nccl=$(basename $(readlink -f $NCCL/bins/libnccl.so.2))"
extra_x=""
for kv in ${EXTRA_NCCL:-}; do extra_x="$extra_x -x $kv"; done
exec mpirun -np "$NP" --host "$HOSTS" \
  --mca plm_rsh_agent "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
  --mca oob_tcp_if_include bond0 --mca btl_tcp_if_include bond0 \
  --mca pml ob1 --mca btl self,vader,tcp --mca btl_base_warn_component_unused 0 \
  -x LD_LIBRARY_PATH -x NCCL_SOCKET_IFNAME=bond0 -x NCCL_DEBUG="${NCCL_DEBUG:-WARN}" $extra_x \
  "$BIN" -b "$BEG" -e "$END" -f 2 -g 1
```

### Run it

```bash
# from the head, hop to c01 and launch (logs to NFS so a hang can't hide output):
ssh -i ~/.ssh/nvis_container cmods@10.29.136.8 '
  LOG=/home/cmods/workspace/nccl/run_2node.log
  timeout 180 ssh -o StrictHostKeyChecking=no bos01-b05-11-s1-dgx-05-c01 \
    "bash /home/cmods/workspace/nccl/run_2node_nccl.sh > $LOG 2>&1"
  tail -45 "$LOG"'
```

Knobs: `HOSTS="nodeA:4,nodeB:4"`, `NP=8`, `BEG=512M`, `END=2G`,
`EXTRA_NCCL="NCCL_DEBUG=INFO"` (to print transport selection lines).

## Result (c01 ↔ c02, 8 ranks)

| size | busbw (out-of-place) | #wrong |
|---|---|---|
| 512 MB | **692.9 GB/s** | 0 |
| 1 GB | **716.0 GB/s** | 0 |
| 2 GB | **822.8 GB/s** | 0 |

**Avg bus bandwidth: 743.9 GB/s** • NCCL 2.28.9+cuda13 • correctness OK • no hang, no Xid.

**Verdict:** ~700–820 GB/s is NVLink/MNNVL territory (through the NVSwitch fabric). RoCE/IB
across these NICs would deliver only tens of GB/s per NIC — so the cross-node collective is
genuinely riding NVLink, not the network.

## MPI follow-up (still pending — for the `mnubergemm`/`dcgmi mndiag` path)

1. **Pin the wireup where `nv-hostengine` will honor it.** Applied on c01/c02
   `/etc/openmpi/openmpi-mca-params.conf` (and an NFS `~/.openmpi/mca-params.conf` for `cmods`):

   ```
   oob_tcp_if_include = bond0
   btl_tcp_if_include = bond0
   ```

   If that still isn't picked up by the DCGM-spawned `mpirun`, add a `99-` systemd drop-in
   setting `OMPI_MCA_oob_tcp_if_include=bond0` / `OMPI_MCA_btl_tcp_if_include=bond0` for
   `nvidia-dcgm` (systemd sorts drop-ins by filename; must sort after the package `50-`).
2. **Then retry** the 2-node `mnubergemm`: `dcgmi mndiag -r mnubergemm --hostList "c01;c02"`
   as `cmods`. (See the `dcgm-mndiag-mnubergemm` cursor rule / `docs/DCGM_MNDIAG_MNUBERGEMM.md`.)
3. **Optional rigor:** rerun the NCCL launcher with `EXTRA_NCCL="NCCL_DEBUG=INFO"` and grep for
   `NVLS`/`MNNVL`/`NVLink` vs `NET/IB` to print the transport explicitly — the 744 GB/s already
   settles it.

## Next steps

- Scale the NCCL test to more b05 nodes (bump `HOSTS`/`NP`), then to b06/b07/b08.
- Close out the `mnubergemm` MPI wireup so multi-node DCGM diag matches the NCCL result.
