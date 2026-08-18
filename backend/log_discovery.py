# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Finding the log file that belongs to a rack, on the execution host.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Given a rack (or a multirack run) and a log directory, work out which file holds its output. Harder
than it sounds, and the reason this is its own module: a rack accumulates many runs, the newest is not
always the one wanted, a full-rack run and a partial one are told apart only by the node count in the
basename, and the directory lives on the head node so every listing is an SSH round trip.

Reads identity off basenames via ``naming``; does not re-derive it. The rule from
``cluster-naming-conventions.mdc`` applies here too -- never infer a rack's files from a
``<rack>-[01-NN]`` guess, because the guess does not resolve on sites whose hostnames are shaped
differently.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import host_exec
import native_status
from core import rpath as _rpath
from naming import (
    _full_rack_expected_nodes,
    _is_multirack_log_basename,
    _nemo_nccl_in_log_basename,
    _nemo_single_rack_log_basename,
    _node_count_from_log_basename,
    _normalize_rack_id,
    _nvis_naming,
)

_logger = logging.getLogger(__name__)


def _resolve_nemo_log_basename_in_dir(log_dir: Path, basename: str) -> Path | None:
    """Return path to ``basename`` under log_dir root or failed/; None if missing or unsafe."""
    bn = Path(basename).name
    if bn != basename.strip() or ".." in bn or "/" in bn or "\\" in bn:
        return None
    if not _nemo_nccl_in_log_basename(bn):
        return None
    # The log dir belongs to the execution host, so existence is asked of the
    # connection. Doing it locally used to make every lookup on a remote target
    # return "log file not found" for files that plainly existed.
    root = _rpath(log_dir)
    for sub in (root, root / "failed", root / "sn-llama"):
        cand = _rpath(sub / bn)
        try:
            cand.relative_to(root)
        except ValueError:
            continue
        if host_exec.is_file(cand):
            return cand
    return None


def find_log_paths_for_rack(log_dir: Path, rack: str) -> list[Path]:
    """Paths to single-rack validation logs for *rack* under ``log_dir`` and ``failed/`` (newest first).

    Scans ``*.txt`` and keeps files whose basename maps to exactly this rack via
    ``rack_id_from_log_basename`` (agnostic to pod/rack zero-padding in filenames, e.g. ``p5-r01``
    vs dashboard ``p05-r01``). Excludes multirack and Llama Slurm stdout basenames.
    """
    rk = _normalize_rack_id(rack)
    naming = _nvis_naming()
    # Route log discovery through host_exec (SSH when remote, local when on-node) so parser-results
    # works whether the app runs on a laptop (SSH) or on the head node — same abstraction as scripts.
    base_root = str(native_status._host_resolve(log_dir)).rstrip("/")
    seen: set[str] = set()
    scored: list[tuple[float, Path]] = []
    for base in (base_root, base_root + "/failed"):
        for path, mtime_ns, _size in native_status._host_log_entries(base):
            name = os.path.basename(path)
            if not _nemo_single_rack_log_basename(name):
                continue
            if naming.is_llama_slurm_stdout_basename(name):
                continue
            if _is_multirack_log_basename(name):
                continue
            if naming.rack_id_from_log_basename(name) != rk:
                continue
            if path in seen:
                continue
            seen.add(path)
            scored.append((mtime_ns / 1_000_000_000, native_status._host_path(path)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [p for _m, p in scored]


def pick_log_for_rack(
    log_dir: Path,
    rack: str,
    job_id: str | None,
    *,
    full_rack: bool = False,
    full_rack_nodes: int | None = None,
) -> Path | None:
    """Pick a single-rack validation log for *rack*.

    Default: newest matching log (by mtime); if *job_id* is set, prefer that job's file.

    When *full_rack* is true, only consider logs whose basename encodes the full-rack
    node count (``-18N-`` by default / ``RACK_FULL_RACK_NODES``). A row job_id that
    points at a partial run (e.g. ``-1N-``) is ignored so the dialog can still surface
    the latest full-rack acceptance log.
    """
    cands = find_log_paths_for_rack(log_dir, rack)
    if not cands:
        return None
    if full_rack:
        need = int(full_rack_nodes) if full_rack_nodes and full_rack_nodes > 0 else _full_rack_expected_nodes()
        cands = [p for p in cands if _node_count_from_log_basename(p.name) == need]
        if not cands:
            return None
    j = (job_id or "").strip()
    if j.isdigit():
        suf = f"N-{j}.txt"
        for p in cands:
            if p.name.endswith(suf):
                return p
        # full_rack: job_id may be a partial (1N) sweep — fall through to newest full-rack log
        if not full_rack:
            return cands[0]
    return cands[0]


def pick_logs_for_rack(
    log_dir: Path,
    rack: str,
    job_id: str | None,
    *,
    full_rack: bool = False,
    full_rack_nodes: int | None = None,
) -> dict[str, Path]:
    """One log per validation stage for *rack*: ``{"nccl": path, "nemo": path}``.

    Since the move to llmb-run, stage 1 (NCCL) and stage 2 (NeMo) are separate Slurm jobs
    writing separate logs, so a single job id can only ever address ONE of them. Picking one
    file for the whole dialog therefore guaranteed that one stage's tab was built from the
    other stage's log — which is how a NeMo log came to be charted as an NCCL result.

    An *job_id* pins the stage whose log carries it; the other stage still resolves to its own
    newest log. On the older combined format (one job, both stages in one file) both keys
    resolve to the SAME path, and the caller is expected to parse it once — see
    ``_parser_stage_sources``.

    Stages are identified by ``native_status._stage_kind_of_log_bn`` so this and the dashboard
    can never disagree about which file speaks for which stage.
    """
    cands = find_log_paths_for_rack(log_dir, rack)
    if not cands:
        return {}
    if full_rack:
        need = int(full_rack_nodes) if full_rack_nodes and full_rack_nodes > 0 else _full_rack_expected_nodes()
        cands = [p for p in cands if _node_count_from_log_basename(p.name) == need]
        if not cands:
            return {}

    j = (job_id or "").strip()
    suf = f"N-{j}.txt" if j.isdigit() else None
    out: dict[str, Path] = {}
    # cands is newest-first, so the first hit per stage is that stage's newest log.
    for p in cands:
        kind = native_status._stage_kind_of_log_bn(p.name)
        if kind and kind not in out:
            out[kind] = p
    # An explicit job_id outranks "newest" for its own stage only.
    if suf:
        for p in cands:
            if p.name.endswith(suf):
                kind = native_status._stage_kind_of_log_bn(p.name)
                if kind:
                    out[kind] = p
                break
    if out:
        return out
    # Nothing carried a stage marker in its name (foreign or legacy naming). Fall back to the
    # single-log behaviour so those callers keep working exactly as before.
    one = pick_log_for_rack(log_dir, rack, job_id, full_rack=full_rack, full_rack_nodes=full_rack_nodes)
    return {"nemo": one} if one else {}
