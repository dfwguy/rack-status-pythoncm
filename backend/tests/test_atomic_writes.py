# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""No store may invent its own temp file again.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Nineteen places wrote a JSON or text file by hand, and fourteen of them built the temp name from
the target -- ``path.with_suffix(".tmp")``, ``f"{target}.tmp"``. Every one of those is the
catalog bug waiting for a second process: two writers share the name, the first ``os.replace``
moves it away, and the second gets ``FileNotFoundError``. It took down a boot
("catalog bootstrap failed; continuing with legacy env behavior") the first time two local
instances ran at once.

The idiom is short, obvious and *looks* correct, which is exactly why a rule in a document will
not hold it. This test is the thing that holds it: a new fixed-temp write fails the build and
the message names the helper to use instead.

It scans source rather than behaviour on purpose. The failure needs two concurrent processes to
show up, so no unit test of the *call site* would catch it -- and by the time it shows up it is
on a customer's deployment, reported as "the dashboard lost its configuration".
"""

from __future__ import annotations

import ast
import os
import re
import stat
from pathlib import Path

import json_db
import pytest

BACKEND = Path(__file__).resolve().parents[1]

#: A temp path derived from its target: the whole point is that two writers produce the same
#: name. `unique_temp_path` and `mkstemp` both avoid it.
_DERIVED_TEMP = re.compile(
    r"""
    with_suffix\(\s*["']\.tmp["']\s*\)        # p.with_suffix(".tmp")
  | with_name\([^)]*\+\s*["']\.tmp["']\s*\)   # p.with_name(p.name + ".tmp")
  | with_suffix\([^)]*\+\s*["']\.tmp["']\s*\) # p.with_suffix(p.suffix + ".tmp")
  | f["'][^"']*\{[^}]+\}\.tmp["']             # f"{target}.tmp"
    """,
    re.VERBOSE,
)

#: What makes a derived name safe anyway: something unique mixed in. `validation_activities`
#: already spells `f".{path.name}.{uuid.uuid4().hex}.tmp"`, which is derived from its target
#: *and* collision-free, so the scan must not read it as an offender -- the property being
#: tested is uniqueness, not the absence of a particular idiom.
_MADE_UNIQUE = re.compile(r"uuid4|getpid|token_hex|mkstemp|unique_temp_path")

#: Files allowed to spell a derived temp name, each with the reason.
_ALLOWED = {
    # The helper's own implementation, which is what everything else should use.
    "json_db.py",
    # A remote path written by a shell on another machine, so the local helper cannot create it.
    # It carries its own uuid -- asserted separately below.
    "transport/shellfs.py",
}


def _sources() -> list[Path]:
    return [
        p
        for p in sorted(BACKEND.rglob("*.py"))
        if "tests" not in p.parts and "__pycache__" not in p.parts
    ]


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with comments and docstrings removed, so prose cannot trip the scan."""
    text = path.read_text(errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - a broken file fails elsewhere, louder
        return []
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if i in doc_lines or line.strip().startswith("#"):
            continue
        out.append((i, line))
    return out


def test_no_module_derives_a_temp_file_name_from_its_target():
    """The scan that replaces a convention nobody can enforce by reading."""
    offenders = []
    for path in _sources():
        rel = str(path.relative_to(BACKEND))
        if rel in _ALLOWED:
            continue
        for lineno, line in _code_lines(path):
            if _DERIVED_TEMP.search(line) and not _MADE_UNIQUE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()[:88]}")

    assert not offenders, (
        "these build a temp file name from the target, so two writers collide on it and the "
        "second fails with FileNotFoundError once the first has replaced it.\n  "
        + "\n  ".join(offenders)
        + "\n\nUse json_db.atomic_write / atomic_write_json instead (or unique_temp_path if you "
        "need the path itself). If the file is written by a shell on another host, add the "
        "module to _ALLOWED here with the reason and give the name its own uuid."
    )


def test_the_remote_writer_makes_its_own_name_unique():
    """`transport/shellfs.py` is exempt from the scan, so its uniqueness is asserted directly.

    It builds the temp path inside a shell command on the *head node*, where two dashboards
    writing the same artifact would otherwise share one name and the second `mv` would fail
    after the first had moved it.
    """
    source = (BACKEND / "transport" / "shellfs.py").read_text()
    assert "uuid4().hex" in source, "the remote temp name must not be derived from its target"


# --- the helper itself --------------------------------------------------------------------------


def test_two_writers_never_share_a_temp_name():
    paths = {json_db.unique_temp_path("/var/store/catalog.json") for _ in range(200)}
    assert len(paths) == 200


def test_the_mode_is_what_the_file_is_created_with(tmp_path):
    """Across several writes, since an atomic replace installs the *temp* file's mode.

    A single-write test passes against the broken version, which is the whole lesson of
    `identity-auth-traps.mdc` §3 -- so this writes repeatedly and starts from a hostile mode.
    """
    secret = tmp_path / "secret.key"
    json_db.atomic_write(secret, "k\n", mode=0o600)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600

    os.chmod(secret, 0o644)
    for _ in range(3):
        json_db.atomic_write(secret, "k\n", mode=0o600)
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600, "a rewrite widened the mode"


def test_a_document_that_cannot_be_encoded_does_not_destroy_the_previous_file(tmp_path):
    """Serialising before opening anything is what makes this true."""
    target = tmp_path / "doc.json"
    json_db.atomic_write_json(target, {"keep": 1})

    with pytest.raises(TypeError):
        json_db.atomic_write_json(target, {"bad": object()})

    assert '"keep": 1' in target.read_text()
    assert not [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]


def test_the_lock_is_reentrant_within_a_thread(tmp_path):
    """Nesting must not hang: `flock` is per descriptor, so a second handle blocks on itself.

    No store method calls another locked method today -- verified with an AST walk over the
    identity store's sixteen mutators -- but the failure mode if one ever does is a hung login,
    which is why this is a property rather than a note.
    """
    target = tmp_path / "state.json"
    with json_db.exclusive_lock(target), json_db.exclusive_lock(target):
        json_db.atomic_write_json(target, {"n": 1})
    assert target.is_file()


def test_the_lock_uses_a_sidecar_rather_than_the_file_itself(tmp_path):
    """The target is replaced on every write, so a lock on it guards a doomed inode."""
    target = tmp_path / "state.json"
    with json_db.exclusive_lock(target):
        pass
    assert (tmp_path / "state.json.lock").exists()
