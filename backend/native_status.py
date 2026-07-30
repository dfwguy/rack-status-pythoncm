#!/usr/bin/env python3
"""Build the single-rack status table without ``rack_status.sh``.

Fast path: ``cmsh -c "rack; list"`` synchronously, render rows with placeholders where needed.
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

import json
import os
import re
import shlex
import subprocess
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import host_exec
import nvis_id_templates as _nvis_ids
from platforms import get_platform as _get_platform

_PKG_DIR = Path(__file__).resolve().parent
# Runtime cache lives under repo-root var/ (parent of backend/).
_REPO_ROOT = _PKG_DIR.parent
_NATIVE_LOG_JSON = _REPO_ROOT / "var" / "cache" / "native_log_analysis.json"

# Bump when the per-log probe/parse LOGIC changes (e.g. nccl_parse.inc.sh dialect handling) so stale
# entries keyed only on log mtime/size are discarded on load instead of serving an outdated PASS/FAIL.
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

_MULTIRACK_N_RACK_RE = re.compile(r"(?:[2-9]|[1-9]\d{1,3})rack", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    try:
        v = int((os.environ.get(name) or "").strip())
        return v if v >= 0 else default
    except ValueError:
        return default


def _nemo_nccl_bn(name: str) -> bool:
    low = name.lower()
    if "nemo" in low or "nccl" in low:
        return True
    return _nvis_naming().auxiliary_single_rack_log_basename(name)


def _nemo_single_rack_bn(name: str) -> bool:
    """NEMO validation logs for the single-rack table (excludes NCCL-only multirack sweeps).

    Also accepts standalone single-rack NCCL benchmark logs (``…-NCCL-1Rack-<rack>-…``): the
    ``1rack`` marker keeps this from matching multirack NCCL sweeps (which use N-rack / comma naming).
    """
    low = name.lower()
    if "nemo" in low or "1rack" in low:
        return True
    return _nvis_naming().auxiliary_single_rack_log_basename(name)


def _is_multirack_bn(name: str) -> bool:
    if not _nemo_nccl_bn(name):
        return False
    if _MULTIRACK_N_RACK_RE.search(name):
        return True
    low = name.lower()
    naming = _nvis_naming()
    if (low.startswith("root-nemo-") or low.startswith("root-nccl-")) and naming.multirack_comma_after_rack_re().search(
        low
    ):
        return True
    return len(naming.find_rack_ids_in_string(name)) >= 2


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
    m = re.search(r"N-(\d+)\.txt$", name, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"NEMO-LLAMA3_8B-1N-.+-(\d+)\.txt$", name, re.IGNORECASE)
    return m2.group(1) if m2 else None


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
        DS_LAST_DIAG["error"] = "%s not on PATH on the %s target" % (cmd[0], DS_LAST_DIAG["target"])
        return ""
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=max(30, _env_int("RACK_NATIVE_DS_TIMEOUT_SEC", 300)),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        DS_LAST_DIAG["error"] = "%s: %s" % (type(e).__name__, e)
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


def collect_sinfo_counts() -> dict[tuple[str, str], int]:
    """Map (rack_id, trimmed_state) -> count of nodes (after hostname expansion).

    No bare-name ``which("sinfo")`` guard: the Slurm CLI is not on PATH here
    (``plat.sinfo_cmd()`` supplies the absolute path plus ``SLURM_CONF``), so that probe
    always returned None and short-circuited this function to ``{}``. Every rack then
    rendered ``sinfo: "-"``, was treated as a 0-node non-compute rack, and dropped out of
    the dashboard summary. Matches ``load_squeue_jobs_map()``, which calls
    ``run_slurm_on_host`` directly with no guard.
    """
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
                hp = host_exec.run_slurm_on_host(
                    plat.scontrol_hostnames_cmd(node),
                    timeout=max(5, _env_int("RACK_NATIVE_SCONTROL_TIMEOUT_SEC", 60)),
                )
                hosts = [h.strip() for h in (hp.stdout or "").splitlines() if h.strip()]
            except (OSError, subprocess.TimeoutExpired):
                hosts = []
        else:
            hosts = [node]
        naming = _nvis_naming()
        for h in hosts:
            hh = h.lower().split(".")[0]
            parsed = naming.sinfo_rack_from_host(hh)
            if not parsed:
                continue
            rr, _host = parsed
            key = (rr, st)
            counts[key] = counts.get(key, 0) + 1
    return counts


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
    return ",".join(f"{st}={cnt}" for cnt, st in pairs)


def _slurm_base_job_id(jid_raw: str) -> str | None:
    """Strip Slurm step suffix (``12345.batch`` → ``12345``) for lookup keys."""
    s = (jid_raw or "").strip()
    if not s:
        return None
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
            return f"Slurm job {lj} is running; the validation log is still being written."
        return "Slurm reports a running validation job; elapsed time is shown in the status label."
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
    """
    if not wanted or nemo_lower == "pending":
        return "pending"
    if nemo_lower.startswith("ongoing"):
        return "done" if ok is True else "ongoing"
    # done or fail overall: the stage passed only with a positive probe flag.
    return "done" if ok is True else "fail"


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
    if raw.get("version") != _NATIVE_LOG_CACHE_VERSION:
        return {}
    ent = raw.get("entries")
    return ent if isinstance(ent, dict) else {}


def _save_disk_log_cache(entries: dict[str, Any]) -> None:
    _NATIVE_LOG_JSON.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"version": _NATIVE_LOG_CACHE_VERSION, "entries": entries}, indent=2, sort_keys=True) + "\n"
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
    if raw.get("version") != _NATIVE_LOG_CACHE_VERSION:
        return {}
    ent = raw.get("entries")
    return ent if isinstance(ent, dict) else {}


def _save_multirack_disk_cache(entries: dict[str, Any]) -> None:
    _MULTIRACK_LOG_JSON.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"version": _NATIVE_LOG_CACHE_VERSION, "entries": entries}, indent=2, sort_keys=True) + "\n"
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
        self.log_dir = _host_resolve(log_dir)
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
                with self.lock:
                    self.per_path_probe[keyp] = rec_d
                    if rk:
                        prev = self.by_rack_log.get(rk)
                        replace = prev is None
                        if not replace and isinstance(prev, dict):
                            replace = m_this >= float(prev.get("_mtime") or 0.0)
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

    def build_table_rows(self) -> tuple[list[dict[str, str]], dict[str, Any], list[str], str]:
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
        stage1_summary = empty_stage_summary()
        stage2_summary = empty_stage_summary()
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
            # A rack with 0 Slurm nodes (once sinfo is done) is a non-GB200 rack
            # (switch / mgmt / networking) — keep it in the table/floor but exclude it
            # from the validation statistics so the counts reflect only compute racks.
            countable = not (sinfo_done and rack_total_sinfo_nodes(rack, sct) == 0)
            pr_full = by_rack.get(rack)
            if not pr_full:
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
                        "sinfo": "?" if not sinfo_done else format_sinfo_cell(rack, sct),
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
            in_slurm = bool(jid_s != "-" and jid_s in running)

            if in_slurm:
                tm = job_times.get(jid_s, "?")
                nemo = f"ongoing [{tm}]"
                cat = "ongoing"
            else:
                st = str(pr.get("nemo_state") or "fail").strip().lower()
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
                else:
                    nemo = "fail"
                    cat = "fail"
            # Non-GB200 (0-node) racks stay in the table but are not tallied into the stats.
            if countable:
                if cat == "ongoing":
                    n_on += 1
                elif cat == "done":
                    n_done += 1
                else:
                    n_fail += 1

            siv = "?" if not sinfo_done else format_sinfo_cell(rack, sct)
            if not ds_done:
                ctv = "?"
            else:
                ctv = ct.get(rack, "-")

            vt = str(pr.get("validation_test") or "").strip()
            nco = pr.get("nccl_ok")
            llo = pr.get("llm_ok")
            nc_b = bool(nco) if isinstance(nco, (bool, int)) else None
            ll_b = bool(llo) if isinstance(llo, (bool, int)) else None
            nh = validation_status_hint(
                nemo,
                jid_s,
                nccl_ok=nc_b,
                llm_ok=ll_b,
                validation_test=vt or None,
            )
            stage1, stage2 = compute_stage_states(nemo, nc_b, ll_b, vt or None)
            if countable:
                tally_stage(stage1_summary, stage1)
                tally_stage(stage2_summary, stage2)

            rows.append(
                {
                    "rack": rack,
                    "nemo": nemo,
                    "stage1": stage1,
                    "stage2": stage2,
                    "last_job": jid_s,
                    "sinfo": siv,
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
) -> dict[str, Any]:
    key = str(_host_resolve(log_dir))
    st = _get_state(key, log_dir)
    st.refresh_racks_blocking()
    st.schedule_async_workers(probe_fn)
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
            with self.lock:
                self.last_errors.append(f"sinfo: {e}")
                self.sinfo_done = True

    def _worker_logs_m(
        self,
        probe_fn: Callable[[Path], dict[str, object] | None],
        paths: list[tuple[Path, int, int]],
    ) -> None:
        try:
            disk = _load_multirack_disk_cache()
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
