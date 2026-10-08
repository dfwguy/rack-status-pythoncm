# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The NCCL-version dropdown is derived from the served P2P catalog, not hard-coded in a component.

The validation launch dialog used to carry a literal list of libnccl builds, so shipping a new
one was a frontend edit. These tests pin the backend half of the fix: ``/api/catalog/tools`` reads
the same catalog the P2P fleet view serves (``api.p2p_fleet._load_catalog``), derives the version
list from the images whose filename encodes a libnccl build, and injects it into every tool whose
launch body opts in with ``show_nccl_version``. A tool that does not opt in is left untouched so it
keeps the frontend's shipped fallback, and an empty derivation injects nothing for the same reason.
"""
from __future__ import annotations

import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

from app import app
from catalog import api as catalog_api
from catalog import models
from catalog.store import TABLE_TOOLS, TinyDBRepo, set_repo

# The three images the live NC catalog serves in miniature: two libnccl builds (2.32.3 newer, so a
# lower order) plus a plain benchmark that carries no libnccl suffix and must be excluded.
CATALOG_IMAGES = [
    {"order": 1, "name": "NeMo 26.04.01 + NCCL 2.32.3", "filename": "nvidia+nemo+26.04.01-arm-nccl2.32.3.sqsh"},
    {"order": 2, "name": "NeMo 26.04.01 + NCCL 2.30.7", "filename": "nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"},
    {"order": 3, "name": "nvbandwidth 0.9", "filename": "nvbandwidth-v0.9-arm.sqsh"},
]

EXPECTED_VERSIONS = [
    {"value": "2.32.3", "label": "NeMo 26.04.01 + NCCL 2.32.3", "order": 1},
    {"value": "2.30.7", "label": "NeMo 26.04.01 + NCCL 2.30.7", "order": 2},
]


# --- the pure derivation helper -------------------------------------------------------------


def test_derivation_is_latest_first_and_skips_non_nccl_images():
    assert catalog_api._nccl_versions_from_images(CATALOG_IMAGES) == EXPECTED_VERSIONS


def test_derivation_is_empty_without_nccl_images():
    assert catalog_api._nccl_versions_from_images([{"order": 1, "filename": "nvbandwidth-v0.9-arm.sqsh"}]) == []
    assert catalog_api._nccl_versions_from_images([]) == []


def test_derivation_labels_fall_back_to_the_version():
    images = [{"order": 1, "filename": "something-nccl2.33.0.sqsh"}]
    assert catalog_api._nccl_versions_from_images(images) == [
        {"value": "2.33.0", "label": "2.33.0", "order": 1}
    ]


def test_derivation_dedupes_a_version_keeping_the_lowest_order():
    images = [
        {"order": 5, "name": "late", "filename": "a-nccl2.30.7.sqsh"},
        {"order": 2, "name": "early", "filename": "b-nccl2.30.7.sqsh"},
    ]
    assert catalog_api._nccl_versions_from_images(images) == [
        {"value": "2.30.7", "label": "early", "order": 2}
    ]


def test_catalog_nccl_versions_reads_the_served_catalog(monkeypatch):
    from api import p2p_fleet

    monkeypatch.setattr(
        p2p_fleet, "_load_catalog", lambda *a, cluster_id="", **k: {"images": CATALOG_IMAGES}
    )
    assert catalog_api._catalog_nccl_versions("nc") == EXPECTED_VERSIONS


# --- the injection through /api/catalog/tools -----------------------------------------------


def _tool(tool_id: str, *, show_nccl_version: bool) -> dict:
    """A match-all async-job tool so it survives the platform filter for any resolved cluster."""
    return models.Tool.from_dict(
        {
            "id": tool_id,
            "name": tool_id,
            "script": f"{tool_id}.sh",
            "compatible": {"match": "all"},
            "surfaces": ["rackDetail.validation"],
            "launch": {
                "kind": "async-job",
                "endpoint": f"/api/run-{tool_id}-async",
                "dialog": "launch-options",
                "body": {"show_nccl_version": show_nccl_version},
            },
        }
    ).to_dict()


@pytest.fixture
def client(tmp_path):
    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    repo.upsert(TABLE_TOOLS, _tool("llmb-nccl", show_nccl_version=True))
    repo.upsert(TABLE_TOOLS, _tool("sysinfo-plain", show_nccl_version=False))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    set_repo(None)


def test_list_tools_injects_catalog_versions_into_opted_in_tools(client, monkeypatch):
    from api import p2p_fleet

    monkeypatch.setattr(
        p2p_fleet, "_load_catalog", lambda *a, cluster_id="", **k: {"images": CATALOG_IMAGES}
    )

    data = client.get("/api/catalog/tools?cluster=nc").get_json()
    assert data["ok"] is True
    by_id = {t["id"]: t for t in data["items"]}

    assert by_id["llmb-nccl"]["launch"]["body"]["nccl_versions"] == EXPECTED_VERSIONS
    assert "nccl_versions" not in by_id["sysinfo-plain"]["launch"]["body"]


def test_list_tools_injects_nothing_when_the_catalog_has_no_nccl_images(client, monkeypatch):
    from api import p2p_fleet

    monkeypatch.setattr(
        p2p_fleet,
        "_load_catalog",
        lambda *a, cluster_id="", **k: {"images": [{"order": 1, "filename": "nvbandwidth-v0.9-arm.sqsh"}]},
    )

    data = client.get("/api/catalog/tools?cluster=nc").get_json()
    by_id = {t["id"]: t for t in data["items"]}
    assert "nccl_versions" not in by_id["llmb-nccl"]["launch"]["body"]


# --- the preset-card version subtitle, derived from the code-owned preset -------------------


def _preset_tool_doc(tool_id: str, preset_id: str, subtitle: str) -> dict:
    return models.Tool.from_dict(
        {
            "id": tool_id,
            "name": tool_id,
            "subtitle": subtitle,
            "script": "run-validation-preset.sh",
            "compatible": {"match": "all"},
            "surfaces": ["rackDetail.validation"],
            "launch": {"kind": "validation-preset", "preset_id": preset_id},
        }
    ).to_dict()


def test_preset_version_subtitle_derives_from_the_code_owned_preset():
    # The acceptance sweep pins BOTH a NCCL and a CUDA version -> a derived card subtitle.
    assert catalog_api._preset_version_subtitle("nccl-2.31.2") == "NCCL 2.30.7 · CUDA 13.0+"
    # Llama pins NCCL but not CUDA, so it keeps its prose subtitle (no version derivation).
    assert catalog_api._preset_version_subtitle("llama31-405b-fp8") is None
    assert catalog_api._preset_version_subtitle("not-a-preset") is None


def test_list_tools_shows_the_running_version_on_the_preset_card(tmp_path, monkeypatch):
    from api import p2p_fleet

    monkeypatch.setattr(p2p_fleet, "_load_catalog", lambda *a, cluster_id="", **k: {"images": []})
    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    # A stored card carrying a STALE version string, exactly as the deployed store does.
    repo.upsert(TABLE_TOOLS, _preset_tool_doc("preset-nccl", "nccl-2.31.2", "NCCL 2.31.2 · CUDA 13.3+"))
    app.config["TESTING"] = True
    try:
        with app.test_client() as c:
            items = c.get("/api/catalog/tools?cluster=nc").get_json()["items"]
    finally:
        set_repo(None)
    by_id = {t["id"]: t for t in items}
    # The stale stored subtitle is overridden by the version the preset will actually run.
    assert by_id["preset-nccl"]["subtitle"] == "NCCL 2.30.7 · CUDA 13.0+"
