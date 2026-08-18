# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Rack ids, normalised through the cluster's naming templates.

Author: Rafael M. Koike & Cursor Agent ("Ada")

A rack id is not a hostname. ``NVIS_RACK_ID`` names the dashboard/`cmsh` grouping key; the real
compute hostnames come from Slurm and are a different scheme entirely (see
``.cursor/rules/cluster-naming-conventions.mdc`` -- deriving one from the other has produced pdsh
targets that do not resolve). Everything here is about the first scheme only.

``nvis_id_templates`` caches the compiled templates, and the cache is cleared when the active
cluster changes, so always go through ``_nvis_naming()`` rather than holding onto a naming object.
"""

from __future__ import annotations

import re
from pathlib import Path

import nvis_id_templates
from config_env import _eget


def _nvis_naming() -> nvis_id_templates.NvisNaming:
    return nvis_id_templates.get_nvis_naming()


def _normalize_rack_id(rack: str) -> str:
    return _nvis_naming().normalize_rack_id(rack)


# --- Log-basename classification -------------------------------------------------------------
# A validation log's NAME carries its rack, job id, node count and which tool wrote it. Several
# domains need to read that -- parser results, log listings, validation activities, analytics -- so
# these live here rather than being re-derived per module. The patterns themselves belong to
# nvis_id_templates; these are the thin, named questions callers actually ask.


def _nemo_nccl_in_log_basename(name: str) -> bool:
    return nvis_id_templates.nemo_nccl_in_log_basename(name, _nvis_naming())


def _nemo_single_rack_log_basename(name: str) -> bool:
    return nvis_id_templates.nemo_single_rack_log_basename(name, _nvis_naming())


def _jobid_from_nemo_log_basename(name: str) -> str | None:
    return nvis_id_templates.job_id_from_log_basename(name)


def _is_multirack_log_basename(name: str) -> bool:
    return nvis_id_templates.is_multirack_log_basename(name, _nvis_naming())


def _multirack_node_count_suffix(name: str) -> str:
    return nvis_id_templates.node_count_suffix_from_basename(name)


def _rack_id_from_nemo_1rack_filename(name: str) -> str | None:
    """Return normalized rack id if basename looks like a NEMO 1Rack log, else None."""
    return _nvis_naming().rack_id_from_nemo_1rack_filename(name)


def _rack_id_from_log_basename(name: str) -> str | None:
    """Rack id from single-rack NEMO validation basename (1Rack-, root-CK-, …); not Llama Slurm stdout."""
    return _nvis_naming().rack_id_from_log_basename(name)


def _ct_power_token_re() -> re.Pattern[str]:
    """Tray blob tokens from ``ct_down_unk`` (pattern follows ``NVIS_*`` templates)."""
    naming = _nvis_naming()
    rack_part = nvis_id_templates._template_to_regex(naming.rack_template).replace("(?i)", "")
    join = re.escape(naming.ct_joiner)
    return re.compile(rf"({rack_part}{join}\[[^\]]+\])", re.IGNORECASE)


def _ct_power_tray_full_re() -> re.Pattern[str]:
    return _nvis_naming().validation_target_re()


def _ct_power_single_ct_re() -> re.Pattern[str]:
    naming = _nvis_naming()
    rack_part = nvis_id_templates._template_to_regex(naming.rack_template).replace("(?i)", "")
    join = re.escape(naming.ct_joiner)
    return re.compile(rf"^({rack_part}){join}\[(\d+)\]$", re.IGNORECASE)


def _normalize_nvswitch_host_for_rack(rack: str, raw: object) -> str:
    """Require a full NVSwitch hostname belonging to *rack* (``NVIS_NVSW_ID`` template)."""
    rk = _normalize_rack_id(rack)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("nvswitch_host is required")
    return _nvis_naming().normalize_nvsw_host(rk, raw)


def _nvswitch_host_sort_key(hostname: str) -> tuple[int, str]:
    m = re.search(r"-nvsw-(\d+)$", hostname, flags=re.IGNORECASE)
    if m:
        return (int(m.group(1)), hostname.lower())
    return (0, hostname.lower())


# --- Multirack identity and expected node counts ----------------------------------------------
# A multirack log's basename carries the site prefix, the racks it spanned and the node count. The
# display label and the "was this a full rack?" question are both read straight off the name, so they
# belong with the other basename readers rather than with whoever happens to need them.


def _multirack_site_prefix() -> str:
    """Pod id for ``r[01-04]``-style names without ``pN-`` (e.g. ``p5``). See RACK_MULTIRACK_SITE_PREFIX (defaults to p5)."""
    raw = _eget("RACK_MULTIRACK_SITE_PREFIX")
    if raw is None or not str(raw).strip():
        return "p5"
    return str(raw).strip().lower()


def multirack_display_rack_from_basename(name: str) -> str:
    """Human label like ``p5-r01+p5-r02 (36N)`` for multirack log filenames."""
    suffix = _multirack_node_count_suffix(name)
    naming = _nvis_naming()
    racks = sorted(naming.find_rack_ids_in_string(name))
    if len(racks) >= 2:
        return "+".join(racks) + suffix
    pod_re = naming.multirack_pod_r_bracket_re()
    if pod_re:
        m = pod_re.search(name)
        if m:
            expanded = naming.expand_pod_r_inner(m.group(1), m.group(2))
            if expanded:
                return "+".join(expanded) + suffix
    bare_re = naming.multirack_bare_r_bracket_re()
    if bare_re:
        m2 = bare_re.search(name)
        if m2:
            expanded = naming.expand_site_r_inner(_multirack_site_prefix(), m2.group(1))
            if expanded:
                return "+".join(expanded) + suffix
    stem = Path(name).stem
    return (stem[:120] if stem else name) + suffix


_FULL_RACK_NODES_RE = re.compile(r"-(\d+)N-", re.IGNORECASE)


def _node_count_from_log_basename(name: str) -> int | None:
    """Slurm node count from ``…-<N>N-<jobid>.txt`` (e.g. ``18`` from ``…-18N-75270.txt``)."""
    m = _FULL_RACK_NODES_RE.search(name or "")
    if not m:
        return None
    try:
        return int(m.group(1), 10)
    except ValueError:
        return None


def _full_rack_expected_nodes() -> int:
    """Expected full-rack Slurm node count for parser ``full_rack`` filtering (default 18 / NVL72)."""
    raw = (_eget("RACK_FULL_RACK_NODES") or "").strip()
    if raw.isdigit():
        n = int(raw, 10)
        if n > 0:
            return n
    return 18

def _multirack_physical_rack_ids(name: str) -> list[str]:
    """Rack ids for Slurm sinfo aggregation; mirrors ``multirack_display_rack_from_basename`` cases."""
    naming = _nvis_naming()
    racks = sorted(naming.find_rack_ids_in_string(name))
    if len(racks) >= 2:
        return list(racks)
    pod_re = naming.multirack_pod_r_bracket_re()
    if pod_re:
        m = pod_re.search(name)
        if m:
            expanded = naming.expand_pod_r_inner(m.group(1), m.group(2))
            if expanded:
                return expanded
    bare_re = naming.multirack_bare_r_bracket_re()
    if bare_re:
        m2 = bare_re.search(name)
        if m2:
            expanded = naming.expand_site_r_inner(_multirack_site_prefix(), m2.group(1))
            if expanded:
                return expanded
    return []

def _expand_r_inner_to_rack_nums(inner: str) -> list[int]:
    """Expand ``13-14,16`` or ``01-04`` inside ``r[…]`` brackets to rack number ints."""
    out: list[int] = []
    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a, 10), int(b, 10)
            except ValueError:
                continue
            if lo <= hi:
                out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part, 10))
            except ValueError:
                continue
    return sorted(set(out))
