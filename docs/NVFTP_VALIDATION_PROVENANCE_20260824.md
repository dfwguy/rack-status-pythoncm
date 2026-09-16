# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVFTP validation provenance — 2026-08-24

Rack Status uses its existing audited, exact-target wrappers as the executable source of truth. The
NVFTP `working_HLT_NEMO_NCCL_LLAMA` receipt is preserved read-only at:

`/cluster/OVA/scripts/nscaletx-16k-validation/working_HLT_NEMO_NCCL_LLAMA/20260824`

The archive is evidence, not an alternate launcher directory. Fixed 8K node rosters and power-cap
HLT scripts from that receipt are deliberately not catalogued, copied into `bin/scripts`, or exposed
through Rack Status.

## Selective ports

The matching live LLMB/NCCL, NeMo, Llama, and MNUberGEMM wrappers were retained. Two reviewed fixes
were ported from the NVFTP MNUberGEMM preset dispatcher into `bin/scripts/run-validation-preset.sh`:

1. A pre-staged local image can skip the all-node stage allocation only when
   `NVIS_LOCAL_STAGE_VERIFIED_SHA256` exactly matches the code-owned artifact digest, local storage
   is selected, and rsync is disabled.
2. MNUberGEMM fanout monitoring consumes complete `squeue` output before testing it, avoiding the
   `pipefail`/`grep -q` SIGPIPE race that could treat active jobs as terminal.

All other differences remain live-tree-owned and require a separate review before porting.

The production host does not currently have a reviewed `llmb-run` installation. The containerized
NCCL adapter therefore defaults honestly to the snapshot's exact five-collective sbatch recipe.
`nvftp_nccl_five_collective_20260824.sbatch` verifies the immutable snapshot SHA before use and
applies one hash-bound portability transform: the historical `racks=112 layout=…` status marker is
rewritten to `racks=dynamic layout=exact-request`. The workload commands and correctness gates are
unchanged. If a reviewed LLMB tree and executable are configured later, the same adapter advertises
and selects the 24-test LLMB contract instead.

## Allowlisted image receipt

| Use | Filename | Bytes | SHA256 |
| --- | --- | ---: | --- |
| NeMo / containerized NCCL canonical | `nvidia+nemo+26.04.01.sqsh` | 42245001216 | `e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca` |
| Optional Exemplar stock ARM (not in the minimal download set) | `nvidia+nemo+26.04.01-arm.sqsh` | 42245001216 | `55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc` |
| Llama / NCCL 2.30.7 | `nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh` | 42477232128 | `0829d0c77f38db54848afd095862093a7c62e98778b0b8664b8edff69e273747` |
| MNUberGEMM | `mnubergemm-26.03-v1.17-arm.sqsh` | 13949554688 | `9832dda6e80c8719e74f81ce617cac3abb77399e48d5385a38e86a040cad93e6` |

The canonical and stock ARM NeMo images have the same byte count but different digests. They must
not be treated as interchangeable. Rack Status keeps filenames, sizes, and SHA256 values in code;
configuration can move only the shared root and node-local root.

## Runtime paths

- Shared images: `/cluster/OVA/containers/nvftp-8k-known-good/20260824`
- Node cache: `/opt/containers`
- Validation logs: `/cluster/OVA/logs/nscaletx-16k-validation`
- Provenance archive: `/cluster/OVA/scripts/nscaletx-16k-validation`

These roots are registered Rack Status settings with platform/catalog defaults. Production also
declares them in `rack-config.env`, and Compose bind-mounts them at the same absolute paths.
