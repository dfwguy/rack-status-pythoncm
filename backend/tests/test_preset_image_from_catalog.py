# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A launch stages the image the cluster's catalog names -- never one written into the code.

The NeMo preset and the LLMB NCCL container carried the image's filename, byte size and SHA256 as
literals on both sides of the launch. NC's catalog was corrected to the real digest of
``nvidia+nemo+26.04.01-arm.sqsh`` (a03187c1...) while the literals kept the old one (55130500...),
so every preset launch failed ``IMAGE_STAGE_CORRUPT`` on a healthy image (Slurm 199859) -- while
Multirack NEMO, which reads the catalog, passed the same gate on the same nodes. Now every staging
launch resolves the entry whose Required by names it, and the launcher receives that entry.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import app as app_module
import pytest
import validation_launch
import validation_presets
from catalog import models

from tests.conftest import PRESET_IMAGES
from tests.test_validation_presets import _bash4

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "bin" / "scripts"
NEMO_FILE, NEMO_BYTES, NEMO_SHA = PRESET_IMAGES["nemo-26.04.01"]


def _image(filename: str, required_by: list[str], *, size: int = 1024, sha: str = "e5" * 32):
    return models.P2pCatalogImage(order=1, name=filename, filename=filename, bytes=size, sha256=sha,
                                  required_by=required_by)


class TestLaunchImage:
    def test_the_catalog_entry_that_names_the_preset_is_the_image(self, monkeypatch, preset_image_catalog):
        monkeypatch.setattr(validation_presets, "shared_container_root", lambda: "/cluster/containers")
        monkeypatch.setenv("NVIS_NODE_CONTAINER_DIR", "/opt/containers")
        image = validation_presets.launch_image("nemo-26.04.01")
        assert (image.filename, image.bytes, image.sha256) == (NEMO_FILE, NEMO_BYTES, NEMO_SHA)
        assert image.source == f"/cluster/containers/{NEMO_FILE}"
        assert image.local == f"/opt/containers/{NEMO_FILE}"
        assert image.env() == {"NVIS_LAUNCH_IMAGE": NEMO_FILE, "NVIS_LAUNCH_IMAGE_BYTES": str(NEMO_BYTES),
                               "NVIS_LAUNCH_IMAGE_SHA256": NEMO_SHA}
        assert image.artifact("local")["path"] == image.local
        assert image.artifact("nfs")["path"] == image.source

    def test_an_edit_in_the_catalog_is_what_the_launch_stages(self, preset_image_catalog):
        # The NC case: the catalog is corrected to the real digest; the launch must follow it.
        preset_image_catalog(models.P2pCatalog(cluster_id="default", images=[
            _image(NEMO_FILE, ["nemo-26.04.01"], size=42245001216, sha="a03187c1" + "0" * 56)]))
        assert validation_presets.launch_image("nemo-26.04.01").sha256 == "a03187c1" + "0" * 56

    def test_no_entry_names_the_fix(self, preset_image_catalog):
        preset_image_catalog(models.P2pCatalog(cluster_id="default", images=[_image(NEMO_FILE, ["nemo-xrack"])]))
        with pytest.raises(validation_presets.LaunchImageError) as err:
            validation_presets.launch_image("nemo-26.04.01")
        assert "required by 'nemo-26.04.01'" in str(err.value)
        assert "Required by" in str(err.value) and "Catalog" in str(err.value)

    @pytest.mark.parametrize(("size", "sha"), [(0, "e5" * 32), (1024, ""), (1024, "not-a-digest")])
    def test_an_unpinned_image_is_refused_not_staged_unchecked(self, preset_image_catalog, size, sha):
        preset_image_catalog(models.P2pCatalog(cluster_id="default", images=[
            _image(NEMO_FILE, ["nemo-26.04.01"], size=size, sha=sha)]))
        with pytest.raises(validation_presets.LaunchImageError, match="not pinned"):
            validation_presets.launch_image("nemo-26.04.01")

    def test_two_entries_naming_one_preset_are_refused(self, preset_image_catalog):
        preset_image_catalog(models.P2pCatalog(cluster_id="default", images=[
            _image("a.sqsh", ["nemo-26.04.01"]), _image("b.sqsh", ["nemo-26.04.01"])]))
        with pytest.raises(validation_presets.LaunchImageError, match="more than one"):
            validation_presets.launch_image("nemo-26.04.01")

    def test_no_active_cluster_is_refused(self, monkeypatch, preset_image_catalog):
        from catalog import context as catalog_context

        monkeypatch.setattr(catalog_context, "resolve", lambda: (None, None, None))
        with pytest.raises(validation_presets.LaunchImageError, match="no active cluster"):
            validation_presets.launch_image("nemo-26.04.01")

    def test_an_unknown_store_is_a_programming_error(self, preset_image_catalog):
        with pytest.raises(ValueError, match="unknown image store"):
            validation_presets.launch_image("nemo-26.04.01", "elsewhere")


class TestThePresetCatalogue:
    def test_every_staging_preset_names_a_store_and_no_image(self):
        for preset in validation_presets.PRESETS:
            assert preset.image_store in (None, *validation_presets.IMAGE_STORES), preset.id
        assert validation_presets.resolve_preset("nccl-2.31.2").image_store is None

    def test_the_picker_shows_the_catalog_image(self, preset_image_catalog):
        rows = {row["id"]: row for row in validation_presets.catalogue()}
        assert rows["nemo-26.04.01"]["image"]["sha256"] == NEMO_SHA
        assert rows["nemo-26.04.01"]["image"]["required_by"] == "nemo-26.04.01"
        assert rows["nccl-2.31.2"]["image"] is None

    def test_the_picker_says_what_to_add_when_the_catalog_lacks_it(self, preset_image_catalog):
        preset_image_catalog(models.P2pCatalog(cluster_id="default"))
        row = next(r for r in validation_presets.catalogue() if r["id"] == "nemo-26.04.01")
        assert "add nemo-26.04.01 to the Required by" in row["image"]["error"]


class TestTheConsumers:
    """The names a catalog image's Required by may carry -- what the Catalog Editor offers."""

    def test_every_staging_launch_is_offered(self):
        ids = [c["id"] for c in validation_presets.image_consumers()]
        staging = [p.id for p in validation_presets.PRESETS if p.image_store]
        assert ids[: len(staging)] == staging
        assert validation_presets.LLMB_NCCL_CONTAINER in ids
        assert "nccl-2.31.2" not in ids  # stages no container
        assert len(ids) == len(set(ids))

    def test_the_legacy_nemo_launchers_are_offered(self):
        # They resolve their image through _TOOL_IMAGE_ENV; a tool added there and not here would be
        # a launch the editor could never point an image at.
        from api import validation_runs

        ids = {c["id"] for c in validation_presets.image_consumers()}
        assert set(validation_runs._TOOL_IMAGE_ENV) <= ids


def _prepare(monkeypatch, tmp_path, body: dict):
    nodes = body["nodes"]
    monkeypatch.setattr(validation_launch, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_launch, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_launch, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(validation_launch, "_validation_node_eligibility",
                        lambda raw: ({node: "idle" for node in raw}, []))
    with app_module.app.app_context():
        return validation_launch._prepare_validation_preset(body)


_NEMO_BODY = {"preset_id": "nemo-26.04.01", "scope": "multirack", "storage": "local",
              "nodes": ["sys1-gpu1-su19", "sys1-gpu55-su19"]}


class TestThePlan:
    def test_the_plan_stages_and_hands_over_the_catalog_image(self, monkeypatch, tmp_path, preset_image_catalog):
        plan, err = _prepare(monkeypatch, tmp_path, dict(_NEMO_BODY))
        assert err is None and plan is not None
        assert plan["artifact"]["sha256"] == NEMO_SHA
        assert plan["artifact"]["size"] == NEMO_BYTES
        assert plan["env_updates"]["NVIS_LAUNCH_IMAGE"] == NEMO_FILE
        assert plan["env_updates"]["NVIS_LAUNCH_IMAGE_SHA256"] == NEMO_SHA
        assert "image catalog SHA256" in plan["checks"]

    def test_a_catalog_edit_after_review_changes_the_plan_hash(self, monkeypatch, tmp_path, preset_image_catalog):
        reviewed, _ = _prepare(monkeypatch, tmp_path, dict(_NEMO_BODY))
        preset_image_catalog(models.P2pCatalog(cluster_id="default", images=[
            _image(NEMO_FILE, ["nemo-26.04.01"], size=NEMO_BYTES, sha="f6" * 32)]))
        current, _ = _prepare(monkeypatch, tmp_path, dict(_NEMO_BODY))
        assert reviewed["plan_hash"] != current["plan_hash"]

    def test_a_preset_with_no_catalog_image_is_refused_before_the_scheduler(
        self, monkeypatch, tmp_path, preset_image_catalog,
    ):
        preset_image_catalog(models.P2pCatalog(cluster_id="default"))
        asked = []
        monkeypatch.setattr(validation_launch, "_resolve_log_dir", lambda _raw=None: tmp_path)
        monkeypatch.setattr(validation_launch, "_all_slurm_nodenames", lambda: _NEMO_BODY["nodes"])
        monkeypatch.setattr(validation_launch, "_validation_node_eligibility",
                            lambda raw: asked.append(raw) or ({}, []))
        with app_module.app.app_context():
            plan, err = validation_launch._prepare_validation_preset(dict(_NEMO_BODY))
        assert plan is None and err is not None
        assert err[1] == 409
        body = err[0].get_json()
        assert body["reason"] == "missing_preset_image" and body["preset_id"] == "nemo-26.04.01"
        assert asked == []

    def test_a_preset_without_a_container_needs_no_catalog(self, monkeypatch, tmp_path, preset_image_catalog):
        preset_image_catalog(models.P2pCatalog(cluster_id="default"))
        plan, err = _prepare(monkeypatch, tmp_path, {"preset_id": "nccl-2.31.2", "scope": "node",
                                                    "nodes": ["n1"], "storage": "nfs"})
        assert err is None and plan["artifact"] is None
        assert "NVIS_LAUNCH_IMAGE" not in plan["env_updates"]


#: Every source a launch's image passes through. A 64-hex literal in any of them is a digest that
#: will go stale the next time a site re-imports the image.
_LAUNCH_SOURCES = [
    REPO / "backend" / "validation_presets.py",
    REPO / "backend" / "validation_launch.py",
    REPO / "backend" / "api" / "validation_runs.py",
    SCRIPTS / "run-validation-preset.sh",
    SCRIPTS / "run-llmb-nccl-container.sh",
    SCRIPTS / "llama405b_fp8_1024.sbatch",
    SCRIPTS / "llama405b_fp8_4096.sbatch",
]


@pytest.mark.parametrize("path", _LAUNCH_SOURCES, ids=lambda p: p.name)
def test_no_launch_source_carries_an_image_digest(path):
    found = re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", path.read_text(encoding="utf-8"))
    assert found == [], f"{path.name} hard-codes an image digest; name it in the image catalog instead"


def _dispatch(tmp_path, env_extra: dict[str, str], *, preset: str = "nemo-26.04.01"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NVIS_LAUNCH_IMAGE")}
    env.update({"NVIS_PREPARED_NODE_COUNT": "2", "NVIS_PRESET_SCOPE_MULTIRACK": "1",
                "NVIS_NODE_CONTAINER_DIR": "/opt/containers", "RACK_VALIDATION_CONTAINER_ROOT": "/cluster/containers",
                "NVIS_NODE_IMAGES_P2P": "1", **env_extra})
    return subprocess.run(
        [_bash4(), str(SCRIPTS / "run-validation-preset.sh"), "--preset", preset,
         "--nodelist", "n1,n2", "--storage", "local", "--log-dir", str(tmp_path), "--dry-run"],
        capture_output=True, text=True, env=env, check=False, timeout=60,
    )


class TestTheDispatcher:
    def test_it_gates_on_the_image_rack_status_hands_it(self, tmp_path):
        proc = _dispatch(tmp_path, {"NVIS_LAUNCH_IMAGE": NEMO_FILE, "NVIS_LAUNCH_IMAGE_BYTES": str(NEMO_BYTES),
                                    "NVIS_LAUNCH_IMAGE_SHA256": NEMO_SHA, "NVIS_NODE_CONFIG_DIR": str(tmp_path)})
        assert proc.returncode == 0, proc.stderr
        assert f"NVIS_STAGE_EXPECTED_SHA256={NEMO_SHA}" in proc.stdout
        assert f"NVIS_STAGE_EXPECTED_SIZE={NEMO_BYTES}" in proc.stdout
        assert f"NVIS_STAGE_SOURCE=/cluster/containers/{NEMO_FILE}" in proc.stdout
        assert f"NVIS_NEMO_CONT=/opt/containers/{NEMO_FILE}" in proc.stdout

    def test_a_manual_run_naming_only_the_file_reads_the_published_catalog(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        (config / "image-catalog.tsv").write_text(
            "order\tname\tfilename\tbytes\tsha256\n"
            f"1\tNeMo\t{NEMO_FILE}\t{NEMO_BYTES}\t{'9a' * 32}\n", encoding="utf-8")
        proc = _dispatch(tmp_path, {"NVIS_LAUNCH_IMAGE": NEMO_FILE, "NVIS_NODE_CONFIG_DIR": str(config)})
        assert proc.returncode == 0, proc.stderr
        assert f"NVIS_STAGE_EXPECTED_SHA256={'9a' * 32}" in proc.stdout

    def test_with_no_image_it_stops_and_says_where_to_name_one(self, tmp_path):
        proc = _dispatch(tmp_path, {"NVIS_NODE_CONFIG_DIR": str(tmp_path)})
        assert proc.returncode == 2
        assert "add nemo-26.04.01 to the Required by" in proc.stderr
        assert "PLAN:" not in proc.stdout

    def test_an_image_the_published_catalog_does_not_pin_is_refused(self, tmp_path):
        proc = _dispatch(tmp_path, {"NVIS_LAUNCH_IMAGE": NEMO_FILE, "NVIS_NODE_CONFIG_DIR": str(tmp_path)})
        assert proc.returncode == 2
        assert "is not pinned in the image catalog" in proc.stderr

    @pytest.mark.parametrize("name", ["../etc/passwd.sqsh", "/abs/path.sqsh", "x.tar"])
    def test_the_image_must_be_a_bare_squashfs_name(self, tmp_path, name):
        proc = _dispatch(tmp_path, {"NVIS_LAUNCH_IMAGE": name, "NVIS_LAUNCH_IMAGE_BYTES": "1",
                                    "NVIS_LAUNCH_IMAGE_SHA256": "e5" * 32})
        assert proc.returncode == 2
        assert "bare *.sqsh name" in proc.stderr


def test_the_activity_for_a_multirack_preset_is_tagged_multirack(monkeypatch, tmp_path, preset_image_catalog):
    """End to end through the launch route: the record the activities table draws says multirack."""
    from api import validation_runs

    recorded = {}
    monkeypatch.setattr(validation_runs, "_record_validation_activity",
                        lambda tool, **kw: recorded.update(tool=tool, **kw) or kw["activity_id"])
    plan = {"preset_id": "nemo-26.04.01", "scope": "multirack", "racks": ["U23", "U24"], "nodes": ["a", "b"],
            "node_count": 2, "log_dir": str(tmp_path), "command_display": "x", "plan_hash": "h", "cmd": ["true"],
            "timeout": 60, "env_updates": {}}
    monkeypatch.setattr(validation_runs, "_validation_preset_compatibility_error", lambda _b: None)
    monkeypatch.setattr(validation_runs, "_prepare_validation_preset", lambda _b: (plan, None))
    monkeypatch.setattr(validation_runs.threading, "Thread", lambda **_kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(validation_runs, "_stress_jobs", {})
    with app_module.app.test_request_context():
        validation_runs._run_validation_preset_async({"plan_hash": "h"})
    assert recorded["tool"] == "nemo-xrack" and recorded["scope"] == "multirack"

    import validation_activities as va

    monkeypatch.setattr(va, "ACTIVITIES_FILE", tmp_path / "validation_activities.json")
    monkeypatch.setattr(va, "ACTIVITY_ARCHIVE_DB", tmp_path / "validation_activities.sqlite3")
    monkeypatch.setattr(va, "_archive_sync_signature", None)
    kw = {k: v for k, v in recorded.items() if k not in ("tool",)}
    aid = va.record_activity(recorded["tool"], **{k: kw[k] for k in ("stage", "scope", "rack", "racks", "log_dir",
                                                                       "command_display", "activity_id")})
    assert va.get_activity(aid)["stage"] == "multirack"
