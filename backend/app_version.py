# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""What build is this? -- the single answer, read from disk.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Two files can answer, and they answer different questions:

``backend/build-info.json``
    Written by ``bin/write-build-info.sh`` just before an image is built. Authoritative, because it
    describes *this artifact*: the release version plus a per-build stamp, so two images cut from
    the same release commit are still distinguishable. Absent in a plain working tree.

    It lives beside the code, under ``backend/``, on purpose. A deployment that bind-mounts the
    backend directory over the image's would *hide* a copy baked in anywhere under that path, and
    ``/app/var`` is worse still -- it is masked by a named volume, which Docker only seeds from the
    image when the volume is empty, so a file written there is invisible on any deployment that has
    ever run. Writing it into the tree and letting ``COPY backend/`` carry it into the image means
    the mounted and the image-only cases read the same file.

``version.py`` (repo root)
    The release version, owned by the ci-toolkit release job. Parsed, never imported -- importing
    would execute module code on the request path, and this file exists for CI's ``grep``, not for
    us. Reported with a ``-dev`` suffix, because a tree with no build stamp was not built.

Deliberately reads no environment variable. The previous implementation read only
``RACK_WEB_VERSION``, which is set nowhere in this repo, so every deployment reported ``dev`` while
``version.py`` said 0.1.5 -- a version string that is always wrong is worse than none.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

#: ``VERSION = "1.2.3-rc1"`` in either quote style. Parsed rather than imported.
_VERSION_RE = re.compile(r"""^\s*VERSION\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)

_cached: str | None = None


def reset_cache() -> None:
    """Forget the resolved version. For tests, and for a deliberate re-read."""
    global _cached
    _cached = None


def _from_build_info() -> str | None:
    path = APP_DIR / "build-info.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing (a working tree), unreadable, a directory, or truncated by a half-finished
        # build. Any of those means "ask the next source", not "fail the request".
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip()
    if not version:
        return None
    build = str(data.get("build") or "").strip()
    return f"{version}+{build}" if build else version


def _from_version_py() -> str | None:
    try:
        raw = (REPO_ROOT / "version.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _VERSION_RE.search(raw)
    if not m:
        return None
    return f"{m.group(1).strip()}-dev"


def app_version() -> str:
    """The build's version string, e.g. ``0.1.5+20260814T1607.e8ea22e`` or ``0.1.5-dev``.

    Cached: it is stamped into ``/api/app-stats`` and ``/api/cluster-overview`` payloads, so it
    must not stat two files per request. Never raises -- a version string is not worth an error
    response, hence ``"unknown"`` as the floor.
    """
    global _cached
    if _cached is None:
        _cached = _from_build_info() or _from_version_py() or "unknown"
    return _cached
