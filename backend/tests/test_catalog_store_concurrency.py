# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Two processes may share a catalog file, because two of them do.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Found on a laptop running two local instances -- ports 5050 and 5056, one catalog. Both seed at
boot, so ~44 writes land at once, and the boot died with::

    FileNotFoundError: .../catalog.json.tmp -> .../catalog.json
    ERROR app: catalog bootstrap failed; continuing with legacy env behavior

It is not only a laptop story: a deploy's ``cli.py check`` overlapping a running server, or a
container restarting while the old one drains, is the same shape.

**Three defects, and only the first one announced itself.** The temp file was a fixed
``<path>.tmp`` shared by every writer, so the first ``os.replace`` moved it away and the second
found nothing. Fixing that exposed the two that had been hiding behind the crash: TinyDB caches
a ``_next_id`` counter, so both processes claimed the same id (``ValueError: Document with ID 4
already exists``), and each write-back was computed from a stale whole-file read, so **63 of 240
documents survived**. Measured, not reasoned: the numbers below are what the harness reported
before the fix.

That is why this test asserts a document *count* rather than "it did not raise". A store that
loses three quarters of its writes raises nothing at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

WRITERS = 4
PER_WRITER = 40


def _worker_script(target: Path) -> str:
    return textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(BACKEND)!r})
        os.environ["NVIS_CATALOG_FILE"] = {str(target)!r}
        from catalog.store import TinyDBRepo

        repo = TinyDBRepo({str(target)!r})
        for i in range({PER_WRITER}):
            repo.upsert("tools", {{"id": f"tool-{{os.getpid()}}-{{i}}", "n": i}})
    """)


@pytest.mark.skipif(os.name == "nt", reason="fcntl-based locking is POSIX-only")
def test_concurrent_processes_neither_crash_nor_lose_writes(tmp_path):
    """Real processes, not threads: the bug was invisible to a `threading.RLock`."""
    target = tmp_path / "catalog.json"
    script = tmp_path / "writer.py"
    script.write_text(_worker_script(target), encoding="utf-8")

    procs = [
        subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(WRITERS)
    ]
    failures = []
    for proc in procs:
        _out, err = proc.communicate(timeout=180)
        if proc.returncode != 0:
            failures.append(err.strip().splitlines()[-1] if err.strip() else "unknown")

    assert not failures, f"a writer died: {failures}"

    stored = json.loads(target.read_text(encoding="utf-8")).get("tools", {})
    expected = WRITERS * PER_WRITER
    assert len(stored) == expected, (
        f"{len(stored)} of {expected} documents survived — a write-back computed from a stale "
        "read silently discarded the rest"
    )


@pytest.mark.skipif(os.name == "nt", reason="fcntl-based locking is POSIX-only")
def test_a_failed_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    """One file per failed write would litter `var/store`, and none of them is named usefully."""
    import json_db
    from catalog import store as catalog_store

    target = tmp_path / "catalog.json"
    repo = catalog_store.TinyDBRepo(target)
    repo.upsert("tools", {"id": "real"})

    def explode(*_a, **_k):
        raise RuntimeError("serialisation failed")

    # Patched where the serialisation actually happens. `json_db.atomic_write_json` encodes the
    # whole document *before* opening anything, so a document that cannot be encoded has not
    # already replaced the previous file -- which is what the assertions below check.
    monkeypatch.setattr(json_db.json, "dumps", explode)
    with pytest.raises(RuntimeError):
        repo.upsert("tools", {"id": "doomed"})

    assert not [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    # ...and the previous catalog is still the catalog: the replace never happened.
    surviving = json.loads(target.read_text(encoding="utf-8"))["tools"].values()
    assert [d["id"] for d in surviving] == ["real"]


def test_the_catalog_is_configuration_not_a_secret(tmp_path):
    """0644 on purpose, and asserted so a later "harden everything" pass has to think.

    An atomic replace installs the *temp* file's mode (`identity-auth-traps.mdc` §3), so the
    mode is given at `os.open` rather than chmod-ed after. The owner-only files are the identity
    store and `policies.json`; this one is read by tooling and belongs in a diff.
    """
    from catalog import store as catalog_store

    target = tmp_path / "catalog.json"
    repo = catalog_store.TinyDBRepo(target)
    repo.upsert("tools", {"id": "x"})
    for _ in range(3):  # across several writes, per the same trap
        repo.upsert("tools", {"id": "x", "n": 1})
    assert oct(target.stat().st_mode & 0o777) == "0o644"
