#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Build the single-rack status table without ``rack_status.sh``.

Fast path: cache ``cmsh -c "rack; list"`` (15-minute default TTL), render rows with placeholders where needed.
Background threads: ``cmsh -c ds`` (device status), ``sinfo`` (+ ``scontrol`` hostnames), and log-dir
parsing via ``probe_fn`` (typically ``rack_status.sh --probe-json``). Results merge into in-memory
state and the next ``/api/status`` read shows updated cells.

Tray hosts on each ``cmsh -c ds`` line are matched using **Settings → naming templates**
(``NVIS_CT_ID``, ``NVIS_RACK_ID``): literals plus ``[\\d+]`` (default) or ``[01-99]`` digit slots.
Bright default: ``p[\\d+]-r[\\d+]-ct[\\d+]``. CBR default:
``CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99][01-99]``. Legacy raw-regex keys are still honored when set.

Set ``RACK_STATUS_USE_SHELL_TABLE=1`` to use the legacy shell table instead.

Rack ids from ``rack; list`` are canonicalized (``p5-r1`` → ``p5-r01``). By default ``pN-r00`` is
omitted (stray ``pN-r0`` tokens); set ``RACK_CMSH_INCLUDE_R00=1`` if rack 0 exists in your site naming.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import host_exec
import nvis_id_templates as _nvis_ids
import requests
import results_history
import scheduler_runtime
from platforms import get_platform as _get_platform

_logger = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
# Runtime cache lives under repo-root var/ (parent of backend/).
_REPO_ROOT = _PKG_DIR.parent
_NATIVE_LOG_JSON = _REPO_ROOT / "var" / "cache" / "native_log_analysis.json"

# Bump when the per-log probe/parse LOGIC changes (e.g. nccl_parse.inc.sh dialect handling) so stale
# entries keyed only on log mtime/size are discarded on load instead of serving an outdated PASS/FAIL.
#
# Deliberately shared by BOTH the single-rack and multirack caches below: they are produced by the
# same probe path, so a logic change invalidates both. Giving them separate counters would let one
# keep serving verdicts from superseded parse logic.
_NATIVE_LOG_CACHE_VERSION = 2


def _job_env_overrides_file() -> Path:
    """Path to dashboard job-env JSON; prefer ``NVIS_JOB_ENV_OVERRIDES_FILE`` (set by ``app.py``)."""
    raw = (os.environ.get("NVIS_JOB_ENV_OVERRIDES_FILE") or "").strip()
    if raw:
        return Path(raw).resolve()
    return _REPO_ROOT / "var" / "cache" / "job_env_overrides.json"

# cmsh ``ds`` output: default one regex per line via ``finditer``; three capture groups (hostname, rack, tray).
_DEFAULT_CMSH_DS_NODE_PATTERN = r"(?i)((p\d+-r\d+)-ct(\d+))"
_NVIS_NAMING_ENV_KEYS = frozenset(
    {
        "NVIS_RACK_ID",
        "NVIS_CT_ID",
        "NVIS_NVSW_ID",
    }
)
_LEGACY_CMSH_DS_ENV_KEYS = frozenset(
    {
        "RACK_CMSH_DS_NODE_PATTERN",
        "RACK_CMSH_DS_NODE_TOKEN_PATTERN",
        "RACK_CMSH_DS_NODE_FIND_PATTERN",
        "RACK_CMSH_DS_NODE_PARSE_PATTERN",
    }
)
_CMSH_DS_ENV_KEYS = _NVIS_NAMING_ENV_KEYS | _LEGACY_CMSH_DS_ENV_KEYS
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
    _nvis_ids.clear_nvis_naming_cache()


def _nvis_naming() -> _nvis_ids.NvisNaming:
    return _nvis_ids.get_nvis_naming(
        _nvis_rack_id_template(),
        _nvis_ct_id_template(),
        _nvis_nvsw_id_template(),
    )


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


def _nvis_rack_id_template() -> str:
    return _native_env_ds("NVIS_RACK_ID", _nvis_ids.DEFAULT_NVIS_RACK_ID)


def _nvis_ct_id_template() -> str:
    return _native_env_ds("NVIS_CT_ID", _nvis_ids.DEFAULT_NVIS_CT_ID)


def _nvis_nvsw_id_template() -> str:
    return _native_env_ds("NVIS_NVSW_ID", _nvis_ids.DEFAULT_NVIS_NVSW_ID)


def _legacy_ds_configured() -> bool:
    for k in _LEGACY_CMSH_DS_ENV_KEYS:
        if k == "RACK_CMSH_DS_NODE_PATTERN":
            v = _native_env_ds(k, _DEFAULT_CMSH_DS_NODE_PATTERN).strip()
            if v and v != _DEFAULT_CMSH_DS_NODE_PATTERN:
                return True
        elif _native_env_ds(k, "").strip():
            return True
    return False


def validate_nvis_naming_settings(
    rack_t: str, ct_t: str, nvsw_t: str = ""
) -> str | None:
    """Syntax validation for NVIS_* templates (no hardcoded sample hostnames)."""
    return _nvis_ids.validate_nvis_naming_triplet(rack_t, ct_t, nvsw_t)


def validate_cmsh_ds_node_pattern(t: str) -> str | None:
    """Legacy regex mode (deprecated in Settings UI)."""
    t = t.strip()
    if not t:
        return None
    try:
        re.compile(t)
    except re.error as e:
        return f"invalid regex: {e}"
    return None


def validate_cmsh_ds_node_token_pattern(t: str) -> str | None:
    t = t.strip()
    if not t:
        return None
    try:
        re.compile(t)
    except re.error as e:
        return f"invalid regex: {e}"
    return None


def validate_cmsh_ds_find_parse_patterns(find_pat: str, parse_pat: str) -> str | None:
    fp = find_pat.strip()
    pp = parse_pat.strip()
    if not fp and not pp:
        return None
    if bool(fp) ^ bool(pp):
        return "set both FIND and PARSE, or neither"
    try:
        re.compile(fp)
        re.compile(pp)
    except re.error as e:
        return f"invalid regex: {e}"
    return None


_RACK_ALIAS_CACHE: list | None = None


def _rack_alias_rules() -> list:
    """Optional hostname-rack -> BCM-rack rewrites, for sites where the BCM rack
    object id is NOT a prefix of the tray hostname.

    NscaleTX: BCM rack objects are ``WH1H1``..``WH1H22`` while trays are
    ``ink50-dh01-h01-11``. Without a rewrite the ds parse can never attach trays to
    racks and every rack row renders "-".

    Configure with RACK_ID_ALIAS_RULES = "<regex>=><replacement>[;<regex>=><replacement>]"
    (Python re.sub syntax, matched case-insensitively against the lowercased
    hostname-derived rack id). Unset = no rewriting, original behaviour.
    """
    global _RACK_ALIAS_CACHE
    if _RACK_ALIAS_CACHE is None:
        rules = []
        raw = (os.environ.get("RACK_ID_ALIAS_RULES") or "").strip()
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=>" not in part:
                continue
            pat, repl = part.split("=>", 1)
            try:
                rules.append((re.compile(pat.strip(), re.IGNORECASE), repl.strip()))
            except re.error:
                continue
        _RACK_ALIAS_CACHE = rules
    return _RACK_ALIAS_CACHE


def _normalize_rack_key_for_ds(rack_raw: str) -> str:
    """Align ds-derived rack ids with ``rack; list`` (``p5-r1`` → ``p5-r01``)."""
    rr = rack_raw.strip().lower()
    for pat, repl in _rack_alias_rules():
        if pat.match(rr):
            rr = pat.sub(repl, rr)
            break
    c = canonical_rack_id(rr)
    return c if c else rr


def _rack_tray_from_hostname_token(tok: str) -> tuple[str, str] | None:
    """Split rack + tray from a hostname token (NVIS templates or legacy ``-ct`` suffix)."""
    t = tok.strip().lower()
    if not _legacy_ds_configured():
        hm = _nvis_naming().matcher().match_hostname(t)
        if hm:
            return _normalize_rack_key_for_ds(hm.rack_id), hm.tray
    m2 = re.match(r"^(?P<rack>.+)-ct(?P<tray>.+)$", t)
    if not m2:
        return None
    tray = m2.group("tray").strip()
    if not tray:
        return None
    rack = _normalize_rack_key_for_ds(m2.group("rack").strip().lower())
    return rack, tray


def _condense_tray_tokens(nums_s: str) -> str:
    """Build inner of ``-ct[…]``: numeric ranges when all tokens are digits, else sorted unique list."""
    parts = [p for p in nums_s.split() if p.strip()]
    if not parts:
        return ""
    if all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        if not _legacy_ds_configured():
            return _nvis_naming().condense_tray_range_inner(nums)
        return condense_ct_nums(nums)
    uniq = sorted(set(parts), key=lambda x: (len(x), x.lower()))
    return ",".join(uniq)


_MULTIRACK_LOG_JSON = _REPO_ROOT / "var" / "cache" / "native_multirack_log_analysis.json"

def _env_int(name: str, default: int) -> int:
    try:
        v = int((os.environ.get(name) or "").strip())
        return v if v >= 0 else default
    except ValueError:
        return default


# Log-basename classification lives in nvis_id_templates so this module, app.py and the shell
# probes cannot drift apart on which table a log belongs to. These wrappers exist only to bind
# this module's NvisNaming (built from its own job-env-override subset, not the process env);
# keep them free of logic.


def _nemo_nccl_bn(name: str) -> bool:
    return _nvis_ids.nemo_nccl_in_log_basename(name, _nvis_naming())


def _nemo_single_rack_bn(name: str) -> bool:
    return _nvis_ids.nemo_single_rack_log_basename(name, _nvis_naming())


def _stage_kind_of_log_bn(name: str) -> str | None:
    return _nvis_ids.stage_kind_of_log_basename(name)


def _is_multirack_bn(name: str) -> bool:
    return _nvis_ids.is_multirack_log_basename(name, _nvis_naming())


_NODELIST_LINE_RE = re.compile(r"^\s*NODELIST:\s*(.+)\s*$", re.IGNORECASE | re.MULTILINE)


def extract_physical_rack_ids_from_multirack_log(path: Path) -> list[str]:
    """Physical rack ids involved in a multirack run, read from log content (not the filename).

    If a ``NODELIST:`` line exists near the start of the file, racks are taken only from that line
    (comma-separated Slurm hostlist segments). Otherwise the first *RACK_MULTIRACK_LOG_RACK_SCAN_BYTES*
    of the file are scanned for ``pN-rMM`` tokens (same word-boundary rules as cmsh rack scraping).
    """
    max_b = max(8192, _env_int("RACK_MULTIRACK_LOG_RACK_SCAN_BYTES", 524288))
    # Read through the connection: the log lives on the execution host, which for a
    # containerised deployment is not this filesystem. Only the head of the file is
    # needed, so the tail cap is zero.
    text, meta = host_exec.read_text(path, head_bytes=max_b, tail_bytes=0, whole_bytes=max_b)
    if meta.get("read_error"):
        return []
    naming = _nvis_naming()
    racks: set[str] = set()
    nodem = _NODELIST_LINE_RE.search(text)
    if nodem:
        rest = nodem.group(1).strip()
        for piece in rest.split(","):
            piece = piece.strip()
            if not piece:
                continue
            racks.update(naming.find_rack_ids_in_string(piece))
    else:
        racks.update(naming.find_rack_ids_in_string(text))
    out = sorted(racks, key=naming.rack_sort_key)
    if not _include_cmsh_rack_r00():
        out = [rid for rid in out if not naming.is_r00_rack(rid)]
    return out


def _host_resolve(p: Path) -> PurePosixPath:
    """Canonicalise *p* against the execution host.

    Delegates to the active connection, so the path is resolved wherever it
    actually lives rather than against this process's filesystem (which, for a
    containerised app driving a head node, is the wrong machine entirely).
    """
    return _host_path(host_exec.resolve(p))


def _host_path(path: str) -> PurePosixPath:
    """Wrap an execution-host path string.

    Always a :class:`PurePosixPath`: the file belongs to the exec host, so local
    filesystem operations on it are a category error. Ask the connection instead
    (``host_exec.is_file`` / ``stat`` / ``read_text``), which reaches the right
    host either way.
    """
    return PurePosixPath(str(path))


def _host_log_entries(base: str) -> list[tuple[str, int, int]]:
    """List ``*.txt``/``*.out`` files (non-recursive) in *base* on the EXEC host as ``(path, mtime_ns, size)``.

    Routes through ``host_exec`` so log discovery + stat honor ``RACK_EXEC_MODE`` exactly like
    script execution: SSH to the target when remote, local filesystem when on-node. One ``find``
    call per dir (GNU ``-printf``) avoids a stat round-trip per file over SSH.

    ``.out`` is included so multi-rack NCCL launchers (``run_nccl_mpi.sh`` → ``nccl-mpi_*.out`` /
    ``llmb-nccl_*.out``) are discoverable; callers filter by basename (``_nemo_nccl_bn`` /
    ``_is_multirack_bn`` / single-rack ``nemo``/``1rack``), so single-rack views are unaffected.

    One batched directory listing via the connection, so a remote target costs a
    single round trip rather than a stat per file.
    """
    entries = host_exec.stat_dir(base, patterns=("*.txt", "*.out"))
    return [
        (path, int(st.mtime * 1_000_000_000), int(st.size))
        for path, st in entries.items()
    ]


def _glob_multirack_logs(log_dir: Path) -> list[tuple[Path, int, int]]:
    """Multirack NEMO/NCCL logs under *log_dir* root and ``failed/`` as ``(path, mtime_ns, size)``."""
    root = str(_host_resolve(log_dir)).rstrip("/")
    seen: set[str] = set()
    out: list[tuple[Path, int, int]] = []
    for base in (root, root + "/failed"):
        for path, mtime_ns, size in _host_log_entries(base):
            name = os.path.basename(path)
            if name in seen:
                continue
            if not _nemo_nccl_bn(name) or not _is_multirack_bn(name):
                continue
            seen.add(name)
            out.append((_host_path(path), mtime_ns, size))
    out.sort(key=lambda t: t[0].name.lower())
    return out


def _rack_from_1rack_log(name: str) -> str | None:
    return _nvis_naming().rack_id_from_log_basename(name)


def _jobid_from_log_name(name: str) -> str | None:
    # Llama-first, matching app.py and validation_activities: a hostname ending in "N" would
    # otherwise let the generic ``N-<jid>`` pattern win on a Llama basename.
    return _nvis_ids.job_id_from_log_basename(name)


def _glob_one_rack_logs(log_dir: Path) -> list[tuple[Path, int, int]]:
    """``*.txt`` in *log_dir* root only (NEMO single-rack validation) as ``(path, mtime_ns, size)``;
    excludes multirack and Llama stdout. Routed through ``host_exec`` (local or SSH)."""
    base = str(_host_resolve(log_dir))
    naming = _nvis_naming()
    out: list[tuple[Path, int, int]] = []
    for path, mtime_ns, size in _host_log_entries(base):
        name = os.path.basename(path)
        if not _nemo_single_rack_bn(name):
            continue
        if naming.is_llama_slurm_stdout_basename(name):
            continue
        if _is_multirack_bn(name):
            continue
        out.append((_host_path(path), mtime_ns, size))
    out.sort(key=lambda t: t[0].name.lower())
    return out


def _rack_sort_key(rid: str) -> tuple[int, ...]:
    return _nvis_naming().rack_sort_key(rid)


def _include_cmsh_rack_r00() -> bool:
    """When false (default), drop ``…-r00`` from the cmsh-derived rack list (avoids phantom ``p5-r00`` from stray ``p5-r0`` tokens)."""
    v = (os.environ.get("RACK_CMSH_INCLUDE_R00") or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def canonical_rack_id(rid: str) -> str | None:
    """Normalize rack ids per ``NVIS_RACK_ID`` (e.g. ``p5-r1`` → ``p5-r01`` for Bright templates)."""
    return _nvis_naming().canonical_rack_id(rid)


def extract_rack_ids_from_cmsh_text(text: str) -> list[str]:
    """Parse rack ids from ``cmsh -c 'rack; list'`` using ``NVIS_RACK_ID`` (legacy regex when configured)."""
    text = text or ""
    naming = _nvis_naming()
    canon: set[str] = set()
    if not _legacy_ds_configured():
        for rid in naming.extract_rack_ids_from_text(text):
            canon.add(rid)
    else:
        legacy = re.compile(r"\bp\d+-r\d+\b", re.IGNORECASE)
        for m in legacy.finditer(text):
            c = naming.canonical_rack_id(m.group(0))
            if c:
                canon.add(c)
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        mapped = naming.map_cmsh_rack_list_token(parts[0])
        if mapped:
            canon.add(mapped)
    out = sorted(canon, key=naming.rack_sort_key)
    if not _include_cmsh_rack_r00():
        out = [rid for rid in out if not naming.is_r00_rack(rid)]
    return out


def _cmsh_rack_list() -> list[str]:
    plat = _get_platform()
    cmd = plat.list_racks_cmd()
    if not host_exec.which(cmd[0]):
        return []
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=max(30, _env_int("RACK_NATIVE_CMSH_TIMEOUT_SEC", 120)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return extract_rack_ids_from_cmsh_text(proc.stdout or "")


DS_LAST_DIAG: dict = {}


def _cmsh_ds_text() -> str:
    """Fetch `cmsh -c ds`. Records a diagnostic instead of silently returning "".

    The original swallowed OSError/TimeoutExpired and returned an empty string, so a
    failing remote call was indistinguishable from a cluster with no devices — every
    rack row then rendered "-" with no error anywhere. DS_LAST_DIAG is surfaced via
    /api/ds-diag.
    """
    plat = _get_platform()
    cmd = plat.device_status_cmd()
    DS_LAST_DIAG.clear()
    DS_LAST_DIAG["cmd"] = " ".join(cmd)
    DS_LAST_DIAG["target"] = host_exec.exec_kind()
    DS_LAST_DIAG["remote"] = DS_LAST_DIAG["target"] == "ssh"
    if not host_exec.which(cmd[0]):
        DS_LAST_DIAG["error"] = "{} not on PATH on the {} target".format(cmd[0], DS_LAST_DIAG["target"])
        return ""
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=max(30, _env_int("RACK_NATIVE_DS_TIMEOUT_SEC", 300)),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        DS_LAST_DIAG["error"] = f"{type(e).__name__}: {e}"
        return ""
    out = proc.stdout or ""
    DS_LAST_DIAG["returncode"] = getattr(proc, "returncode", None)
    DS_LAST_DIAG["stdout_len"] = len(out)
    DS_LAST_DIAG["stdout_head"] = out[:400]
    DS_LAST_DIAG["stderr_head"] = (getattr(proc, "stderr", "") or "")[:400]
    return out


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
    if not _legacy_ds_configured():
        return _parse_cmsh_ds_nvis_templates(ds_text)
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
        blob = _nvis_naming().ct_blob(rr, ranges)
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


def pythoncm_ct_column_by_rack(rows: Iterable[dict[str, str]]) -> dict[str, str]:
    """Render the legacy CT column exclusively from PythonCM device status rows."""
    by_rack: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"DN": [], "INST": [], "UP": []}
    )
    for row in rows:
        hostname = str(row.get("node") or "").strip()
        rack = _normalize_rack_key_for_ds(str(row.get("rack") or "").strip())
        parsed = _rack_tray_from_hostname_token(hostname)
        if parsed:
            parsed_rack, tray = parsed
            # PythonCM's Rack entity is a physical EH/WH coordinate on Nscale;
            # the hostname-derived rack is the logical action/log namespace.
            rack = parsed_rack
        else:
            tray = hostname
        if not rack or not tray:
            continue
        state = str(row.get("device_status") or "UNKNOWN").strip().upper()
        if state == "UP":
            bucket = "UP"
        elif state in {"DOWN", "UNREACHABLE", "INSTALLER_FAILED", "INSTALLER_UNREACHABLE"}:
            bucket = "DN"
        else:
            bucket = "INST"
        by_rack[rack][bucket].append(tray)

    result: dict[str, str] = {}
    for rack, buckets in by_rack.items():
        parts: list[str] = []
        for label in ("DN", "INST", "UP"):
            values = buckets[label]
            if not values:
                continue
            inner = _condense_tray_tokens(" ".join(values))
            blob = _nvis_naming().ct_blob(rack, inner) if inner else ",".join(values)
            parts.append(f"{label}:{blob}")
        result[rack] = ";".join(parts) if parts else "-"
    return result


def _parse_cmsh_ds_nvis_templates(ds_text: str) -> dict[str, str]:
    """Parse ``cmsh ds`` using ``NVIS_CT_ID`` / ``NVIS_RACK_ID`` templates from Settings."""
    try:
        matcher = _nvis_ids.naming_matcher_from_env(_nvis_rack_id_template(), _nvis_ct_id_template())
    except ValueError:
        return {}
    ct_acc: dict[str, str] = defaultdict(str)
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
        for hm in matcher.find_in_line(line):
            # Skip NVSwitch trays: the compute CT template matches the
            # "<rack>-<slot>" prefix inside "<rack>-<slot>-nv", which would fold
            # NVSwitch slots (19-27) into the compute tray range.
            after = line[line.find(hm.hostname) + len(hm.hostname):]
            if after[:3].lower().startswith("-nv"):
                continue
            ingest(hm.hostname, hm.rack_id, hm.tray, st)

    ct_dn: dict[str, str] = {}
    ct_inst: dict[str, str] = {}
    ct_up: dict[str, str] = {}
    for acc_k, nums_s in ct_acc.items():
        rr, stb = acc_k.split("|", 1)
        ranges = _condense_tray_tokens(nums_s)
        if not ranges:
            continue
        blob = _nvis_naming().ct_blob(rr, ranges)
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


def _sinfo_source() -> str:
    """Availability source, independent of the command-submission backend.

    PythonCM remains the control/submission backend on NscaleTX, but its device
    status is not a scheduler availability signal.  Sites without Slurm can
    retain the legacy PythonCM-derived view by leaving this unset.
    """
    configured = (os.environ.get("RACK_NATIVE_SINFO_SOURCE") or "").strip().lower()
    if configured in {"slurm", "pythoncm"}:
        return configured
    return "pythoncm" if scheduler_runtime.is_pythoncm() else "slurm"


def _sinfo_state_key(raw: str) -> str:
    """Preserve Slurm's no-response ``*`` as a parseable ``_nr`` suffix."""
    value = (raw or "").strip().lower()
    no_response = value.endswith("*")
    base = sinfo_state_trim(value)
    return f"{base}_nr" if base and no_response else base


def _slurm_ct_column_by_rack(rows: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Render the per-tray status column from the same live Slurm snapshot.

    Rack availability and the tray list must never come from different control
    planes.  PythonCM device status can lag healthy Slurm nodes by minutes (or
    longer), which previously produced contradictory rows such as
    ``sinfo=idle=18`` alongside ten red ``DN`` trays.  Keep drained/maintenance
    nodes distinct as ``INST`` and reserve ``DN`` for a real down/failing or
    non-responding Slurm state.
    """
    by_rack: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"DN": [], "INST": [], "UP": [], "UNK": []}
    )
    naming = _nvis_naming()
    for hostname, state_raw in rows:
        parsed = naming.sinfo_rack_from_host(hostname.lower().split(".")[0])
        if not parsed:
            continue
        rack, canonical_host = parsed
        tray_parsed = _rack_tray_from_hostname_token(canonical_host)
        tray = tray_parsed[1] if tray_parsed else canonical_host
        state = (state_raw or "unknown").strip().lower()
        no_response = state.endswith("_nr")
        base = state[:-3] if no_response else state
        if no_response or base in {"down", "fail", "failing"}:
            bucket = "DN"
        elif base in {"drain", "drained", "draining", "maint", "inval", "invalid"}:
            bucket = "INST"
        elif base in {"idle", "alloc", "allocated", "mix", "mixed", "comp", "completing"}:
            bucket = "UP"
        else:
            bucket = "UNK"
        by_rack[rack][bucket].append(tray)

    result: dict[str, str] = {}
    for rack, buckets in by_rack.items():
        parts: list[str] = []
        for label in ("DN", "INST", "UP", "UNK"):
            values = buckets[label]
            if not values:
                continue
            inner = _condense_tray_tokens(" ".join(values))
            blob = naming.ct_blob(rack, inner) if inner else ",".join(values)
            parts.append(f"{label}:{blob}")
        result[rack] = ";".join(parts) if parts else "-"
    return result


def collect_sinfo_snapshot() -> tuple[dict[tuple[str, str], int], dict[str, str]]:
    """Return rack counts and exact-node status from one control-plane read."""
    if _sinfo_source() == "pythoncm":
        rows = scheduler_runtime.nodes()
        counts: dict[tuple[str, str], int] = {}
        for row in rows:
            rack = str(row.get("rack") or "").strip()
            if not rack:
                continue
            key = (rack, str(row.get("state") or "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return counts, pythoncm_ct_column_by_rack(rows)

    plat = _get_platform()
    opts = (os.environ.get("SINFO_OPTS") or "").strip()
    extra = "-N -h -o '%N %t'"
    if opts:
        extra += f" {opts}"
    try:
        proc = host_exec.run_slurm_on_host(
            plat.sinfo_cmd(extra),
            timeout=max(30, _env_int("RACK_NATIVE_SINFO_TIMEOUT_SEC", 120)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}, {}

    counts: dict[tuple[str, str], int] = {}
    node_rows: list[tuple[str, str]] = []
    naming = _nvis_naming()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        node, st_raw = parts[0], parts[1]
        st = _sinfo_state_key(st_raw)
        if not st:
            continue
        hosts: list[str] = []
        if "[" in node and "]" in node:
            try:
                hp = host_exec.run_slurm_on_host(
                    plat.scontrol_hostnames_cmd(node),
                    timeout=max(5, _env_int("RACK_NATIVE_SCONTROL_TIMEOUT_SEC", 60)),
                )
                hosts = [h.strip() for h in (hp.stdout or "").splitlines() if h.strip()]
            except (OSError, subprocess.TimeoutExpired):
                hosts = []
        else:
            hosts = [node]
        for host in hosts:
            parsed = naming.sinfo_rack_from_host(host.lower().split(".")[0])
            if not parsed:
                continue
            rack, canonical_host = parsed
            key = (rack, st)
            counts[key] = counts.get(key, 0) + 1
            node_rows.append((canonical_host, st))
    return counts, _slurm_ct_column_by_rack(node_rows)


def collect_sinfo_counts() -> dict[tuple[str, str], int]:
    """Map (rack_id, trimmed_state) -> count of nodes (after hostname expansion).

    No bare-name ``which("sinfo")`` guard: the Slurm CLI is not on PATH here
    (``plat.sinfo_cmd()`` supplies the absolute path plus ``SLURM_CONF``), so that probe
    always returned None and short-circuited this function to ``{}``. Every rack then
    rendered ``sinfo: "-"``, was treated as a 0-node non-compute rack, and dropped out of
    the dashboard summary. Matches ``load_squeue_jobs_map()``, which calls
    ``run_slurm_on_host`` directly with no guard.
    """
    return collect_sinfo_snapshot()[0]


def shlex_split_safe(s: str) -> list[str]:
    import shlex

    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def rack_total_sinfo_nodes(rack: str, counts: dict[tuple[str, str], int]) -> int:
    """Total Slurm nodes reported by sinfo for *rack* (sum across all node states).

    Zero means the rack has no compute nodes in Slurm — i.e. a non-GB200 rack (switch /
    management / networking). Used to keep such "0/0" racks out of the validation
    statistics (they are not part of the node/rack count we need to validate).
    """
    return sum(cnt for (rr, _st), cnt in counts.items() if rr == rack)


def format_sinfo_cell(rack: str, counts: dict[tuple[str, str], int]) -> str:
    pairs: list[tuple[int, str]] = []
    for (rr, st), cnt in counts.items():
        if rr != rack:
            continue
        pairs.append((cnt, st))
    if not pairs:
        return "-"
    pairs.sort(key=lambda x: (-x[0], x[1]))
    return ",".join(
        f"{st[:-3] + '*' if st.endswith('_nr') else st}={cnt}"
        for cnt, st in pairs
    )


_NODE_DOWN_SHEET_ID = "1FQxSyWRvAvK5l0z9vsBP2Wd8j6iIcE5pUGiVjRLahc4"
_NODE_DOWN_SHEET_RANGE = "Node down status report!A1:B10"
_NODE_DOWN_SHEET_CACHE_LOCK = threading.Lock()
_NODE_DOWN_SHEET_CACHE: dict[str, Any] = {}
_NODE_DOWN_SHEET_CACHE_MONOTONIC = 0.0
_SHEET_UNAVAILABLE_STATES = {
    "down", "drain", "drained", "draining", "fail", "failing", "maint", "unknown"
}


def _node_down_sheet_summary() -> dict[str, Any]:
    """Read the same headline cells operators use in Node down status report.

    The small result is cached because /api/status can have many simultaneous
    readers.  This is only reconciliation metadata; live Slurm remains the
    source of the rack colors and live counters.
    """
    global _NODE_DOWN_SHEET_CACHE, _NODE_DOWN_SHEET_CACHE_MONOTONIC
    ttl = max(30, _env_int("RACK_NODE_DOWN_SHEET_TTL_SEC", 120))
    now = time.monotonic()
    with _NODE_DOWN_SHEET_CACHE_LOCK:
        if _NODE_DOWN_SHEET_CACHE and now - _NODE_DOWN_SHEET_CACHE_MONOTONIC < ttl:
            return dict(_NODE_DOWN_SHEET_CACHE)

        token_path = Path(
            (os.environ.get("RACK_NODE_DOWN_SHEET_TOKEN_FILE") or "/run/secrets/gsheets-token").strip()
        )
        sheet_id = (
            os.environ.get("RACK_NODE_DOWN_SHEET_ID") or _NODE_DOWN_SHEET_ID
        ).strip()
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if not token.startswith("ya29."):
                raise ValueError("Google Sheets token is missing or invalid")
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{_NODE_DOWN_SHEET_RANGE}"
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=max(3, _env_int("RACK_NODE_DOWN_SHEET_TIMEOUT_SEC", 10)),
            )
            response.raise_for_status()
            values = response.json().get("values") or []
            metrics: dict[str, int] = {}
            for row in values:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                label = str(row[0]).strip()
                raw_count = str(row[1]).replace(",", "").strip()
                if raw_count.isdigit():
                    metrics[label] = int(raw_count)
            stamp_text = str(values[1][0]) if len(values) > 1 and values[1] else ""
            stamp_match = re.search(r"Updated by Codex at ([^.]+ UTC)", stamp_text)
            result: dict[str, Any] = {
                "ok": True,
                "spreadsheet_id": sheet_id,
                "tab": "Node down status report",
                "updated_at": stamp_match.group(1) if stamp_match else "",
                "unavailable": metrics.get("Unavailable in Slurm"),
                "responding": metrics.get("Slurmd responding"),
                "no_response": metrics.get("Slurmd not responding (*)"),
                "drained": metrics.get("Drained"),
                "down_other": metrics.get("Down / fail / maint / unknown"),
            }
        except Exception as exc:  # noqa: BLE001 - sheet outage must not take down rack status
            result = {"ok": False, "error": str(exc)[:240]}
        _NODE_DOWN_SHEET_CACHE = result
        _NODE_DOWN_SHEET_CACHE_MONOTONIC = now
        return dict(result)


def availability_summary_from_counts(
    counts: dict[tuple[str, str], int], *, include_sheet: bool = True
) -> dict[str, Any]:
    """Build cluster counters using the node-down sheet's exact state policy."""
    total = ready = unavailable = no_response = drained = down_other = invalid = 0
    for (_rack, state_key), count in counts.items():
        nr = state_key.endswith("_nr")
        base = state_key[:-3] if nr else state_key
        total += count
        if nr:
            no_response += count
        if base == "idle" and not nr:
            ready += count
        if base in {"drain", "drained", "draining"}:
            drained += count
        if base in _SHEET_UNAVAILABLE_STATES:
            unavailable += count
            if base not in {"drain", "drained", "draining"}:
                down_other += count
        if base in {"inval", "invalid"}:
            invalid += count

    out: dict[str, Any] = {
        "source": _sinfo_source(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "total_nodes": total,
        "ready_nodes": ready,
        "unavailable_nodes": unavailable,
        "no_response_nodes": no_response,
        "drained_nodes": drained,
        "down_other_nodes": down_other,
        "invalid_nodes": invalid,
        "definition": "Node down status report",
    }
    if include_sheet and _sinfo_source() == "slurm":
        sheet = _node_down_sheet_summary()
        sheet_count = sheet.get("unavailable")
        if isinstance(sheet_count, int):
            sheet["delta"] = unavailable - sheet_count
            sheet["matches_live"] = unavailable == sheet_count
        out["sheet"] = sheet
    return out


def _slurm_base_job_id(jid_raw: str) -> str | None:
    """Normalize a scheduler reference (including PythonCM command UUIDs)."""
    s = (jid_raw or "").strip()
    if not s:
        return None
    if scheduler_runtime.is_pythoncm():
        return s if scheduler_runtime.valid_reference(s) else None
    base = s.split(".", 1)[0] if "." in s else s
    return base if base.isdigit() else None


def validation_status_hint(
    nemo: str,
    last_job: str,
    *,
    nccl_ok: bool | None = None,
    llm_ok: bool | None = None,
    validation_test: str | None = None,
) -> str:
    """Short explanation for dashboard hover (pending / ongoing / fail); empty for done."""
    n = (nemo or "").strip()
    lj = (last_job or "").strip()
    if n == "done" or n.lower() == "done":
        return ""
    vt = (validation_test or "").strip() or "VALIDATION_TEST"
    if n == "pending" or n.lower() == "pending":
        return (
            "No NEMO or NCCL validation log in the log directory matches this rack yet "
            "(rack appears in the rack list from CM, but no matching log basename)."
        )
    if n.startswith("ongoing"):
        if lj and lj != "-":
            return f"Scheduler job {lj} is running; the validation log is still being written."
        return "The scheduler reports a running validation job; elapsed time is shown in the status label."
    if n == "fail":
        parts: list[str] = []
        if nccl_ok is False:
            parts.append(
                f"NCCL suite not satisfied for {vt} (missing busbw line(s) or below threshold)."
            )
        if llm_ok is False:
            parts.append(
                "LLM / Nemotron segment is not OK, training has not reached the target step count, "
                "or the log still looks incomplete."
            )
        if parts:
            return " ".join(parts)
        return "Validation did not reach done; inspect the log or adjust VALIDATION_TEST in Settings."
    return ""


# Per-rack validation is a staged process (single-rack scope):
#   Stage 1 = NCCL benchmark  -> tracked by ``nccl_ok``
#   Stage 2 = NeMo / LLM train -> tracked by ``llm_ok``
# (Stage 3 = cross-rack is a *multi-rack* concern surfaced in the multirack tab, not per rack.)
# A rack is considered individually validated once BOTH stage 1 and stage 2 are done.
STAGE_LABELS = {"stage1": "NCCL", "stage2": "NeMo"}


def _durable_completed_stage_logs_by_rack() -> dict[str, dict[str, dict[str, Any]]]:
    """Build probe-shaped stage evidence from the persistent results database.

    ``native_log_analysis.json`` is intentionally replaceable parser cache.  A
    completed acceptance run must not revert to fail/pending when that cache is
    rotated, a worker starts cold, or a later retry fails.  Results history is
    the durable source of successful NCCL/NeMo evidence.
    """
    try:
        completed = results_history.latest_successful_runs_by_rack(("nccl", "nemo"))
    except Exception:  # status still renders while history storage is unhealthy
        _logger.warning("could not load durable validation completion evidence", exc_info=True)
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}

    def as_probe(kind: str, run: dict[str, Any], *, current: bool) -> dict[str, Any]:
        status = str(run.get("status") or "unknown").strip().lower()
        passed = status == "pass"
        terminal_failure = status in {"fail", "timeout"}
        job_id = str(run.get("job_id") or "").strip()
        try:
            log_mtime = float(run.get("log_mtime") or 0)
        except (TypeError, ValueError):
            log_mtime = 0.0
        probe: dict[str, Any] = {
            "nemo_state": "done" if passed else "fail" if terminal_failure else "pending",
            "nccl_ok": (passed if kind == "nccl" else False) if (passed or terminal_failure) else None,
            "llm_ok": (passed if kind == "nemo" else False) if (passed or terminal_failure) else None,
            "jid": int(job_id) if job_id.isdigit() else job_id,
            "_path": str(run.get("source_log_path") or ""),
            "_mtime": log_mtime,
            "_durable_completion": passed,
            "_completed_at": str(run.get("ended_at") or run.get("started_at") or ""),
        }
        if current:
            probe["_authoritative_current"] = True
        return probe

    for kind in ("nccl", "nemo"):
        for rack, run in (completed.get(kind) or {}).items():
            if not isinstance(run, dict):
                continue
            out.setdefault(str(rack), {})[kind] = as_probe(kind, run, current=False)

    # The settled table above is intentionally monotonic acceptance history. A
    # separate collector row represents the *current* attempt for each rack and
    # must win even when it is a failure or an incomplete run; otherwise an old
    # pass paints a broken rack green forever. Missing current rows do not erase
    # settled history, and a history-store error does not break status rendering.
    try:
        current = results_history.latest_runs_by_rack(
            ("nccl", "nemo"), source="nscale16k_current_rack_evidence"
        )
    except Exception:
        _logger.warning("could not load current validation evidence", exc_info=True)
        current = {}
    for kind in ("nccl", "nemo"):
        for rack, run in (current.get(kind) or {}).items():
            if isinstance(run, dict):
                out.setdefault(str(rack), {})[kind] = as_probe(kind, run, current=True)
    return out


_NCCL_TOKENS = frozenset(
    {
        "nccl_all_reduce",
        "all_reduce",
        "allreduce",
        "nccl_alltoall",
        "alltoall",
        "nccl_all_toall",
        "nccl_allgather",
        "all_gather",
        "allgather",
    }
)
_LLM_TOKENS = frozenset({"llm", "nemotron"})
_DEFAULT_VALIDATION_TEST = "nccl_all_reduce,nccl_alltoall,nccl_allgather,llm"


def validation_wants(validation_test: str | None) -> tuple[bool, bool]:
    """Return ``(want_nccl, want_llm)`` from a ``VALIDATION_TEST`` string.

    Mirrors ``validation_test_env.sh``: an empty/unset value means the default suite
    (all NCCL collectives + llm). ``nccl_ok`` / ``llm_ok`` from the probe are vacuously
    ``True`` when their stage is *not* part of the suite, so we must consult the suite to
    tell "passed" from "not part of this run".
    """
    raw = (validation_test or "").strip()
    if not raw:
        raw = _DEFAULT_VALIDATION_TEST
    want_nccl = False
    want_llm = False
    for tok in raw.split(","):
        t = tok.strip().lower()
        if t in _NCCL_TOKENS:
            want_nccl = True
        elif t in _LLM_TOKENS:
            want_llm = True
    return want_nccl, want_llm


def _stage_state(
    nemo_lower: str,
    ok: bool | None,
    wanted: bool,
) -> str:
    """Resolve a single stage's state given the overall status and its probe flag.

    A stage that is not part of the validation suite reports ``pending`` (not started) —
    e.g. NeMo when only NCCL was run — instead of a vacuous ``done``.

    ``ok is None`` means the stage has no probe evidence of its own (its log does not exist, or
    could not be classified). That is **not** a failure: it reports ``pending``. Only an explicit
    ``False`` — a probe that ran and returned a negative verdict — is ``fail``. Without this, a
    rack that only ever ran NCCL renders as "NeMo FAIL" citing the NCCL job id.
    """
    if not wanted or nemo_lower == "pending":
        return "pending"
    if nemo_lower.startswith("ongoing"):
        return "done" if ok is True else "ongoing"
    # done or fail overall: the stage passed only with a positive probe flag.
    if ok is True:
        return "done"
    if ok is None:
        return "pending"
    return "fail"


def compute_stage_states(
    nemo: str,
    nccl_ok: bool | None,
    llm_ok: bool | None,
    validation_test: str | None = None,
) -> tuple[str, str]:
    """Derive per-stage states ``(stage1, stage2)`` from the combined ``nemo`` status.

    Stage 1 tracks the NCCL suite (``nccl_ok``); stage 2 tracks NeMo/LLM training (``llm_ok``).
    Each returned value is one of ``done`` | ``fail`` | ``ongoing`` | ``pending``.
    A stage not included in ``validation_test`` reports ``pending`` (not started), so NeMo
    never shows ``done`` when only NCCL has run.
    """
    n = (nemo or "").strip().lower()
    want_nccl, want_llm = validation_wants(validation_test)
    return (
        _stage_state(n, nccl_ok, want_nccl),
        _stage_state(n, llm_ok, want_llm),
    )


def empty_stage_summary() -> dict[str, int]:
    return {"done": 0, "fail": 0, "ongoing": 0, "pending": 0}


def tally_stage(summary: dict[str, int], state: str) -> None:
    key = state if state in summary else "pending"
    summary[key] += 1


def load_squeue_jobs_map() -> tuple[set[str], dict[str, str]]:
    """Return (job_ids, job_id -> %M elapsed string).

    Job ids are normalized to the base numeric id (no ``.batch`` / ``.extern`` suffix) so they
    match ``last_job`` parsed from log filenames.
    """
    if scheduler_runtime.is_pythoncm():
        return scheduler_runtime.active_job_times()
    user = (os.environ.get("SQUEUE_USER") or os.environ.get("USER") or "root").strip()
    plat = _get_platform()
    try:
        proc = host_exec.run_slurm_on_host(
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
    requested = [str(item).strip() for item in want if str(item).strip()]
    if scheduler_runtime.is_pythoncm():
        # PythonCM is the primary provider on Nscale, but reviewed validation
        # fallbacks still have numeric Slurm job ids.  The durable activity
        # registry is an overlay, not live evidence: feeding one of its stale
        # RUNNING rows back into task hydration creates a circular state that
        # can never reach sacct.  Trust the registry only for native PythonCM
        # UUIDs; force explicit numeric fallbacks through the targeted squeue
        # lookup below.
        pythoncm_refs = [
            ref for ref in requested
            if scheduler_runtime.reference_kind(ref) == "pythoncm"
        ]
        refs, times = scheduler_runtime.active_job_times(pythoncm_refs)
        running.update(refs)
        for ref, elapsed in times.items():
            job_times.setdefault(ref, elapsed)
        requested = [
            ref for ref in requested
            if scheduler_runtime.reference_kind(ref) == "slurm"
        ]
        for ref in requested:
            running.discard(ref)
            job_times.pop(ref, None)
        if not requested:
            return
    if _env_truthy("RACK_SQUEUE_JOB_LOOKUP_DISABLED"):
        return
    need: list[str] = []
    seen: set[str] = set()
    for j in requested:
        if not j or not scheduler_runtime.valid_reference(str(j).strip()):
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
            proc = host_exec.run_slurm_on_host(
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
            if scheduler_runtime.valid_reference(s):
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


def _load_log_cache(path: Path) -> dict[str, Any]:
    """Probe entries from *path*, or ``{}`` if absent, unreadable or written by older logic.

    No lock: writers publish with ``os.replace``, so a reader sees either the whole old file or
    the whole new one.
    """
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") != _NATIVE_LOG_CACHE_VERSION:
        return {}
    ent = raw.get("entries")
    return ent if isinstance(ent, dict) else {}


@contextlib.contextmanager
def _log_cache_write_lock(path: Path):
    """Serialize the read-merge-write in :func:`_save_log_cache` across Gunicorn workers.

    The lock is a sidecar file, not the cache itself: the cache is published with ``os.replace``,
    which swaps in a new inode, so a lock held on the cache file would guard an inode the next
    writer no longer opens. Best-effort -- if the lock cannot be taken the save still proceeds,
    since losing a cache entry only costs a re-probe.
    """
    lock_path = path.with_name(path.name + ".lock")
    f = None
    # Acquisition is kept separate from the body so that a read-only var/ (no lock file) falls
    # through to an unlocked save instead of skipping the save, and so a failure raised by the
    # body is never mistaken for a failure to lock.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 - released in finally below
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
        except OSError:
            pass
    except OSError:
        f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass
            f.close()


def _save_log_cache(path: Path, local: dict[str, Any], alive: set[str]) -> None:
    """Merge *local* into the cache at *path* and prune entries whose log is gone.

    Re-reads under an exclusive lock instead of writing the snapshot this worker loaded minutes
    ago. Two workers probing concurrently both take ~a minute of remote commands; with a
    last-writer-wins save, whichever finished second erased the other's fresh entries and those
    logs were re-probed on the very next cycle. The probe itself stays outside the lock -- only
    the merge is serialized, so concurrent probing is still parallel.

    *alive* is the set of paths this worker just scanned; entries outside it are dropped so the
    cache does not grow forever as logs are deleted.
    """
    with _log_cache_write_lock(path):
        merged = dict(_load_log_cache(path))
        merged.update(local)
        if alive:
            merged = {k: v for k, v in merged.items() if k in alive}
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"version": _NATIVE_LOG_CACHE_VERSION, "entries": merged}, indent=2, sort_keys=True) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(blob, encoding="utf-8")
        os.replace(str(tmp), str(path))


def _multirack_node_count_suffix_from_basename(name: str) -> str:
    return _nvis_ids.node_count_suffix_from_basename(name)


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
_native_history_lock = threading.Lock()
_native_history_seen: set[str] = set()


def _archive_completed_native_probe(
    path: Path,
    *,
    mtime_ns: int,
    size: int,
    rack: str | None,
    kind: str | None,
    probe: dict[str, Any],
) -> None:
    """Persist fingerprint-validated one-rack completion evidence.

    The JSON probe file is a replaceable cache, not an acceptance ledger.  Both
    freshly parsed probes and cache hits arrive here only after the worker has
    matched the cache entry to the current file ``mtime_ns`` and ``size``.
    Successful stage evidence is therefore safe to archive in results history;
    failures remain visible in the live attempt/cache view but cannot erase an
    earlier acceptance pass.
    """
    passed = (kind == "nccl" and probe.get("nccl_ok") is True) or (
        kind == "nemo" and probe.get("llm_ok") is True
    )
    if not passed or not rack or kind not in {"nccl", "nemo"}:
        return

    mtime = mtime_ns / 1_000_000_000
    run_id = results_history.make_run_id(str(path), mtime, size)
    with _native_history_lock:
        if run_id in _native_history_seen:
            return
        try:
            existing = results_history.get_run(run_id)
            if not existing or existing.get("status") != "pass":
                results_history.ingest_run(
                    {
                        "run_id": run_id,
                        "source_log_path": str(path),
                        "log_basename": path.name,
                        "log_mtime": mtime,
                        "log_size": size,
                        "workload_kind": kind,
                        "rack": rack,
                        "job_id": probe.get("jid"),
                        "status": "pass",
                        "source": "native_status_probe",
                        "extra": {"fingerprint_validated": True},
                    }
                )
            _native_history_seen.add(run_id)
        except Exception:
            # History is a durability overlay.  A locked/unavailable DB must not
            # prevent the live rack table from publishing its parser result.
            _logger.warning("could not archive native completion probe for %s", path, exc_info=True)


class NativeRackTableState:
    __slots__ = (
        "availability_summary",
        "by_rack_log",
        "by_rack_stage_log",
        "ct_by_rack",
        "ds_done",
        "ds_thread",
        "key",
        "last_errors",
        "lock",
        "log_dir",
        "log_done",
        "log_thread",
        "per_path_probe",
        "racks",
        "racks_refresh_lock",
        "racks_refreshed_monotonic",
        "sinfo_counts",
        "sinfo_done",
        "sinfo_thread",
    )

    def __init__(self, key: str, log_dir: Path) -> None:
        self.key = key
        self.log_dir = _host_resolve(log_dir)
        self.lock = threading.RLock()
        self.racks: list[str] = []
        self.racks_refresh_lock = threading.Lock()
        self.racks_refreshed_monotonic = 0.0
        self.ct_by_rack: dict[str, str] = {}
        self.sinfo_counts: dict[tuple[str, str], int] = {}
        self.availability_summary: dict[str, Any] = {}
        self.per_path_probe: dict[str, dict[str, Any]] = {}
        self.by_rack_log: dict[str, dict[str, Any]] = {}
        # rack -> stage kind ("nccl" | "nemo") -> newest probe record of THAT kind.
        # by_rack_log alone keeps only the newest log of any kind, which silently discards a
        # passing NCCL result as soon as a NeMo run for the same rack finishes after it.
        self.by_rack_stage_log: dict[str, dict[str, dict[str, Any]]] = {}
        self.ds_thread: threading.Thread | None = None
        self.sinfo_thread: threading.Thread | None = None
        self.log_thread: threading.Thread | None = None
        self.ds_done = False
        self.sinfo_done = False
        self.log_done = False
        self.last_errors: list[str] = []

    def refresh_racks_blocking(self) -> None:
        """Refresh slow-changing BCM inventory at most once per configured TTL.

        Device state still refreshes on the normal status cadence. Keeping the
        last good rack inventory on a transient cmsh failure prevents an empty
        table and avoids multiplying CMDaemon work across concurrent readers.
        Set RACK_NATIVE_RACK_LIST_TTL_SEC=0 only for diagnostics.
        """
        ttl = max(0, _env_int("RACK_NATIVE_RACK_LIST_TTL_SEC", 900))
        with self.racks_refresh_lock:
            now = time.monotonic()
            with self.lock:
                if self.racks and ttl > 0 and now - self.racks_refreshed_monotonic < ttl:
                    return
            racks = (
                scheduler_runtime.rack_names(force=True)
                if scheduler_runtime.is_pythoncm()
                else _cmsh_rack_list()
            )
            if not racks:
                return
            with self.lock:
                self.racks = racks
                self.racks_refreshed_monotonic = now

    def _worker_ds(self) -> None:
        try:
            if _sinfo_source() == "slurm":
                # The Slurm worker owns both aggregate availability and the
                # exact tray list.  Letting this concurrent PythonCM worker
                # write ct_by_rack creates a last-writer-wins race and brings
                # stale DN trays back after the correct Slurm snapshot lands.
                with self.lock:
                    self.ds_done = True
                return
            if scheduler_runtime.is_pythoncm():
                col = pythoncm_ct_column_by_rack(scheduler_runtime.nodes())
            else:
                text = _cmsh_ds_text()
                col = parse_cmsh_ds_to_ct_column_by_rack(text)
            with self.lock:
                self.ct_by_rack = col
                self.ds_done = True
        except Exception as e:
            _logger.debug("_worker_ds: ignoring error", exc_info=True)
            with self.lock:
                self.last_errors.append(f"ds: {e}")
                self.ds_done = True

    def _worker_sinfo(self) -> None:
        try:
            c, slurm_ct = collect_sinfo_snapshot()
            availability = availability_summary_from_counts(c)
            with self.lock:
                self.sinfo_counts = c
                self.availability_summary = availability
                if _sinfo_source() == "slurm" and slurm_ct:
                    # Exact node state and aggregate counts now share one live
                    # Slurm snapshot; never mix current counts with stale BCM DN.
                    self.ct_by_rack = slurm_ct
                    self.ds_done = True
                self.sinfo_done = True
        except Exception as e:
            _logger.debug("_worker_sinfo: ignoring error", exc_info=True)
            with self.lock:
                self.last_errors.append(f"sinfo: {e}")
                self.sinfo_done = True

    def _worker_logs(self, probe_fn: Callable[[Path], dict[str, object] | None]) -> None:
        try:
            disk = _load_log_cache(_NATIVE_LOG_JSON)
            entries = _glob_one_rack_logs(self.log_dir)
            alive: set[str] = set()
            dirty = False
            for p, mtime_ns, size in entries:
                keyp = str(p)
                alive.add(keyp)
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
                m_this = mtime_ns / 1_000_000_000
                kind = _stage_kind_of_log_bn(p.name)
                _archive_completed_native_probe(
                    p,
                    mtime_ns=mtime_ns,
                    size=size,
                    rack=rk,
                    kind=kind,
                    probe=rec_d,
                )
                with self.lock:
                    self.per_path_probe[keyp] = rec_d
                    if rk:
                        prev = self.by_rack_log.get(rk)
                        replace = prev is None
                        if not replace and isinstance(prev, dict):
                            replace = m_this >= float(prev.get("_mtime") or 0.0)
                        if replace:
                            self.by_rack_log[rk] = {**rec_d, "_path": str(p), "_mtime": m_this}
                        if kind:
                            bucket = self.by_rack_stage_log.setdefault(rk, {})
                            prev_k = bucket.get(kind)
                            replace_k = prev_k is None
                            if not replace_k and isinstance(prev_k, dict):
                                replace_k = m_this >= float(prev_k.get("_mtime") or 0.0)
                            if replace_k:
                                bucket[kind] = {**rec_d, "_path": str(p), "_mtime": m_this}
            if dirty or any(k not in alive for k in disk):
                _save_log_cache(_NATIVE_LOG_JSON, disk, alive)
        except Exception as e:
            _logger.debug("_worker_logs: ignoring error", exc_info=True)
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

    def wait_for_scheduled_workers(self, timeout_sec: float) -> list[str]:
        """Wait for the worker generation feeding a canonical snapshot.

        A cold web worker must not replace the shared SQLite snapshot with
        placeholder rows before its Slurm and log probes have recovered the
        completed NCCL/NeMo evidence.
        """
        with self.lock:
            workers = (
                ("ds", self.ds_thread),
                ("sinfo", self.sinfo_thread),
                ("logs", self.log_thread),
            )
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        for _worker_id, thread in workers:
            if thread is None:
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
        return [
            worker_id
            for worker_id, thread in workers
            if thread is not None and thread.is_alive()
        ]

    def build_table_rows(self) -> tuple[list[dict[str, str]], dict[str, Any], list[str], str]:
        running, job_times = load_squeue_jobs_map()
        # Open validation activities carry the live scheduler ref for a just-launched stage,
        # even when the newest on-disk *-1Rack-* artefact is still from a prior failed job.
        try:
            import validation_activities as _va

            live_jobs = _va.live_stage_jobs_by_rack()
        except Exception:  # noqa: BLE001 — the overlay is best-effort; the rack table must still
            # render if the activities store is missing, locked or corrupt.
            live_jobs = {}
        durable_stage_logs = _durable_completed_stage_logs_by_rack()
        with self.lock:
            racks = list(self.racks)
            ct = dict(self.ct_by_rack)
            sct = dict(self.sinfo_counts)
            by_rack = {k: dict(v) for k, v in self.by_rack_log.items()}
            by_rack_stage = {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in self.by_rack_stage_log.items()}
            errs = list(self.last_errors)
            ds_done = self.ds_done
            sinfo_done = self.sinfo_done
            sinfo_source = _sinfo_source()
        rows: list[dict[str, str]] = []
        n_done = n_fail = n_on = n_pending = 0
        stage1_summary = empty_stage_summary()
        stage2_summary = empty_stage_summary()
        want: set[str] = set()
        for kinds in live_jobs.values():
            if not isinstance(kinds, dict):
                continue
            for jid in kinds.values():
                sj = str(jid or "").strip()
                if scheduler_runtime.valid_reference(sj):
                    want.add(sj)
        for rack in sorted(racks, key=_rack_sort_key):
            pr_full = by_rack.get(rack)
            stage_logs = {
                kind: dict(src)
                for kind, src in (by_rack_stage.get(rack) or {}).items()
                if isinstance(src, dict)
            }
            durable_for_rack = durable_stage_logs.get(rack) or {}
            # Acceptance completion is monotonic.  A failed retry remains in
            # history/attempt views, but it cannot replace a prior pass in the
            # completion table.  An actually queued live job still overrides
            # this below and paints the relevant stage ongoing.
            for kind, src in durable_for_rack.items():
                if isinstance(src, dict):
                    stage_logs[kind] = dict(src)
            if not pr_full and stage_logs:
                pr_full = max(
                    stage_logs.values(),
                    key=lambda src: (
                        float(src.get("_mtime") or 0),
                        str(src.get("_completed_at") or ""),
                    ),
                )
            if not pr_full:
                continue
            pr0 = {k: v for k, v in pr_full.items() if k not in ("_path", "_mtime")}
            path0 = pr_full.get("_path") or ""
            path0_s = str(path0) if path0 else ""
            bn0 = Path(path0_s).name if path0_s else ""
            lj0 = _last_job_for_log_row(bn0, pr0)
            if lj0 != "-" and scheduler_runtime.valid_reference(lj0):
                want.add(lj0)
            for src in stage_logs.values():
                if not isinstance(src, dict):
                    continue
                sp = str(src.get("_path") or "")
                sbn = Path(sp).name if sp else ""
                probe = {k: v for k, v in src.items() if k not in ("_path", "_mtime")}
                lj = _last_job_for_log_row(sbn, probe)
                if lj != "-" and scheduler_runtime.valid_reference(lj):
                    want.add(lj)
        augment_running_set_for_job_ids(running, job_times, want)
        for rack in sorted(racks, key=_rack_sort_key):
            # A rack with 0 Slurm nodes (once sinfo is done) is a non-GB200 rack
            # (switch / mgmt / networking) — keep it in the table/floor but exclude it
            # from the validation statistics so the counts reflect only compute racks.
            expected_compute = bool(
                (os.environ.get("RACK_NATIVE_EXPECTED_RACK_NUMBERS") or "").strip()
            )
            countable = expected_compute or not (
                sinfo_done and rack_total_sinfo_nodes(rack, sct) == 0
            )
            siv = "?" if not sinfo_done else format_sinfo_cell(rack, sct)
            if expected_compute and sinfo_done and rack_total_sinfo_nodes(rack, sct) == 0:
                siv = f"unknown (missing from {sinfo_source.title()})"
            live = live_jobs.get(rack) if isinstance(live_jobs.get(rack), dict) else {}
            live_nccl = str((live or {}).get("nccl") or "").strip()
            live_nemo = str((live or {}).get("nemo") or "").strip()
            pr_full = by_rack.get(rack)
            stage_logs = {
                kind: dict(src)
                for kind, src in (by_rack_stage.get(rack) or {}).items()
                if isinstance(src, dict)
            }
            for kind, src in (durable_stage_logs.get(rack) or {}).items():
                if isinstance(src, dict):
                    stage_logs[kind] = dict(src)
            if not pr_full and stage_logs:
                pr_full = max(
                    stage_logs.values(),
                    key=lambda src: (
                        float(src.get("_mtime") or 0),
                        str(src.get("_completed_at") or ""),
                    ),
                )
            if not pr_full:
                # No results log yet — still paint ongoing when an activity-tracked job is live.
                nccl_live = bool(live_nccl and live_nccl in running)
                nemo_live = bool(live_nemo and live_nemo in running)
                if nccl_live or nemo_live:
                    live_jid = live_nccl if nccl_live else live_nemo
                    tm = job_times.get(live_jid, "?")
                    nemo = f"ongoing [{tm}]"
                    stage1 = "ongoing" if nccl_live else "pending"
                    stage2 = "ongoing" if nemo_live else "pending"
                    if countable:
                        n_on += 1
                        tally_stage(stage1_summary, stage1)
                        tally_stage(stage2_summary, stage2)
                    rows.append(
                        {
                            "rack": rack,
                            "nemo": nemo,
                            "stage1": stage1,
                            "stage2": stage2,
                            "last_job": live_jid,
                            "sinfo": siv,
                            "availability_source": sinfo_source,
                            "ct_down_unk": "?" if not ds_done else ct.get(rack, "-"),
                            "nemo_hint": validation_status_hint(nemo, live_jid),
                        }
                    )
                    continue
                if countable:
                    n_pending += 1
                    tally_stage(stage1_summary, "pending")
                    tally_stage(stage2_summary, "pending")
                rows.append(
                    {
                        "rack": rack,
                        "nemo": "pending",
                        "stage1": "pending",
                        "stage2": "pending",
                        "last_job": "-",
                        "sinfo": siv,
                        "availability_source": sinfo_source,
                        "ct_down_unk": "?" if not ds_done else ct.get(rack, "-"),
                        "nemo_hint": validation_status_hint("pending", "-"),
                    }
                )
                continue

            pr = {k: v for k, v in pr_full.items() if k not in ("_path", "_mtime")}
            path = pr_full.get("_path") or ""
            path_s = str(path) if path else ""
            bn = Path(path_s).name if path_s else ""
            jid_s = _last_job_for_log_row(bn, pr)

            # Judge each stage from the newest log of its OWN kind. NCCL and NeMo run as
            # separate jobs writing separate logs, so `pr` (newest log of any kind) only ever
            # carries evidence for one of the two stages; using it for both makes the stage
            # whose log is older read as a failure even though it passed.
            #
            # An empty stage map is NOT treated as "not validated": _stage_kind_of_log_bn needs
            # the exact "-NEMO-1Rack-"/"-NCCL-1Rack-" infix, while the file filter accepts any
            # name containing "nemo" or "1rack", so a site naming its logs differently would
            # have every rack read as pending regardless of a passing probe. Those racks fall
            # through to the single-log path below, where an unclassifiable probe still yields
            # pending via the `not st` branch -- which is the case worth protecting.
            nccl_src = stage_logs.get("nccl") or pr_full
            nemo_src = stage_logs.get("nemo") or pr_full
            split_stage_logs = "nccl" in stage_logs and "nemo" in stage_logs
            # A NON-EMPTY stage map that lacks one stage means that stage has genuinely never run
            # on this rack (j15/j16 on 2026-08-07: NCCL only, no NeMo job was ever submitted).
            # Guarded on a non-empty map on purpose, so the unclassifiable-log-naming case
            # described above still falls through to the probe instead of going all-pending.
            nccl_missing = bool(stage_logs) and "nccl" not in stage_logs
            nemo_missing = bool(stage_logs) and "nemo" not in stage_logs

            # Per-stage queue membership — do not paint both stages "ongoing" from one job id.
            def _jid_of(src: dict[str, Any]) -> str:
                sp = str(src.get("_path") or "")
                sbn = Path(sp).name if sp else ""
                probe = {k: v for k, v in src.items() if k not in ("_path", "_mtime")}
                return _last_job_for_log_row(sbn, probe)

            nccl_jid = _jid_of(nccl_src) if stage_logs.get("nccl") else ("-" if split_stage_logs else jid_s)
            nemo_jid = _jid_of(nemo_src) if stage_logs.get("nemo") else ("-" if split_stage_logs else jid_s)
            # Single-log racks: only the matching stage inherits the newest-log job id.
            if not split_stage_logs:
                kind_newest = _stage_kind_of_log_bn(bn)
                if kind_newest == "nccl":
                    nccl_jid, nemo_jid = jid_s, "-"
                elif kind_newest == "nemo":
                    nccl_jid, nemo_jid = "-", jid_s
                else:
                    nccl_jid = nemo_jid = jid_s
            nccl_in_slurm = bool(nccl_jid != "-" and nccl_jid in running)
            nemo_in_slurm = bool(nemo_jid != "-" and nemo_jid in running)
            # Prefer activity-tracked live jobs over a stale results-log jid (re-launch case).
            if live_nccl and live_nccl in running:
                nccl_in_slurm = True
                nccl_jid = live_nccl
            if live_nemo and live_nemo in running:
                nemo_in_slurm = True
                nemo_jid = live_nemo
            in_slurm = nccl_in_slurm or nemo_in_slurm
            live_jid = nccl_jid if nccl_in_slurm else (nemo_jid if nemo_in_slurm else jid_s)

            if in_slurm:
                tm = job_times.get(live_jid, "?")
                nemo = f"ongoing [{tm}]"
                cat = "ongoing"
            elif split_stage_logs:
                # Both stages have their own log: the rack is done only when each stage's own
                # log passes. Never read one stage's verdict off the other stage's log.
                cat = "done" if (bool(nccl_src.get("nccl_ok")) and bool(nemo_src.get("llm_ok"))) else "fail"
                nemo = cat
            elif nccl_missing or nemo_missing:
                # Exactly one stage has ever run on this rack. Judge it from its OWN log. If that
                # stage passed, the rack is INCOMPLETE (pending the other stage), not failed —
                # a stage that was never submitted is not a failure. If it failed, the rack fails.
                ran_ok = (
                    bool(nccl_src.get("nccl_ok")) if nemo_missing else bool(nemo_src.get("llm_ok"))
                )
                cat = "pending" if ran_ok else "fail"
                nemo = cat
            else:
                st = str(pr.get("nemo_state") or "").strip().lower()
                if st == "ongoing":
                    # Disk probe cache is keyed by log mtime/size only. After the Slurm job ends,
                    # we can still serve a stale probe that says "ongoing". If Slurm does not list
                    # the job, re-classify like rack_status.sh once the run is no longer queued.
                    nccl_ok = bool(pr.get("nccl_ok"))
                    llm_ok = bool(pr.get("llm_ok"))
                    cat = "done" if (nccl_ok and llm_ok) else "fail"
                    nemo = cat
                elif st == "done":
                    nemo = "done"
                    cat = "done"
                elif not st:
                    # The probe could not classify this log (it failed, timed out, or returned
                    # nothing). "Could not determine" is not evidence of a failure -- report it
                    # as not-yet-validated instead of inventing a red result.
                    nemo = "pending"
                    cat = "pending"
                else:
                    nemo = "fail"
                    cat = "fail"
            # Non-GB200 (0-node) racks stay in the table but are not tallied into the stats.
            if countable:
                if cat == "ongoing":
                    n_on += 1
                elif cat == "done":
                    n_done += 1
                elif cat == "pending":
                    n_pending += 1
                else:
                    n_fail += 1

            if not ds_done:
                ctv = "?"
            else:
                ctv = ct.get(rack, "-")

            vt = str(pr.get("validation_test") or "").strip()
            nco = nccl_src.get("nccl_ok")
            llo = nemo_src.get("llm_ok")
            nc_b = bool(nco) if isinstance(nco, (bool, int)) else None
            ll_b = bool(llo) if isinstance(llo, (bool, int)) else None
            # A stage with no log of its own has NO evidence, so its flag must be tri-state None,
            # not the False that fell through from the other stage's log. parser.sh reports
            # llm_ok=0 when handed an NCCL log and nccl_ok=0 when handed a NeMo log — it conflates
            # "absent" with "failed" in both directions, and that is what painted j15/j16 red.
            if nccl_missing:
                nc_b = None
            if nemo_missing:
                ll_b = None
            nh = validation_status_hint(
                nemo,
                live_jid if in_slurm else jid_s,
                nccl_ok=nc_b,
                llm_ok=ll_b,
                validation_test=vt or None,
            )
            stage1, stage2 = compute_stage_states(nemo, nc_b, ll_b, vt or None)
            # When one stage never ran, the rack-level cell is "pending" — but compute_stage_states
            # maps a "pending" rack to pending for BOTH stages, which would throw away the other
            # stage's real verdict (j15/j16 passed NCCL at ~916 GB/s and must still read stage1
            # done). Derive each stage from its OWN evidence instead. Placed before the in-Slurm
            # overrides below so a live job still wins.
            if nccl_missing or nemo_missing:
                stage1 = "pending" if nc_b is None else ("done" if nc_b else "fail")
                stage2 = "pending" if ll_b is None else ("done" if ll_b else "fail")
            # Override per-stage ongoing from that stage's own job id — a live Stage-1 NCCL
            # must not force Stage-2 NeMo to "running" when its job is not queued. When the
            # rack-level cell is painted ongoing from one stage's live job, demote the other
            # stage back to its probe verdict (fail/done/pending).
            if nccl_in_slurm:
                stage1 = "ongoing"
            elif stage1 == "ongoing" and not nccl_in_slurm:
                stage1 = "done" if nc_b is True else ("fail" if nc_b is False else "pending")
            if nemo_in_slurm:
                stage2 = "ongoing"
            elif stage2 == "ongoing" and not nemo_in_slurm:
                stage2 = "done" if ll_b is True else ("fail" if ll_b is False else "pending")
            if countable:
                tally_stage(stage1_summary, stage1)
                tally_stage(stage2_summary, stage2)

            rows.append(
                {
                    "rack": rack,
                    "nemo": nemo,
                    "stage1": stage1,
                    "stage2": stage2,
                    "last_job": live_jid if in_slurm else jid_s,
                    "sinfo": siv,
                    "availability_source": sinfo_source,
                    "ct_down_unk": ctv,
                    "nemo_hint": nh,
                }
            )
        summary = {
            "done": n_done,
            "fail": n_fail,
            "ongoing": n_on,
            "pending": n_pending,
            "stage1": stage1_summary,
            "stage2": stage2_summary,
        }
        legend = (
            "PythonCM rack/device inventory — availability: live Slurm; async device/log probes"
            if scheduler_runtime.is_pythoncm() and sinfo_source == "slurm"
            else "PythonCM rack/device inventory — async: PythonCM node state and log probes"
            if scheduler_runtime.is_pythoncm()
            else "rack; list (cmsh) — native table; async: ds, sinfo, log probes"
        )
        footnotes = errs[:12]
        return rows, summary, footnotes, legend


def _worker_snapshot(st: NativeRackTableState) -> dict[str, Any]:
    """Serializable thread status for /api/status (single log_dir native state)."""

    def _alive(t: threading.Thread | None) -> bool:
        return t is not None and t.is_alive()

    with st.lock:
        snapshot = {
            "mode": "native",
            "rack_count": len(st.racks),
            "workers": [
                {
                    "id": "ds",
                    "label": (
                        "Exact tray state owned by live Slurm"
                        if _sinfo_source() == "slurm"
                        else
                        "PythonCM device status"
                        if scheduler_runtime.is_pythoncm()
                        else "cmsh device status (ds)"
                    ),
                    "thread_alive": _alive(st.ds_thread),
                    "finished_once": st.ds_done,
                },
                {
                    "id": "sinfo",
                    "label": (
                        "Live Slurm node availability"
                        if _sinfo_source() == "slurm"
                        else "PythonCM node availability"
                    ),
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
        if st.availability_summary:
            snapshot["availability"] = dict(st.availability_summary)
        return snapshot


def worker_snapshot_for_log_dir(log_dir: Path) -> dict[str, Any] | None:
    """Return ``_worker_snapshot`` for *log_dir* if native state exists, else ``None``."""
    key = str(_host_resolve(log_dir))
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
    key = str(_host_resolve(log_dir))
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
    *,
    wait_for_workers: bool = False,
    worker_timeout_sec: float = 300,
) -> dict[str, Any]:
    key = str(_host_resolve(log_dir))
    st = _get_state(key, log_dir)
    st.refresh_racks_blocking()
    st.schedule_async_workers(probe_fn)
    if wait_for_workers:
        unfinished = st.wait_for_scheduled_workers(worker_timeout_sec)
        if unfinished:
            names = ", ".join(unfinished)
            raise TimeoutError(
                f"Native rack-status workers did not finish within "
                f"{worker_timeout_sec:g}s: {names}"
            )
    rows, summary, footnotes, legend = st.build_table_rows()
    out: dict[str, Any] = {
        "ok": True,
        "returncode": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "log_dir": str(_host_resolve(log_dir)),
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
        "last_errors",
        "lock",
        "log_dir",
        "log_done",
        "log_paths",
        "log_thread",
        "per_path_probe",
        "sinfo_counts",
        "sinfo_done",
        "sinfo_thread",
    )

    def __init__(self, key: str, log_dir: Path) -> None:
        self.key = key
        self.log_dir = _host_resolve(log_dir)
        self.lock = threading.RLock()
        self.log_paths: list[tuple[Path, int, int]] = []
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
            _logger.debug("_worker_sinfo_m: ignoring error", exc_info=True)
            with self.lock:
                self.last_errors.append(f"sinfo: {e}")
                self.sinfo_done = True

    def _worker_logs_m(
        self,
        probe_fn: Callable[[Path], dict[str, object] | None],
        paths: list[tuple[Path, int, int]],
    ) -> None:
        try:
            disk = _load_log_cache(_MULTIRACK_LOG_JSON)
            alive: set[str] = set()
            dirty = False
            for p, mtime_ns, size in paths:
                keyp = str(p)
                alive.add(keyp)
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
            if dirty or any(k not in alive for k in disk):
                _save_log_cache(_MULTIRACK_LOG_JSON, disk, alive)
        except Exception as e:
            _logger.debug("_worker_logs_m: ignoring error", exc_info=True)
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
        for p, _mtime_ns, _size in paths:
            # Paths are PurePosixPath (exec-host) — never .resolve() (AttributeError → HTML 500).
            # Probe cache keys are str(p) from _worker_logs_m; keep the same form here.
            keyp = str(p)
            probe = probe_map.get(keyp, {})
            lj = _last_job_for_log_row(p.name, probe)
            if lj != "-" and scheduler_runtime.valid_reference(lj):
                want.add(lj)
        augment_running_set_for_job_ids(running, job_times, want)
        rows: list[dict[str, str]] = []
        for p, _mtime_ns, _size in paths:
            bn = p.name
            keyp = str(p)
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
            vt = str(probe.get("validation_test") or "").strip() if probe else ""
            nco = probe.get("nccl_ok") if probe else None
            llo = probe.get("llm_ok") if probe else None
            nc_b = bool(nco) if isinstance(nco, (bool, int)) else None
            ll_b = bool(llo) if isinstance(llo, (bool, int)) else None
            nh = validation_status_hint(
                nemo,
                last_job,
                nccl_ok=nc_b,
                llm_ok=ll_b,
                validation_test=vt or None,
            )
            rows.append(
                {
                    # SPA reads rack / display_rack; rack_display kept for older clients.
                    "rack": rack_disp,
                    "display_rack": rack_disp,
                    "rack_display": rack_disp,
                    "log_file": bn,
                    "nemo": nemo,
                    "last_job": last_job,
                    "sinfo": siv,
                    "nemo_hint": nh,
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
                    "label": (
                        "PythonCM node availability"
                        if scheduler_runtime.is_pythoncm()
                        else "Slurm sinfo / scontrol hostnames"
                    ),
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
    key = str(_host_resolve(log_dir))
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
    key = str(_host_resolve(log_dir))
    st = _get_multirack_state(key, log_dir)
    st.refresh_paths_blocking()
    st.schedule_workers(probe_fn)
    rows = st.build_rows(rack_label_fn, rack_ids_fn)
    return {
        "ok": True,
        "log_dir": str(_host_resolve(log_dir)),
        "rows": rows,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "native_workers": _multirack_worker_snapshot(st),
    }
