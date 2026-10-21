# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Turning a rack id into a node target a command can actually use.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The bridge between the two namespaces that ``cluster-naming-conventions.mdc`` insists are different:
the rack/dashboard id (``NVIS_RACK_ID``, from ``cmsh``) and the real compute hostnames (Slurm/DNS).
``naming.py`` deliberately covers only the first; this covers producing a target from both, which is
what pdsh, sysinfo and every sbatch actually need.

Kept out of ``naming.py`` because it is not template arithmetic: the default path asks live Slurm
inventory for the rack's real ``NodeName`` values and only falls back to the ``NVIS_*`` template when
inventory is unavailable. That order matters -- a naive ``<rack>-[01-NN]`` produces targets that do
not resolve, and the failure looks like a network problem.
"""
from __future__ import annotations

import os
import re

import config_env
import host_exec
from config_env import _eget, _env_int
from core import is_valid_node_hostname
from core import slurm_job_base_id as _slurm_job_base_id
from naming import _full_rack_expected_nodes, _nvis_naming
from platforms import get_platform as _get_platform
from slurm_query import (
    _all_slurm_nodenames,
    _dedupe_hostnames_preserve_spelling,
    _rack_node_hostnames_from_inventory,
)


def _scontrol_hostnames_preserving_case(nodespec: str) -> list[str]:
    """Hostnames from ``scontrol show hostnames``, one per line, preserving Slurm ``NodeName`` spelling."""
    nodespec = nodespec.strip()
    if not nodespec or nodespec.lower() in ("none", "null"):
        return []
    proc = host_exec.run_slurm_on_host(
        _get_platform().scontrol_hostnames_cmd(nodespec),
        timeout=max(30, _env_int("RACK_SCONTROL_HOSTNAMES_TIMEOUT", 120)),
    )
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _split_pdsh_w_top_level_commas(w: str) -> list[str]:
    """Split a pdsh ``-w`` comma list without splitting commas inside ``[…]`` host ranges."""
    w = (w or "").strip()
    if not w:
        return []
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in w:
        if ch == "[":
            depth += 1
            cur.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            s = "".join(cur).strip()
            if s:
                out.append(s)
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _expand_pdsh_w_slurm_nodenames(w: str) -> str:
    """Expand each Slurm nodeset fragment via ``scontrol show hostnames`` so pdsh uses NodeName casing.

    pdsh bracket expansion can lower-case host tokens, which breaks ssh/DNS on some sites. When Slurm
    expansion fails for a fragment, the original fragment is kept. Set ``RACK_PDSH_W_SKIP_SLURM_EXPAND``
    to skip this step entirely.
    """
    w = (w or "").strip()
    if not w:
        return w
    if os.environ.get("RACK_PDSH_W_SKIP_SLURM_EXPAND", "").strip().lower() in ("1", "true", "yes"):
        return w
    parts_out: list[str] = []
    for seg in _split_pdsh_w_top_level_commas(w):
        hosts = _scontrol_hostnames_preserving_case(seg)
        if hosts:
            parts_out.extend(hosts)
        else:
            parts_out.append(seg)
    return ",".join(parts_out)


_CT_RANGE_INNER_RE = re.compile(r"^[0-9,\-]+$")


def _validation_ct_inner() -> str:
    s = (_eget("RACK_VALIDATION_CT_RANGE", "01-18") or "").strip()
    if not s or not _CT_RANGE_INNER_RE.match(s):
        return "01-18"
    return s


def launch_scope_for_target(target: str) -> tuple[str, list[str]]:
    """``("rack", [])`` when *target* is a whole rack, ``("node", hosts)`` when it is fewer nodes.

    A rack is validated only by a run over all of its nodes (``RACK_FULL_RACK_NODES``); a run
    over some of them is evidence for those nodes. The launch records which one it is, so the
    rack table and the per-node view each read only their own runs. A target that does not
    expand to plain hostnames (a template the site resolves later) stays a rack launch.
    """
    from universal_parser import expand_nodelist

    hosts = [h for h in expand_nodelist((target or "").strip()) if h]
    if not hosts or any("[" in h or not is_valid_node_hostname(h) for h in hosts):
        return "rack", []
    if len(hosts) >= _full_rack_expected_nodes():
        return "rack", []
    return "node", hosts


def nccl_env_scope(rack_count: int, node_count: int) -> str:
    """Which of a cluster's ``nccl_env`` lists (``models.NCCL_ENV_SCOPES``) a launch receives.

    The span NCCL actually runs over, which is not the evidence scope above: a launch on 16 of a
    rack's 18 nodes is node *evidence* (``launch_scope_for_target``), but NCCL still runs it inside
    one rack's NVLink domain, so it takes the rack list. More than one rack is ``multirack`` -- the
    scale-out fabric carries traffic; exactly one node is ``node``; anything else is ``rack``. A caller
    that knows nodes but not their racks passes ``rack_count=0`` and gets ``rack`` for several nodes.
    """
    if rack_count > 1:
        return "multirack"
    if node_count == 1:
        return "node"
    return "rack"


def nccl_env_scope_for_target(target: str) -> str:
    """``nccl_env_scope`` for a launch target (a hostlist, or a rack template).

    The racks are the ones the hosts are placed in, not the one the launch names: a Stage-1 launch
    against rack U22 with sixteen hosts from U22 and sixteen from U23 crosses the scale-out fabric,
    and took the single-rack list until this counted (Nscale NC Slurm 189003, 2026-10-12). Hosts
    the inventory has not placed count as one rack, as before. A target that does not expand to
    plain hostnames is a template the site resolves later, which is always a whole rack.
    """
    from universal_parser import expand_nodelist

    hosts = [h for h in expand_nodelist((target or "").strip()) if h]
    if not hosts or any("[" in h or not is_valid_node_hostname(h) for h in hosts):
        return "rack"
    try:
        import native_status

        placed = native_status.placed_racks_of(hosts) or {}
    except Exception:  # noqa: BLE001 - placement is a refinement; unplaced keeps the old answer
        placed = {}
    return nccl_env_scope(max(1, len(set(placed.values()))), len(hosts))


#: The launch env key the rank-layout library reads (bin/scripts/lib/rank_layout.inc.sh).
NODE_PLACEMENT_ENV = "NVIS_NODE_PLACEMENT"


def placement_env(groups: dict[str, str]) -> dict[str, str]:
    """``NVIS_NODE_PLACEMENT`` for ``rack -> hostlist`` (any Slurm hostlist), or ``{}`` for none.

    The llmb launchers lay their ranks out rack by rack from it, because Slurm's own node order
    interleaves racks whose hostnames do not group by rack and Megatron builds its tensor- and
    expert-parallel groups from consecutive ranks (Nscale NC, Slurm 216666, 2026-10-20).
    """
    entries = [f"{rack}={hosts}" for rack, hosts in groups.items()
               if rack and hosts and not set("=;") & set(rack + hosts)]
    return {NODE_PLACEMENT_ENV: ";".join(entries)} if entries else {}


def node_placement_env(target: str) -> dict[str, str]:
    """The placement of a launch target's hosts, as :func:`placement_env`, from the node inventory.

    Hosts the inventory has not placed are left out (the launcher keeps them last, in natural order);
    a target that is not a plain hostlist -- a rack template the site resolves later -- has none.
    """
    from universal_parser import expand_nodelist

    hosts = [h for h in expand_nodelist((target or "").strip()) if h]
    if not hosts or any("[" in h or not is_valid_node_hostname(h) for h in hosts):
        return {}
    try:
        import native_status

        placed = native_status.placed_racks_of(hosts) or {}
    except Exception:  # noqa: BLE001 - placement is a refinement; without it ranks keep Slurm's order
        placed = {}
    groups: dict[str, list[str]] = {}
    for host in hosts:
        if host in placed:
            groups.setdefault(placed[host], []).append(host)
    return placement_env({rack: ",".join(members) for rack, members in groups.items()})


def validation_target_for_racks(racks: list[str], override: str | None) -> str:
    """The node target for a launch that spans *racks* (one rack: ``validation_target_for_rack``).

    An explicit hostname selection keeps the hosts that belong to ANY of the racks the launch names.
    Checking it against one rack -- the only rack a Stage-1 launch could name -- silently turned a
    16+16 U22+U23 selection into a 16-node U22 run (Nscale NC, 2026-10-12): the hosts of the other
    rack were dropped as foreign. They are foreign only to a rack the launch did not name, and every
    named rack is authorized (the resolver reads ``rack`` and ``racks``). With no selection the
    target is each rack in full. A ``<rack>-ct[...]`` template names one rack, so it is refused here.
    """
    racks = [r for r in dict.fromkeys(r.strip() for r in racks) if r]
    if len(racks) <= 1:
        return validation_target_for_rack(racks[0] if racks else "", override)
    ov = (override or "").strip()
    if not ov:
        return ",".join(validation_target_for_rack(r, None) for r in racks)
    if "[" in ov:
        raise ValueError("a rack template targets one rack; select hostnames for a run across racks")
    tokens = [t.strip() for t in ov.split(",") if t.strip()]
    inventory_on = os.environ.get("RACK_TARGET_FROM_INVENTORY", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )
    members: dict[str, str] = {}
    if inventory_on:
        for r in racks:
            members.update({h.casefold(): h for h in _rack_node_hostnames_from_inventory(r)})
    if not members:
        bad = [t for t in tokens if not is_valid_node_hostname(t)]
        if bad:
            raise ValueError("invalid node hostname(s): " + ", ".join(repr(t) for t in bad[:5]))
        return ",".join(dict.fromkeys(tokens))
    chosen = [members[t.casefold()] for t in tokens if t.casefold() in members]
    if not chosen:
        raise ValueError(f"none of the selected hosts is in racks {', '.join(racks)}")
    return ",".join(dict.fromkeys(chosen))


def validation_target_for_rack(rack: str, override: str | None) -> str:
    """Build the node target for run_singlerack.sh / sysinfo / pdsh.

    Override precedence:

    1. An override that is a **plain comma-list of real node hostnames** (no ``[`` range
       syntax) is honored as an explicit node selection — e.g. the UI sends the selected
       trays directly when the site's hostnames don't fold into ``<rack><joiner>[NN]``
       (Boston: ``bos01-b07-11-s1-dgx-07-c01``). With no rack, this is the node-scoped path.
       The list is intersected with live Slurm
       inventory for the rack when available (drops typos / foreign hosts); if inventory
       is unavailable the tokens are trusted as-is.
    2. An override in ``<rack><joiner>[ranges]`` template form is validated/normalized by
       the naming schema.
    3. With no override, the target is the **full rack** from live Slurm inventory (real
       ``NodeName`` values), falling back to the ``NVIS_*`` template when inventory is
       unavailable in Direct mode. Translate mode requires placement or explicit node names.
       Set ``RACK_TARGET_FROM_INVENTORY=0`` to force the template path in Direct mode.
    """
    ov = (override or "").strip()
    inventory_on = (
        os.environ.get("RACK_TARGET_FROM_INVENTORY", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    if ov and "[" not in ov:
        # Plain (non-range) override → treat as an explicit hostname selection.
        tokens = [t.strip() for t in ov.split(",") if t.strip()]
        if tokens:
            inv = (
                _rack_node_hostnames_from_inventory(rack) if rack else _all_slurm_nodenames()
            ) if inventory_on else []
            if inv:
                inv_cf = {h.casefold(): h for h in inv}
                chosen = [inv_cf[t.casefold()] for t in tokens if t.casefold() in inv_cf]
                if chosen:
                    # de-dup, preserve request order
                    seen: set[str] = set()
                    out = [h for h in chosen if not (h in seen or seen.add(h))]
                    return ",".join(out)
                # None matched inventory: fall through to template validation so a bad
                # override surfaces as an error instead of silently targeting the rack.
            else:
                # Inventory unavailable, so there is nothing to intersect against -- but the
                # tokens are still operator-supplied and still reach ``pdsh -w``, ``scontrol
                # update nodename=`` and script argv. This branch was the one path by which a
                # string nobody had looked at became a node target, so it gets the charset check
                # at minimum. Raises ValueError, which the routes already answer as 400.
                bad = [t for t in tokens if not is_valid_node_hostname(t)]
                if bad:
                    raise ValueError(
                        "invalid node hostname(s): " + ", ".join(repr(t) for t in bad[:5])
                    )
                return ",".join(tokens)
    if not ov and inventory_on:
        hosts = _rack_node_hostnames_from_inventory(rack)
        if hosts:
            return ",".join(hosts)
    if not rack:
        raise ValueError("an explicit node target is required when rack is empty")
    if config_env.rack_mapping_mode() != "direct":
        # Only Direct hostnames encode the rack; a template target here names hosts that don't exist.
        raise ValueError("rack placement is unavailable; select explicit node hostnames")
    return _nvis_naming().build_validation_target(
        rack, _validation_ct_inner(), override
    )


# Host list for a FINISHED job, from sacct rather than squeue: the parser and the Grafana embed both ask
# about runs that have already ended, and squeue only knows about live ones. Here rather than in
# slurm_query because it reuses this module's hostname expansion, and slurm_query cannot import back.


def _hostnames_for_slurm_job(job_raw: str | None) -> tuple[list[str], str]:
    bid = _slurm_job_base_id(job_raw)
    if not bid:
        return [], "no_job_id"
    proc = host_exec.run_slurm_on_host(
        _get_platform().sacct_job_cmd(bid),
        timeout=max(20, _env_int("RACK_SACCT_TIMEOUT", 90)),
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return [], "sacct_failed"
    hosts_flat: list[str] = []
    for line in (proc.stdout or "").splitlines():
        seg = line.strip()
        if not seg:
            continue
        for part in seg.split("+"):
            part = part.strip()
            if not part:
                continue
            hosts_flat.extend(_scontrol_hostnames_preserving_case(part))
    hosts = _dedupe_hostnames_preserve_spelling(hosts_flat)
    return hosts, ("sacct" if hosts else "sacct_empty")
