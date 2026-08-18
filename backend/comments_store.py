# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Operator comments on racks and multirack runs: the JSON store behind them.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Free-text notes an operator attaches to a rack or a specific multirack run ("waiting on the cable
swap", "known bad tray"), read back into the status table so the next person sees them. Small,
file-backed, and read on every status poll.

Extracted because three callers need it and they are in different modules now: the comments API, the
status payload merge, and the NVFTP cache sync that ships the file between hosts.

``_comments_file`` is deliberately ONE definition. There were two -- app.py read a bare
``os.environ["RACK_COMMENTS_FILE"]`` while nvftp_cache_sync used ``_eget`` plus ``expanduser`` -- so a
path set through dashboard Settings (rather than the process environment) was visible to one and not
the other, and a ``~`` path resolved in one only. Two readers of the same setting pointing at two
different files loses comments silently. This keeps the superset behaviour: the full config ladder,
with ``~`` expanded.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path

from config_env import _eget, _env_int
from naming import _normalize_rack_id
from paths import REPO_ROOT

_logger = logging.getLogger(__name__)


def _comments_file() -> Path:
    """Path to the rack comments JSON (``RACK_COMMENTS_FILE``, else ``REPO_ROOT/rack_comments.json``)."""
    raw = _eget("RACK_COMMENTS_FILE", "") or ""
    if raw.strip():
        return Path(os.path.expanduser(raw.strip())).resolve()
    return (REPO_ROOT / "rack_comments.json").resolve()


def _normalize_multirack_comment_basename(raw: object) -> str | None:
    """Basename of a NEMO/NCCL multirack log file; used as storage suffix under ``mr:<basename>``."""
    if not isinstance(raw, str):
        return None
    bn = raw.strip()
    if not bn or bn != Path(bn).name or ".." in bn or "/" in bn or "\\" in bn:
        return None
    if len(bn) > 512:
        return None
    low = bn.lower()
    if "nemo" not in low and "nccl" not in low:
        return None
    return bn


def _multirack_comment_storage_key(basename: str) -> str | None:
    bn = _normalize_multirack_comment_basename(basename)
    return f"mr:{bn.lower()}" if bn else None


def _split_stored_comments(raw: object) -> tuple[dict[str, str], dict[str, str]]:
    """Split JSON object into rack comments (``pN-rMM``) and multirack log comments (``mr:<basename>``)."""
    racks: dict[str, str] = {}
    multirack: dict[str, str] = {}
    if not isinstance(raw, dict):
        return racks, multirack
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        t = v.strip()
        if not t:
            continue
        lk = k.strip()
        if lk.lower().startswith("mr:"):
            sk = _multirack_comment_storage_key(lk[3:])
            if sk:
                multirack[sk] = t
            continue
        try:
            rk = _normalize_rack_id(lk)
        except ValueError:
            continue
        racks[rk] = t
    return racks, multirack


def read_comments() -> dict[str, str]:
    p = _comments_file()
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
                try:
                    raw = json.load(f)
                except json.JSONDecodeError:
                    raw = {}
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
        racks, mr = _split_stored_comments(raw)
        return {**racks, **mr}
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _load_comment_maps_locked(f) -> tuple[dict[str, str], dict[str, str]]:
    blob = f.read()
    try:
        raw = json.loads(blob) if blob.strip() else {}
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return _split_stored_comments(raw)


def set_comment_for_rack(rack: str, comment: str | None) -> dict[str, str]:
    """Set or clear comment for rack. Returns full comments map after update (preserves multirack keys)."""
    rack_key = _normalize_rack_id(rack)
    max_len = _env_int("RACK_COMMENT_MAX_LEN", 4000)
    if max_len < 1:
        max_len = 4000
    p = _comments_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("{}\n", encoding="utf-8")
    with open(p, "r+", encoding="utf-8") as f:
        locked = False
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass
        try:
            f.seek(0)
            racks, mr = _load_comment_maps_locked(f)
            text = (comment or "").strip()
            if not text:
                racks.pop(rack_key, None)
            else:
                racks[rack_key] = text[:max_len]
            merged = {**racks, **mr}
            new_blob = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            f.seek(0)
            f.truncate()
            f.write(new_blob)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
            return merged
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass


def set_comment_for_multirack_log(basename: str, comment: str | None) -> dict[str, str]:
    """Set or clear comment for a multirack log basename (``root-NEMO-…``.txt). Key on disk: ``mr:<basename>``."""
    sk = _multirack_comment_storage_key(basename)
    if not sk:
        raise ValueError("invalid multirack log basename for comment (expected .txt with nemo or nccl in name)")
    max_len = _env_int("RACK_COMMENT_MAX_LEN", 4000)
    if max_len < 1:
        max_len = 4000
    p = _comments_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("{}\n", encoding="utf-8")
    with open(p, "r+", encoding="utf-8") as f:
        locked = False
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError:
            pass
        try:
            f.seek(0)
            racks, mr = _load_comment_maps_locked(f)
            text = (comment or "").strip()
            if not text:
                mr.pop(sk, None)
            else:
                mr[sk] = text[:max_len]
            merged = {**racks, **mr}
            new_blob = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            f.seek(0)
            f.truncate()
            f.write(new_blob)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
            return merged
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass

# Normalising a stored comments map is this module's business: it is the shape read_comments
# returns, and app.py only ever had it because everything used to live there.


def _normalize_comments_map(raw: object) -> dict[str, str]:
    """Legacy rack-only normalization (``pN-rMM`` keys). Prefer :func:`_split_stored_comments`."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        try:
            rk = _normalize_rack_id(k)
        except ValueError:
            continue
        t = v.strip()
        if t:
            out[rk] = t
    return out
