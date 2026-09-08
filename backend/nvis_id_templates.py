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
    r"\[\\d\+\]|\[(\d{2})-(\d{2})\]|\[(?:A-Z|a-z)\]",
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


# ---------------------------------------------------------------------------
# Log basename eligibility (universal ingest)
#
# These predicates decide which files in the job log directory the dashboard
# table, Open Log, parser-results and the exports are allowed to CONSIDER. They
# say nothing about rack attribution: ``rack_id_from_log_basename()`` still
# requires exactly one rack id in the basename, so widening here cannot
# mis-attribute a log.
#
# Default mode is "open": accept any plausible result log (known extension,
# unknown extension, or no extension at all) and reject only genuine noise.
# Set NVIS_LOG_BASENAME_MODE=strict to restore the legacy marker allowlist.
# ---------------------------------------------------------------------------

# Canonical tool -> basename substrings. Used for strict mode, for
# ``log_tool_from_basename()`` (so callers can rank a real benchmark log above
# an incidental one) and to seed ``_AUX_LOG_MARKERS``.
_LOG_TOOL_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clusterkit", ("root-ck-", "clusterkit")),
    ("mnubergemm", ("mnubergemm", "ubergemm")),
    ("nvbandwidth", ("nvbandwidth", "nvbw")),
    ("nvloom", ("nvloom",)),
    ("hpl", ("hpl", "linpack", "hpcg")),
    ("dcgm", ("dcgm", "dcgmi")),
    ("nvswitch", ("nvswitch", "nvsw", "fabricmanager", "nvlink")),
    ("imex", ("imex",)),
    ("sysinfo", ("sysinfo", "nvidia-bug-report", "nvsm", "topo")),
    ("healthcheck", ("healthcheck", "health-check", "health_check", "nvhealth")),
    ("bringup", ("bringup", "bring-up")),
    ("nemo", ("nemo", "nemotron", "megatron")),
    ("nccl", ("nccl", "allreduce", "all_reduce", "all-reduce", "alltoall",
              "all_to_all", "alltoallv", "allgather", "all_gather",
              "reducescatter", "reduce_scatter", "sendrecv", "busbw")),
    ("llmb", ("llmb", "llama", "llm-benchmark")),
    ("nvlmapper", ("nvlmapper",)),
    ("gpuburn", ("gpuburn", "gpu-burn", "gpu_burn", "burnin", "burn-in")),
    ("stress", ("stress", "stressapptest", "memtest", "soak")),
    ("gemm", ("gemm", "cublas", "matmul")),
    ("perftest", ("ib_write", "ib_read", "ib_send", "perftest", "osu_", "mlxlink")),
    ("storage", ("fio", "iozone", "ior", "mdtest")),
    ("network", ("iperf", "ethtool", "lldp", "ufm")),
    ("slurm", ("slurm-", "sacct", "sbatch", "squeue")),
    ("benchmark", ("benchmark", "bench-", "-bench", "validation", "validate", "diag", "smoke")),
    ("system", ("dmesg", "syslog", "console", "boot", "ipmi", "bmc", "sel-")),
)

# Backwards-compatible flat marker tuple. The original 14 markers are still the
# first entries; everything the tool table knows about is appended.
_AUX_LOG_MARKERS_DEFAULT: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            "root-ck-",
            "clusterkit",
            "mnubergemm",
            "ubergemm",
            "nvbandwidth",
            "nvloom",
            "hpl",
            "dcgm",
            "nvswitch",
            "imex",
            "sysinfo",
            "healthcheck",
            "health-check",
            "bringup",
        )
        + tuple(m for _tool, ms in _LOG_TOOL_MARKERS for m in ms)
    )
)

# Compression wrappers stripped before looking at the real extension
# (``nvidia-bug-report.log.gz`` -> ``.log``).
_LOG_COMPRESSION_EXTS: frozenset[str] = frozenset(
    {"gz", "bz2", "xz", "zst", "zstd", "lz4", "lzma", "br"}
)

# Extensions that are unambiguously a result/report artifact.
_LOG_RESULT_EXTS: frozenset[str] = frozenset(
    {
        "log", "out", "txt", "err", "json", "jsonl", "ndjson", "csv", "tsv",
        "yaml", "yml", "xml", "md", "rpt", "report", "results", "res",
        "stdout", "stderr", "dat", "data", "prof", "trace", "metrics", "tbl",
        "xls", "xlsx", "nsys-rep", "qdrep", "sqlite3",
    }
)

# Dot-components that mark a file as editor/OS/backup noise anywhere in the name
# (``app.py.bak.20260728``, ``run.txt.swp``, ``x.tmp``).
_LOG_NOISE_COMPONENTS: frozenset[str] = frozenset(
    {
        "bak", "backup", "bkp", "tmp", "temp", "swp", "swo", "swn", "swx",
        "lock", "pid", "part", "partial", "crdownload", "download", "orig",
        "rej", "save", "dpkg-dist", "dpkg-new", "dpkg-old", "rpmsave",
        "rpmnew", "sock", "inprogress", "writing",
    }
)

# Executables / archives / media / binaries: never a readable result log.
_LOG_BINARY_EXTS: frozenset[str] = frozenset(
    {
        "sh", "bash", "zsh", "ksh", "csh", "fish", "py", "pyc", "pyo", "pyd",
        "pl", "rb", "lua", "js", "ts", "tsx", "jsx", "c", "h", "cc", "cpp",
        "hpp", "go", "rs", "java", "class", "jar", "so", "o", "a", "ko", "ko.xz",
        "dll", "exe", "bin", "img", "iso", "efi", "rom", "fw",
        "tar", "tgz", "tbz", "tbz2", "txz", "zip", "7z", "rar", "cpio", "ar",
        "rpm", "deb", "whl", "egg", "apk", "dmg", "pkg",
        "png", "jpg", "jpeg", "gif", "bmp", "svg", "ico", "webp", "tif", "tiff",
        "pdf", "ps", "eps", "ttf", "otf", "woff", "woff2", "eot",
        "mp3", "mp4", "mkv", "mov", "avi", "webm", "wav", "flac",
        "db", "sqlite", "pem", "key", "crt", "cer", "pfx", "gpg", "asc", "sig",
        "css", "scss", "html", "htm", "map", "wasm", "ttc", "core",
    }
)

_EDITOR_NOISE_RE = re.compile(r"(?:~$|^#.*#$|^\.#)")

# ``4Rack``/``36Rack`` (N >= 2) marker — mirrors ``_MULTIRACK_N_RACK_RE`` in app.py and
# native_status.py. Used only to refuse single-rack attribution of a multi-rack sweep.
_MULTIRACK_N_RACK_RE = re.compile(r"(?:[2-9]|[1-9]\d{1,3})rack", re.IGNORECASE)

# The COMPACTED tag run-nemo-xrack.sh writes when the full rack list would overflow NAME_MAX:
#   <first-rack>+<last-rack>+<N>r-<hash>      e.g. …-e01+ink50-dh01-h22+32r-a811542455-576N-…
# It kicks in past 120 characters of rack tag, so roughly every run of eight racks or more.
# Only two rack ids survive in the name, which is why counting ids reported a 32-rack, 576-node
# run as TWO racks and parked it in the Rack=2 bucket of the analytics scaling table.
_MULTIRACK_COMPACT_RE = re.compile(r"\+(\d{1,4})r-[0-9a-f]{6,}", re.IGNORECASE)


def _log_basename_mode() -> str:
    """``open`` (default: accept any plausible log) or ``strict`` (legacy marker allowlist)."""
    v = (os.environ.get("NVIS_LOG_BASENAME_MODE") or "").strip().lower()
    return "strict" if v in ("strict", "markers", "legacy", "allowlist") else "open"


def _env_marker_list(var: str) -> list[str]:
    raw = (os.environ.get(var) or "").strip()
    if not raw:
        return []
    return [m.strip().lower() for m in raw.replace(";", ",").split(",") if m.strip()]


def _basename_only(name: str) -> str:
    """Last path component of *name*, whitespace-stripped (accepts POSIX or Windows separators)."""
    s = (name or "").strip().replace("\\", "/")
    while s.endswith("/"):
        s = s[:-1]
    return s.rsplit("/", 1)[-1]


def _split_log_ext_chain(base: str) -> tuple[str, list[str]]:
    """Split ``foo.log.gz`` into (``foo``, ``['log', 'gz']``). Leading dot is not a separator."""
    if not base:
        return "", []
    lead = ""
    body = base
    while body.startswith("."):
        lead += "."
        body = body[1:]
    parts = body.split(".")
    return lead + parts[0], [p.lower() for p in parts[1:]]


def is_log_noise_basename(name: str) -> bool:
    """True for files that are never a result log: dotfiles, editor swap/autosave, backups, temps.

    Directories are not distinguishable from a basename alone; callers list files
    only. A trailing ``/`` (or an empty / ``.`` / ``..`` name) is treated as noise.
    """
    raw = (name or "").strip()
    if not raw or raw.endswith(("/", "\\")):
        return True
    base = _basename_only(raw)
    if not base or base in (".", ".."):
        return True
    if base.startswith("."):
        return True
    if _EDITOR_NOISE_RE.search(base):
        return True
    _stem, exts = _split_log_ext_chain(base)
    return bool(any(e in _LOG_NOISE_COMPONENTS for e in exts))


def _effective_log_exts(exts: list[str]) -> list[str]:
    """Extension chain with trailing compression wrappers removed."""
    out = list(exts)
    while out and out[-1] in _LOG_COMPRESSION_EXTS:
        out.pop()
    return out


def is_plausible_log_basename(name: str) -> bool:
    """True when *name* could be a job/benchmark result log worth considering.

    Accepts ``.log .out .txt .err .json .csv`` (and ``.gz``/``.xz``/``.zst``
    variants), Slurm's default ``slurm-<jobid>.out``, unknown extensions and
    extension-less names. Rejects only genuine noise (dotfiles, editor swap
    files, ``*.bak`` / ``*.tmp`` / ``*.lock``) and binary/archive/source/media
    extensions that can never be parsed as text.

    Escape hatches: ``NVIS_EXTRA_LOG_MARKERS`` force-accepts any basename
    containing one of its comma-separated substrings; ``NVIS_LOG_EXCLUDE_MARKERS``
    force-rejects.
    """
    raw = (name or "").strip()
    base = _basename_only(raw)
    # Hard rejects that no escape hatch may override: empty, ``.``/``..`` and
    # anything that names a directory (trailing separator).
    if not base or base in (".", "..") or raw.endswith(("/", "\\")):
        return False
    low = base.lower()
    for m in _env_marker_list("NVIS_LOG_EXCLUDE_MARKERS"):
        if m in low:
            return False
    forced = any(m in low for m in _env_marker_list("NVIS_EXTRA_LOG_MARKERS"))
    if forced:
        return True
    if is_log_noise_basename(base):
        return False
    _stem, exts = _split_log_ext_chain(base)
    eff = _effective_log_exts(exts)
    if not eff:
        # No extension left: bare name, or a pure archive like ``run.gz``.
        return True
    ext = eff[-1]
    if ext in _LOG_RESULT_EXTS:
        return True
    if ext in _LOG_BINARY_EXTS:
        return False
    if ext.isdigit():
        # Rotated log (``messages.1``, ``run.out.2``): keep if what precedes it is a log ext.
        prev = eff[-2] if len(eff) >= 2 else ""
        return prev not in _LOG_BINARY_EXTS
    # Unknown extension (e.g. ``run.mnub``, ``h22.results2``) -> accept.
    return True


def log_tool_from_basename(name: str) -> str | None:
    """Canonical tool token for *name* (``mnubergemm``, ``nccl``, ``hpl``, …) or ``None``.

    Purely advisory: callers may use it to rank a genuine benchmark log above an
    incidental one (e.g. when picking the newest log per rack). It is NOT a gate.
    """
    low = _basename_only(name).lower()
    if not low:
        return None
    for tool, markers in _LOG_TOOL_MARKERS:
        if any(m in low for m in markers):
            return tool
    return None


def is_known_tool_log_basename(name: str) -> bool:
    """Legacy strict predicate: basename contains a known tool marker (or NVIS_EXTRA_LOG_MARKERS)."""
    low = _basename_only(name).lower()
    if not low:
        return False
    markers = list(_AUX_LOG_MARKERS_DEFAULT) + _env_marker_list("NVIS_EXTRA_LOG_MARKERS")
    return any(m in low for m in markers)


# ---------------------------------------------------------------------------
# Log-basename classification (single source of truth)
#
# app.py and native_status.py each used to carry their own copy of these four
# predicates plus a verbatim duplicate of ``_MULTIRACK_N_RACK_RE``. They classify
# which table a log belongs to, so a drifted copy shows a rack in one view and not
# the other. They live here because this module already owns the naming templates
# these predicates depend on.
#
# ``naming`` is a parameter rather than a module lookup because callers do not share
# one instance: native_status builds its NvisNaming from its own job-env-override
# subset, app.py from the process env. Passing it keeps each caller's behaviour exact.
# ---------------------------------------------------------------------------


def nemo_nccl_in_log_basename(name: str, naming: NvisNaming | None = None) -> bool:
    """Logs with NEMO or NCCL in the basename (multirack tab, exports, legacy shell table)."""
    low = (name or "").lower()
    if "nemo" in low or "nccl" in low:
        return True
    return (naming or get_nvis_naming()).auxiliary_single_rack_log_basename(name)


def nemo_single_rack_log_basename(name: str, naming: NvisNaming | None = None) -> bool:
    """Single-rack validation logs for the dashboard table / Open Log.

    Also accepts standalone single-rack NCCL benchmark logs (``…-NCCL-1Rack-<rack>-…``); the
    ``1rack`` marker is what excludes multirack NCCL sweeps (N-rack / comma naming).
    """
    low = (name or "").lower()
    if "nemo" in low or "1rack" in low:
        return True
    return (naming or get_nvis_naming()).auxiliary_single_rack_log_basename(name)


def stage_kind_of_log_basename(name: str) -> str | None:
    """Which validation stage a single-rack log carries evidence for: ``nccl`` | ``nemo`` | None.

    Stage 1 (NCCL) and stage 2 (NeMo) are separate Slurm jobs and therefore land in separate
    logs. Each stage must be judged from the newest log of ITS OWN kind, otherwise whichever
    job finished last decides both stages and the other one reads as a failure.
    """
    low = (name or "").lower()
    if "-nccl-1rack-" in low:
        return "nccl"
    if "-nemo-1rack-" in low:
        return "nemo"
    return None


def is_multirack_log_basename(name: str, naming: NvisNaming | None = None) -> bool:
    """True for multi-rack logs: excluded from the single-rack table, listed in the multirack tab."""
    n = naming or get_nvis_naming()
    if not nemo_nccl_in_log_basename(name, n):
        return False
    if _MULTIRACK_N_RACK_RE.search(name or ""):
        return True
    low = (name or "").lower()
    # run-nemo-xrack.sh: root-NEMO-p5-r[…]-ct[…],p5-r[…]-ct[…]-<n>N-<jid>.txt
    if (low.startswith(("root-nemo-", "root-nccl-"))) and (
        n.multirack_comma_after_rack_re().search(low)
    ):
        return True
    return len(n.find_rack_ids_in_string(name)) >= 2


def multirack_n_rack_marker(name: str) -> str | None:
    """The ``4Rack`` / ``36Rack`` marker in *name*, or ``None``.

    Exposed so callers do not reach for the module-private regex and end up copying it.
    """
    m = _MULTIRACK_N_RACK_RE.search(name or "")
    return m.group(0) if m else None


def multirack_rack_count(name: str) -> int | None:
    """Rack count declared by *name*, in either dialect, or ``None`` if it declares neither.

    Two spellings exist and only one was understood:
      * ``36Rack``  — the long form
      * ``+32r-<hash>`` — the compacted form, used once the full rack list would overflow the
        255-byte filename limit

    Callers fall back to counting rack ids in the name, which is right for the long form and
    badly wrong for the compacted one: it keeps only the first and last rack, so every large run
    counted as two.
    """
    base = _basename_only(name or "")
    m = _MULTIRACK_COMPACT_RE.search(base)
    if m:
        try:
            return max(1, int(m.group(1), 10))
        except ValueError:
            return None
    tok = multirack_n_rack_marker(base)
    if tok:
        try:
            return max(1, int(tok[:-4], 10))
        except ValueError:
            return None
    return None


def job_id_from_log_basename(name: str) -> str | None:
    """Slurm job id from a validation log basename, or ``None``.

    Llama stdout is checked first: ``…NEMO-LLAMA3_8B-1N-<host>-<jid>.txt`` has no ``N-<jid>``
    tail, and a hostname ending in ``N`` would otherwise make the generic pattern ambiguous.
    """
    base = _basename_only(name)
    m = re.search(r"NEMO-LLAMA3_8B-1N-.+-(\d+)\.txt$", base, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"N-(\d+)\.txt$", base, re.IGNORECASE)
    if m2:
        return m2.group(1)
    # Other tools (MNUBERGEMM, nvbandwidth, …): plain trailing -<jid>.txt. This branch existed only
    # in the shell jobid_from_fname, so the two disagreed on exactly those names.
    m3 = re.search(r"-(\d+)\.txt$", base, re.IGNORECASE)
    if m3:
        return m3.group(1)
    # MPI launcher stdout: nccl-mpi_<rack>+<rack>_<jid>.out — underscore separator, .out extension.
    m4 = re.search(r"[_-](\d+)\.out$", base, re.IGNORECASE)
    return m4.group(1) if m4 else None


def node_count_from_log_basename(name: str) -> int | None:
    """Node count from a ``-<N>N-`` / ``-<N>N.txt`` basename field, or ``None``.

    Load-bearing: it is how an 18-node acceptance run is told from a 1-node smoke test that
    otherwise shares the same filename shape.
    """
    base = _basename_only(name)
    m = re.search(r"-(\d+)N-\d", base, re.IGNORECASE) or re.search(
        r"-(\d+)N\.txt$", base, re.IGNORECASE
    )
    if not m:
        return None
    try:
        return int(m.group(1), 10)
    except ValueError:
        return None


def node_count_suffix_from_basename(name: str) -> str:
    """`` (36N)`` for display when the basename carries a node count, else ``""``."""
    n = node_count_from_log_basename(name)
    return f" ({n}N)" if n is not None else ""


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
        elif m.group(1) is None:
            # Single-letter slot (``[A-Z]`` / ``[a-z]``) for sites with row-letter
            # racks (e.g. ``bos01-a05`` vs ``bos01-b05``). Matched case-insensitively.
            segments.append(("slot", str(slot_i), "alpha"))
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


def _rebuild_through_slot(parsed: ParsedTemplate, values: list[str], up_to: int) -> str:
    """Rebuild the template literal/slot string up to and including slot ``up_to``.

    Trailing separators (``-_/:.``) are stripped so the resulting grouping key is
    stable (e.g. ``bos01-a`` rather than ``bos01-a-``). Returns ``""`` when
    ``up_to`` is negative (i.e. the requested level does not exist).
    """
    if up_to < 0:
        return ""
    out: list[str] = []
    slot_i = 0
    for seg in parsed.segments:
        if seg[0] == "lit":
            out.append(seg[1])
        else:
            if slot_i > up_to:
                break
            idx = int(seg[1])
            out.append(values[idx] if idx < len(values) else "")
            slot_i += 1
            if slot_i > up_to:
                break
    return re.sub(r"[-_/:.]+$", "", "".join(out))


def _format_slot_groups(parsed: ParsedTemplate, groups: Iterable[str]) -> list[str]:
    """Map regex capture groups to canonical slot strings (zero-pad only for ``range2`` slots)."""
    out: list[str] = []
    for slot_i, g in enumerate(groups):
        kind = parsed.slot_kind(slot_i)
        if kind in ("digits", "alpha"):
            out.append(g)
        else:
            out.append(g.zfill(2))
    return out


def _slot_regex_pat(kind: SlotKind, *, loose: bool) -> str:
    if kind == "digits":
        return r"(\d+)"
    if kind == "alpha":
        return r"([A-Za-z])"
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
class RackParts:
    """A rack id decomposed into its datacenter hierarchy (section -> row -> rack).

    Levels are derived from the trailing slots of ``NVIS_RACK_ID``: the last slot
    is the rack, the second-to-last the row, the third-to-last the section. Sites
    with fewer slots simply collapse the higher levels (Boston ``bos01-[A-Z][01-99]``
    -> row letter + rack; Bright ``p[\\d+]-r[\\d+]`` -> pod-as-row + rack).
    """

    rack_id: str
    section_key: str
    section_label: str
    row_key: str
    row_label: str
    rack_label: str
    sort_key: tuple[int, ...]


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
            "node_id_mode": (
                "rack_ct_suffix"
                if self.ct_template.slot_count > self.rack_template.slot_count
                else "full_hostname"
            ),
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
        # Letter slots (``[A-Z]``) sort by their character ordinal so row ``a`` precedes ``b``.
        return tuple(int(g, 10) if g.isdigit() else ord(g.lower()) for g in m.groups()) + (0,)

    def is_r00_rack(self, rid: str) -> bool:
        c = self.canonical_rack_id(rid)
        if not c:
            return False
        m = self.rack_re.fullmatch(c)
        if not m or not m.groups():
            return False
        last = m.groups()[-1]
        return last.isdigit() and int(last, 10) == 0

    def rack_parts(self, rid: str) -> RackParts | None:
        """Decompose a rack id into section/row/rack parts using the rack template.

        This is the single authoritative decomposition consumed by the dashboard
        hierarchy (floor view, etc.) so the SPA never re-implements template
        parsing. Returns ``None`` when *rid* does not match ``NVIS_RACK_ID``.
        """
        canon = self.canonical_rack_id(rid)
        if not canon:
            return None
        m = self.rack_scan_re.fullmatch(canon)
        if not m:
            return None
        values = _format_slot_groups(self.rack_template, list(m.groups()))
        n = len(values)
        rack_label = values[-1] if n >= 1 else canon
        row_label = values[-2] if n >= 2 else ""
        row_key = _rebuild_through_slot(self.rack_template, values, n - 2) if n >= 2 else canon
        section_key = _rebuild_through_slot(self.rack_template, values, n - 3) if n >= 3 else ""
        section_label = section_key or ("Cluster" if n >= 2 else canon)
        return RackParts(
            rack_id=canon,
            section_key=section_key,
            section_label=section_label,
            row_key=row_key,
            row_label=row_label,
            rack_label=rack_label,
            sort_key=self.rack_sort_key(canon),
        )

    def build_hierarchy(self, rack_ids: Iterable[str]) -> dict:
        """Group rack ids into a ``section -> row -> rack`` tree (JSON-serializable).

        The single source of truth for datacenter layout: any surface that needs
        rows/sections consumes this instead of parsing rack ids itself. Racks that
        don't match ``NVIS_RACK_ID`` are returned under ``unmatched`` (never
        silently dropped). ``levels`` lists which grouping levels are meaningful
        for the active naming (``section`` is omitted for 2-slot templates such as
        Boston's ``bos01-[A-Z][01-99]``).
        """
        sections: dict[str, dict] = {}
        rows: dict[tuple[str, str], dict] = {}
        unmatched: list[str] = []
        has_section = False
        has_row = False
        for rid in rack_ids:
            parts = self.rack_parts(rid)
            if parts is None:
                unmatched.append(rid)
                continue
            if parts.section_key:
                has_section = True
            if parts.row_key != parts.rack_id:
                has_row = True
            sec = sections.get(parts.section_key)
            if sec is None:
                sec = {"key": parts.section_key, "label": parts.section_label, "rows": []}
                sections[parts.section_key] = sec
            rkey = (parts.section_key, parts.row_key)
            row = rows.get(rkey)
            if row is None:
                row = {"key": parts.row_key, "label": parts.row_label, "racks": []}
                rows[rkey] = row
                sec["rows"].append(row)
            row["racks"].append({"rack": parts.rack_id, "label": parts.rack_label})

        def _row_key(row: dict) -> tuple[int, ...]:
            return self.rack_sort_key(row["racks"][0]["rack"]) if row["racks"] else (10**9,)

        for row in rows.values():
            row["racks"].sort(key=lambda r: self.rack_sort_key(r["rack"]))
        for sec in sections.values():
            sec["rows"].sort(key=_row_key)
        ordered = sorted(
            sections.values(),
            key=lambda s: _row_key(s["rows"][0]) if s["rows"] else (10**9,),
        )
        levels: list[str] = []
        if has_section:
            levels.append("section")
        if has_row:
            levels.append("row")
        levels.append("rack")
        return {"levels": levels, "sections": ordered, "unmatched": unmatched}

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

    # Tool markers that identify a single-rack validation log by basename. Kept as a field for
    # backwards compatibility (and used by ``NVIS_LOG_BASENAME_MODE=strict``); in the default
    # "open" mode eligibility no longer depends on this list — see
    # ``is_plausible_log_basename()``. Extend at runtime with
    # NVIS_EXTRA_LOG_MARKERS="foo,bar" (comma-separated, case-insensitive).
    _AUX_LOG_MARKERS: tuple[str, ...] = _AUX_LOG_MARKERS_DEFAULT

    def auxiliary_single_rack_log_basename(self, name: str) -> bool:
        """True for any basename that could be a single-rack result log.

        This is the ONE predicate all three basename gates funnel through
        (``_nemo_single_rack_log_basename``, ``_nemo_nccl_in_log_basename``,
        ``rack_id_from_log_basename``), so it decides what the dashboard table,
        Open Log, parser-results and the exports will even consider.

        Default (``NVIS_LOG_BASENAME_MODE`` unset / ``open``): accept ANY plausible
        result log — known tools (MNUBERGEMM, ClusterKit, nvbandwidth, HPL, DCGM,
        NVLoom, bringup, NCCL, …), unknown tools, Slurm's default
        ``slurm-<jobid>.out``, and ``.log/.out/.txt/.err/.json/.csv`` plus their
        compressed variants. Only genuine noise is rejected (dotfiles, editor swap
        files, ``*.bak``/``*.tmp``/``*.lock``, binaries/archives/media).

        Set ``NVIS_LOG_BASENAME_MODE=strict`` to fall back to the legacy tool-marker
        allowlist.

        Rack attribution is unchanged and independent: the caller still requires
        EXACTLY ONE rack id in the basename, and multirack / Llama basenames are
        excluded upstream, so widening this cannot pull in a multi-rack sweep or
        mis-attribute a log to the wrong rack.
        """
        if _log_basename_mode() == "strict":
            markers = list(self._AUX_LOG_MARKERS) + _env_marker_list("NVIS_EXTRA_LOG_MARKERS")
            low = _basename_only(name).lower()
            return bool(low) and any(m in low for m in markers)
        return is_plausible_log_basename(name)

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
        """Rack id from a single-rack result log basename (1Rack-, auxiliary e.g. root-CK-…).

        Excludes Llama Slurm stdout (``…NEMO-LLAMA3_8B-1N-…``) and any sweep that
        names two or more racks. NCCL sweeps used to be excluded as well, as a
        side effect of the marker allowlist; since that predicate was widened to
        accept any plausible result log, an NCCL log naming exactly one rack is
        attributed to it like any other tool's.
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
        # A sweep that names N >= 2 racks in its basename (``…-4Rack-…``) must never be
        # attributed to one rack even when only one rack id is spelled out. Guards the
        # widened auxiliary predicate; ``1Rack``/single-rack names are unaffected.
        if _MULTIRACK_N_RACK_RE.search(_basename_only(name)):
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
