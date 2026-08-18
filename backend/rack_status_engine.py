# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The rack-status engine: producing the canonical fleet dataset every surface renders.

``/api/status`` and ``/api/cluster-overview`` are two views of ONE dataset, and the exports are a third.
Before this module they shared 41 helpers inside ``app.py``, which is why none of those routes could be
separated from each other -- extracting this is what unblocks all of them.

The dataset is expensive: ``rack_status.sh`` over the whole fleet on the far side of an SSH hop. So the
engine is mostly about not producing it more than once, and the layers are deliberate:

* ``get_cluster_snapshot`` wraps ``cluster_snapshot.ClusterSnapshotService`` with
  ``_collect_canonical_rack_status`` as the collector, so the snapshot is shared ACROSS PROCESSES with a
  lease. Two gunicorn workers answering two browser tabs must not both run the fleet scan; the lease is
  what makes the second one wait for the first instead.
* ``_rack_status_registry`` + ``_rack_status_cache_key`` keep the in-process side keyed by log dir,
  because one instance now serves several clusters and a cache keyed by anything coarser would serve
  one site's rows for another's request.
* ``_maybe_build_tmp_status_from_log_cache`` answers from the on-disk log-analysis cache when the scan
  cannot run yet, so a cold start shows the last known truth rather than an empty fleet.

Two side inputs live here because only the engine needs them: ``get_physical_rf0_map`` (the physical
rack -> management IP map used to annotate rows) and the cmsh rack-list file, where
``_ensure_cmsh_rack_list_env_for_subprocess`` is the part to be careful with -- ``rack_status.sh`` reads
that path from its ENVIRONMENT, so the file and the env var have to be arranged together or the script
silently sees no racks.

State here is module-level and shared by reference (the locks, the registry, the repo cache). Mutate it,
never rebind it: ``_physical_rf0_map = {}`` in an importer would give that module a private map while the
rest of the app kept using this one.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import cluster_snapshot
import host_exec
import native_status
from comments_store import read_comments
from config_env import _eget, _env_int, _job_env_int, _subprocess_env
from core import rpath as _rpath
from core import truthy_env as _truthy_env
from naming import (
    _is_multirack_log_basename,
    _jobid_from_nemo_log_basename,
    _nemo_single_rack_log_basename,
    _nvis_naming,
    _rack_id_from_log_basename,
)
from paths import (
    REPO_ROOT,
    _rack_status_script_path,
    _script_exists,
)
from platforms import get_platform as _get_platform

_logger = logging.getLogger(__name__)

try:  # optional: only present when the catalog package is importable
    from catalog import context as catalog_context

    _CATALOG_AVAILABLE = True
except Exception:  # pragma: no cover - a missing catalog must not stop the engine
    # Broad on purpose: the catalog reaches TinyDB and the filesystem, so any failure here has to
    # degrade to "no active-cluster identity" rather than take rack status down with it.
    _logger.debug("catalog context unavailable; snapshot identity falls back", exc_info=True)
    catalog_context = None
    _CATALOG_AVAILABLE = False


_physical_rf0_lock = threading.Lock()


_physical_rf0_map: dict[str, str] = {}


_physical_rf0_loaded = False


_rack_status_cache_lock = threading.Lock()


_rack_status_registry: set[str] = set()


_cluster_snapshot_repo_lock = threading.Lock()


_cluster_snapshot_repo_cache: tuple[str, cluster_snapshot.SnapshotRepository] | None = None


_PHYSICAL_RF0_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _parse_physical_rf0_stdout(text: str) -> dict[str, str]:
    """hostname(lower) -> first IPv4 after that line (skips interfaces / noise until get ip)."""
    out: dict[str, str] = {}
    raw_lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines: list[str] = []
    for ln in raw_lines:
        t = ln.strip()
        if not t or t.startswith((">", "#")):
            continue
        lines.append(t)

    loose = os.environ.get("RACK_PHYSICAL_RF0_LOOSE_HOSTNAMES", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    naming = _nvis_naming()

    def host_ok(h: str) -> bool:
        if naming.is_tray_hostname(h):
            return True
        if not loose:
            return False
        if _PHYSICAL_RF0_IPV4_RE.match(h.split("/")[0].strip()):
            return False
        if len(h) < 2 or len(h) > 253:
            return False
        return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", h))

    i = 0
    while i < len(lines):
        h = lines[i]
        i += 1
        if not host_ok(h):
            continue
        ip_token = ""
        while i < len(lines):
            cand = lines[i].split()[0].split("/")[0].strip()
            i += 1
            if _PHYSICAL_RF0_IPV4_RE.match(cand):
                ip_token = cand
                break
        if ip_token:
            out[h.lower()] = ip_token
    return out


def get_physical_rf0_map() -> dict[str, str]:
    """Run cmsh foreach once per process; merged into /api/status. Disable with RACK_PHYSICAL_RF0_MAP_DISABLED=1."""
    global _physical_rf0_loaded, _physical_rf0_map
    if os.environ.get("RACK_PHYSICAL_RF0_MAP_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return {}
    with _physical_rf0_lock:
        if _physical_rf0_loaded:
            return dict(_physical_rf0_map)
        _physical_rf0_map = {}
        plat = _get_platform()
        cmd = plat.physical_rf0_cmd()
        if not host_exec.which(cmd[0]):
            _physical_rf0_loaded = True
            return {}
        try:
            proc = host_exec.run_on_host(
                cmd,
                timeout=max(20, _env_int("RACK_PHYSICAL_RF0_TIMEOUT", 180)),
            )
        except (OSError, subprocess.TimeoutExpired):
            _physical_rf0_loaded = True
            return {}
        if proc.returncode == 0 and proc.stdout:
            _physical_rf0_map = _parse_physical_rf0_stdout(proc.stdout)
        _physical_rf0_loaded = True
        return dict(_physical_rf0_map)


def _extra_script_args() -> list[str]:
    extra = _eget("RACK_STATUS_EXTRA_ARGS", "") or ""
    if not extra.strip():
        return []
    return shlex.split(extra)


_FOOTNOTE_MARKERS = (
    "squeue disabled",
    "sinfo disabled",
    "cmsh not in PATH",
    "cmsh produced no",
    "cmsh -c ds produced",
    "rack(s) in logs but not matched",
)


def parse_rack_output(text: str) -> dict:
    """Parse stdout from rack_status.sh with NO_COLOR=1."""
    lines = text.splitlines()
    legend = ""
    rows: list[dict[str, str]] = []
    summary: dict[str, int] | None = None
    footnotes: list[str] = []

    hline_re = re.compile(r"^\s*\+[-+]+\+")
    row_re = re.compile(r"^\s*\|")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("=========="):
            continue
        if stripped.startswith(("rack; list", "cm: rack; list")):
            legend = stripped
            continue
        if hline_re.match(line):
            continue
        if row_re.match(line):
            parts = [p.strip() for p in line.split("|")]
            if parts and parts[0] == "":
                parts = parts[1:]
            if parts and parts[-1] == "":
                parts = parts[:-1]
            if len(parts) < 5:
                continue
            if parts[0].lower() == "rack" and any(p.lower() == "nemo" for p in parts):
                continue
            # New: rack, nemo, last_job, sinfo, ct_down_unk [, nemo_hint]. Legacy: rack, cm, nemo, last_job, sinfo, ct_down_unk.
            legacy_cm = len(parts) >= 6 and parts[1].lower() in ("yes", "no", "?", "n/a")
            if legacy_cm:
                rows.append(
                    {
                        "rack": parts[0],
                        "nemo": parts[2],
                        "last_job": parts[3],
                        "sinfo": parts[4],
                        "ct_down_unk": parts[5],
                    }
                )
            else:
                hint = parts[5] if len(parts) > 5 else ""
                rows.append(
                    {
                        "rack": parts[0],
                        "nemo": parts[1],
                        "last_job": parts[2],
                        "sinfo": parts[3],
                        "ct_down_unk": parts[4],
                        "nemo_hint": hint,
                    }
                )
            continue
        m = re.search(
            r"Summary:\s*(\d+)\s*done\s+(\d+)\s*fail\s+(\d+)\s*ongoing\s+(\d+)\s*pending",
            line,
        )
        if m:
            summary = {
                "done": int(m.group(1)),
                "fail": int(m.group(2)),
                "ongoing": int(m.group(3)),
                "pending": int(m.group(4)),
            }
            continue
        if any(mk in stripped for mk in _FOOTNOTE_MARKERS) and stripped not in footnotes:
            footnotes.append(stripped)

    return {
        "legend": legend,
        "rows": rows,
        "summary": summary,
        "footnotes": footnotes,
    }


_CMSH_RACK_LIST_LOCK = threading.Lock()


_CMSH_RACK_LIST_STATE: str = "pending"  # pending | ok | failed


_CMSH_RACK_LIST_PATH = REPO_ROOT / "var" / "cache" / "cmsh_rack_list.txt"


def _try_populate_cmsh_rack_list_file() -> None:
    """Run ``cmsh -c 'rack; list'`` at most once per process; write one rack id per line for rack_status.sh."""
    global _CMSH_RACK_LIST_STATE
    if _truthy_env("RACK_CMSH_RACK_LIST_CACHE_DISABLED"):
        with _CMSH_RACK_LIST_LOCK:
            if _CMSH_RACK_LIST_STATE == "pending":
                _CMSH_RACK_LIST_STATE = "failed"
        return

    with _CMSH_RACK_LIST_LOCK:
        if _CMSH_RACK_LIST_STATE != "pending":
            return
        plat = _get_platform()
        cmd = plat.list_racks_cmd()
        if not host_exec.which(cmd[0]):
            _CMSH_RACK_LIST_STATE = "failed"
            return
        try:
            proc = host_exec.run_on_host(
                cmd,
                timeout=max(30, _env_int("RACK_CMSH_RACK_LIST_TIMEOUT_SEC", 120)),
            )
        except (OSError, subprocess.TimeoutExpired):
            _CMSH_RACK_LIST_STATE = "failed"
            return
        text = proc.stdout or ""
        ids = native_status.extract_rack_ids_from_cmsh_text(text)
        if proc.returncode != 0 and not ids:
            _CMSH_RACK_LIST_STATE = "failed"
            return
        body = "\n".join(ids) + ("\n" if ids else "")
        try:
            _CMSH_RACK_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CMSH_RACK_LIST_PATH.with_name(_CMSH_RACK_LIST_PATH.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(str(tmp), str(_CMSH_RACK_LIST_PATH))
        except OSError:
            _CMSH_RACK_LIST_STATE = "failed"
            return
        _CMSH_RACK_LIST_STATE = "ok"


def _ensure_cmsh_rack_list_env_for_subprocess(env: dict) -> None:
    """Always set RACK_CMSH_RACK_LIST_FILE to the default cache path (rack_status uses it only if readable)."""
    if _truthy_env("RACK_CMSH_RACK_LIST_CACHE_DISABLED"):
        env.pop("RACK_CMSH_RACK_LIST_FILE", None)
        return
    _try_populate_cmsh_rack_list_file()
    try:
        env["RACK_CMSH_RACK_LIST_FILE"] = str(_CMSH_RACK_LIST_PATH.resolve())
    except OSError:
        pass


def run_rack_status(log_dir: Path, include_raw: bool = False) -> dict:
    if _truthy_env("RACK_STATUS_DEMO"):
        import demo_data
        return demo_data.generate_demo_status(log_dir, include_raw)

    if _truthy_env("RACK_STATUS_USE_SHELL_TABLE"):
        script = _rack_status_script_path()
        if not _script_exists(script):
            raise FileNotFoundError(f"rack_status script not found: {script}")

        env = _subprocess_env({"NO_COLOR": "1"})
        _ensure_cmsh_rack_list_env_for_subprocess(env)

        tmp_pre: Path | None = None
        proc: subprocess.CompletedProcess | None = None
        try:
            tmp_pre = _maybe_build_tmp_status_from_log_cache(log_dir)
            if tmp_pre is not None:
                cmd = [str(script), *_extra_script_args(), "--tmp-status", str(tmp_pre)]
            else:
                cmd = [str(script), *_extra_script_args()]
            proc = host_exec.run_on_host(
                cmd,
                cwd=str(log_dir),
                env=env,
                timeout=max(1, _env_int("RACK_STATUS_TIMEOUT", 300)),
            )
        finally:
            if tmp_pre is not None:
                try:
                    tmp_pre.unlink(missing_ok=True)
                except OSError:
                    pass
        assert proc is not None
        parsed = parse_rack_output(proc.stdout)
        out: dict = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "log_dir": str(log_dir),
            "script": str(script),
            "stderr": proc.stderr,
            **parsed,
        }
        if include_raw:
            out["stdout"] = proc.stdout
        try:
            out["comments"] = read_comments()
        except Exception:
            _logger.debug("run_rack_status: ignoring error", exc_info=True)
            out["comments"] = {}
        out["physical_rf0"] = get_physical_rf0_map()
        out["native_workers"] = {
            "mode": "shell_table",
            "note": "Table from rack_status.sh; native async workers are not used.",
        }
        return out

    out = native_status.build_native_rack_status(
        log_dir,
        include_raw,
        _probe_log_json_via_rack_status,
        # Never persist the placeholder table produced before a cold worker's
        # log scan has recovered completed NCCL/NeMo evidence.
        wait_for_workers=not include_raw,
        worker_timeout_sec=max(1, _env_int("RACK_STATUS_TIMEOUT", 300)),
    )
    summ = out.get("summary")
    if not isinstance(summ, dict):
        out["summary"] = {"done": 0, "fail": 0, "ongoing": 0, "pending": 0}
    try:
        out["comments"] = read_comments()
    except Exception:
        _logger.debug("run_rack_status: ignoring error", exc_info=True)
        out["comments"] = {}
    out["physical_rf0"] = get_physical_rf0_map()
    return out


def _rack_status_background_interval_sec() -> int:
    return max(5, _job_env_int("RACK_STATUS_BACKGROUND_INTERVAL_SEC", 30))


def _cluster_snapshot_db_path() -> Path:
    configured = (os.environ.get("RACK_CLUSTER_SNAPSHOT_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (REPO_ROOT / "var" / "cache" / "cluster_snapshots.sqlite3").resolve()


def _cluster_snapshot_repository() -> cluster_snapshot.SnapshotRepository:
    global _cluster_snapshot_repo_cache
    path = str(_cluster_snapshot_db_path())
    with _cluster_snapshot_repo_lock:
        if _cluster_snapshot_repo_cache is None or _cluster_snapshot_repo_cache[0] != path:
            _cluster_snapshot_repo_cache = (
                path,
                cluster_snapshot.SnapshotRepository(path),
            )
        return _cluster_snapshot_repo_cache[1]


def _cluster_snapshot_identity() -> str:
    """Stable, non-secret active cluster identity used in the shared snapshot key."""
    if _CATALOG_AVAILABLE and catalog_context is not None:
        try:
            active = catalog_context.get_active()
            project_id = str(active.get("project") or "").strip()
            cluster_id = str(active.get("cluster") or "").strip()
            if project_id or cluster_id:
                return f"catalog:{project_id}/{cluster_id}"
        except Exception:
            _logger.debug("_cluster_snapshot_identity: ignoring error", exc_info=True)
    configured = (
        os.environ.get("RACK_CLUSTER_ID")
        or os.environ.get("RACK_CLUSTER_NAME")
        or os.environ.get("CLUSTER_NAME")
        or os.environ.get("RACK_SSH_HOST")
        or ""
    ).strip()
    return f"cluster:{configured or socket.gethostname()}"


def _collect_canonical_rack_status(log_dir: Path) -> dict:
    """Collect inventory before it becomes a persisted snapshot.

    ``run_rack_status`` / ``build_native_rack_status`` already produce rows and
    ``native_workers``. Do not call ``_merge_live_native_workers_into_status``
    here — that rebuilds the table and re-runs ``squeue``.
    """
    return run_rack_status(log_dir, include_raw=False)


def get_cluster_snapshot(
    log_dir: Path,
    *,
    force_refresh: bool = False,
    wait: bool = True,
) -> cluster_snapshot.ClusterSnapshot:
    """Return the canonical cross-process inventory snapshot for both APIs."""
    log_root = _rack_status_cache_key(log_dir)
    _register_rack_status_log_dir(log_dir)
    service = cluster_snapshot.ClusterSnapshotService(
        _cluster_snapshot_repository(),
        collector=lambda: _collect_canonical_rack_status(log_dir),
        lease_seconds=max(30, _env_int("RACK_STATUS_TIMEOUT", 300) + 30),
    )
    return service.get_cluster_snapshot(
        _cluster_snapshot_identity(),
        log_root,
        max_age_seconds=_rack_status_background_interval_sec(),
        force_refresh=force_refresh,
        wait=wait,
    )


def _rack_status_cache_key(log_dir: Path) -> str:
    # Do NOT local-resolve in SSH mode: the background loop uses this key as the actual log_dir,
    # and resolving a remote path on the app host corrupts it (/home/... -> /System/Volumes/Data/...),
    # so the SSH `find` would look for a nonexistent path and every rack stays "pending".
    return str(_rpath(log_dir))


def _register_rack_status_log_dir(log_dir: Path) -> str:
    key = _rack_status_cache_key(log_dir)
    with _rack_status_cache_lock:
        _rack_status_registry.add(key)
    return key


def _probe_log_json_via_rack_status(abs_path: Path) -> dict[str, object] | None:
    """Run ``rack_status.sh --probe-json`` for one log file (same nemo/nccl/squeue heuristics as the table)."""
    script = _rack_status_script_path()
    if not _script_exists(script):
        return None
    ap = _rpath(abs_path)
    mvt = (_eget("RACK_MULTIRACK_VALIDATION_TEST") or "nccl_all_reduce").strip() or "nccl_all_reduce"
    vt_if_unset = mvt if _is_multirack_log_basename(ap.name) else "nccl_all_reduce,llm"
    env = _subprocess_env({"NO_COLOR": "1"}, validation_test_if_unset=vt_if_unset)
    _ensure_cmsh_rack_list_env_for_subprocess(env)
    # ``env=`` is LOCAL ONLY -- run_on_host drops it for an SSH target, which is how this
    # deployment runs the script. Without env_extra the remote never sees VALIDATION_TEST and
    # validation_test_env.sh falls back to its strictest suite
    # (all_reduce + alltoall + allgather + llm) applied to whichever single log is newest,
    # so racks that passed every stage still render red. Forward the acceptance knobs
    # explicitly; env_extra bypasses the FORWARD_ENV_PREFIXES allowlist.
    env_extra = {"NO_COLOR": "1", "VALIDATION_TEST": env.get("VALIDATION_TEST", vt_if_unset)}
    for _k in ("NEMO_TARGET_STEPS", "RACK_VALIDATION_CT_RANGE", "NVIS_RACK_ID"):
        _v = (env.get(_k) or "").strip()
        if _v:
            env_extra[_k] = _v
    try:
        proc = host_exec.run_on_host(
            [str(script), "--probe-json", str(ap)],
            cwd=str(ap.parent),
            env=env,
            env_extra=env_extra,
            timeout=max(60, _env_int("RACK_MULTIRACK_PROBE_TIMEOUT", 180)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        out = json.loads(raw.splitlines()[-1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


_LOG_ANALYSIS_CACHE_VERSION = 2


_LOG_ANALYSIS_CACHE_PATH = REPO_ROOT / "var" / "cache" / "log_analysis_cache.json"


def _glob_one_rack_nemo_logs_for_rack_status(log_dir: Path) -> list[Path]:
    """NEMO single-rack validation logs in log_dir root (not failed/, not NCCL-only, no Llama stdout)."""
    root = _rpath(log_dir)
    naming = _nvis_naming()
    out: list[Path] = []
    listing = [PurePosixPath(s) for s in host_exec.stat_dir(root, patterns=("*.txt",))]
    for p in sorted(listing, key=lambda x: x.name.lower()):
        if not _nemo_single_rack_log_basename(p.name):
            continue
        if naming.is_llama_slurm_stdout_basename(p.name):
            continue
        if _is_multirack_log_basename(p.name):
            continue
        out.append(p)
    return out


def _log_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    return (int(mtime_ns), int(st.st_size))


def _load_log_analysis_cache_entries() -> dict[str, dict[str, object]]:
    p = _LOG_ANALYSIS_CACHE_PATH
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            locked = False
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                pass
            try:
                raw = json.load(f)
            except Exception:
                _logger.debug("_load_log_analysis_cache_entries: ignoring error", exc_info=True)
                raw = {}
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") != _LOG_ANALYSIS_CACHE_VERSION:
        return {}
    ent = raw.get("entries")
    if not isinstance(ent, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    for k, v in ent.items():
        if isinstance(v, dict) and isinstance(k, str):
            out[k] = v
    return out


def _save_log_analysis_cache_entries(entries: dict[str, dict[str, object]]) -> None:
    p = _LOG_ANALYSIS_CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(
        {"version": _LOG_ANALYSIS_CACHE_VERSION, "entries": entries},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
            except OSError:
                pass
            try:
                f.write(blob)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            finally:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass
        os.replace(str(tmp), str(p))
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _slurm_running_job_ids_for_rack_status() -> set[str]:
    """Slurm job ids for SQUEUE_USER or USER (same idea as rack_status load_squeue_jobs)."""
    user = (os.environ.get("SQUEUE_USER") or os.environ.get("USER") or "root").strip()
    plat = _get_platform()
    try:
        proc = host_exec.run_slurm_on_host(
            plat.squeue_cmd(f"-u {shlex.quote(user)} -h -o %i"),
            timeout=max(5, _env_int("RACK_LOG_ANALYSIS_SQUEUE_TIMEOUT_SEC", 60)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    out: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        j = line.strip()
        if j.isdigit():
            out.add(j)
    return out


def _tmp_status_row_sort_key(row: tuple[str, str, int, int, int]) -> tuple[int, int, int, str, str]:
    rack, jid, _on, _nc, _ll = row
    m = re.match(r"^p(\d+)-r(\d+)$", rack.strip().lower())
    if m:
        return (int(m.group(1)), int(m.group(2)), int(jid) if jid.isdigit() else 999_999, rack, jid)
    if rack.strip() == "?":
        return (99_999, 99_999, int(jid) if jid.isdigit() else 999_999, rack, jid)
    return (50_000, 50_000, int(jid) if jid.isdigit() else 999_999, rack, jid)


def _maybe_build_tmp_status_from_log_cache(log_dir: Path) -> Path | None:
    """Build a tmp_status TSV and return its path for ``rack_status.sh --tmp-status``, or None."""
    if _truthy_env("RACK_LOG_ANALYSIS_CACHE_DISABLED"):
        return None
    extra = _extra_script_args()
    if any(a == "--tmp-status" for a in extra):
        return None
    paths = _glob_one_rack_nemo_logs_for_rack_status(log_dir)
    if not paths:
        return None
    max_files = max(0, _env_int("RACK_LOG_ANALYSIS_CACHE_MAX_FILES", 200))
    if len(paths) > max_files:
        return None

    running = _slurm_running_job_ids_for_rack_status()
    entries = _load_log_analysis_cache_entries()
    dirty = False
    rows: list[tuple[str, str, int, int, int]] = []
    alive: set[str] = set()

    for p in paths:
        key = str(p.resolve())
        alive.add(key)
        fp = _log_fingerprint(p)
        if fp is None:
            continue
        mtime_ns, size = fp
        hit = entries.get(key)
        nc: int | None = None
        ll: int | None = None
        if isinstance(hit, dict):
            try:
                if int(hit["mtime_ns"]) == mtime_ns and int(hit["size"]) == size:
                    nc = int(hit["nccl_ok"]) & 1
                    ll = int(hit["llm_ok"]) & 1
            except (KeyError, TypeError, ValueError):
                nc = ll = None
        if nc is None or ll is None:
            probe = _probe_log_json_via_rack_status(p) or {}
            nc = 1 if probe.get("ok") and probe.get("nccl_ok") else 0
            ll = 1 if probe.get("ok") and probe.get("llm_ok") else 0
            entries[key] = {"mtime_ns": mtime_ns, "size": size, "nccl_ok": nc, "llm_ok": ll}
            dirty = True
        jid = _jobid_from_nemo_log_basename(p.name) or "?"
        rack = _rack_id_from_log_basename(p.name) or "?"
        ongoing = 1 if jid in running else 0
        rows.append((rack, jid, ongoing, nc, ll))

    pruned = {k: v for k, v in entries.items() if k in alive}
    if len(pruned) != len(entries):
        entries = pruned
        dirty = True

    if dirty:
        _save_log_analysis_cache_entries(entries)

    rows.sort(key=_tmp_status_row_sort_key)
    _LOG_ANALYSIS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tpath_s = tempfile.mkstemp(
            prefix="rack_tmp_status_",
            suffix=".tsv",
            dir=str(_LOG_ANALYSIS_CACHE_PATH.parent),
            text=True,
        )
    except OSError:
        fd, tpath_s = tempfile.mkstemp(prefix="rack_tmp_status_", suffix=".tsv", text=True)
    os.close(fd)
    tpath = Path(tpath_s)
    try:
        with open(tpath, "w", encoding="utf-8") as wf:
            for rack, jid, on, nc, ll in rows:
                wf.write(f"{rack}\t{jid}\t{on}\t{nc}\t{ll}\n")
    except OSError:
        try:
            tpath.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return tpath
