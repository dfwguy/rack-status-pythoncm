# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A NeMo launch runs on the hosts the dashboard placed, with the image the catalog names.

Nscale NC, 2026-10-11: a Multirack NeMo launch from the UI ran ``run-nemo-xrack.sh
'Q2-ct[01-18],Q5-ct[01-18]'`` -- tray names built from the template, hosts that do not exist -- and
then died on "set NEMO_SQSH_SRC". The single-rack NeMo launch died the same way (U20, the same day):
Rack Status had no NeMo image of its own, and the racks that passed Stage 2 were launched by an agent
that put the path in the request. The members now come from the node inventory, as the NCCL launch's
do, and the image from the catalog entry whose ``required_by`` names the tool.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module
import native_status as ns
import p2p_catalog
import validation_presets
from api import validation_runs
from catalog import models

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
Q2 = [f"sys1-gso1-p-phy-gpu{n}-su22" for n in (1, 2)]
Q5 = [f"sys1-gso1-p-phy-gpu{n}-su22" for n in (55, 56)]
STOCK = "nvidia+nemo+26.04.01-arm.sqsh"


# --- the hosts ----------------------------------------------------------------------------------


@pytest.fixture
def placed(monkeypatch):
    monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
    members = {"Q2": Q2, "Q5": Q5}
    monkeypatch.setattr(validation_runs, "_rack_node_hostnames_from_inventory", lambda r: members.get(r, []))
    monkeypatch.setattr(validation_runs, "_normalize_rack_id", lambda r: r)
    monkeypatch.setattr(validation_runs, "_script_exists", lambda _p: True)


def _prepare_xrack(tmp_path, racks):
    with app_module.app.app_context():
        return validation_runs._prepare_run_nemo_xrack({"racks": racks, "log_dir": str(tmp_path)})


def test_translate_hands_the_nemo_launcher_the_placed_hosts(placed, tmp_path):
    prep, err = _prepare_xrack(tmp_path, ["Q5", "Q2"])
    assert err is None
    assert prep["nodes"] == Q2 + Q5
    assert prep["cmd"][-4:] == ["--nodelist", ",".join(Q2 + Q5), "Q2", "Q5"]
    assert "-ct[" not in prep["display_cmd"]
    assert prep["display_cmd"].endswith(f"--nodelist {','.join(Q2 + Q5)} Q2 Q5")


def test_a_rack_with_no_placed_node_is_refused(placed, tmp_path):
    prep, err = _prepare_xrack(tmp_path, ["Q2", "Z9"])
    assert prep is None
    response, status = err
    assert status == 409 and "Z9" in response.get_json()["error"]


@pytest.mark.skipif(sys.platform != "linux", reason="the launcher uses GNU stat -c (Linux)")
def test_the_launcher_runs_exactly_the_listed_hosts_and_names_the_log_for_the_racks(tmp_path):
    """run-nemo-xrack.sh --nodelist end to end, with Slurm and pdsh stubbed."""
    bin_dir, logs, shared = tmp_path / "bin", tmp_path / "logs", tmp_path / "shared"
    for d in (bin_dir, logs, shared):
        d.mkdir()
    stubs = {
        "scontrol": '[[ "$1" == show && "$2" == hostname* ]] && tr "," "\\n" <<<"$3"; exit 0',
        "sbatch": 'printf "%s\\n" "$@" > "$SBATCH_ARGS_OUT"; echo "Submitted batch job 4242"',
        # One marker per host, which is what the launcher counts before it submits.
        "pdsh": ('while [[ $# -gt 0 ]]; do case "$1" in -w) h="$2"; shift 2;; -R) shift 2;; *) c="$*"; break;; esac; done\n'
                 'for x in ${h//,/ }; do case "$c" in *CACHE_OK*) echo "$x: CACHE_OK";; *GPU_READY*) echo "$x: GPU_READY";; esac; done'),
        "sinfo": "exit 0", "squeue": "exit 0", "srun": "exit 0",
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!/bin/bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    image = tmp_path / STOCK
    image.write_bytes(b"\0" * 4096)
    (shared / STOCK).write_bytes(b"\0" * 4096)
    args_out = tmp_path / "sbatch.args"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SBATCH_ARGS_OUT": str(args_out),
           "NEMO_SQSH_SRC": str(image), "NVIS_NEMO_USE_VETTED": "0", "RACK_JOB_LOG_DIR": str(logs),
           "RACK_VALIDATION_CONTAINER_ROOT": str(shared), "NVIS_NODE_CONTAINER_DIR": str(tmp_path / "node")}
    env.pop("NVIS_NEMO_XRACK", None)
    r = subprocess.run(["bash", str(SCRIPTS / "run-nemo-xrack.sh"), "--nodelist", ",".join(Q2 + Q5), "Q2", "Q5"],
                       cwd=logs, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    argv = args_out.read_text().splitlines()
    assert argv[argv.index("-w") + 1] == ",".join(Q2 + Q5)
    assert argv[argv.index("-N") + 1] == "4"
    assert f"--output={logs}/root-NEMO-Q2+Q5-4N-%j.txt" in argv
    assert any("NVIS_NEMO_XRACK=1" in a for a in argv), "two racks must take the cross-rack path"


# --- the image ----------------------------------------------------------------------------------


def _catalog(*images: tuple[str, list[str]]) -> models.P2pCatalog:
    return models.P2pCatalog(cluster_id="c1", images=[
        models.P2pCatalogImage(order=i, name=f, filename=f, required_by=req) for i, (f, req) in enumerate(images, 1)
    ])


@pytest.fixture
def cluster(monkeypatch):
    """An active cluster c1 whose shared container directory is /cluster/containers."""
    monkeypatch.setattr(validation_runs.catalog_context, "resolve", lambda: (None, SimpleNamespace(id="c1"), None))
    monkeypatch.setattr(validation_presets, "shared_container_root", lambda: "/cluster/containers")
    monkeypatch.setattr(validation_runs, "_eget", lambda key, default=None: default)

    def use(cat: models.P2pCatalog) -> None:
        monkeypatch.setattr(p2p_catalog, "get_catalog", lambda _cid, _repo=None: cat)

    return use


def _apply(tool_id: str, body: dict | None = None) -> tuple[dict, object]:
    prep: dict = {"env_updates": {}}
    with app_module.app.app_context():
        err = validation_runs._apply_tool_image(tool_id, body or {}, prep)
    return prep, err


@pytest.mark.parametrize("tool_id", ["validation-singlerack", "nemo-xrack"])
def test_the_catalog_image_that_names_the_tool_is_the_one_it_runs(cluster, tool_id):
    cluster(_catalog(("nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh", []),
                     (STOCK, ["validation-singlerack", "nemo-xrack"])))
    prep, err = _apply(tool_id)
    assert err is None
    assert prep["env_updates"]["NEMO_SQSH_SRC"] == f"/cluster/containers/{STOCK}"
    assert prep["image"] == f"/cluster/containers/{STOCK}"


def test_an_image_the_launch_names_wins(cluster):
    cluster(_catalog((STOCK, ["nemo-xrack"])))
    prep, err = _apply("nemo-xrack", {"env": {"NEMO_SQSH_SRC": "/cluster/containers/other.sqsh"}})
    assert err is None
    assert "NEMO_SQSH_SRC" not in prep["env_updates"]  # the body's own env carries it to the launcher
    assert prep["image"] == "/cluster/containers/other.sqsh"


def test_without_a_catalog_image_the_legacy_setting_still_serves(cluster, monkeypatch):
    cluster(_catalog((STOCK, [])))
    monkeypatch.setattr(validation_runs, "_eget", lambda key, default=None: "/srv/nemo.sqsh" if key == "NEMO_SQSH_SRC" else default)
    prep, err = _apply("validation-singlerack")
    assert err is None and prep["image"] == "/srv/nemo.sqsh"


def test_no_image_known_here_leaves_the_launcher_to_decide(cluster):
    """A site may set NEMO_SQSH_SRC where the launcher runs, which this process cannot see."""
    cluster(_catalog((STOCK, [])))
    prep, err = _apply("nemo-xrack")
    assert err is None
    assert prep == {"env_updates": {}}


def test_the_launchers_say_where_the_image_is_set():
    for script, tool in (("run-nemo-xrack.sh", "nemo-xrack"), ("run_singlerack.sh", "validation-singlerack")):
        text = (SCRIPTS / script).read_text()
        assert f"add this tool ({tool}) to the Required by" in text, script


def test_two_images_naming_one_tool_is_a_catalog_that_has_not_decided(cluster):
    cluster(_catalog((STOCK, ["nemo-xrack"]), ("nvidia+nemo+26.04.01-arm-nccl2.32.3.sqsh", ["nemo-xrack"])))
    _prep, err = _apply("nemo-xrack")
    response, status = err
    assert status == 409 and "more than one" in response.get_json()["error"]


def test_tools_without_an_image_contract_are_untouched(cluster):
    cluster(_catalog((STOCK, ["llama"])))
    prep, err = _apply("llama")
    assert err is None and prep == {"env_updates": {}}


def test_the_image_is_part_of_the_reviewed_plan():
    from validation_launch import launch_digest

    base = {"cmd": ["bash", "run-nemo-xrack.sh"], "racks": ["Q2", "Q5"]}
    assert launch_digest({**base, "image": "/a.sqsh"}) != launch_digest({**base, "image": "/b.sqsh"})
    assert launch_digest(base) == launch_digest(dict(base))  # absent stays absent


def test_image_for_tool_reads_required_by():
    cat = _catalog(("a.sqsh", ["x"]), (STOCK, ["nemo-xrack", "validation-singlerack"]))
    p2p_catalog_get = p2p_catalog.get_catalog
    try:
        p2p_catalog.get_catalog = lambda _cid, _repo=None: cat
        assert p2p_catalog.image_for_tool("c1", "nemo-xrack").filename == STOCK
        assert p2p_catalog.image_for_tool("c1", "llama") is None
        assert p2p_catalog.image_for_tool("c1", "") is None
    finally:
        p2p_catalog.get_catalog = p2p_catalog_get
