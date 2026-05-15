#!/usr/bin/env python3
"""Build the single-rack status table without ``rack_status.sh``.

Fast path: ``cmsh -c "rack; list"`` synchronously, render rows with placeholders where needed.
Background threads: ``cmsh -c ds`` (device status), ``sinfo`` (+ ``scontrol`` hostnames), and log-dir
parsing via ``probe_fn`` (typically ``rack_status.sh --probe-json``). Results merge into in-memory
state and the next ``/api/status`` read shows updated cells.

Tray hosts on each ``cmsh -c ds`` line are matched by ``RACK_CMSH_DS_NODE_PATTERN`` (default: one regex
with three capture groups: full hostname, rack id ``pN-rMM``, tray). Rack ids are canonicalized to
``pN-rMM`` two-digit rack suffix so ``p5-r1`` merges with ``p5-r01`` from ``rack; list``.

Optional: ``RACK_CMSH_DS_NODE_TOKEN_PATTERN`` (one capture: hostname token) with automatic ``…-ct…``
split; or legacy ``RACK_CMSH_DS_NODE_FIND_PATTERN`` + ``RACK_CMSH_DS_NODE_PARSE_PATTERN``. Override
via process env or the dashboard job-environment JSON.

Set ``RACK_STATUS_USE_SHELL_TABLE=1`` to use the legacy shell table instead.

Rack ids from ``rack; list`` are canonicalized (``p5-r1`` → ``p5-r01``). By default ``pN-r00`` is
omitted (stray ``pN-r0`` tokens); set ``RACK_CMSH_INCLUDE_R00=1`` if rack 0 exists in your site naming.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import host_exec
from platforms import get_platform as _get_platform

_PKG_DIR = Path(__file__).resolve().parent
_NATIVE_LOG_JSON = _PKG_DIR / "var" / "cache" / "native_log_analysis.json"


def _job_env_overrides_file() -> Path:
    """Path to dashboard job-env JSON; prefer ``NVIS_JOB_ENV_OVERRIDES_FILE`` (set by ``app.py``)."""
    raw = (os.environ.get("NVIS_JOB_ENV_OVERRIDES_FILE") or "").strip()
    if raw:
        return Path(raw).resolve()
    return _PKG_DIR / "var" / "cache" / "job_env_overrides.json"

# cmsh ``ds`` output: default one regex per line via ``finditer``; three capture groups (hostname, rack, tray).
_DEFAULT_CMSH_DS_NODE_PATTERN = r"(?i)((p\d+-r\d+)-ct(\d+))"
_CMSH_DS_ENV_KEYS = frozenset(
    {
        "RACK_CMSH_DS_NODE_PATTERN",
        "RACK_CMSH_DS_NODE_TOKEN_PATTERN",
        "RACK_CMSH_DS_NODE_FIND_PATTERN",
        "RACK_CMSH_DS_NODE_PARSE_PATTERN",
    }
)
_je_file_mtime: float | None = None
_je_subset: dict[str, str] = {}
_ds_line_pat_effective: str | None = None
_ds_line_re_compiled: re.Pattern[str] | None = None


def invalidate_job_env_file_cache() -> None:
    """Call after dashboard writes ``job_env_overrides.json`` so ds regex / env merge reloads."""
    global _je_file_mtime, _je_subset, _ds_line_pat_effective, _ds_line_re_compiled
    _je_file_mtime = None
    _je_subset = {}
    _ds_line_pat_effective = None
    _ds_line_re_compiled = None


def _reload_cmsh_ds_job_env_subset() -> None:
    """Load only ds-related keys from ``var/cache/job_env_overrides.json`` (mtime-based cache)."""
    global _je_file_mtime, _je_subset, _ds_line_pat_effective, _ds_line_re_compiled
    try:
        m = _job_env_overrides_file().stat().st_mtime
    except OSError:
        if _je_file_mtime is not None or _je_subset:
            _je_file_mtime = None
            _je_subset = {}
            _ds_line_pat_effective = None
            _ds_line_re_compiled = None
        return
    if _je_file_mtime == m:
        return
    subset: dict[str, str] = {}
    try:
        with open(_job_env_overrides_file(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k not in _CMSH_DS_ENV_KEYS:
                    continue
                if isinstance(v, str) and v.strip():
                    subset[k] = v.strip()[:8192]
                elif v is not None and str(v).strip():
                    subset[k] = str(v).strip()[:8192]
    except (OSError, json.JSONDecodeError, TypeError, UnicodeDecodeError):
        subset = {}
    _je_subset = subset
    _je_file_mtime = m
    _ds_line_pat_effective = None
    _ds_line_re_compiled = None


def _native_env_ds(key: str, default: str) -> str:
    _reload_cmsh_ds_job_env_subset()
    if key in _je_subset and _je_subset[key].strip():
        return _je_subset[key].strip()
    v = (os.environ.get(key) or "").strip()
    return v if v else default


def cmsh_ds_node_pattern_effective() -> str:
    p = _native_env_ds("RACK_CMSH_DS_NODE_PATTERN", _DEFAULT_CMSH_DS_NODE_PATTERN)
    return p.strip() or _DEFAULT_CMSH_DS_NODE_PATTERN


def _ds_node_line_re() -> re.Pattern[str]:
    global _ds_line_pat_effective, _ds_line_re_compiled
    pat = cmsh_ds_node_pattern_effective()
    if _ds_line_re_compiled is not None and _ds_line_pat_effective == pat:
        return _ds_line_re_compiled
    try:
        _ds_line_re_compiled = re.compile(pat)
        _ds_line_pat_effective = pat
    except re.error:
        _ds_line_re_compiled = re.compile(_DEFAULT_CMSH_DS_NODE_PATTERN)
        _ds_line_pat_effective = _DEFAULT_CMSH_DS_NODE_PATTERN
    return _ds_line_re_compiled


def validate_cmsh_ds_node_pattern(t: str) -> str | None:
    """Return error message or ``None`` if *t* matches ``[UP] p5-r01-ct03`` with groups 1–3 populated."""
    t = t.strip()
    if not t:
        return None
    try:
        r = re.compile(t)
    except re.error as e:
        return f"invalid regex: {e}"
    line = "[UP] p5-r01-ct03"
    ms = list(r.finditer(line))
    if not ms:
        return "must match at least one occurrence on a sample line like '[UP] p5-r01-ct03'"
    m0 = ms[0]
    if len(m0.groups()) < 3:
        return "need three capture groups: (1) full hostname, (2) rack pN-rMM, (3) tray — e.g. (?i)((p\\\\d+-r\\\\d+)-ct(\\\\d+))"
    g1, g2, g3 = m0.group(1), m0.group(2), m0.group(3)
    if not g1 or not g2 or not g3:
        return "groups 1–3 must be non-empty for sample p5-r01-ct03"
    return None


def validate_cmsh_ds_node_token_pattern(t: str) -> str | None:
    """Return error or ``None`` if *t* is a valid token regex with exactly one capture group."""
    t = t.strip()
    if not t:
        return None
    try:
        r = re.compile(t)
    except re.error as e:
        return f"invalid regex: {e}"
    line = "[UP] p5-r01-ct03"
    ms = list(r.finditer(line))
    if not ms:
        return "must match on a sample line like '[UP] p5-r01-ct03'"
    m0 = ms[0]
    if len(m0.groups()) < 1 or m0.group(1) is None:
        return "use exactly one capture group for the full hostname token (e.g. (?i)(p\\\\d+-r\\\\d+-ct\\\\d+))"
    tok = (m0.group(1) or "").strip()
    if _rack_tray_from_hostname_token(tok) is None:
        return "captured token must split into rack and tray at a ``-ct`` boundary (e.g. p5-r01-ct18)"
    return None


def validate_cmsh_ds_find_parse_patterns(find_pat: str, parse_pat: str) -> str | None:
    """Validate legacy two-pattern mode: FIND on a line, PARSE on each token (two capture groups: rack, tray)."""
    fp = find_pat.strip()
    pp = parse_pat.strip()
    if not fp and not pp:
        return None
    if bool(fp) ^ bool(pp):
        return "set both RACK_CMSH_DS_NODE_FIND_PATTERN and RACK_CMSH_DS_NODE_PARSE_PATTERN, or neither"
    try:
        fre = re.compile(fp)
    except re.error as e:
        return f"FIND invalid regex: {e}"
    try:
        pre = re.compile(pp)
    except re.error as e:
        return f"PARSE invalid regex: {e}"
    line = "[UP] x p5-r01-ct03 y"
    toks: list[str] = []
    for fm in fre.finditer(line):
        if fm.lastindex:
            toks.append(fm.group(1))
        else:
            toks.append(fm.group(0))
    if not toks:
        return "FIND must match at least one token on a sample line with p5-r01-ct03"
    pm = pre.match(toks[0].strip())
    if not pm or len(pm.groups()) < 2:
        return "PARSE must match each FIND token with two capture groups: (1) rack pN-rMM, (2) tray"
    return None


def _normalize_rack_key_for_ds(rack_raw: str) -> str:
    """Align ds-derived rack ids with ``rack; list`` (``p5-r1`` → ``p5-r01``)."""
    rr = rack_raw.strip().lower()
    c = canonical_rack_id(rr)
    return c if c else rr


def _rack_tray_from_hostname_token(tok: str) -> tuple[str, str] | None:
    """Split ``…-ct<tray>`` from a token; Bright first, then generic last ``-ct`` split."""
    t = tok.strip().lower()
    m = re.match(r"^(?P<rack>p\d+-r\d+)-ct(?P<tray>.+)$", t)
    if m:
        rack = _normalize_rack_key_for_ds(m.group("rack"))
        tray = m.group("tray").strip()
        if tray:
            return rack, tray
    m2 = re.match(r"^(?P<rack>.+)-ct(?P<tray>.+)$", t)
    if not m2:
        return None
    rack_raw = m2.group("rack").strip().lower()
    tray = m2.group("tray").strip()
    if not tray:
        return None
    rack = _normalize_rack_key_for_ds(rack_raw)
    return rack, tray


def _condense_tray_tokens(nums_s: str) -> str:
    """Build inner of ``-ct[…]``: numeric ranges when all tokens are digits, else sorted unique list."""
    parts = [p for p in nums_s.split() if p.strip()]
    if not parts:
        return ""
    if all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        return condense_ct_nums(nums)
    uniq = sorted(set(parts), key=lambda x: (len(x), x.lower()))
    return ",".join(uniq)


_MULTIRACK_LOG_JSON = _PKG_DIR / "var" / "cache" / "native_multirack_log_analysis.json"

_MULTIRACK_N_RACK_RE = re.compile(r"(?:[2-9]|[1-9]\d{1,3})rack", re.IGNORECASE)
# Word boundaries: avoid matching ``p5-r1`` / ``p5-r0`` inside ``p5-r10`` / ``p5-r01`` when scraping text.
_CMSH_RACK_ID_RE = re.compile(r"\bp\d+-r\d+\b", re.IGNORECASE)
_NEMO_1RACK_RE = re.compile(r"1Rack-(\bp\d+-r\d+\b)-", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    try:
        v = int((os.environ.get(name) or "").strip())
        return v if v >= 0 else default
    except ValueError:
        return default


def _nemo_nccl_bn(name: str) -> bool:
    low = name.lower()
    return "nemo" in low or "nccl" in low


def _is_multirack_bn(name: str) -> bool:
    if not _nemo_nccl_bn(name):
        return False
    if _MULTIRACK_N_RACK_RE.search(name):
        return True
    low = name.lower()
    if (low.startswith("root-nemo-") or low.startswith("root-nccl-")) and re.search(
        r",\s*p\d+-r", low
    ):
        return True
    racks = {m.group(1).lower() for m in re.finditer(r"\b(p\d+-r\d+)\b", name, re.IGNORECASE)}
    return len(racks) >= 2


_NODELIST_LINE_RE = re.compile(r"^\s*NODELIST:\s*(.+)\s*$", re.IGNORECASE | re.MULTILINE)


def extract_physical_rack_ids_from_multirack_log(path: Path) -> list[str]:
    """Physical rack ids involved in a multirack run, read from log content (not the filename).

    If a ``NODELIST:`` line exists near the start of the file, racks are taken only from that line
    (comma-separated Slurm hostlist segments). Otherwise the first *RACK_MULTIRACK_LOG_RACK_SCAN_BYTES*
    of the file are scanned for ``pN-rMM`` tokens (same word-boundary rules as cmsh rack scraping).
    """
    max_b = max(8192, _env_int("RACK_MULTIRACK_LOG_RACK_SCAN_BYTES", 524288))
    try:
        with open(path, "rb") as f:
            chunk = f.read(max_b)
    except OSError:
        return []
    text = chunk.decode("utf-8", errors="replace")
    racks: set[str] = set()
    nodem = _NODELIST_LINE_RE.search(text)
    if nodem:
        rest = nodem.group(1).strip()
        for piece in rest.split(","):
            piece = piece.strip()
            if not piece:
                continue
            for m in _CMSH_RACK_ID_RE.finditer(piece):
                c = canonical_rack_id(m.group(0).lower())
                if c:
                    racks.add(c)
    else:
        for m in _CMSH_RACK_ID_RE.finditer(text):
            c = canonical_rack_id(m.group(0).lower())
            if c:
                racks.add(c)
    out = sorted(racks, key=_rack_sort_key)
    if not _include_cmsh_rack_r00():
        out = [rid for rid in out if not re.fullmatch(r"p\d+-r00$", rid, flags=re.IGNORECASE)]
    return out


def _glob_multirack_logs(log_dir: Path) -> list[Path]:
    """Multirack NEMO/NCCL logs under *log_dir* root and ``failed/`` (basename deduped)."""
    root = log_dir.resolve()
    seen: set[str] = set()
    out: list[Path] = []
    for base in (root, root / "failed"):
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.txt"), key=lambda x: x.name.lower()):
            if not p.is_file() or p.name in seen:
                continue
            if not _nemo_nccl_bn(p.name) or not _is_multirack_bn(p.name):
                continue
            seen.add(p.name)
            out.append(p)
    return out


def _rack_from_1rack_log(name: str) -> str | None:
    m = _NEMO_1RACK_RE.search(name)
    return m.group(1).strip().lower() if m else None


def _jobid_from_log_name(name: str) -> str | None:
    m = re.search(r"N-(\d+)\.txt$", name, re.IGNORECASE)
    return m.group(1) if m else None


def _glob_one_rack_logs(log_dir: Path) -> list[Path]:
    root = log_dir.resolve()
    out: list[Path] = []
    for p in sorted(root.glob("*.txt"), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        if not _nemo_nccl_bn(p.name):
            continue
        if _is_multirack_bn(p.name):
            continue
        out.append(p)
    return out


def _rack_sort_key(rid: str) -> tuple[int, int, str]:
    m = re.match(r"^p(\d+)-r(\d+)$", rid.strip().lower())
    if not m:
        return (10**9, 10**9, rid)
    return (int(m.group(1)), int(m.group(2)), rid)


def _include_cmsh_rack_r00() -> bool:
    """When false (default), drop ``…-r00`` from the cmsh-derived rack list (avoids phantom ``p5-r00`` from stray ``p5-r0`` tokens)."""
    v = (os.environ.get("RACK_CMSH_INCLUDE_R00") or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def canonical_rack_id(rid: str) -> str | None:
    """Normalize ``p5-r1`` / ``p05-r01`` / ``p5-r0`` → ``p5-r01`` / ``p5-r00`` (two-digit rack suffix).

    cmsh output and hostnames often mix ``p5-r1`` with real names ``p5-r01``; deduping by canonical
    form removes phantom single-digit rows when the padded rack is already listed.
    """
    m = re.fullmatch(r"p(\d+)-r(\d+)$", rid.strip().lower())
    if not m:
        return None
    pod = int(m.group(1), 10)
    rack = int(m.group(2), 10)
    return f"p{pod}-r{rack:02d}"


def extract_rack_ids_from_cmsh_text(text: str) -> list[str]:
    """Parse ``pNN-rMM`` rack ids from ``cmsh -c 'rack; list'`` output (or similar).

    Uses word boundaries so hostnames like ``p5-r10-ct2`` yield ``p5-r10`` only, never ``p5-r1``.
    Canonicalizes rack numbers to two digits so ``p5-r1`` merges with ``p5-r01``.
    """
    raw = {m.group(0).lower() for m in _CMSH_RACK_ID_RE.finditer(text or "")}
    canon: set[str] = set()
    for piece in raw:
        c = canonical_rack_id(piece)
        if c:
            canon.add(c)
    out = sorted(canon, key=_rack_sort_key)
    if not _include_cmsh_rack_r00():
        out = [rid for rid in out if not re.fullmatch(r"p\d+-r00$", rid, flags=re.IGNORECASE)]
    return out


def _cmsh_rack_list() -> list[str]:
    plat = _get_platform()
    cmd = plat.list_racks_cmd()
    if not host_exec.is_remote() and not shutil.which(cmd[0]):
        return []
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=max(30, _env_int("RACK_NATIVE_CMSH_TIMEOUT_SEC", 120)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return extract_rack_ids_from_cmsh_text(proc.stdout or "")


def _cmsh_ds_text() -> str:
    plat = _get_platform()
    cmd = plat.device_status_cmd()
    if not host_exec.is_remote() and not shutil.which(cmd[0]):
        return ""
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=max(30, _env_int("RACK_NATIVE_DS_TIMEOUT_SEC", 300)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def condense_ct_nums(nums: list[int]) -> str:
    if not nums:
        return ""
    sorted_u = sorted(set(nums))
    max_n = max(sorted_u)
    w = 2 if max_n < 100 else len(str(max_n))
    out: list[str] = []
    s = e = sorted_u[0]
    for n in sorted_u[1:]:
        if n == e + 1:
            e = n
        else:
            if s == e:
                out.append(f"{s:0{w}d}")
            else:
                out.append(f"{s:0{w}d}-{e:0{w}d}")
            s = e = n
    if s == e:
        out.append(f"{s:0{w}d}")
    else:
        out.append(f"{s:0{w}d}-{e:0{w}d}")
    return ",".join(out)


def parse_cmsh_ds_to_ct_column_by_rack(ds_text: str) -> dict[str, str]:
    """Replicate rack_status.sh ct_dn / INST / UP blob merge into one column string per rack."""
    find_s = _native_env_ds("RACK_CMSH_DS_NODE_FIND_PATTERN", "").strip()
    parse_s = _native_env_ds("RACK_CMSH_DS_NODE_PARSE_PATTERN", "").strip()
    token_s = _native_env_ds("RACK_CMSH_DS_NODE_TOKEN_PATTERN", "").strip()
    mode = "single"
    fre: re.Pattern[str] | None = None
    pre: re.Pattern[str] | None = None
    tre: re.Pattern[str] | None = None
    if find_s and parse_s:
        try:
            fre = re.compile(find_s)
            pre = re.compile(parse_s)
            mode = "find_parse"
        except re.error:
            mode = "single"
            fre = pre = None
    elif token_s:
        try:
            tre = re.compile(token_s)
            mode = "token"
        except re.error:
            tre = None
            mode = "single"

    ct_acc: dict[str, str] = defaultdict(str)  # "rack|ST" -> space-separated tray labels
    ct_seen: set[str] = set()

    def ingest(_host: str, rack_raw: str, tray_tok: str, st: str) -> None:
        rr = _normalize_rack_key_for_ds(rack_raw)
        nn = tray_tok.strip()
        if not nn:
            return
        key = f"{rr}|{st}|{nn}"
        if key in ct_seen:
            return
        ct_seen.add(key)
        acc_key = f"{rr}|{st}"
        ct_acc[acc_key] = (ct_acc[acc_key] + " " + nn).strip()

    for line in ds_text.splitlines():
        if not line.strip():
            continue
        m = re.search(r"\[([^\]]+)\]", line)
        if not m:
            continue
        bracket = re.sub(r"\s+", "", m.group(1)).lower()
        if bracket == "up":
            st = "UP"
        elif bracket in ("down", "unreachable"):
            st = "DN"
        else:
            st = "INST"

        if mode == "find_parse" and fre is not None and pre is not None:
            for fm in fre.finditer(line):
                tok = (fm.group(1) if fm.lastindex else fm.group(0)).strip()
                if not tok:
                    continue
                pm = pre.match(tok)
                if not pm or len(pm.groups()) < 2:
                    continue
                ingest(tok.lower(), pm.group(1), pm.group(2), st)
        elif mode == "token" and tre is not None:
            for tm in tre.finditer(line):
                if len(tm.groups()) < 1 or tm.group(1) is None:
                    continue
                tok = (tm.group(1) or "").strip()
                spl = _rack_tray_from_hostname_token(tok)
                if not spl:
                    continue
                rr_t, nn_t = spl
                ingest(tok.lower(), rr_t, nn_t, st)
        else:
            for hm in _ds_node_line_re().finditer(line):
                # Do not use ``Match.lastindex`` for nested groups: it is the index of the last
                # opened group (often 1) even when ``groups()`` has three entries.
                if len(hm.groups()) < 3:
                    continue
                h = hm.group(1).lower()
                ingest(h, hm.group(2), hm.group(3), st)

    ct_dn: dict[str, str] = {}
    ct_inst: dict[str, str] = {}
    ct_up: dict[str, str] = {}
    for acc_k, nums_s in ct_acc.items():
        rr, stb = acc_k.split("|", 1)
        ranges = _condense_tray_tokens(nums_s)
        if not ranges:
            continue
        blob = f"{rr}-ct[{ranges}]"
        if stb == "DN":
            ct_dn[rr] = blob
        elif stb == "INST":
            ct_inst[rr] = blob
        else:
            ct_up[rr] = blob
    out: dict[str, str] = {}
    racks = set(ct_dn) | set(ct_inst) | set(ct_up)
    for rr in racks:
        parts: list[str] = []
        if rr in ct_dn:
            parts.append(f"DN:{ct_dn[rr]}")
        if rr in ct_inst:
            parts.append(f"INST:{ct_inst[rr]}")
        if rr in ct_up:
            parts.append(f"UP:{ct_up[rr]}")
        out[rr] = ";".join(parts) if parts else "-"
    return out


def sinfo_state_trim(s: str) -> str:
    s = s.lower()
    while s:
        if s[-1] in "*~$#:":
            s = s[:-1]
        else:
            break
    return s


def collect_sinfo_counts() -> dict[tuple[str, str], int]:
    """Map (rack_id, trimmed_state) -> count of nodes (after hostname expansion)."""
    plat = _get_platform()
    if not host_exec.is_remote() and not shutil.which("sinfo"):
        return {}
    opts = (os.environ.get("SINFO_OPTS") or "").strip()
    extra = "-N -h -o '%N %t'"
    if opts:
        extra += f" {opts}"
    try:
        proc = host_exec.run_on_host(
            plat.sinfo_cmd(extra),
            timeout=max(30, _env_int("RACK_NATIVE_SINFO_TIMEOUT_SEC", 120)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    counts: dict[tuple[str, str], int] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        node, st_raw = parts[0], parts[1]
        st = sinfo_state_trim(st_raw)
        if not st:
            continue
        hosts: list[str] = []
        if "[" in node and "]" in node:
            try:
                hp = host_exec.run_on_host(
                    plat.scontrol_hostnames_cmd(node),
                    timeout=max(5, _env_int("RACK_NATIVE_SCONTROL_TIMEOUT_SEC", 60)),
                )
                hosts = [h.strip() for h in (hp.stdout or "").splitlines() if h.strip()]
            except (OSError, subprocess.TimeoutExpired):
                hosts = []
        else:
            hosts = [node]
        for h in hosts:
            hh = h.lower().split(".")[0]
            m = re.match(r"^(p\d+-r\d+)(-ct\d+)?$", hh, re.IGNORECASE)
            if not m:
                continue
            rr = m.group(1).lower()
            key = (rr, st)
            counts[key] = counts.get(key, 0) + 1
    return counts


def shlex_split_safe(s: str) -> list[str]:
    import shlex

    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def format_sinfo_cell(rack: str, counts: dict[tuple[str, str], int]) -> str:
    pairs: list[tuple[int, str]] = []
    for (rr, st), cnt in counts.items():
        if rr != rack:
            continue
        pairs.append((cnt, st))
    if not pairs:
        return "-"
    pairs.sort(key=lambda x: (-x[0], x[1]))
    return ",".join(f"{st}={cnt}" for cnt, st in pairs)


def _slurm_base_job_id(jid_raw: str) -> str | None:
    """Strip Slurm step suffix (``12345.batch`` → ``12345``) for lookup keys."""
    s = (jid_raw or "").strip()
    if not s:
        return None
    base = s.split(".", 1)[0] if "." in s else s
    return base if base.isdigit() else None


def load_squeue_jobs_map() -> tuple[set[str], dict[str, str]]:
    """Return (job_ids, job_id -> %M elapsed string).

    Job ids are normalized to the base numeric id (no ``.batch`` / ``.extern`` suffix) so they
    match ``last_job`` parsed from log filenames.
    """
    user = (os.environ.get("SQUEUE_USER") or os.environ.get("USER") or "root").strip()
    plat = _get_platform()
    try:
        proc = host_exec.run_on_host(
            plat.squeue_cmd(f"-u {shlex.quote(user)} -h -o '%i|%M'"),
            timeout=max(5, _env_int("RACK_NATIVE_SQUEUE_TIMEOUT_SEC", 60)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set(), {}
    ids: set[str] = set()
    times: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        jid_raw, tm = line.split("|", 1)
        base = _slurm_base_job_id(jid_raw)
        if base:
            ids.add(base)
            times[base] = tm.strip() or "?"
    return ids, times


def _env_truthy(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def augment_running_set_for_job_ids(
    running: set[str],
    job_times: dict[str, str],
    want: Iterable[str],
) -> None:
    """For numeric job ids not yet in *running*, query ``squeue -j`` (any owner Slurm allows).

    Multirack / NEMO jobs are often submitted as a different Unix user than the nvis-rack-status process;
    ``squeue -u $USER`` then misses them while ``squeue -j <id>`` still shows RUNNING/PENDING.
    Disable with ``RACK_SQUEUE_JOB_LOOKUP_DISABLED=1``.
    """
    if _env_truthy("RACK_SQUEUE_JOB_LOOKUP_DISABLED"):
        return
    need: list[str] = []
    seen: set[str] = set()
    for j in want:
        if not j or not str(j).strip().isdigit():
            continue
        sj = str(j).strip()
        if sj in running or sj in seen:
            continue
        seen.add(sj)
        need.append(sj)
    if not need:
        return
    plat = _get_platform()
    chunk = 48
    timeout = max(5, _env_int("RACK_NATIVE_SQUEUE_JOB_LOOKUP_TIMEOUT_SEC", 45))
    for i in range(0, len(need), chunk):
        batch = need[i : i + chunk]
        try:
            proc = host_exec.run_on_host(
                plat.squeue_cmd(f"-j {','.join(batch)} -h -o '%i|%M'"),
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            jid_raw, tm = line.split("|", 1)
            base = _slurm_base_job_id(jid_raw)
            if base:
                running.add(base)
                job_times.setdefault(base, tm.strip() or "?")


def _last_job_for_log_row(basename: str, probe: dict[str, Any]) -> str:
    """Slurm job id for a log row: probe JSON first, then ``N-<jid>.txt``-style basename."""
    if probe.get("ok"):
        jraw = probe.get("jid")
        if isinstance(jraw, int):
            s = str(jraw)
            if s.isdigit():
                return s
        s = str(jraw or "").strip()
        if s and s != "?":
            b = _slurm_base_job_id(s)
            if b:
                return b
            if s.isdigit():
                return s
    bn = (basename or "").strip()
    jfb = _jobid_from_log_name(bn) if bn else None
    return jfb if jfb else "-"


def _log_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    return (int(mtime_ns), int(st.st_size))


def _load_disk_log_cache() -> dict[str, Any]:
    if not _NATIVE_LOG_JSON.is_file():
        return {}
    try:
        with open(_NATIVE_LOG_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    ent = raw.get("entries")
    return ent if isinstance(ent, dict) else {}


def _save_disk_log_cache(entries: dict[str, Any]) -> None:
    _NATIVE_LOG_JSON.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n"
    tmp = _NATIVE_LOG_JSON.with_name(_NATIVE_LOG_JSON.name + ".tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(str(tmp), str(_NATIVE_LOG_JSON))


def _load_multirack_disk_cache() -> dict[str, Any]:
    if not _MULTIRACK_LOG_JSON.is_file():
        return {}
    try:
        with open(_MULTIRACK_LOG_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    ent = raw.get("entries")
    return ent if isinstance(ent, dict) else {}


def _save_multirack_disk_cache(entries: dict[str, Any]) -> None:
    _MULTIRACK_LOG_JSON.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n"
    tmp = _MULTIRACK_LOG_JSON.with_name(_MULTIRACK_LOG_JSON.name + ".tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(str(tmp), str(_MULTIRACK_LOG_JSON))


def _multirack_node_count_suffix_from_basename(name: str) -> str:
    """Same node-count suffix as app ``_multirack_node_count_suffix`` (for display only)."""
    m = re.search(r"-(\d+)N-\d", name, re.IGNORECASE)
    if m:
        return f" ({m.group(1)}N)"
    m2 = re.search(r"-(\d+)N\.txt$", name, re.IGNORECASE)
    if m2:
        return f" ({m2.group(1)}N)"
    return ""


def _multirack_rack_display_label(
    basename: str,
    rack_ids: list[str],
    rack_label_fn: Callable[[str], str],
) -> str:
    if rack_ids:
        return "+".join(rack_ids) + _multirack_node_count_suffix_from_basename(basename)
    return rack_label_fn(basename)


def _normalize_physical_racks_probe(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        if x is None:
            continue
        s = str(x).strip().lower()
        if not s:
            continue
        c = canonical_rack_id(s) or s
        if c not in seen:
            seen.add(c)
            out.append(c)
    out.sort(key=_rack_sort_key)
    return out


def format_multirack_sinfo_cell(
    rack_ids: list[str],
    sct: dict[tuple[str, str], int],
    sinfo_done: bool,
) -> str:
    """One cell: Slurm states per physical rack (ids already resolved, e.g. from log scan)."""
    if not sinfo_done:
        return "?"
    ids = rack_ids
    if not ids:
        return "-"
    parts: list[str] = []
    for rid in ids:
        cell = format_sinfo_cell(rid, sct)
        parts.append(f"{rid} {cell}")
    return "; ".join(parts)


_states_lock = threading.Lock()
_states: dict[str, NativeRackTableState] = {}


class NativeRackTableState:
    __slots__ = (
        "key",
        "log_dir",
        "lock",
        "racks",
        "ct_by_rack",
        "sinfo_counts",
        "per_path_probe",
        "by_rack_log",
        "ds_thread",
        "sinfo_thread",
        "log_thread",
        "ds_done",
        "sinfo_done",
        "log_done",
        "last_errors",
    )

    def __init__(self, key: str, log_dir: Path) -> None:
        self.key = key
        self.log_dir = log_dir.resolve()
        self.lock = threading.RLock()
        self.racks: list[str] = []
        self.ct_by_rack: dict[str, str] = {}
        self.sinfo_counts: dict[tuple[str, str], int] = {}
        self.per_path_probe: dict[str, dict[str, Any]] = {}
        self.by_rack_log: dict[str, dict[str, Any]] = {}
        self.ds_thread: threading.Thread | None = None
        self.sinfo_thread: threading.Thread | None = None
        self.log_thread: threading.Thread | None = None
        self.ds_done = False
        self.sinfo_done = False
        self.log_done = False
        self.last_errors: list[str] = []

    def refresh_racks_blocking(self) -> None:
        racks = _cmsh_rack_list()
        with self.lock:
            self.racks = racks

    def _worker_ds(self) -> None:
        try:
            text = _cmsh_ds_text()
            col = parse_cmsh_ds_to_ct_column_by_rack(text)
            with self.lock:
                self.ct_by_rack = col
                self.ds_done = True
        except Exception as e:
            with self.lock:
                self.last_errors.append(f"ds: {e}")
                self.ds_done = True

    def _worker_sinfo(self) -> None:
        try:
            c = collect_sinfo_counts()
            with self.lock:
                self.sinfo_counts = c
                self.sinfo_done = True
        except Exception as e:
            with self.lock:
                self.last_errors.append(f"sinfo: {e}")
                self.sinfo_done = True

    def _worker_logs(self, probe_fn: Callable[[Path], dict[str, object] | None]) -> None:
        try:
            disk = _load_disk_log_cache()
            paths = _glob_one_rack_logs(self.log_dir)
            alive: set[str] = set()
            dirty = False
            for p in paths:
                keyp = str(p.resolve())
                alive.add(keyp)
                fp = _log_fingerprint(p)
                if not fp:
                    continue
                mtime_ns, size = fp
                hit = disk.get(keyp) if isinstance(disk.get(keyp), dict) else None
                rec: dict[str, Any] | None = None
                if isinstance(hit, dict):
                    try:
                        if int(hit.get("mtime_ns", -1)) == mtime_ns and int(hit.get("size", -1)) == size:
                            rec = hit.get("probe")
                            if isinstance(rec, dict):
                                rec = dict(rec)
                    except (TypeError, ValueError):
                        rec = None
                if rec is None:
                    pr = probe_fn(p) or {}
                    rec = {k: pr.get(k) for k in ("ok", "jid", "ongoing", "nccl_ok", "llm_ok", "nemo_state", "squeue_time")}
                    disk[keyp] = {"mtime_ns": mtime_ns, "size": size, "probe": rec}
                    dirty = True
                rec_d = rec if isinstance(rec, dict) else {}
                rk = _rack_from_1rack_log(p.name)
                try:
                    m_this = p.stat().st_mtime
                except OSError:
                    m_this = 0.0
                with self.lock:
                    self.per_path_probe[keyp] = rec_d
                    if rk:
                        prev = self.by_rack_log.get(rk)
                        replace = prev is None
                        if not replace and isinstance(prev, dict):
                            pp = prev.get("_path")
                            if isinstance(pp, str) and pp:
                                try:
                                    replace = m_this >= Path(pp).stat().st_mtime
                                except OSError:
                                    replace = True
                            else:
                                replace = True
                        if replace:
                            self.by_rack_log[rk] = {**rec_d, "_path": str(p), "_mtime": m_this}
            pruned = {k: v for k, v in disk.items() if k in alive}
            if len(pruned) != len(disk):
                dirty = True
            disk = pruned
            if dirty:
                _save_disk_log_cache(disk)
        except Exception as e:
            with self.lock:
                self.last_errors.append(f"logs: {e}")
        finally:
            with self.lock:
                self.log_done = True

    def schedule_async_workers(self, probe_fn: Callable[[Path], dict[str, object] | None]) -> None:
        with self.lock:
            if self.ds_thread is None or not self.ds_thread.is_alive():
                self.ds_thread = threading.Thread(target=self._worker_ds, name=f"native-ds-{self.key}", daemon=True)
                self.ds_thread.start()
            if self.sinfo_thread is None or not self.sinfo_thread.is_alive():
                self.sinfo_thread = threading.Thread(
                    target=self._worker_sinfo, name=f"native-sinfo-{self.key}", daemon=True
                )
                self.sinfo_thread.start()
            if self.log_thread is None or not self.log_thread.is_alive():
                self.log_thread = threading.Thread(
                    target=self._worker_logs,
                    name=f"native-logs-{self.key}",
                    daemon=True,
                    args=(probe_fn,),
                )
                self.log_thread.start()

    def build_table_rows(self) -> tuple[list[dict[str, str]], dict[str, int], list[str], str]:
        running, job_times = load_squeue_jobs_map()
        with self.lock:
            racks = list(self.racks)
            ct = dict(self.ct_by_rack)
            sct = dict(self.sinfo_counts)
            by_rack = {k: dict(v) for k, v in self.by_rack_log.items()}
            errs = list(self.last_errors)
            ds_done = self.ds_done
            sinfo_done = self.sinfo_done
        rows: list[dict[str, str]] = []
        n_done = n_fail = n_on = n_pending = 0
        want: set[str] = set()
        for rack in sorted(racks, key=_rack_sort_key):
            pr_full = by_rack.get(rack)
            if not pr_full:
                continue
            pr0 = {k: v for k, v in pr_full.items() if k not in ("_path", "_mtime")}
            path0 = pr_full.get("_path") or ""
            path0_s = str(path0) if path0 else ""
            bn0 = Path(path0_s).name if path0_s else ""
            lj0 = _last_job_for_log_row(bn0, pr0)
            if lj0 != "-" and lj0.isdigit():
                want.add(lj0)
        augment_running_set_for_job_ids(running, job_times, want)
        for rack in sorted(racks, key=_rack_sort_key):
            pr_full = by_rack.get(rack)
            if not pr_full:
                n_pending += 1
                rows.append(
                    {
                        "rack": rack,
                        "nemo": "pending",
                        "last_job": "-",
                        "sinfo": "?" if not sinfo_done else format_sinfo_cell(rack, sct),
                        "ct_down_unk": "?" if not ds_done else ct.get(rack, "-"),
                    }
                )
                continue

            pr = {k: v for k, v in pr_full.items() if k not in ("_path", "_mtime")}
            path = pr_full.get("_path") or ""
            path_s = str(path) if path else ""
            bn = Path(path_s).name if path_s else ""
            jid_s = _last_job_for_log_row(bn, pr)
            in_slurm = bool(jid_s != "-" and jid_s in running)

            if in_slurm:
                tm = job_times.get(jid_s, "?")
                nemo = f"ongoing [{tm}]"
                n_on += 1
            else:
                st = str(pr.get("nemo_state") or "fail").strip().lower()
                if st == "ongoing":
                    # Disk probe cache is keyed by log mtime/size only. After the Slurm job ends,
                    # we can still serve a stale probe that says "ongoing". If Slurm does not list
                    # the job, re-classify like rack_status.sh once the run is no longer queued.
                    nccl_ok = bool(pr.get("nccl_ok"))
                    llm_ok = bool(pr.get("llm_ok"))
                    if nccl_ok and llm_ok:
                        nemo = "done"
                        n_done += 1
                    else:
                        nemo = "fail"
                        n_fail += 1
                elif st == "done":
                    nemo = "done"
                    n_done += 1
                elif st == "fail":
                    nemo = "fail"
                    n_fail += 1
                else:
                    nemo = "fail"
                    n_fail += 1

            siv = "?" if not sinfo_done else format_sinfo_cell(rack, sct)
            if not ds_done:
                ctv = "?"
            else:
                ctv = ct.get(rack, "-")

            rows.append(
                {
                    "rack": rack,
                    "nemo": nemo,
                    "last_job": jid_s,
                    "sinfo": siv,
                    "ct_down_unk": ctv,
                }
            )
        summary = {
            "done": n_done,
            "fail": n_fail,
            "ongoing": n_on,
            "pending": n_pending,
        }
        legend = "rack; list (cmsh) — native table; async: ds, sinfo, log probes"
        footnotes = errs[:12]
        return rows, summary, footnotes, legend


def _worker_snapshot(st: NativeRackTableState) -> dict[str, Any]:
    """Serializable thread status for /api/status (single log_dir native state)."""

    def _alive(t: threading.Thread | None) -> bool:
        return t is not None and t.is_alive()

    with st.lock:
        return {
            "mode": "native",
            "rack_count": len(st.racks),
            "workers": [
                {
                    "id": "ds",
                    "label": "cmsh device status (ds)",
                    "thread_alive": _alive(st.ds_thread),
                    "finished_once": st.ds_done,
                },
                {
                    "id": "sinfo",
                    "label": "Slurm sinfo / scontrol hostnames",
                    "thread_alive": _alive(st.sinfo_thread),
                    "finished_once": st.sinfo_done,
                },
                {
                    "id": "logs",
                    "label": "Log analysis (--probe-json)",
                    "thread_alive": _alive(st.log_thread),
                    "finished_once": st.log_done,
                },
            ],
            "recent_errors": list(st.last_errors[-8:]),
        }


def worker_snapshot_for_log_dir(log_dir: Path) -> dict[str, Any] | None:
    """Return ``_worker_snapshot`` for *log_dir* if native state exists, else ``None``."""
    key = str(log_dir.resolve())
    with _states_lock:
        st = _states.get(key)
    if st is None:
        return None
    return _worker_snapshot(st)


def live_table_snapshot_for_log_dir(log_dir: Path) -> dict[str, Any] | None:
    """Rebuild rack rows/summary from in-memory async workers (sinfo, ds, log probes).

    Cached ``/api/status`` payloads can otherwise keep placeholder ``?`` cells from the first
    snapshot taken before ``ds_done`` / ``sinfo_done`` flipped true.
    """
    key = str(log_dir.resolve())
    with _states_lock:
        st = _states.get(key)
    if st is None:
        return None
    rows, summary, footnotes, legend = st.build_table_rows()
    return {
        "rows": rows,
        "summary": summary,
        "footnotes": footnotes,
        "legend": legend,
    }


def _get_state(key: str, log_dir: Path) -> NativeRackTableState:
    with _states_lock:
        st = _states.get(key)
        if st is None:
            st = NativeRackTableState(key, log_dir)
            _states[key] = st
        return st


def build_native_rack_status(
    log_dir: Path,
    include_raw: bool,
    probe_fn: Callable[[Path], dict[str, object] | None],
) -> dict[str, Any]:
    key = str(log_dir.resolve())
    st = _get_state(key, log_dir)
    st.refresh_racks_blocking()
    st.schedule_async_workers(probe_fn)
    rows, summary, footnotes, legend = st.build_table_rows()
    out: dict[str, Any] = {
        "ok": True,
        "returncode": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "log_dir": str(log_dir.resolve()),
        "script": "native_status.py",
        "stderr": "\n".join(footnotes) if footnotes else "",
        "legend": legend,
        "rows": rows,
        "summary": summary,
        "footnotes": footnotes,
    }
    if include_raw:
        out["stdout"] = json.dumps({"racks": rows, "summary": summary}, indent=2)
    out["native_workers"] = _worker_snapshot(st)
    return out


# --- multirack tab (async: sinfo/scontrol + log probes only; no cmsh ds) ---

_multirack_states_lock = threading.Lock()
_multirack_states: dict[str, MultirackTableState] = {}


class MultirackTableState:
    __slots__ = (
        "key",
        "log_dir",
        "lock",
        "log_paths",
        "per_path_probe",
        "sinfo_counts",
        "sinfo_thread",
        "log_thread",
        "sinfo_done",
        "log_done",
        "last_errors",
    )

    def __init__(self, key: str, log_dir: Path) -> None:
        self.key = key
        self.log_dir = log_dir.resolve()
        self.lock = threading.RLock()
        self.log_paths: list[Path] = []
        self.per_path_probe: dict[str, dict[str, Any]] = {}
        self.sinfo_counts: dict[tuple[str, str], int] = {}
        self.sinfo_thread: threading.Thread | None = None
        self.log_thread: threading.Thread | None = None
        self.sinfo_done = False
        self.log_done = False
        self.last_errors: list[str] = []

    def refresh_paths_blocking(self) -> None:
        paths = _glob_multirack_logs(self.log_dir)
        with self.lock:
            self.log_paths = paths

    def _worker_sinfo_m(self) -> None:
        try:
            c = collect_sinfo_counts()
            with self.lock:
                self.sinfo_counts = c
                self.sinfo_done = True
        except Exception as e:
            with self.lock:
                self.last_errors.append(f"sinfo: {e}")
                self.sinfo_done = True

    def _worker_logs_m(
        self,
        probe_fn: Callable[[Path], dict[str, object] | None],
        paths: list[Path],
    ) -> None:
        try:
            disk = _load_multirack_disk_cache()
            alive: set[str] = set()
            dirty = False
            for p in paths:
                keyp = str(p.resolve())
                alive.add(keyp)
                fp = _log_fingerprint(p)
                if not fp:
                    continue
                mtime_ns, size = fp
                hit = disk.get(keyp) if isinstance(disk.get(keyp), dict) else None
                rec: dict[str, Any] | None = None
                if isinstance(hit, dict):
                    try:
                        if int(hit.get("mtime_ns", -1)) == mtime_ns and int(hit.get("size", -1)) == size:
                            rec = hit.get("probe")
                            if isinstance(rec, dict):
                                rec = dict(rec)
                    except (TypeError, ValueError):
                        rec = None
                if rec is None:
                    pr = probe_fn(p) or {}
                    rec = {k: pr.get(k) for k in ("ok", "jid", "ongoing", "nccl_ok", "llm_ok", "nemo_state", "squeue_time")}
                    need_save = True
                else:
                    rec = dict(rec)
                    need_save = False
                racks_norm = _normalize_physical_racks_probe(rec.get("physical_racks"))
                if not racks_norm:
                    racks_norm = extract_physical_rack_ids_from_multirack_log(p)
                prev_r = rec.get("physical_racks")
                if racks_norm and prev_r != racks_norm:
                    rec["physical_racks"] = racks_norm
                    need_save = True
                if need_save:
                    disk[keyp] = {"mtime_ns": mtime_ns, "size": size, "probe": rec}
                    dirty = True
                rec_d = rec if isinstance(rec, dict) else {}
                with self.lock:
                    self.per_path_probe[keyp] = rec_d
            pruned = {k: v for k, v in disk.items() if k in alive}
            if len(pruned) != len(disk):
                dirty = True
            disk = pruned
            if dirty:
                _save_multirack_disk_cache(disk)
        except Exception as e:
            with self.lock:
                self.last_errors.append(f"multirack logs: {e}")
        finally:
            with self.lock:
                self.log_done = True

    def schedule_workers(self, probe_fn: Callable[[Path], dict[str, object] | None]) -> None:
        with self.lock:
            if self.sinfo_thread is None or not self.sinfo_thread.is_alive():
                self.sinfo_thread = threading.Thread(
                    target=self._worker_sinfo_m,
                    name=f"multirack-sinfo-{self.key}",
                    daemon=True,
                )
                self.sinfo_thread.start()
            if self.log_thread is None or not self.log_thread.is_alive():
                paths_copy = list(self.log_paths)
                self.log_thread = threading.Thread(
                    target=self._worker_logs_m,
                    name=f"multirack-logs-{self.key}",
                    daemon=True,
                    args=(probe_fn, paths_copy),
                )
                self.log_thread.start()

    def build_rows(
        self,
        rack_label_fn: Callable[[str], str],
        rack_ids_fn: Callable[[str], list[str]],
    ) -> list[dict[str, str]]:
        running, job_times = load_squeue_jobs_map()
        with self.lock:
            paths = list(self.log_paths)
            sct = dict(self.sinfo_counts)
            probe_map = {k: dict(v) for k, v in self.per_path_probe.items()}
            sinfo_done = self.sinfo_done
        want: set[str] = set()
        for p in paths:
            keyp = str(p.resolve())
            probe = probe_map.get(keyp, {})
            lj = _last_job_for_log_row(p.name, probe)
            if lj != "-" and lj.isdigit():
                want.add(lj)
        augment_running_set_for_job_ids(running, job_times, want)
        rows: list[dict[str, str]] = []
        for p in paths:
            bn = p.name
            keyp = str(p.resolve())
            probe = probe_map.get(keyp, {})
            last_job = _last_job_for_log_row(bn, probe)
            in_slurm = bool(last_job != "-" and last_job in running)
            if in_slurm:
                nemo = f"ongoing [{job_times.get(last_job, '?')}]"
            elif not probe:
                nemo = "pending"
            elif probe.get("ok") and probe.get("ongoing"):
                # Same stale-cache case as single-rack: ongoing flag can outlive the Slurm job.
                if bool(probe.get("nccl_ok")) and bool(probe.get("llm_ok")):
                    nemo = "done"
                else:
                    nemo = "fail"
            elif probe.get("ok") and str(probe.get("nemo_state") or "") == "done":
                nemo = "done"
            else:
                nemo = "fail"
            racks_from_log = _normalize_physical_racks_probe(probe.get("physical_racks"))
            rack_ids_sinfo = racks_from_log if racks_from_log else list(rack_ids_fn(bn))
            rack_disp = _multirack_rack_display_label(bn, racks_from_log, rack_label_fn)
            siv = format_multirack_sinfo_cell(rack_ids_sinfo, sct, sinfo_done)
            rows.append(
                {
                    "rack_display": rack_disp,
                    "log_file": bn,
                    "nemo": nemo,
                    "last_job": last_job,
                    "sinfo": siv,
                }
            )
        return rows


def _multirack_worker_snapshot(st: MultirackTableState) -> dict[str, Any]:
    def _alive(t: threading.Thread | None) -> bool:
        return t is not None and t.is_alive()

    with st.lock:
        return {
            "mode": "native_multirack",
            "log_file_count": len(st.log_paths),
            "workers": [
                {
                    "id": "sinfo",
                    "label": "Slurm sinfo / scontrol hostnames",
                    "thread_alive": _alive(st.sinfo_thread),
                    "finished_once": st.sinfo_done,
                },
                {
                    "id": "logs",
                    "label": "Multirack log analysis (--probe-json)",
                    "thread_alive": _alive(st.log_thread),
                    "finished_once": st.log_done,
                },
            ],
            "recent_errors": list(st.last_errors[-8:]),
        }


def _get_multirack_state(key: str, log_dir: Path) -> MultirackTableState:
    with _multirack_states_lock:
        st = _multirack_states.get(key)
        if st is None:
            st = MultirackTableState(key, log_dir)
            _multirack_states[key] = st
        return st


def worker_snapshot_for_multirack_log_dir(log_dir: Path) -> dict[str, Any] | None:
    key = str(log_dir.resolve())
    with _multirack_states_lock:
        st = _multirack_states.get(key)
    if st is None:
        return None
    return _multirack_worker_snapshot(st)


def build_multirack_status(
    log_dir: Path,
    probe_fn: Callable[[Path], dict[str, object] | None],
    rack_label_fn: Callable[[str], str],
    rack_ids_fn: Callable[[str], list[str]],
) -> dict[str, Any]:
    """Multirack table: glob log list synchronously; fill validation + sinfo via two background threads."""
    key = str(log_dir.resolve())
    st = _get_multirack_state(key, log_dir)
    st.refresh_paths_blocking()
    st.schedule_workers(probe_fn)
    rows = st.build_rows(rack_label_fn, rack_ids_fn)
    return {
        "ok": True,
        "log_dir": str(log_dir.resolve()),
        "rows": rows,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "native_workers": _multirack_worker_snapshot(st),
    }
