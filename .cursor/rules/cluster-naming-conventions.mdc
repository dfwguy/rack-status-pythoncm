---
description: Two distinct cluster naming schemes (rack/dashboard id vs real compute node hostname) and how to avoid targeting non-existent nodes
alwaysApply: true
---

# Cluster Naming: rack id ≠ compute node hostname

Every cluster has **two different names** for compute. Conflating them generates
pdsh/sysinfo targets that don't resolve (`Name or service not known`) or that
point at non-existent hosts. Always confirm both before building node targets.

1. **Rack / dashboard id** (`NVIS_RACK_ID`, from `cmsh rack list`) — the short
   grouping key shown in the UI floor/row view. Coarse, row-level.
2. **Real compute node hostname** (Slurm/DNS, what `pdsh`/`sysinfo`/`ssh` must
   target) — the actual resolvable host. Get the ground truth from
   `sinfo -N -h -o %N` and verify with `getent hosts <node>`.

`NVIS_CT_ID` must expand to the **real node hostname (scheme 2)**, NOT a naive
`<rack>-[01-NN]` derived from the rack id.

## Boston Markley (bos01) — concrete example

| Scheme | Example | Source |
|---|---|---|
| Rack/dashboard id | `bos01-a01`, `bos01-a05`, `bos01-b03` | `cmsh rack list` |
| Real node hostname | `bos01-a05-11-s1-dgx-01-c01` (`.cm.cluster`, resolves to `10.29.137.x`) | `sinfo -N`, `getent hosts` |

```text
# ❌ WRONG — what a rack-id-derived CT template produced (does not resolve)
sysinfo target: bos01-a01-[01-18]   →  pdsh: "Name or service not known"

# ✅ RIGHT — real Slurm node names
sysinfo target: bos01-a05-11-s1-dgx-01-c[01-18]  (verify each with getent hosts)
```

## Checklist before generating node targets for a new cluster

- [ ] `sinfo -N -h -o %N | head` — capture the real node hostname pattern.
- [ ] `getent hosts <one-node>` — confirm it resolves from the execution host.
- [ ] Set `NVIS_CT_ID` from the real pattern, not the rack id.
- [ ] Note: `cmsh` is often **head-node-only**; real node names come from Slurm.
