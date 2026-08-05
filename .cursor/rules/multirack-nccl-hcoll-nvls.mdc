---
description: Multi-rack NCCL/MPI jobs on GB300 NVL72 — the two silent killers (HCOLL at MPI_Init, NVLS cliqueId collision at NCCL init) and how to tell them apart
alwaysApply: true
---

# Multi-rack NCCL/MPI on GB300 NVL72 — two failures that single-rack runs never show

Validated on Nscale TX (`ink50-dh01`, 5 racks h18-h22, 90 nodes / 360 ranks, NCCL 2.30.7,
HPC-X 2.25.1, GB300) 2026-08-05. **A passing single-rack run proves nothing about multi-rack.**
The same rack that hits 224 GB/s busbw at 18 nodes fails both ways below at 90 nodes.

## A. Dies in `MPI_Init` with rc=134 → HCOLL

Signature: `mca_coll_hcoll_comm_query() Hcol library init failed`, `hmca_bcol_ucx_p2p_preconnect()
failed`, `ibv_create_ah(… dgid=::ffff:<bond0 IP> …) for DC ep create on mlx5_bond_0 failed:
Connection timed out`, then fatal `Transport retry count exceeded on mlx5_0:1/IB (synd 0x15)`
→ SIGABRT.

- **Cause:** `--mca pml ob1 --mca btl tcp,self` does NOT constrain collectives. HCOLL opens its
  own UCX endpoints at `MPI_Init` and straddles planes — `mlx5_bond_0` is the **RoCE/Ethernet**
  bond, `mlx5_0..3` are the 800G IB rails.
- **Fix:** `OMPI_MCA_coll_hcoll_enable=0`. `lib/fabric.inc.sh` only disables UCC; five other
  scripts (`mnubergemm_stress.sbatch`, `nccl_a2a_debug.sbatch`, the NeMo launchers) already set it.
- **Do NOT read the IB retry-exceeded as a fabric fault.** All link counters (`symbol_error`,
  `link_downed`, `port_rcv_errors`, `local_link_integrity_errors`) were **zero**; the unACKed peer
  was itself stalled in HCOLL's RoCE `ibv_create_ah` timeouts. `local_ack_timeout_err` was nonzero
  only on rail 0 — the only device HCOLL used — and zero on every control node.

## B. Hangs forever inside `ncclCommInitRank` → NVLS cliqueId collision

Signature: all N ranks log `Init START`, **zero** log `Init COMPLETE`, no warnings at all, ranks
spinning ~99% CPU with GPUs at 0% util. Backtrace (identical on every rank, any rack):

```
cuMulticastBindMem → nvlsAllocateMem (transport/nvls.cc) → ncclNvlsSetup → initTransportsRank
```

- **Cause:** every rack advertises `CliqueId 32766` (`0x7ffe`, an unset sentinel) while
  `ClusterUUID` is correctly unique per rack. NCCL groups NVLS by clique, so it tries to bind ONE
  multicast team across 360 GPUs spanning 5 separate NVSwitch fabrics. That bind can never
  complete, and it fails silently.
- **Check first:** `nvidia-smi -q | grep -iE "ClusterUUID|CliqueId"` on one node per rack.
  Distinct UUIDs + identical CliqueId = this bug.
- **Workaround:** `NCCL_NVLS_ENABLE=0` (targets the blocked call; keeps NVLink P2P + IB).
  `NCCL_MNNVL_ENABLE=0` is the bigger hammer.
- **Real fix:** unique fabric CliqueId per NVLink domain in the site's fabric/IMEX config. Until
  then this hits **every** multi-rack job — NeMo and LLMB too, not just NCCL.
- IMEX being healthy does not rule this out: `nvidia-imex` active on 90/90 with correct per-rack
  18-node `nodes_config.cfg`, and all GPUs `Fabric State: Completed / Status: Success`.

## Diagnosing a hang (don't guess — get the stack)

`nccl-tests -T/--timeout` only guards **collectives**, never communicator init, so an init hang
runs forever (worse with `TimeLimit=UNLIMITED`). `gdb` is installed on the trays:

```bash
p=$(pgrep -n all_reduce_perf)
gdb -p "$p" -batch -ex "set pagination off" -ex "bt 25"   # -batch detaches cleanly
```

Afterwards confirm the ranks are back in `R` state and no `gdb` remains. Compare `Init START` vs
`Init COMPLETE` counts first — that alone separates "hung in init" from "hung in a collective".
