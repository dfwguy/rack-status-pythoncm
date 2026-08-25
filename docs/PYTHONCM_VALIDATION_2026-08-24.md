# Nscale PythonCM-primary validation record — 2026-08-24 (corrected 2026-08-25)

The original record incorrectly treated compatibility-route smoke tests and
partially converted launchers as acceptance. The first correction then made the
opposite mistake and disabled every non-ProgramRunner feature. The authoritative
state is now the
[`Nscale hybrid README`](../README_NSCALE_PYTHONCM_HYBRID.md) and
[`independent audit`](NSCALE_PYTHONCM_HYBRID_AUDIT_2026-08-25.md).

Facts that remain valid:

- Target: Nscale TX, BCM/PythonCM 11.x.
- Certificate-authenticated inventory returned 220 racks and 3,943
  `PhysicalNode` entities.
- The service certificate did not authorize `cmproc`/ProgramRunner mutation.
- No destructive production mutation was attempted without credentials and an
  approved reserved node.

Correction:

- PythonCM is accepted for read-only inventory/status and is the coded provider
  for power, drain/resume, ProgramRunner commands, UUID status, and cancellation.
- NCCL, LLMB, NeMo, Llama, GPT-OSS, Exemplar/system-info, ClusterKit, NVLoom,
  and MNUberGEMM remain available through explicit Slurm fallbacks because the
  installed recipes have no semantic PythonCM equivalent.
- Workflow, terminal, bring-up/report, management recovery, and Tools surfaces
  are restored and use PythonCM, Slurm, local execution, or SSH according to the
  feature map.
- No live mutation or validation submission is claimed without credentials and
  an approved reserved node.
