# PythonCM validation record — 2026-08-24 (corrected 2026-08-25)

The original record incorrectly treated compatibility-route smoke tests and
partially converted launchers as acceptance. It is superseded by
[`PYTHONCM_ZERO_SLURM_AUDIT_2026-08-25.md`](PYTHONCM_ZERO_SLURM_AUDIT_2026-08-25.md).

Facts that remain valid:

- Target: Nscale TX, BCM/PythonCM 11.x.
- Certificate-authenticated inventory returned 220 racks and 3,943
  `PhysicalNode` entities.
- The service certificate did not authorize `cmproc`/ProgramRunner mutation.
- No destructive production mutation was attempted without credentials and an
  approved reserved node.

Correction:

- No validation family is currently accepted for submission. NCCL, LLMB,
  NeMo, Llama, GPT-OSS, Exemplar/system-info, ClusterKit, NVLoom, and
  MNUberGEMM are all blocked until an exact, semantically equivalent,
  ProgramRunner-only recipe is certified.
- Workflows, terminal, bring-up/report collection, management recovery, and
  Tools/installers are also blocked where they are not PythonCM operations.
- Current test counts, conversion coverage, compatibility remnants, and live
  gates are recorded only in the replacement audit.
