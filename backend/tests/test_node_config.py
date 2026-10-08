# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Settings published into the ``config/`` of the scripts a project runs -- the P2P image catalog first.

The main test deploys a real instance into a local tree, the way an install does, and follows the
catalog to where each project's launchers will look: a project that was never bootstrapped (NC:
project ``default`` on ``gb300-nvl72``, no platform layer) runs from ``multi-platform/``, a
bootstrapped one from its own ``project-<id>/``. A redeploy must leave what was published alone.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import host_exec
import node_config
import p2p_catalog
import pytest
import script_deploy
import script_tree
from catalog import models

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "bin" / "scripts"
BOOTSTRAP = REPO_ROOT / "bin" / "bootstrap-project-scripts.sh"


def _catalog(sha: str):
    return models.P2pCatalog.from_dict({
        "cluster_id": "nc",
        "images": [{"order": 1, "name": "NeMo", "filename": "nvidia+nemo+26.04.01-arm.sqsh",
                    "bytes": 42245001216, "sha256": sha}],
    })


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A local instance: the execution host is this machine, the deployment root is tmp."""
    base = tmp_path / "base"
    monkeypatch.setenv("RACK_EXEC_MODE", "local")
    monkeypatch.setenv("RACK_REMOTE_BASE_PATH", str(base))
    monkeypatch.delenv("NVIS_SCRIPTS_TREE", raising=False)
    host_exec.invalidate_connection()
    script_tree.clear_cache()
    yield base / "scripts"
    host_exec.invalidate_connection()
    script_tree.clear_cache()


def _lookup(scripts: Path, filename: str) -> list[str]:
    """What the deployed launcher library reads from the published catalog."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{scripts}/lib/node_images.inc.sh"; nvis_catalog_image "$1"', "_", filename],
        capture_output=True, text=True, check=False,
        env={k: v for k, v in os.environ.items() if k != "NVIS_NODE_CONFIG_DIR"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


def test_the_catalog_lands_where_each_project_runs(instance):
    tree = instance
    deployed = script_deploy.deploy()
    assert deployed["ok"], deployed
    # config/ ships with the scripts, empty but for its README.
    assert (tree / "multi-platform" / "config" / "README.md").is_file()
    assert not list(tree.rglob("image-catalog.*"))

    # NC's shape: never bootstrapped, and no gb300-nvl72 layer -> runs from multi-platform.
    nc = p2p_catalog.manifest_files(_catalog("ab" * 32), "/cluster/containers")
    out = node_config.publish(nc, project_id="default", platform_id="gb300-nvl72")
    assert out["ok"], out
    assert out["dest"] == str(tree / "multi-platform" / "config")
    assert _lookup(tree / "multi-platform", "nvidia+nemo+26.04.01-arm.sqsh") == ["42245001216", "ab" * 32]

    # A project that gets its own copy gets its own config/, and its own catalog in it.
    proc = subprocess.run(
        ["bash", str(BOOTSTRAP), "--json", "--project", "acme", "--platform", "gb300-nvl72", "--tree", str(tree)],
        capture_output=True, text=True, check=False, env={**os.environ, "RACK_REMOTE_BASE_PATH": ""},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["dest"] == str(tree / "project-acme")
    script_tree.clear_cache()
    assert (tree / "project-acme" / "config" / "README.md").is_file()
    acme = p2p_catalog.manifest_files(_catalog("cd" * 32))
    out = node_config.publish(acme, project_id="acme", platform_id="gb300-nvl72")
    assert out["ok"], out
    assert out["dest"] == str(tree / "project-acme" / "config")
    assert _lookup(tree / "project-acme", "nvidia+nemo+26.04.01-arm.sqsh") == ["42245001216", "cd" * 32]
    # ...without touching the project that still runs from the shared set.
    assert _lookup(tree / "multi-platform", "nvidia+nemo+26.04.01-arm.sqsh") == ["42245001216", "ab" * 32]

    # A redeploy updates the scripts and leaves every published catalog alone.
    assert script_deploy.deploy()["ok"]
    assert (tree / "multi-platform" / "config" / p2p_catalog.CONFIG_TSV).read_bytes() == nc[p2p_catalog.CONFIG_TSV]
    assert (tree / "project-acme" / "config" / p2p_catalog.CONFIG_TSV).read_bytes() == acme[p2p_catalog.CONFIG_TSV]


def test_nothing_is_published_before_the_scripts_are_deployed(instance):
    """Creating a scripts directory that holds only config/ would make a layer launchers resolve
    to with nothing in it."""
    out = node_config.publish({"image-catalog.tsv": b"x"}, project_id="default", platform_id="gb300-nvl72")
    assert out["ok"] is False
    assert "deploy the scripts first" in out["error"]
    assert not instance.exists()


def test_only_the_readme_of_config_ships(tmp_path):
    """A stray local publish in the source tree must never ride a deploy onto a cluster."""
    src = tmp_path / "scripts"
    (src / "config").mkdir(parents=True)
    (src / "config" / "README.md").write_text("doc")
    (src / "config" / "image-catalog.tsv").write_text("stale")
    (src / "run.sh").write_text("#!/bin/bash\n")
    _bundle, manifest = script_deploy.build_bundle(src)
    assert "multi-platform/config/README.md" in manifest
    assert not [m for m in manifest if "image-catalog" in m]
    rsync = (REPO_ROOT / "bin" / "deploy-scripts.sh").read_text()
    assert "--include='config/README.md' --exclude='config/*'" in rsync


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", "", "a b"])
def test_only_bare_names_are_published(instance, name):
    assert script_deploy.deploy()["ok"]
    out = node_config.publish({name: b"x"})
    assert not out["ok"]


@pytest.mark.parametrize("field", ["project_id", "platform_id"])
def test_ids_cannot_steer_the_destination(instance, field):
    out = node_config.publish({"a.tsv": b"x"}, **{field: "../x"})
    assert out["ok"] is False


class _NoExecConn:
    """Writes locally but cannot run commands -- the unconfigured-SSH case."""

    def makedirs(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        return ""

    def write_bytes(self, path, data):
        Path(path).write_bytes(data)
        return ""

    def is_dir(self, path):
        return Path(path).is_dir()

    def run(self, argv, timeout=None):
        raise RuntimeError("NotConfigured: no SSH host")


def test_nothing_is_written_where_commands_cannot_run(tmp_path, monkeypatch):
    """With no SSH host, makedirs/write_bytes hit the LOCAL disk and would plant files in the app's
    own tree. The nonce probe has to refuse before a byte is written."""
    monkeypatch.setattr(node_config.script_tree, "config_dir", lambda *a: str(tmp_path / "scripts" / "config"))
    monkeypatch.setattr(node_config.host_exec, "connection", lambda profile="script": _NoExecConn())
    out = node_config.publish({"a.tsv": b"x"})
    assert out["ok"] is False
    assert "cannot run commands" in out["error"]
    assert not (tmp_path / "scripts").exists()


def test_the_catalog_files_match_the_worker_format():
    files = p2p_catalog.manifest_files(_catalog("ab" * 32), "/cluster/containers")
    assert set(files) == {p2p_catalog.CONFIG_JSON, p2p_catalog.CONFIG_TSV}
    tsv = files[p2p_catalog.CONFIG_TSV].decode().splitlines()
    assert tsv[0] == "order\tname\tfilename\tbytes\tsha256"
    assert tsv[1] == f"1\tNeMo\tnvidia+nemo+26.04.01-arm.sqsh\t42245001216\t{'ab' * 32}"
    worker = (REPO_ROOT / "deploy/p2p/scripts/operations-worker.py").read_text()
    assert 'rows = ["order\\tname\\tfilename\\tbytes\\tsha256"]' in worker
    assert json.loads(files[p2p_catalog.CONFIG_JSON])["canonical_directory"] == "/cluster/containers"
