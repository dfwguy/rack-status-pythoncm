# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Turning one example hostname into naming templates -- proposals, never guesses.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Asking an operator to write ``CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99]`` from memory is how a
deployment ends up with targets that do not resolve. Asking them for one hostname they already
know is easy. This module is the bridge, and it is careful about what it cannot know.

**One hostname is genuinely ambiguous, and the ambiguity is not hypothetical.** Given
``bos01-a05-11-s1-dgx-01-c01`` nothing in the string says that ``-s1-dgx-`` is fixed
infrastructure text rather than data. Given ``CBR01-DH01-SVR-CN010213`` the tray could be
``13``, ``213`` or ``0213`` -- ``nvis_id_templates`` warns in its own docstring that consecutive
``[\\d+]`` slots are ambiguous because the regex is greedy. And geometry (18 trays or 8,
Nscale's non-contiguous ``11-18,28-37``) is not in a hostname at all.

So this never returns "the" answer. It returns candidates, each one **already proved against
the operator's own example**: every proposal is compiled with ``NvisNaming.from_templates`` and
run through ``match_hostname``, and a candidate that cannot parse the hostname it was derived
from is dropped rather than offered. The caller shows the decomposition -- cluster, row, rack,
tray -- so the operator confirms a parse they can see instead of approving a regex they cannot.

The families come from the four deployments whose templates are pinned in
``test_nvis_id_templates.py``; the generic segmentation is the fallback for a site that is none
of them.
"""

from __future__ import annotations

import re

import nvis_id_templates as nid

#: A run of digits, which is what every template slot ultimately matches.
_DIGITS = re.compile(r"\d+")

#: A single lower/upper letter used as a row id (Nscale's `h15`, Boston's `a05`). Recognised
#: because `nvis_id_templates` has slot tokens for exactly this -- `[a-z]` and `[A-Z]`.
_ROW_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z])(\d+)")


def _slot_for(digits: str) -> str:
    """The narrowest slot token that matches *digits* and canonicalises the same way.

    Two-digit fields become ``[01-99]`` rather than ``[\\d+]``: the module's own guidance is to
    prefer the fixed-width token where the field is fixed width, because adjacent ``[\\d+]``
    slots cannot be split unambiguously. Anything else stays variable, which is how cmsh spells
    the Bright ids.
    """
    return "[01-99]" if len(digits) == 2 else r"[\d+]"


def _templates_from_segments(host: str, rack_upto: int) -> tuple[str, str]:
    """Replace digit runs with slots, splitting after the *rack_upto*-th run.

    `rack_upto` is the number of digit groups that belong to the rack id; everything after it is
    tray/node identity. That single number is what the caller is really choosing between, and it
    is why the proposals differ.
    """
    rack_parts: list[str] = []
    ct_parts: list[str] = []
    pos = 0
    for index, m in enumerate(_DIGITS.finditer(host), start=1):
        literal = host[pos : m.start()]
        slot = _slot_for(m.group(0))
        if index <= rack_upto:
            rack_parts.append(literal + slot)
        ct_parts.append(literal + slot)
        pos = m.end()
    tail = host[pos:]
    # Trailing literal belongs to the CT template only; a rack id stops at its last slot.
    return "".join(rack_parts), "".join(ct_parts) + tail


def _row_letter_variant(rack_t: str, ct_t: str) -> tuple[str, str] | None:
    """The same templates with the rack's last letter-before-a-slot turned into a letter slot.

    `ink50-dh01-h15-15` is why this exists: `h` is a row id, so leaving it literal builds a
    cluster that can only ever see row h. `bos01-a05-...` is the same shape.

    It is a *variant*, not a correction, because one hostname cannot distinguish a row letter
    from fixed text -- the `d` in `dh01` and the `s` in `s1-dgx` are letters before digits too.
    Both readings are offered, each proved against the example, and the operator picks the one
    that matches their site. Guessing here would silently pin a cluster to one row.
    """
    # The letter immediately before the rack template's final slot: the row in every deployment
    # whose templates are pinned in the tests.
    m = re.search(r"([A-Za-z])(\[01-99\]|\[\\d\+\])$", rack_t)
    if not m:
        return None
    letter, slot = m.group(1), m.group(2)
    token = "[a-z]" if letter.islower() else "[A-Z]"
    head = rack_t[: m.start(1)]
    new_rack = head + token + slot
    # The CT template shares the rack's leading literals, so the same substring is replaced
    # there -- `validate_nvis_naming_triplet` requires the rack to prefix the CT.
    if not ct_t.startswith(rack_t):
        return None
    new_ct = new_rack + ct_t[len(rack_t) :]
    return new_rack, new_ct


def _split_trailing_run(host: str, rack_upto: int) -> tuple[str, str] | None:
    """Templates that read a long final digit run as adjacent fixed-width fields.

    This is the CBR/Firmus shape and the reason the module's docstring warns about greedy
    slots: `CBR01-DH01-SVR-CN010213` ends in one run of six digits that is really three
    two-digit fields -- row, rack, tray -- so the rack id is `...-SVR-CN0102` and the tray is
    `13`. Treating the run as one slot (the default segmentation) yields a "tray" of `010213`,
    which parses the example and is useless.

    Offered only when the run divides into two-digit fields, because that is the case the
    fixed-width `[01-99]` token exists for and the only one where the split is unambiguous.
    """
    runs = list(_DIGITS.finditer(host))
    if not runs:
        return None
    last = runs[-1]
    body = last.group(0)
    if len(body) < 4 or len(body) % 2:
        return None

    head_parts: list[str] = []
    pos = 0
    for m in runs[:-1]:
        head_parts.append(host[pos : m.start()] + _slot_for(m.group(0)))
        pos = m.end()
    head = "".join(head_parts) + host[pos : last.start()]

    fields = len(body) // 2
    # The last two-digit field is the tray; everything before it extends the rack id.
    rack_t = head + "[01-99]" * (fields - 1)
    ct_t = head + "[01-99]" * fields
    if rack_upto < len(runs) - 1:
        return None
    return rack_t, ct_t


def _plausibility(candidate: dict) -> tuple[int, int]:
    """Sort key: the least surprising reading first.

    A tray number is one to three digits in every deployment we have seen, so a candidate
    claiming a six-digit tray is offering to read a whole rack id as a node index. It is kept --
    it *does* parse the example, and a site might genuinely number that way -- but it is not
    what an operator should be shown first.
    """
    tray = str(candidate.get("parsed", {}).get("tray") or "")
    implausible_tray = 0 if 1 <= len(tray) <= 3 else 1
    # Among equally plausible readings, the more specific rack id first: it is the one that
    # names a rack rather than a whole hall.
    return (implausible_tray, -int(candidate.get("rack_digit_groups") or 0))


def _nvsw_from(rack_template: str) -> str:
    """A plausible NVSwitch template: the rack id plus the conventional suffix.

    A compute hostname says nothing about switch naming, so this is the documented default
    shape rather than a derivation, and the wizard shows it as editable.
    """
    return rack_template + r"-nvsw-[\d+]"


def _proof(rack_t: str, ct_t: str, nvsw_t: str, host: str) -> dict | None:
    """Compile the triplet and parse *host* with it. `None` when it does not hold up."""
    problem = nid.validate_nvis_naming_triplet(rack_t, ct_t, nvsw_t)
    if problem:
        return None
    try:
        naming = nid.NvisNaming.from_templates(rack_t, ct_t, nvsw_t)
        hit = naming.matcher().match_hostname(host)
    except (ValueError, re.error):
        # A candidate that will not compile is simply not offered. Narrow on purpose: anything
        # else here is a real fault in the template machinery and should surface, not be
        # swallowed into "no proposals" with no explanation.
        return None
    if hit is None or not hit.rack_id:
        return None
    schema = naming.schema_dict()
    return {
        "rack_id_template": rack_t,
        "ct_id_template": ct_t,
        "nvsw_id_template": nvsw_t,
        "ct_joiner": schema.get("ct_joiner", ""),
        "node_id_mode": schema.get("node_id_mode", ""),
        "parsed": {"hostname": hit.hostname, "rack_id": hit.rack_id, "tray": hit.tray},
    }


def _decompose(rack_id: str) -> dict[str, str]:
    """Split a rack id into the parts an operator recognises: cluster / row / rack.

    Best effort and labelled as such in the response. `rack_parts` in `nvis_id_templates` does
    the same job for the floor view, so the vocabulary matches what the dashboard already shows.
    """
    out = {"cluster": "", "row": "", "rack": rack_id}
    m = _ROW_LETTER.search(rack_id)
    if m:
        out["row"] = m.group(1)
    head = rack_id.split("-")[0] if "-" in rack_id else ""
    if head:
        out["cluster"] = head
    return out


def propose(hostname: str) -> dict:
    """Candidate naming triplets for *hostname*, most likely first.

    The candidates differ in one decision -- how many leading digit groups belong to the rack id
    rather than to the tray -- because that is the ambiguity a single example cannot resolve.
    Each is proved against the example before being offered.
    """
    host = (hostname or "").strip()
    if not host:
        return {"ok": False, "error": "give one example compute node hostname"}
    if re.search(r"\s", host):
        return {"ok": False, "error": "a hostname has no spaces in it"}

    groups = _DIGITS.findall(host)
    if not groups:
        return {
            "ok": False,
            "error": (
                f"{host!r} has no digits, so there is nothing to turn into a rack or tray "
                "number. Templates need at least one numeric field."
            ),
        }
    if len(groups) < 2:
        return {
            "ok": False,
            "error": (
                f"{host!r} has one numeric field, so it cannot distinguish a rack from a tray. "
                "Give a full compute node hostname (rack id plus node/tray number)."
            ),
        }

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    # Most plausible first: the tray is the LAST digit group, so the rack takes the rest. That is
    # true of every deployment whose templates are pinned in the tests.
    order = [len(groups) - 1] + [n for n in range(1, len(groups)) if n != len(groups) - 1]
    for rack_upto in order:
        base = _templates_from_segments(host, rack_upto)
        variants = [base]
        row_variant = _row_letter_variant(*base)
        if row_variant:
            variants.append(row_variant)
        split_variant = _split_trailing_run(host, rack_upto)
        if split_variant:
            variants.append(split_variant)
        for rack_t, ct_t in variants:
            if (rack_t, ct_t) in seen:
                continue
            seen.add((rack_t, ct_t))
            proved = _proof(rack_t, ct_t, _nvsw_from(rack_t), host)
            if proved is None:
                continue
            proved["breakdown"] = _decompose(proved["parsed"]["rack_id"])
            proved["rack_digit_groups"] = rack_upto
            proved["row_is_a_slot"] = (rack_t, ct_t) != base
            candidates.append(proved)

    if not candidates:
        return {
            "ok": False,
            "error": (
                f"could not derive a template that parses {host!r}. Enter the templates by hand "
                "on this step -- the field help shows the token syntax."
            ),
        }

    candidates.sort(key=_plausibility)
    return {
        "ok": True,
        "hostname": host,
        "candidates": candidates,
        # Said out loud, because the alternative is an operator trusting a guess. The UI shows
        # this beside the parse.
        "caveat": (
            "Derived from one hostname, so check the rack id and tray below. A site with fixed "
            "text between numeric fields (for example -s1-dgx-) or with adjacent numeric fields "
            "may need the templates adjusted by hand."
        ),
    }
