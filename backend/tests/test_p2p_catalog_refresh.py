# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The node-side catalog refresh: current pins in, and never a bad catalog over a good one.

Author: Rafael M. Koike

``deploy/p2p/scripts/catalog-refresh.sh`` runs every 15 minutes on ~800 nodes, unattended. The
property worth pinning is the asymmetric one: a valid origin catalog replaces a stale one, while a
truncated, malformed or unreachable one leaves the node's existing catalog byte-for-byte intact --
a refresh that could blank or corrupt the pins would turn a staleness fix into a fleet outage.
The origin is a ``file://`` URL, which curl serves exactly like http for this purpose.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts" / "catalog-refresh.sh"
HEADER = "order\tname\tfilename\tbytes\tsha256\n"
SHA = "a" * 64

pytestmark = pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")


def _catalog(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


GOOD_OLD = _catalog(f"1\tNeMo\tnemo.sqsh\t100\t{SHA}")
GOOD_NEW = _catalog(f"1\tNeMo\tnemo.sqsh\t100\t{SHA}", f"2\tNeMo NCCL 2.32.3\tnemo-nccl.sqsh\t200\t{'b' * 64}")


def _run(base: Path, origin: Path | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "NVIS_P2P_BASE": str(base)}
    env.pop("NVIS_P2P_CATALOG_URL", None)
    if origin is not None:
        env["NVIS_P2P_CATALOG_URL"] = origin.as_uri()
    # check=False: refusals are outcomes these tests assert on.
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


@pytest.fixture
def node(tmp_path):
    """A node whose catalog holds GOOD_OLD, and an origin file the test fills in."""
    base = tmp_path / "node"
    (base / "catalog").mkdir(parents=True)
    (base / "catalog" / "images.tsv").write_text(GOOD_OLD)
    return base, tmp_path / "origin-images.tsv"


def _catalog_on(base: Path) -> str:
    return (base / "catalog" / "images.tsv").read_text()


def _status(base: Path) -> str:
    return (base / "run" / "catalog-refresh.status").read_text()


def test_new_pins_replace_a_stale_catalog(node):
    base, origin = node
    origin.write_text(GOOD_NEW)
    res = _run(base, origin)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "CATALOG_REFRESH_UPDATED rows=2" in res.stdout
    assert _catalog_on(base) == GOOD_NEW
    assert "CATALOG_REFRESH_UPDATED rows=2" in _status(base)
    # No temp file left behind in the catalog directory.
    assert sorted(p.name for p in (base / "catalog").iterdir()) == ["images.tsv"]


def test_an_unchanged_catalog_is_not_rewritten(node):
    base, origin = node
    origin.write_text(GOOD_OLD)
    before = (base / "catalog" / "images.tsv").stat().st_ino
    res = _run(base, origin)
    assert res.returncode == 0 and "CATALOG_REFRESH_CURRENT rows=1" in res.stdout
    assert (base / "catalog" / "images.tsv").stat().st_ino == before


def test_an_unpinned_image_is_a_valid_row(node):
    """Catalog sync writes a blank sha256 for an image added but not yet imported."""
    base, origin = node
    origin.write_text(_catalog(f"1\tNeMo\tnemo.sqsh\t100\t{SHA}", "2\tNew\tnew.sqsh\t0\t"))
    assert _run(base, origin).returncode == 0
    assert "new.sqsh" in _catalog_on(base)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("", id="empty-response"),
        pytest.param(HEADER, id="header-only"),
        pytest.param(GOOD_NEW.replace("order\t", "ordr\t"), id="wrong-header"),
        pytest.param(GOOD_NEW.replace("\n", "\r\n"), id="crlf"),
        pytest.param(_catalog(f"1\tNeMo\tnemo.sqsh\t100\t{'A' * 64}"), id="uppercase-sha"),
        pytest.param(_catalog(f"1\tNeMo\tnemo.sqsh\t100\t{'a' * 63}"), id="short-sha"),
        pytest.param(_catalog(f"1\tNeMo\t../etc/passwd\t100\t{SHA}"), id="path-filename"),
        pytest.param(_catalog(f"1\tNeMo\tnemo.tar\t100\t{SHA}"), id="not-sqsh"),
        pytest.param(_catalog(f"1\tNeMo\tnemo.sqsh\t1e9\t{SHA}"), id="non-numeric-bytes"),
        pytest.param(_catalog("1\tNeMo\tnemo.sqsh\t100"), id="missing-field"),
        pytest.param(GOOD_NEW[:-40], id="truncated"),
        # A whole last row without its newline: valid fields, but `while read` consumers on the node
        # would skip that image, and it is how a cut-off download usually ends.
        pytest.param(GOOD_NEW.rstrip("\n"), id="no-final-newline"),
    ],
)
def test_a_bad_catalog_never_replaces_a_good_one(node, bad):
    base, origin = node
    origin.write_text(bad)
    res = _run(base, origin)
    assert res.returncode != 0
    assert "CATALOG_REFRESH_REJECTED reason=invalid-catalog" in res.stdout
    assert _catalog_on(base) == GOOD_OLD
    assert "REJECTED" in _status(base)


def test_an_unreachable_origin_keeps_the_catalog(node, tmp_path):
    base, _ = node
    res = _run(base, tmp_path / "no-such-origin.tsv")
    assert res.returncode != 0
    assert "CATALOG_REFRESH_FAILED reason=fetch" in res.stdout
    assert _catalog_on(base) == GOOD_OLD


def test_the_origin_url_is_required(node):
    base, _ = node
    res = _run(base, None)
    assert res.returncode != 0
    assert "NVIS_P2P_CATALOG_URL is required" in res.stderr
    assert _catalog_on(base) == GOOD_OLD
