#!/usr/bin/env python3
"""Simple hostname / rack id templates for NVIS Rack Status (Settings → Job environment).

Templates use literal text plus digit placeholders:

- ``[NN-NN]`` — two-digit field (matched as ``\\d{1,2}`` in scan mode, canonicalized with zero-padding).
- ``[\\d+]`` — one or more digits (matched as ``\\d+``; canonical form preserves cmsh spelling).

Default Bright templates (variable width, as spelled in cmsh)::

    NVIS_RACK_ID=p[\\d+]-r[\\d+]
    NVIS_CT_ID=p[\\d+]-r[\\d+]-ct[\\d+]
    NVIS_NVSW_ID=p[\\d+]-r[\\d+]-nvsw-[\\d+]

Optional fixed two-digit fields (legacy / zero-padded dashboard rows)::

    NVIS_RACK_ID=p[01-99]-r[01-99]
    NVIS_CT_ID=p[01-99]-r[01-99]-ct[01-99]

Default CBR-style (``[\\d+]`` pod/hall + ``[01-99]`` for fixed-width ``SVR-CN`` suffix)::

    NVIS_RACK_ID=CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99]
    NVIS_CT_ID=CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99][01-99]

Consecutive ``[\\d+]`` slots without separators are ambiguous (regex greed); prefer
``[01-99]`` for fixed two-digit fields (e.g. the ``SVR-CN`` suffix on CBR racks).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

_TEMPLATE_TOKEN_RE = re.compile(
    r"\[\\d\+\]|\[(\d{2})-(\d{2})\]",
)

# Defaults when Settings leaves naming fields empty (cmsh spelling: p5-r03-ct12, …).
DEFAULT_NVIS_RACK_ID = r"p[\d+]-r[\d+]"
DEFAULT_NVIS_CT_ID = r"p[\d+]-r[\d+]-ct[\d+]"
DEFAULT_NVIS_NVSW_ID = r"p[\d+]-r[\d+]-nvsw-[\d+]"

# CBR / Firmus: variable pod/hall + fixed two-digit ``SVR-CN`` / ``NSW-NV`` suffix pairs.
CBR_NVIS_RACK_ID = r"CBR[\d+]-DH[\d+]-SVR-CN[01-99][01-99]"
CBR_NVIS_CT_ID = r"CBR[\d+]-DH[\d+]-SVR-CN[01-99][01-99][01-99]"
CBR_NVIS_NVSW_ID = r"CBR[\d+]-DH[\d+]-NSW-NV[01-99][01-99][01-99]"

# Legacy Bright: zero-padded two-digit slots (``p5-r1`` → ``p05-r01``).
PADDED_NVIS_RACK_ID = "p[01-99]-r[01-99]"
PADDED_NVIS_CT_ID = "p[01-99]-r[01-99]-ct[01-99]"
PADDED_NVIS_NVSW_ID = "p[01-99]-r[01-99]-nvsw-[01-99]"

_CT_BRACKET_INNER_RE = re.compile(r"^[\d,\-\s]+$")

SlotKind = str  # "range2" | "digits"


@dataclass(frozen=True)
class ParsedTemplate:
    """Decomposed template: literals and numbered digit slots."""

    raw: str
    segments: tuple[tuple[str, ...], ...]  # ('lit', text) | ('slot', index, kind)

    @property
    def slot_count(self) -> int:
        return sum(1 for s in self.segments if s[0] == "slot")

    def slot_kind(self, index: int) -> SlotKind:
        for seg in self.segments:
            if seg[0] == "slot" and int(seg[1]) == index:
                return seg[2]
        return "range2"

    def uses_digits_slots(self) -> bool:
        return any(s[0] == "slot" and s[2] == "digits" for s in self.segments)


def parse_template(template: str) -> ParsedTemplate:
    """Split *template* into literal and ``[NN-NN]`` / ``[\\d+]`` slot segments."""
    t = (template or "").strip()
    if not t:
        raise ValueError("template is empty")
    segments: list[tuple[str, ...]] = []
    pos = 0
    slot_i = 0
    for m in _TEMPLATE_TOKEN_RE.finditer(t):
        if m.start() > pos:
            lit = t[pos : m.start()]
            if lit:
                segments.append(("lit", lit))
        if m.group(0) == r"[\d+]":
            segments.append(("slot", str(slot_i), "digits"))
        else:
            lo, hi = m.group(1), m.group(2)
            if int(lo) > int(hi):
                raise ValueError(f"invalid range [{lo}-{hi}] (low > high)")
            segments.append(("slot", str(slot_i), "range2"))
        slot_i += 1
        pos = m.end()
    if pos < len(t):
        segments.append(("lit", t[pos:]))
    if slot_i == 0:
        raise ValueError("template must include at least one [NN-NN] or [\\d+] slot")
    return ParsedTemplate(raw=t, segments=tuple(segments))


def validate_nvis_template(template: str, *, label: str) -> str | None:
    """Syntax-only validation (no hardcoded sample hostnames)."""
    try:
        parse_template(template)
    except ValueError as e:
        return f"{label}: {e}"
    return None


def validate_nvis_naming_triplet(
    rack_t: str, ct_t: str, nvsw_t: str = ""
) -> str | None:
    """Ensure CT has at least as many slots as RACK; literals share a common prefix."""
    rack_t = (rack_t or "").strip() or DEFAULT_NVIS_RACK_ID
    ct_t = (ct_t or "").strip() or DEFAULT_NVIS_CT_ID
    for label, tpl in (
        ("NVIS_RACK_ID", rack_t),
        ("NVIS_CT_ID", ct_t),
        ("NVIS_NVSW_ID", nvsw_t),
    ):
        if not tpl:
            continue
        err = validate_nvis_template(tpl, label=label)
        if err:
            return err
    try:
        rack_p = parse_template(rack_t)
        ct_p = parse_template(ct_t)
    except ValueError as e:
        return str(e)
    if ct_p.slot_count < rack_p.slot_count:
        return "NVIS_CT_ID must have at least as many digit slots as NVIS_RACK_ID"
    if not _rack_is_prefix_of_ct(rack_p, ct_p):
        return (
            "NVIS_RACK_ID must match the start of NVIS_CT_ID "
            "(same literals and slot order, CT may add more slots at the end)"
        )
    return None


def _rack_is_prefix_of_ct(rack_p: ParsedTemplate, ct_p: ParsedTemplate) -> bool:
    if len(rack_p.segments) > len(ct_p.segments):
        return False
    for rs, cs in zip(rack_p.segments, ct_p.segments):
        if rs != cs:
            return False
    return True


def _build_from_template(parsed: ParsedTemplate, slot_values: list[str]) -> str:
    out: list[str] = []
    for seg in parsed.segments:
        if seg[0] == "lit":
            out.append(seg[1])
        else:
            idx = int(seg[1])
            if idx >= len(slot_values):
                raise IndexError("not enough slot values for template")
            out.append(slot_values[idx])
    return "".join(out)


def _format_slot_groups(parsed: ParsedTemplate, groups: Iterable[str]) -> list[str]:
    """Map regex capture groups to canonical slot strings (zero-pad only for ``range2`` slots)."""
    out: list[str] = []
    slot_i = 0
    for g in groups:
        kind = parsed.slot_kind(slot_i)
        if kind == "digits":
            out.append(g)
        else:
            out.append(g.zfill(2))
        slot_i += 1
    return out


def _slot_regex_pat(kind: SlotKind, *, loose: bool) -> str:
    if kind == "digits":
        return r"(\d+)"
    return r"(\d{1,2})" if loose else r"(\d{2})"


def _template_to_regex(parsed: ParsedTemplate, *, loose: bool = False) -> str:
    parts: list[str] = []
    for seg in parsed.segments:
        if seg[0] == "lit":
            parts.append(re.escape(seg[1]))
        else:
            parts.append(_slot_regex_pat(seg[2], loose=loose))
    return "(?i)" + "".join(parts)


def _bright_pod_style_rack(parsed: ParsedTemplate) -> bool:
    """True when rack template is Bright ``p…-r…`` (pod + rack digit slots)."""
    if len(parsed.segments) < 2:
        return False
    return parsed.segments[0] == ("lit", "p") and parsed.segments[1][0] == "slot"


def _rack_r_slot_pad_width(parsed: ParsedTemplate) -> int:
    """Zero-pad width for the rack-index slot (index 1 in ``p*-r*`` templates); 0 = no padding."""
    if parsed.slot_count < 2:
        return 2
    return 2 if parsed.slot_kind(1) == "range2" else 0


def _ct_validation_joiner(ct_p: ParsedTemplate, rack_p: ParsedTemplate) -> str:
    """Literal between rack id and ``[tray-ranges]`` in validation / ct_down blobs (e.g. ``-ct``)."""
    if ct_p.slot_count <= rack_p.slot_count:
        return ""
    idx = 0
    for seg in ct_p.segments:
        if seg[0] == "slot":
            idx += 1
            if idx == rack_p.slot_count:
                lit_after = ""
                pos = ct_p.segments.index(seg) + 1
                if pos < len(ct_p.segments) and ct_p.segments[pos][0] == "lit":
                    lit_after = ct_p.segments[pos][1]
                return lit_after
    return ""


def _tray_slot_index(ct_p: ParsedTemplate) -> int:
    return ct_p.slot_count - 1


@dataclass(frozen=True)
class HostnameMatch:
    hostname: str
    rack_id: str
    tray: str


@dataclass(frozen=True)
class NamingMatcher:
    rack_template: ParsedTemplate
    ct_template: ParsedTemplate
    ct_re: re.Pattern[str]

    def match_hostname(self, token: str) -> HostnameMatch | None:
        m = self.ct_re.fullmatch(token.strip())
        if not m:
            return None
        groups = _format_slot_groups(self.ct_template, m.groups())
        rack_id = _build_from_template(
            self.rack_template, groups[: self.rack_template.slot_count]
        )
        tray_i = self.ct_template.slot_count - 1
        tray = groups[tray_i] if tray_i < len(groups) else ""
        return HostnameMatch(
            hostname=token.strip(),
            rack_id=rack_id,
            tray=tray.strip(),
        )

    def find_in_line(self, line: str) -> list[HostnameMatch]:
        out: list[HostnameMatch] = []
        seen: set[str] = set()
        for m in self.ct_re.finditer(line):
            tok = m.group(0)
            hm = self.match_hostname(tok)
            if hm:
                key = hm.hostname.casefold()
                if key not in seen:
                    seen.add(key)
                    out.append(hm)
        return out


@dataclass(frozen=True)
class NvisNaming:
    """Compiled naming rules from NVIS_* templates (Settings / job env)."""

    rack_template: ParsedTemplate
    ct_template: ParsedTemplate
    nvsw_template: ParsedTemplate | None
    rack_re: re.Pattern[str]
    rack_scan_re: re.Pattern[str]
    ct_re: re.Pattern[str]
    ct_scan_re: re.Pattern[str]
    nvsw_re: re.Pattern[str] | None
    ct_joiner: str
    tray_slot_index: int

    @classmethod
    def from_templates(
        cls,
        rack_raw: str | None = None,
        ct_raw: str | None = None,
        nvsw_raw: str | None = None,
    ) -> NvisNaming:
        rack_t = (rack_raw or "").strip() or DEFAULT_NVIS_RACK_ID
        ct_t = (ct_raw or "").strip() or DEFAULT_NVIS_CT_ID
        nvsw_s = (nvsw_raw or "").strip()
        rack_p = parse_template(rack_t)
        ct_p = parse_template(ct_t)
        nvsw_p = parse_template(nvsw_s) if nvsw_s else None
        return cls(
            rack_template=rack_p,
            ct_template=ct_p,
            nvsw_template=nvsw_p,
            rack_re=re.compile(_template_to_regex(rack_p)),
            rack_scan_re=re.compile(_template_to_regex(rack_p, loose=True)),
            ct_re=re.compile(_template_to_regex(ct_p)),
            ct_scan_re=re.compile(_template_to_regex(ct_p, loose=True)),
            nvsw_re=re.compile(_template_to_regex(nvsw_p)) if nvsw_p else None,
            ct_joiner=_ct_validation_joiner(ct_p, rack_p),
            tray_slot_index=_tray_slot_index(ct_p),
        )

    def matcher(self) -> NamingMatcher:
        """Hostname matcher for cmsh ``ds`` lines (loose digit slots for Slurm/cmsh spelling)."""
        return NamingMatcher(
            rack_template=self.rack_template,
            ct_template=self.ct_template,
            ct_re=self.ct_scan_re,
        )

    def schema_dict(self) -> dict[str, str]:
        return {
            "rack_id": self.rack_template.raw,
            "ct_id": self.ct_template.raw,
            "nvsw_id": self.nvsw_template.raw if self.nvsw_template else "",
            "ct_joiner": self.ct_joiner,
            "node_id_mode": "rack_ct_suffix" if self.ct_joiner == "-ct" else "full_hostname",
        }

    def canonical_rack_id(self, rid: str) -> str | None:
        m = self.rack_scan_re.fullmatch(rid.strip())
        if not m:
            return None
        groups = _format_slot_groups(self.rack_template, m.groups())
        return _build_from_template(self.rack_template, groups)

    def map_cmsh_rack_list_token(self, token: str) -> str | None:
        """Map a ``cmsh -c 'rack; list'`` rack object id to the dashboard row id (``NVIS_RACK_ID``).

        On some BCM sites the rack object is named ``…-RK0102`` while validation logs and
        ``NVIS_RACK_ID`` use ``…-SVR-CN0102`` (same four-digit suffix). Bright sites usually
        already use the row id in ``rack; list``.
        """
        t = (token or "").strip()
        if not t or t.isdigit():
            return None
        c = self.canonical_rack_id(t)
        if c:
            return c
        rack_lit = "".join(seg[1] for seg in self.rack_template.segments if seg[0] == "lit")
        if "SVR-CN" not in rack_lit.upper():
            return None
        m = re.fullmatch(r"(?P<prefix>.+)-RK(?P<suf>\d{4})$", t, re.IGNORECASE)
        if not m:
            return None
        return self.canonical_rack_id(f"{m.group('prefix')}-SVR-CN{m.group('suf')}")

    def normalize_rack_id(self, rack: str) -> str:
        c = self.canonical_rack_id(rack)
        if not c:
            raise ValueError(f"invalid rack id (expected pattern like {self.rack_template.raw})")
        return c

    def is_rack_id(self, rack: str) -> bool:
        return self.canonical_rack_id(rack) is not None

    def extract_rack_ids_from_text(self, text: str) -> list[str]:
        canon: set[str] = set()
        for m in self.rack_scan_re.finditer(text or ""):
            c = self.canonical_rack_id(m.group(0))
            if c:
                canon.add(c)
        return sorted(canon, key=self.rack_sort_key)

    def find_rack_ids_in_string(self, s: str) -> set[str]:
        out: set[str] = set()
        for m in self.rack_scan_re.finditer(s or ""):
            c = self.canonical_rack_id(m.group(0))
            if c:
                out.add(c)
        return out

    def rack_sort_key(self, rid: str) -> tuple[int, ...]:
        m = self.rack_scan_re.fullmatch(rid.strip())
        if not m:
            return (10**9,) * (self.rack_template.slot_count + 1) + (hash(rid) % 10**6,)
        return tuple(int(g, 10) for g in m.groups()) + (0,)

    def is_r00_rack(self, rid: str) -> bool:
        c = self.canonical_rack_id(rid)
        if not c:
            return False
        m = self.rack_re.fullmatch(c)
        if not m or not m.groups():
            return False
        return int(m.groups()[-1], 10) == 0

    def build_tray_hostname(self, rack_id: str, tray: str) -> str:
        """Full CT hostname from canonical rack id and tray token."""
        m = self.rack_scan_re.fullmatch(rack_id.strip())
        if not m:
            return f"{rack_id}{self.ct_joiner}{tray}"
        groups = _format_slot_groups(self.rack_template, m.groups())
        tray_s = tray.strip()
        tray_kind = self.ct_template.slot_kind(self.tray_slot_index)
        if tray_s.isdigit() and tray_kind == "range2":
            groups.append(tray_s.zfill(2))
        else:
            groups.append(tray_s)
        while len(groups) < self.ct_template.slot_count:
            groups.append("00" if self.ct_template.slot_kind(len(groups)) == "range2" else "0")
        return _build_from_template(self.ct_template, groups)

    def tray_range_pad_width(self) -> int:
        """Digit width for tray tokens inside validation ``[…]`` brackets (0 = unpadded)."""
        return 2 if self.ct_template.slot_kind(self.tray_slot_index) == "range2" else 0

    def condense_tray_range_inner(self, nums: list[int]) -> str:
        """Compress tray numbers for ``{rack}{joiner}[inner]`` (respects ``[\\d+]`` vs ``[01-99]``)."""
        if not nums:
            return ""
        sorted_u = sorted(set(nums))
        pad = self.tray_range_pad_width()

        def _fmt(n: int) -> str:
            return f"{n:02d}" if pad else str(n)

        out: list[str] = []
        s = e = sorted_u[0]
        for n in sorted_u[1:]:
            if n == e + 1:
                e = n
            else:
                out.append(_fmt(s) if s == e else f"{_fmt(s)}-{_fmt(e)}")
                s = e = n
        out.append(_fmt(s) if s == e else f"{_fmt(s)}-{_fmt(e)}")
        return ",".join(out)

    def ct_blob(self, rack_id: str, ranges: str) -> str:
        rk = self.canonical_rack_id(rack_id) or rack_id.strip()
        return f"{rk}{self.ct_joiner}[{ranges}]"

    def build_validation_target(
        self, rack: str, ct_inner: str, override: str | None = None
    ) -> str:
        rk = self.normalize_rack_id(rack)
        o = (override or "").strip()
        if o:
            if not self.validation_target_re().fullmatch(o):
                raise ValueError(
                    f"invalid target (expected {self.rack_template.raw}{self.ct_joiner}[ranges])"
                )
            return o
        inner = (ct_inner or "").strip()
        if not inner or not _CT_BRACKET_INNER_RE.match(inner):
            raise ValueError("invalid CT range inner")
        return f"{rk}{self.ct_joiner}[{inner}]"

    def validation_target_re(self) -> re.Pattern[str]:
        esc_rack = _template_to_regex(self.rack_template).replace("(?i)", "")
        esc_join = re.escape(self.ct_joiner)
        return re.compile(
            rf"^{esc_rack}{esc_join}\[[\d,\-\s]+\]$",
            re.IGNORECASE,
        )

    def uptime_pdsh_wildcard(self, rack: str, ct_inner: str) -> str:
        rk = self.normalize_rack_id(rack)
        return f"{rk}{self.ct_joiner}[{ct_inner}]"

    def is_tray_hostname(self, host: str) -> bool:
        return self.ct_re.fullmatch(host.strip()) is not None

    def is_nvsw_hostname(self, host: str) -> bool:
        if self.nvsw_re is None:
            return False
        return self.nvsw_re.fullmatch(host.strip()) is not None

    def normalize_nvsw_host(self, rack: str, raw: str) -> str:
        rk = self.normalize_rack_id(rack)
        h_raw = raw.strip()
        if self.nvsw_re is None or not self.nvsw_re.fullmatch(h_raw):
            raise ValueError(
                f"invalid nvswitch_host (expected pattern like "
                f"{self.nvsw_template.raw if self.nvsw_template else 'NVIS_NVSW_ID'})"
            )
        if not h_raw.casefold().startswith(rk.casefold()):
            raise ValueError("nvswitch_host does not match rack")
        return h_raw

    def nvsw_nodeset_specs(self, rack: str) -> tuple[str, ...]:
        rk = self.normalize_rack_id(rack)
        if self.nvsw_template and "-nvsw-" in self.nvsw_template.raw.lower():
            return (f"@{rk}-nvsw", f"{rk}-nvsw-[01-09]")
        lit = ""
        for seg in self.nvsw_template.segments if self.nvsw_template else ():
            if seg[0] == "lit" and "nvsw" in seg[1].lower():
                lit = seg[1]
                break
        if lit:
            return (f"@{rk}{lit}", f"{rk}{lit}[01-09]")
        return (f"@{rk}-nvsw", f"{rk}-nvsw-[01-09]")

    def nvsw_fallback_hosts(self, rack: str) -> list[str]:
        rk = self.normalize_rack_id(rack)
        if self.nvsw_template:
            lit_tail = ""
            nvsw_slot_kind: SlotKind = "range2"
            for seg in reversed(self.nvsw_template.segments):
                if seg[0] == "lit":
                    lit_tail = seg[1] + lit_tail
                elif seg[0] == "slot":
                    nvsw_slot_kind = seg[2]
                    break
            if lit_tail:
                if nvsw_slot_kind == "digits":
                    return [f"{rk}{lit_tail}{i}" for i in range(1, 10)]
                return [f"{rk}{lit_tail}{i:02d}" for i in range(1, 10)]
        return [f"{rk}-nvsw-{i:02d}" for i in range(1, 10)]

    def sinfo_rack_from_host(self, host: str) -> tuple[str, str | None] | None:
        hh = host.strip().split(".")[0]
        m_ct = self.ct_scan_re.fullmatch(hh)
        if m_ct:
            groups = _format_slot_groups(self.ct_template, m_ct.groups())
            rack = _build_from_template(
                self.rack_template, groups[: self.rack_template.slot_count]
            )
            return rack, hh
        m_r = self.rack_scan_re.fullmatch(hh)
        if m_r:
            groups = _format_slot_groups(self.rack_template, m_r.groups())
            rack = _build_from_template(self.rack_template, groups)
            return rack, None
        return None

    def nemo_1rack_filename_re(self) -> re.Pattern[str]:
        # Loose digit slots: logs often use ``p5-r01`` while the dashboard row id is ``p05-r01``.
        inner = _template_to_regex(self.rack_template, loose=True).replace("(?i)", "")
        # Bright: …1Rack-p05-r01-ct[…]-…  CBR: …1Rack-CBR01-…-CN0102[11-18]-… (``[`` right after rack id)
        return re.compile(rf"1Rack-({inner})(?:-|\[)", re.IGNORECASE)

    def auxiliary_single_rack_log_basename(self, name: str) -> bool:
        """True for non-NEMO basenames that still tie to one rack (e.g. ClusterKit ``root-CK-…``)."""
        low = (name or "").lower()
        if "root-ck-" in low or low.startswith("root-ck-"):
            return True
        if "clusterkit" in low:
            return True
        return False

    def is_llama_slurm_stdout_basename(self, name: str) -> bool:
        """True for ``run-llama.sh`` Slurm stdout (``…NEMO-LLAMA3_8B-1N-<host>-<jid>.txt``), not 1Rack NEMO validation."""
        return "nemo-llama3_8b-1n-" in (name or "").lower()

    def rack_id_from_llama_slurm_basename(self, name: str) -> str | None:
        """Rack id from ``run-llama.sh`` / ``sbatch -o …/sn-llama/…-NEMO-LLAMA3_8B-1N-<hostname>-<jid>.txt``."""
        low = (name or "").lower()
        key = "nemo-llama3_8b-1n-"
        i = low.find(key)
        if i < 0:
            return None
        tail = name[i + len(key) :]
        if not tail.lower().endswith(".txt"):
            return None
        body = tail[:-4]
        if "-" not in body:
            return None
        host, job_tail = body.rsplit("-", 1)
        if not job_tail.isdigit():
            return None
        host = host.strip()
        if not host:
            return None
        hm = self.matcher().match_hostname(host)
        if hm:
            return hm.rack_id
        racks = self.find_rack_ids_in_string(host)
        if len(racks) == 1:
            return next(iter(racks))
        return None

    def rack_id_from_log_basename(self, name: str) -> str | None:
        """Rack id from a single-rack NEMO validation log basename (1Rack-, auxiliary e.g. root-CK-…).

        Excludes NCCL-only sweeps and Llama Slurm stdout (``…NEMO-LLAMA3_8B-1N-…``).
        """
        if self.is_llama_slurm_stdout_basename(name):
            return None
        rid = self.rack_id_from_nemo_1rack_filename(name)
        if rid:
            return rid
        low = (name or "").lower()
        if "nemo" in low:
            racks = self.find_rack_ids_in_string(name)
            if len(racks) == 1:
                return next(iter(racks))
        if not self.auxiliary_single_rack_log_basename(name):
            return None
        racks = self.find_rack_ids_in_string(name)
        if len(racks) != 1:
            return None
        return next(iter(racks))

    def rack_id_from_nemo_1rack_filename(self, name: str) -> str | None:
        m = self.nemo_1rack_filename_re().search(name)
        if not m:
            return None
        return self.canonical_rack_id(m.group(1))

    def multirack_comma_after_rack_re(self) -> re.Pattern[str]:
        first_lit = ""
        for seg in self.rack_template.segments:
            if seg[0] == "lit" and seg[1]:
                first_lit = seg[1][:3].lower()
                break
        if first_lit:
            return re.compile(rf",\s*{re.escape(first_lit)}", re.IGNORECASE)
        return re.compile(r",\s*\w", re.IGNORECASE)

    def multirack_pod_r_bracket_re(self) -> re.Pattern[str] | None:
        if not _bright_pod_style_rack(self.rack_template):
            return None
        return re.compile(r"(p\d+)-r\[([\d,\-]+)\]", re.IGNORECASE)

    def multirack_bare_r_bracket_re(self) -> re.Pattern[str] | None:
        if not _bright_pod_style_rack(self.rack_template):
            return None
        return re.compile(r"(?:^|[\-_])r\[([\d,\-]+)\]", re.IGNORECASE)

    def expand_pod_r_inner(self, pod: str, inner: str) -> list[str]:
        nums = _expand_bracket_inner(inner)
        w = _rack_r_slot_pad_width(self.rack_template)
        if w:
            return [f"{pod.lower()}-r{n:0{w}d}" for n in nums]
        return [f"{pod.lower()}-r{n}" for n in nums]

    def expand_site_r_inner(self, site_prefix: str, inner: str) -> list[str]:
        nums = _expand_bracket_inner(inner)
        pod = site_prefix.strip().lower() or "p5"
        w = _rack_r_slot_pad_width(self.rack_template)
        if w:
            return [f"{pod}-r{n:0{w}d}" for n in nums]
        return [f"{pod}-r{n}" for n in nums]


def _expand_bracket_inner(inner: str) -> list[int]:
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


@lru_cache(maxsize=16)
def _naming_cached(rack_raw: str, ct_raw: str, nvsw_raw: str) -> NvisNaming:
    return NvisNaming.from_templates(rack_raw, ct_raw, nvsw_raw)


def clear_nvis_naming_cache() -> None:
    _naming_cached.cache_clear()
    _matcher_cached.cache_clear()


def get_nvis_naming(
    rack_template: str | None = None,
    ct_template: str | None = None,
    nvsw_template: str | None = None,
) -> NvisNaming:
    """Load naming from explicit templates or ``NVIS_*`` env vars."""
    rack = (rack_template if rack_template is not None else os.environ.get("NVIS_RACK_ID") or "").strip()
    ct = (ct_template if ct_template is not None else os.environ.get("NVIS_CT_ID") or "").strip()
    nvsw = (
        nvsw_template if nvsw_template is not None else os.environ.get("NVIS_NVSW_ID") or ""
    ).strip()
    if not rack:
        rack = DEFAULT_NVIS_RACK_ID
    if not ct:
        ct = DEFAULT_NVIS_CT_ID
    return _naming_cached(rack, ct, nvsw)


@lru_cache(maxsize=16)
def _matcher_cached(rack_raw: str, ct_raw: str) -> NamingMatcher:
    return get_nvis_naming(rack_raw, ct_raw).matcher()


def naming_matcher_from_env(
    rack_template: str | None = None,
    ct_template: str | None = None,
) -> NamingMatcher:
    rack = (rack_template or "").strip() or DEFAULT_NVIS_RACK_ID
    ct = (ct_template or "").strip() or DEFAULT_NVIS_CT_ID
    return _matcher_cached(rack, ct)


def rack_id_scan_re(rack_template: str | None = None) -> re.Pattern[str]:
    """Loose regex to find rack ids inside ``cmsh rack; list`` text."""
    rack = (rack_template or "").strip() or DEFAULT_NVIS_RACK_ID
    parsed = parse_template(rack)
    return re.compile(_template_to_regex(parsed, loose=True))


def canonical_rack_id(rid: str, naming: NvisNaming | None = None) -> str | None:
    n = naming or get_nvis_naming()
    return n.canonical_rack_id(rid)
