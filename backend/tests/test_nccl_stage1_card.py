# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The rack Stage-1 card says what the selected launcher runs.

The card read "5 rounds all_reduce, 1K–16G" while RACK_NCCL_FULLRACK_LAUNCHER (default llmb) ran
llmb-run's 24-test LLMB set (Nscale NC, Slurm 207363: tests=24). The card is now derived from the
setting on every catalog read, and the setting is editable on the Settings page.
"""
from __future__ import annotations

import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import nccl_stage1
from app import app
from catalog import models
from catalog.store import TABLE_TOOLS, TinyDBRepo, set_repo


@pytest.mark.parametrize(("raw", "want"), [
    (None, "llmb"), ("", "llmb"), ("llmb", "llmb"), ("LLMB", "llmb"), ("something-else", "llmb"),
    ("mpi", "mpi"), ("sbatch", "mpi"), ("direct", "mpi"), ("nccl_mpi", "mpi"), (" MPI ", "mpi"),
])
def test_the_launcher_is_the_setting(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("RACK_NCCL_FULLRACK_LAUNCHER", raising=False)
    else:
        monkeypatch.setenv("RACK_NCCL_FULLRACK_LAUNCHER", raw)
    assert nccl_stage1.launcher() == want


def test_the_llmb_card_says_test_set_and_hides_options_it_ignores(monkeypatch):
    monkeypatch.delenv("RACK_NCCL_FULLRACK_LAUNCHER", raising=False)
    c = nccl_stage1.card()
    assert "rounds" not in c["subtitle"]
    assert "24 tests" in c["description"] and "alltoall" in c["description"]
    assert c["body"] == {"show_iters": False, "show_nccl_version": False}


def test_the_mpi_card_says_rounds_of_all_reduce(monkeypatch):
    monkeypatch.setenv("RACK_NCCL_FULLRACK_LAUNCHER", "mpi")
    c = nccl_stage1.card()
    assert c["subtitle"].startswith(f"{nccl_stage1.DEFAULT_MPI_ROUNDS} rounds all_reduce")
    assert c["body"] == {"show_iters": True, "show_nccl_version": True}


def test_only_the_stage1_tool_is_rewritten():
    other = {"subtitle": "keep", "launch": {"endpoint": "/api/run-llmb-nccl-async", "body": {"x": 1}}}
    nccl_stage1.apply_card(other)
    assert other == {"subtitle": "keep", "launch": {"endpoint": "/api/run-llmb-nccl-async", "body": {"x": 1}}}


def test_other_launch_options_survive_the_rewrite(monkeypatch):
    monkeypatch.delenv("RACK_NCCL_FULLRACK_LAUNCHER", raising=False)
    item = {"launch": {"endpoint": nccl_stage1.ENDPOINT, "body": {"show_iters": True, "keep": "me"}}}
    nccl_stage1.apply_card(item)
    assert item["launch"]["body"] == {"show_iters": False, "show_nccl_version": False, "keep": "me"}
    assert item["ai_context"] and item["description"] and item["subtitle"]


@pytest.fixture
def client(tmp_path):
    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    # An operator's catalog seeded before the fix still stores the old text.
    repo.upsert(TABLE_TOOLS, models.Tool.from_dict({
        "id": "nccl-fullrack-rounds", "name": "NCCL Benchmark (Stage-1)", "script": "nccl_fullrack_rounds.sh",
        "compatible": {"match": "all"}, "surfaces": ["rackDetail.validation"],
        "subtitle": "5 rounds all_reduce, 1K–16G",
        "description": "Standalone Stage-1 NCCL acceptance: N rounds of all_reduce (1K..16G).",
        "launch": {"kind": "async-job", "endpoint": "/api/run-nccl-fullrack-async", "dialog": "launch-options",
                   "body": {"show_nccl_version": True, "show_iters": True}},
    }).to_dict())
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    set_repo(None)


@pytest.mark.parametrize(("setting", "subtitle", "iters"), [
    (None, "LLMB NCCL test set (llmb-run)", False),
    ("mpi", f"{nccl_stage1.DEFAULT_MPI_ROUNDS} rounds all_reduce, up to 16G", True),
])
def test_the_served_card_follows_the_setting_over_stored_text(client, monkeypatch, setting, subtitle, iters):
    if setting is None:
        monkeypatch.delenv("RACK_NCCL_FULLRACK_LAUNCHER", raising=False)
    else:
        monkeypatch.setenv("RACK_NCCL_FULLRACK_LAUNCHER", setting)
    items = {t["id"]: t for t in client.get("/api/catalog/tools?cluster=nc").get_json()["items"]}
    card = items["nccl-fullrack-rounds"]
    assert card["subtitle"] == subtitle
    assert card["launch"]["body"]["show_iters"] is iters


def test_the_launcher_is_editable_on_the_settings_page():
    import config_env

    entry = next(e for e in config_env.JOB_ENV_UI_DEFINITIONS if e["key"] == "RACK_NCCL_FULLRACK_LAUNCHER")
    assert entry["section"] == "jobs_cluster" and "llmb" in entry["description"] and "mpi" in entry["description"]


@pytest.mark.parametrize(("setting", "script"), [(None, "run_llmb_nccl.sh"), ("mpi", "run_nccl_mpi.sh")])
def test_the_endpoint_runs_the_launcher_the_card_describes(monkeypatch, setting, script):
    from api import validation_runs

    if setting is None:
        monkeypatch.delenv("RACK_NCCL_FULLRACK_LAUNCHER", raising=False)
    else:
        monkeypatch.setenv("RACK_NCCL_FULLRACK_LAUNCHER", setting)
    monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda raw=None: "/logs")
    monkeypatch.setattr(validation_runs, "_script_exists", lambda p: True)
    monkeypatch.setattr(validation_runs, "_llmb_env_updates", lambda body, scope: {})
    with app.app_context():
        prep, err = validation_runs._prepare_run_nccl_fullrack({"rack": "p5-r01"})
    assert err is None, err and err[0].get_json()
    assert any(str(a).endswith(script) for a in prep["cmd"])
    if setting == "mpi":
        assert prep["cmd"][prep["cmd"].index("--rounds") + 1] == str(nccl_stage1.DEFAULT_MPI_ROUNDS)
