# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""GB300 Llama 3.1 405B Exemplar launch target and profile validation."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module
import naming as naming_mod
import nvis_id_templates as nid
from api import validation_runs

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
RACKS = [f"ink50-dh01-d{n:02d}" for n in range(19, 23)] + [
    f"ink50-dh01-j{n:02d}" for n in range(4, 8)
]


def _prepare(monkeypatch, body: dict):
    naming = nid.get_nvis_naming(
        "ink50-dh01-[a-z][\\d+]",
        "ink50-dh01-[a-z][\\d+]-[\\d+]",
    )
    monkeypatch.setattr(validation_runs, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(naming_mod, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(validation_runs, "_script_exists", lambda _path: True)
    original_eget = validation_runs._eget

    def fake_eget(key: str, default: str | None = None):
        if key == "RACK_EXEMPLAR_LLAMA_CT_RANGE":
            return "11-18,28-35"
        if key == "RACK_LLMB_INSTALL":
            return "/home/cmods/workspace/llmb"
        return original_eget(key, default)

    monkeypatch.setattr(validation_runs, "_eget", fake_eget)
    with app_module.app.app_context():
        return validation_runs._prepare_run_exemplar_llama(body)


def test_exact_eight_rack_target_is_512_gpu_subset(monkeypatch):
    prepared, err = _prepare(
        monkeypatch,
        {
            "racks": RACKS,
            "dtype": "nvfp4",
            "image_profile": "nccl2.30.7",
            "tuning": "baseline",
        },
    )
    assert err is None
    assert prepared is not None
    assert prepared["nodelist"] == (
        "ink50-dh01-d19-[11-18,28-35],ink50-dh01-d20-[11-18,28-35],"
        "ink50-dh01-d21-[11-18,28-35],ink50-dh01-d22-[11-18,28-35],"
        "ink50-dh01-j04-[11-18,28-35],ink50-dh01-j05-[11-18,28-35],"
        "ink50-dh01-j06-[11-18,28-35],ink50-dh01-j07-[11-18,28-35]"
    )
    assert prepared["dtype"] == "nvfp4"
    assert "--image nccl2.30.7" in prepared["display_cmd"]
    assert "--tuning baseline" in prepared["display_cmd"]


def test_each_racks_hosts_travel_with_the_launch_for_the_rank_layout(monkeypatch):
    prepared, err = _prepare(monkeypatch, {"racks": RACKS, "dtype": "fp8"})
    assert err is None and prepared is not None
    placement = prepared["env_updates"]["NVIS_NODE_PLACEMENT"].split(";")
    assert placement[0] == "ink50-dh01-d19=ink50-dh01-d19-[11-18,28-35]"
    assert len(placement) == 8


def test_fp8_stock_control_and_dry_run_are_explicit(monkeypatch):
    prepared, err = _prepare(
        monkeypatch,
        {
            "racks": RACKS,
            "dtype": "fp8",
            "image_profile": "stock",
            "tuning": "gb300",
            "dry_run": True,
        },
    )
    assert err is None
    assert prepared is not None
    assert prepared["dry_run"] is True
    assert prepared["cmd"][-1] == "--dry-run"
    assert prepared["dtype"] == "fp8"
    assert prepared["image_profile"] == "stock"
    assert prepared["tuning"] == "gb300"


def test_requires_exactly_eight_unique_racks(monkeypatch):
    for racks in (RACKS[:7], RACKS + [RACKS[0]]):
        prepared, err = _prepare(monkeypatch, {"racks": racks})
        if len(racks) == 9:
            # A duplicate ninth token still resolves to the valid eight-rack set.
            assert err is None
            assert prepared is not None
        else:
            assert prepared is None
            assert err is not None
            response, status = err
            assert status == 400
            assert "exactly 8 racks" in response.get_json()["error"]


def test_rejects_invalid_precision_image_and_tuning(monkeypatch):
    for key, value in (
        ("dtype", "bf16"),
        ("image_profile", "latest"),
        ("tuning", "magic"),
    ):
        prepared, err = _prepare(monkeypatch, {"racks": RACKS, key: value})
        assert prepared is None
        assert err is not None
        assert err[1] == 400


def test_wrapper_pins_model_scale_and_baked_nccl_image():
    script = (SCRIPTS / "run-exemplar-llama.sh").read_text(encoding="utf-8")
    assert "--workload pretrain_llama3.1" in script
    assert "--model-size 405b" in script
    assert "--scale 512" in script
    assert "nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh" in script
    assert "--env NCCL_NVLS_ENABLE=0" in script
    assert "NCCL_IB_QPS_PER_CONNECTION=4" in script
    assert '[[ "$job_name" != bringup-* ]]' in script
    assert "non-yieldable job" in script
