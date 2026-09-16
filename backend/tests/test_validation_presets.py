# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import app as app_module
import pytest
import validation_presets
import validation_provenance

REPO = Path(__file__).resolve().parents[2]


def _bash4() -> str:
    """Path to a bash that can run the dispatcher, or skip.

    ``run-validation-preset.sh`` needs bash 4 for ``declare -A`` and ``mapfile``, which is what the
    head node has. macOS ships bash 3.2 as /bin/bash, so invoking a bare "bash" here failed with
    ``declare: -A: invalid option`` on every developer Mac while passing in CI -- a red suite that
    says nothing about the code. Prefer a real bash 4+ if one is installed, and skip with the reason
    if not, so the failure cannot be mistaken for a defect.
    """
    for cand in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        exe = shutil.which(cand) if "/" not in cand else (cand if os.access(cand, os.X_OK) else None)
        if not exe:
            continue
        try:
            major = subprocess.run(
                [exe, "-c", 'printf %s "${BASH_VERSINFO[0]}"'],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if major.isdigit() and int(major) >= 4:
            return exe
    pytest.skip("needs bash 4+ for declare -A / mapfile (macOS /bin/bash is 3.2; brew install bash)")


def test_live_eligibility_resolves_sinfo_through_platform(monkeypatch):
    node = "ink50-dh01-j21-11"
    captured = {}

    def run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, f"{node}|idle\n", "")

    monkeypatch.setattr(
        app_module,
        "_get_platform",
        lambda: SimpleNamespace(
            sinfo_cmd=lambda args: f"/site/slurm/bin/sinfo {args}"
        ),
    )
    monkeypatch.setattr(app_module.host_exec, "run_slurm_on_host", run)
    monkeypatch.setattr(app_module.scheduler_runtime, "is_pythoncm", lambda: False)
    states, reasons = app_module._validation_node_eligibility([node])
    assert captured["cmd"].startswith("/site/slurm/bin/sinfo ")
    assert states == {node: "idle"}
    assert reasons == []


def test_slurm_fallback_eligibility_bypasses_pythoncm_state(monkeypatch):
    node = "ink50-dh01-a02-11"
    captured = {}

    def run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, f"{node}|idle\n", "")

    monkeypatch.setattr(
        app_module,
        "_get_platform",
        lambda: SimpleNamespace(sinfo_cmd=lambda args: f"/site/slurm/bin/sinfo {args}"),
    )
    monkeypatch.setattr(app_module.host_exec, "run_slurm_on_host", run)
    monkeypatch.setattr(app_module.scheduler_runtime, "is_pythoncm", lambda: True)
    states, reasons = app_module._validation_node_eligibility([node], require_slurm=True)
    assert captured["cmd"].startswith("/site/slurm/bin/sinfo ")
    assert states == {node: "idle"}
    assert reasons == []


def test_slurm_not_responding_idle_node_is_ineligible(monkeypatch):
    node = "ink50-dh02-f04-12"

    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, f"{node}|idle*\n", "")

    monkeypatch.setattr(
        app_module,
        "_get_platform",
        lambda: SimpleNamespace(sinfo_cmd=lambda args: f"/site/slurm/bin/sinfo {args}"),
    )
    monkeypatch.setattr(app_module.host_exec, "run_slurm_on_host", run)
    states, reasons = app_module._validation_node_eligibility([node], require_slurm=True)
    assert states == {node: "idle*"}
    assert reasons == [f"{node}=idle*"]


def test_nemo_prepare_requires_live_slurm_eligibility(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-a17-11", "ink50-dh01-a17-12"]
    captured = {}

    def eligibility(raw, *, require_slurm=False):
        captured["require_slurm"] = require_slurm
        return ({node: "idle" for node in raw}, [])

    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())
    monkeypatch.setattr(app_module, "_validation_node_eligibility", eligibility)
    monkeypatch.setattr(app_module, "_verified_validation_provenance", lambda _profile: {})
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "nemo-26.04.01",
            "scope": "rack",
            "racks": ["a17"],
            "nodes": nodes,
            "storage": "local",
            "rsync_to_local": False,
        })
    assert err is None
    assert plan is not None
    assert captured["require_slurm"] is True


def test_mnubergemm_prepare_requires_live_slurm_eligibility(monkeypatch, tmp_path):
    node = "ink50-dh01-a17-11"
    captured = {}

    def eligibility(raw, *, require_slurm=False):
        captured["require_slurm"] = require_slurm
        return ({item: "idle" for item in raw}, [])

    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: [node])
    monkeypatch.setattr(app_module, "_validation_node_eligibility", eligibility)
    monkeypatch.setattr(app_module, "_verified_validation_provenance", lambda _profile: {})
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "mnubergemm-26.03-v1.17",
            "scope": "node",
            "nodes": [node],
            "storage": "local",
            "rsync_to_local": False,
        })
    assert err is None
    assert plan is not None
    assert captured["require_slurm"] is True


def test_llama_prepare_requires_live_slurm_eligibility(monkeypatch, tmp_path):
    nodes = [f"ink50-dh02-k{i // 16 + 1:02d}-{i % 16 + 11}" for i in range(256)]
    captured = {}

    def eligibility(raw, *, require_slurm=False):
        captured["require_slurm"] = require_slurm
        return ({item: "idle" for item in raw}, [])

    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(app_module, "_validation_node_eligibility", eligibility)
    monkeypatch.setattr(app_module, "_verified_validation_provenance", lambda _profile: {})
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "llama31-405b-fp8",
            "scope": "multirack",
            "nodes": nodes,
            "storage": "local",
            "rsync_to_local": False,
        })
    assert err is None
    assert plan is not None
    assert captured["require_slurm"] is True


@pytest.mark.parametrize("preset_id", ("gpt-oss-cpu", "exemplar-system-info"))
def test_external_preset_prepare_requires_live_slurm_eligibility(
    monkeypatch, tmp_path, preset_id
):
    node = "ink50-dh02-e06-11"
    captured = {}

    def eligibility(raw, *, require_slurm=False):
        captured["require_slurm"] = require_slurm
        return ({item: "idle" for item in raw}, [])

    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: [node])
    monkeypatch.setattr(app_module, "_validation_node_eligibility", eligibility)
    monkeypatch.setattr(
        app_module,
        "_external_validation_preset_availability",
        lambda _preset: {"available": True, "status": "ready", "reasons": [], "prerequisites": []},
    )
    monkeypatch.setattr(app_module, "_verified_validation_provenance", lambda _profile: {})
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": preset_id,
            "scope": "node",
            "nodes": [node],
            "storage": "local",
            "rsync_to_local": False,
        })
    assert err is None
    assert plan is not None
    assert captured["require_slurm"] is True


def test_legacy_nemo_prepare_passes_shared_log_and_runtime_identity(monkeypatch, tmp_path):
    script = tmp_path / "run_singlerack.sh"
    log_dir = tmp_path / "shared-logs"
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: log_dir)
    monkeypatch.setattr(app_module, "_validation_script_path", lambda: script)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())
    monkeypatch.setattr(
        app_module,
        "validation_target_for_rack",
        lambda _rack, _target=None: "ink50-dh01-a17-11,ink50-dh01-a17-12",
    )
    monkeypatch.setattr(
        app_module,
        "_validation_runtime_env",
        lambda: {
            "NVIS_SLURM_SUBMIT_UID": "1003",
            "NVIS_SLURM_SUBMIT_GID": "1003",
            "NVIS_SLURM_SUBMIT_HOME": "/home/cmods",
        },
    )

    with app_module.app.app_context():
        prep, err = app_module._prepare_run_validation({"rack": "a17"})

    assert err is None
    assert prep is not None
    assert prep["cmd"] == [
        "bash",
        str(script),
        "ink50-dh01-a17-11,ink50-dh01-a17-12",
        "a17",
        str(log_dir),
    ]
    assert prep["timeout"] == 43200
    assert prep["env_updates"]["NVIS_SLURM_SUBMIT_UID"] == "1003"
    assert str(log_dir) in prep["display_cmd"]


def test_catalogue_is_honest_and_version_pins_are_immutable():
    rows = {row["id"]: row for row in validation_presets.catalogue()}
    assert rows["llama31-405b-fp8"]["requirements"]["exact_nodes"] == 256
    assert "1024 GPUs" in rows["llama31-405b-fp8"]["label"]
    assert rows["nccl-2.29.3"]["defaults"]["cuda_version"] == "13.1"
    assert rows["nccl-2.29.3"]["defaults"]["nccl_version"] == "2.29.3"
    assert rows["nccl-2.29.3"]["defaults"]["storage"] == "local"
    assert rows["nccl-2.29.3"]["defaults"]["rsync"] is False
    assert "500 iterations" in rows["nccl-2.29.3"]["label"]
    assert rows["llama31-405b-fp8"]["defaults"]["storage"] == "local"
    assert rows["llama31-405b-fp8"]["defaults"]["rsync"] is True
    assert rows["llama31-405b-fp8"]["defaults"]["local_container_dir"] == "/opt/containers"
    assert "mnubergemm-26.03-v1.17" in rows
    assert "multirack" in rows["mnubergemm-26.03-v1.17"]["requirements"]["scopes"]
    assert rows["mnubergemm-26.03-v1.17"]["defaults"]["max_runtime_minutes"] == 30
    assert "mnubergemm-26.04.1" not in rows
    with pytest.raises(ValueError, match="unknown validation preset"):
        validation_presets.resolve_preset("mnubergemm-26.04.1")
    llama = validation_presets.resolve_preset("llama31-405b-fp8")
    with pytest.raises(ValueError, match="max_steps=50"):
        validation_presets.normalize_options({"scope": "multirack", "max_steps": 13}, llama)
    mnubergemm = validation_presets.resolve_preset("mnubergemm-26.03-v1.17")
    with pytest.raises(ValueError, match="max_runtime_minutes=30"):
        validation_presets.normalize_options(
            {"scope": "multirack", "max_runtime_minutes": 61}, mnubergemm
        )


def test_image_roots_are_configurable_but_artifacts_remain_allowlisted():
    nemo = validation_presets.resolve_image_paths(
        validation_presets.resolve_preset("nemo-26.04.01"),
        shared_container_root="/reviewed/images",
        local_container_dir="/opt/site-containers",
    )
    assert nemo.shared_image == "/reviewed/images/nvidia+nemo+26.04.01.sqsh"
    assert nemo.local_image == "/opt/site-containers/nvidia+nemo+26.04.01.sqsh"
    assert nemo.image_size == 42245001216
    assert nemo.image_sha256 == "e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca"
    with pytest.raises(ValueError, match="absolute normalized path"):
        validation_presets.resolve_image_paths(nemo, shared_container_root="../images")


def test_prepare_resolves_exact_nodes_for_gpt_fanout(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d01-12"]
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(
        app_module,
        "_external_validation_preset_availability",
        lambda _preset: {"available": True, "status": "ready", "reasons": [], "prerequisites": []},
    )
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda raw, **_kwargs: ({node: "idle" for node in raw}, []),
    )
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "gpt-oss-cpu", "scope": "node", "nodes": nodes,
            "storage": "nfs",
        })
    assert err is None
    assert plan is not None
    assert plan["nodes"] == nodes
    assert plan["node_count"] == 2
    assert plan["preset_id"] == "gpt-oss-cpu"
    assert plan["env_updates"]["NVIS_PREPARED_NODE_COUNT"] == "2"


def test_llama_prepare_is_exact_1024_gpu_contract(monkeypatch, tmp_path):
    nodes = [f"ink50-dh01-d{i // 16 + 1:02d}-{i % 16 + 1}" for i in range(256)]
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda raw, **_kwargs: ({node: "idle" for node in raw}, []),
    )
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "llama31-405b-fp8", "scope": "multirack", "nodes": nodes,
            "storage": "local", "rsync": True, "max_steps": 50,
        })
    assert err is None
    assert plan is not None
    assert plan["node_count"] == 256
    assert plan["max_steps"] == 50
    assert plan["nccl_version"] == "2.30.7"
    assert plan["artifact"]["sha256"] == "0829d0c77f38db54848afd095862093a7c62e98778b0b8664b8edff69e273747"
    assert plan["env_updates"]["NVIS_PREPARED_NODE_COUNT"] == "256"
    assert plan["env_updates"]["NVIS_PRESET_SCOPE_MULTIRACK"] == "1"
    assert plan["env_updates"]["RACK_VALIDATION_NODE_CONTAINER_DIR"] == "/opt/containers"
    assert len(plan["plan_hash"]) == 64


def test_backend_routes_publish_prepare_contract(monkeypatch, tmp_path):
    node = "ink50-dh01-d01-11"
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: [node])
    monkeypatch.setattr(
        app_module,
        "_external_validation_preset_availability",
        lambda _preset: {"available": True, "status": "ready", "reasons": [], "prerequisites": []},
    )
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda raw, **_kwargs: ({item: "idle" for item in raw}, []),
    )
    client = app_module.app.test_client()
    response = client.get("/api/validation-presets")
    assert response.status_code == 200
    assert response.get_json()["presets"]
    response = client.post("/api/validation-presets/prepare", json={
        "preset_id": "gpt-oss-cpu", "scope": "node", "nodes": [node], "storage": "nfs",
    })
    assert response.status_code == 200
    plan = response.get_json()["plan"]
    assert plan["nodes"] == [node]
    assert plan["command_display"]
    assert len(plan["plan_hash"]) == 64
    assert "cmd" not in plan
    launch = client.post("/api/run-validation-preset-async", json={
        "preset_id": "gpt-oss-cpu", "scope": "node", "nodes": [node], "storage": "nfs",
    })
    assert launch.status_code == 409
    assert "plan_hash" in launch.get_json()["error"]


def test_external_catalogue_readiness_lists_exact_missing_paths(monkeypatch):
    monkeypatch.delenv("LLMB_INSTALL", raising=False)
    monkeypatch.delenv("LLMB_REPO", raising=False)
    monkeypatch.setattr(app_module.host_exec, "is_file", lambda _path: False)
    monkeypatch.setattr(app_module.host_exec, "is_dir", lambda _path: False)

    gpt = app_module._external_validation_preset_availability(
        validation_presets.resolve_preset("gpt-oss-cpu")
    )
    exemplar = app_module._external_validation_preset_availability(
        validation_presets.resolve_preset("exemplar-system-info")
    )

    assert gpt["available"] is False
    assert any("tensorrt-llm+release+1.1.0rc5.sqsh" in reason for reason in gpt["reasons"])
    assert any("gpt-oss-120b/metal/model.bin" in reason for reason in gpt["reasons"])
    assert any("cpu_overhead/launch.sh" in reason for reason in gpt["reasons"])
    assert exemplar["available"] is False
    assert any("nvidia+nemo+26.04.01-arm.sqsh" in reason for reason in exemplar["reasons"])
    assert any("microbenchmarks/system_info/launch.sh" in reason for reason in exemplar["reasons"])


def test_external_prepare_fails_before_scheduler_when_prerequisites_are_missing(
    monkeypatch, tmp_path
):
    node = "ink50-dh02-e06-11"
    missing = "/cluster/llmb/images/tensorrt-llm+release+1.1.0rc5.sqsh"
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(
        app_module,
        "_external_validation_preset_availability",
        lambda _preset: {
            "available": False,
            "status": "blocked",
            "reasons": [f"missing required shared_image: {missing}"],
            "prerequisites": [
                {"name": "shared_image", "kind": "file", "path": missing, "present": False}
            ],
        },
    )
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda *_args, **_kwargs: pytest.fail("scheduler eligibility must not run"),
    )

    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "gpt-oss-cpu",
            "scope": "node",
            "nodes": [node],
            "storage": "local",
        })

    assert plan is None
    response, status = err
    assert status == 409
    payload = response.get_json()
    assert payload["availability"]["status"] == "blocked"
    assert missing in payload["error"]


def test_mnubergemm_multirack_plan_is_exact_and_time_capped(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d02-11"]
    rack_nodes = {
        "ink50-dh01-d01": [nodes[0]],
        "ink50-dh01-d02": [nodes[1]],
    }
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda rack: rack)
    monkeypatch.setattr(app_module, "_multirack_campaign_snapshot", lambda: {"active": False})
    monkeypatch.setattr(app_module, "_rack_node_hostnames_from_inventory", lambda rack: rack_nodes[rack])
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda raw, *, require_slurm=False: ({node: "idle" for node in raw}, []),
    )
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "mnubergemm-26.03-v1.17",
            "scope": "multirack",
            "racks": list(rack_nodes),
            "storage": "nfs",
        })
    assert err is None
    assert plan is not None
    assert plan["nodes"] == nodes
    assert plan["node_groups"] == [
        {"rack": "ink50-dh01-d01", "nodes": [nodes[0]]},
        {"rack": "ink50-dh01-d02", "nodes": [nodes[1]]},
    ]
    assert plan["scope"] == "multirack"
    assert plan["max_runtime_minutes"] == 30
    assert "MNUberGEMM payload duration=1800 seconds" in plan["checks"]
    assert any("2400 seconds" in check or "TimeLimit=00:40:00" in check for check in plan["checks"])
    assert plan["env_updates"]["NVIS_PRESET_SCOPE_MULTIRACK"] == "1"
    assert plan["cmd"].count("--group-nodelist") == 2
    assert nodes[0] in plan["command_display"] and nodes[1] in plan["command_display"]

    with app_module.app.app_context():
        unsafe_plan, unsafe_err = app_module._prepare_validation_preset({
            "preset_id": "mnubergemm-26.03-v1.17",
            "scope": "multirack",
            "nodes": nodes,
            "storage": "nfs",
        })
    assert unsafe_plan is None
    assert unsafe_err is not None
    assert unsafe_err[1] == 409
    assert "one resolvable exact-node group" in unsafe_err[0].get_json()["error"]


def test_mnubergemm_stage_only_plan_cannot_launch_workload(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d02-11"]
    rack_nodes = {
        "ink50-dh01-d01": [nodes[0]],
        "ink50-dh01-d02": [nodes[1]],
    }
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda rack: rack)
    monkeypatch.setattr(app_module, "_multirack_campaign_snapshot", lambda: {"active": False})
    monkeypatch.setattr(app_module, "_rack_node_hostnames_from_inventory", lambda rack: rack_nodes[rack])
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda raw, *, require_slurm=False: ({node: "idle" for node in raw}, []),
    )
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "mnubergemm-26.03-v1.17",
            "scope": "multirack",
            "racks": list(rack_nodes),
            "storage": "local",
            "rsync_to_local": True,
            "stage_only": True,
        })
    assert err is None
    assert plan is not None
    assert plan["stage_only"] is True
    assert plan["cmd"] == []
    assert "MNUberGEMM_STAGE_ONLY" in plan["command_display"]
    assert "stage-only: no MNUberGEMM workload submission" in plan["checks"]
    assert plan["artifact"]["path"] == "/opt/containers/mnubergemm-26.03-v1.17-arm.sqsh"


def test_mnubergemm_stress_prepare_forwards_bounded_canary_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda rack: rack)
    monkeypatch.setattr(app_module, "validation_target_for_rack", lambda _rack, target=None: target or "ink50-dh01-d01")
    monkeypatch.setattr(app_module, "_mnubergemm_stress_script_path", lambda: tmp_path / "run_mnubergemm_stress.sh")
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    with app_module.app.app_context():
        prep, err = app_module._prepare_run_mnubergemm_stress({
            "rack": "ink50-dh01-d01",
            "target": "ink50-dh01-d01-11",
            "runtime_seconds": 300,
            "walltime": "00:10:00",
            "storage": "local",
        })
    assert err is None
    assert prep is not None
    assert prep["env_updates"]["RACK_MNUBERGEMM_TIME_TO_RUN"] == "300"
    assert prep["env_updates"]["RACK_MNUBERGEMM_TIME"] == "00:10:00"
    assert prep["env_updates"]["RACK_MNUBERGEMM_CONT"].startswith("/opt/containers/")
    assert "RACK_MNUBERGEMM_TIME_TO_RUN=300" in prep["display_cmd"]


def test_mnubergemm_dispatcher_dry_run_prints_bounded_slurm_command(tmp_path):
    script = REPO / "bin/scripts/run-validation-preset.sh"
    node_scripts = "/cluster/OVA/scripts/nscaletx-16k-validation/runtime/bin/scripts"
    env = {
        **os.environ,
        "NVIS_PREPARED_NODE_COUNT": "2",
        "NVIS_PRESET_SCOPE_MULTIRACK": "1",
        "NVIS_VALIDATION_NODE_SCRIPTS_DIR": node_scripts,
    }
    proc = subprocess.run(
        [
            _bash4(), str(script),
            "--preset", "mnubergemm-26.03-v1.17",
            "--nodelist", "ink50-dh01-d01-11,ink50-dh01-d02-11",
            "--storage", "nfs",
            "--group-nodelist", "ink50-dh01-d01-11",
            "--group-nodelist", "ink50-dh01-d02-11",
            "--log-dir", str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--time=00:40:00" in proc.stdout
    assert "MNUBERGEMM_TIME_TO_RUN=1800" in proc.stdout
    assert f"NVIS_BIN_SCRIPTS={node_scripts}" in proc.stdout
    assert f"--chdir={node_scripts}" in proc.stdout
    assert f"{node_scripts}/mnubergemm.sbatch" in proc.stdout
    assert "/app/bin/scripts/mnubergemm.sbatch" not in proc.stdout
    assert proc.stdout.count("PLAN: sbatch --parsable --no-requeue") == 2
    assert "--nodes=1 --nodelist=ink50-dh01-d01-11" in proc.stdout
    assert "--nodes=1 --nodelist=ink50-dh01-d02-11" in proc.stdout
    assert "VALIDATION_PRESET_DRY_RUN" in proc.stdout


def test_nccl_dispatcher_dry_run_executes_nested_no_submit_plan(tmp_path):
    script = tmp_path / "run-validation-preset.sh"
    script.write_text((REPO / "bin/scripts/run-validation-preset.sh").read_text())
    nested = tmp_path / "run-llmb-nccl-container.sh"
    nested.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'NESTED_ARGS:'; printf ' %q' \"$@\"; printf '\\n'\n"
        "printf 'NESTED_ENV uid=%s gid=%s home=%s iters=%s debug=%s\\n' "
        '"${NVIS_SLURM_SUBMIT_UID:-}" "${NVIS_SLURM_SUBMIT_GID:-}" '
        '"${NVIS_SLURM_SUBMIT_HOME:-}" "${NVIS_NCCL_ITERS:-}" "${NCCL_DEBUG_SUBSYS:-}"\n'
    )
    proc = subprocess.run(
        [
            _bash4(), str(script),
            "--preset", "nccl-2.29.3",
            "--nodelist", "ink50-dh02-e02-11,ink50-dh02-f04-11",
            "--storage", "local",
            "--no-rsync",
            "--local-container-dir", "/opt/containers",
            "--log-dir", str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "NVIS_PREPARED_NODE_COUNT": "2",
            "NVIS_SLURM_SUBMIT_UID": "1003",
            "NVIS_SLURM_SUBMIT_GID": "1003",
            "NVIS_SLURM_SUBMIT_HOME": "/home/cmods",
        },
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "NESTED_ARGS:" in proc.stdout
    assert "--dry-run" in proc.stdout
    assert "NESTED_ENV uid=1003 gid=1003 home=/home/cmods iters=500 debug=ALL" in proc.stdout
    assert "VALIDATION_PRESET_DRY_RUN" in proc.stdout


def test_nemo_multirack_dry_run_is_one_hybrid_job(tmp_path):
    nodes = (
        "ink50-dh02-e02-11,ink50-dh02-e02-12,"
        "ink50-dh02-f04-11,ink50-dh02-f04-12"
    )
    proc = subprocess.run(
        [
            _bash4(), str(REPO / "bin/scripts/run-validation-preset.sh"),
            "--preset", "nemo-26.04.01",
            "--nodelist", nodes,
            "--storage", "local",
            "--no-rsync",
            "--local-container-dir", "/opt/containers",
            "--log-dir", str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "NVIS_PREPARED_NODE_COUNT": "4",
            "NVIS_PRESET_SCOPE_MULTIRACK": "1",
            "NVIS_SLURM_SUBMIT_UID": "1003",
            "NVIS_SLURM_SUBMIT_GID": "1003",
            "NVIS_SLURM_SUBMIT_HOME": "/home/cmods",
        },
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("--output=" + str(tmp_path) + "/nemo-26.04.01-%j.out") == 1
    assert "--nodes=4" in proc.stdout
    assert "--nodelist=" + nodes.replace(",", "\\,") in proc.stdout
    assert "NVIS_NEMO_XRACK=1" in proc.stdout
    assert "NVIS_NCCL_MNNVL=1" in proc.stdout
    assert "NVIS_NEMO_NVLS_ENABLE=0" in proc.stdout
    assert "--uid=1003 --gid=1003" in proc.stdout


def test_nemo_single_rack_dry_run_preserves_full_nvlink_multicast(tmp_path):
    nodes = "ink50-dh02-e02-11,ink50-dh02-e02-12"
    proc = subprocess.run(
        [
            _bash4(), str(REPO / "bin/scripts/run-validation-preset.sh"),
            "--preset", "nemo-26.04.01",
            "--nodelist", nodes,
            "--storage", "local",
            "--no-rsync",
            "--local-container-dir", "/opt/containers",
            "--log-dir", str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "NVIS_PREPARED_NODE_COUNT": "2",
            "NVIS_PRESET_SCOPE_MULTIRACK": "0",
        },
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("--output=" + str(tmp_path) + "/nemo-26.04.01-%j.out") == 1
    assert "NVIS_NEMO_XRACK=0" in proc.stdout
    assert "NVIS_NCCL_MNNVL=1" in proc.stdout
    assert "NVIS_NEMO_NVLS_ENABLE=1" in proc.stdout
    assert "NVIS_NEMO_UB_SKIPMC=0" in proc.stdout


def test_mnubergemm_dispatcher_rechecks_digest_bound_prestage_for_imex(tmp_path):
    script = REPO / "bin/scripts/run-validation-preset.sh"
    digest = "9832dda6e80c8719e74f81ce617cac3abb77399e48d5385a38e86a040cad93e6"
    base_env = {
        **os.environ,
        "NVIS_PREPARED_NODE_COUNT": "2",
        "NVIS_PRESET_SCOPE_MULTIRACK": "1",
        "NVIS_LOCAL_STAGE_VERIFIED_SHA256": digest,
    }
    args = [
        _bash4(), str(script),
        "--preset", "mnubergemm-26.03-v1.17",
        "--nodelist", "ink50-dh01-d01-11,ink50-dh01-d02-11",
        "--storage", "local",
        "--group-nodelist", "ink50-dh01-d01-11",
        "--group-nodelist", "ink50-dh01-d02-11",
        "--log-dir", str(tmp_path),
        "--dry-run",
    ]
    proc = subprocess.run(args, capture_output=True, text=True, env=base_env, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "STAGE_RECHECK_REQUIRED" in proc.stdout
    assert "reason=imex-channel0-prelaunch" in proc.stdout
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0=1" in proc.stdout
    assert "validation-container-stage" in proc.stdout

    bad_env = {**base_env, "NVIS_LOCAL_STAGE_VERIFIED_SHA256": "0" * 64}
    proc = subprocess.run(args, capture_output=True, text=True, env=bad_env, check=False)
    assert proc.returncode == 2
    assert "does not match the preset allowlist" in proc.stderr


def test_stage_failure_keeps_job_id_and_resolved_stdout_receipt(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scontrol = fake_bin / "scontrol"
    scontrol.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == show && $2 == hostnames ]]; then printf '%s\\n' ink50-dh02-e02-11; fi\n"
        "exit 0\n"
    )
    scontrol.chmod(0o755)
    sbatch = fake_bin / "sbatch"
    sbatch.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '778899;nscale'\nexit 17\n")
    sbatch.chmod(0o755)
    proc = subprocess.run(
        [
            _bash4(), str(REPO / "bin/scripts/run-validation-preset.sh"),
            "--preset", "mnubergemm-26.03-v1.17",
            "--nodelist", "ink50-dh02-e02-11",
            "--storage", "local",
            "--rsync",
            "--local-container-dir", "/opt/containers",
            "--log-dir", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "RACK_SLURM_BIN": str(fake_bin),
            "RACK_SLURM_SKIP_MODULE_LOAD": "1",
        },
        check=False,
    )
    assert proc.returncode == 17
    expected = str(tmp_path / "validation-container-stage-778899.out")
    assert f"STAGE_SUBMITTED jobid=778899 output={expected}" in proc.stdout
    assert f"STAGE_FAILED jobid=778899 rc=17 output={expected}" in proc.stderr
    assert "mnubergemm-001" not in proc.stdout


def test_cancelled_stage_is_rejected_even_when_sbatch_wait_returns_zero(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scontrol = fake_bin / "scontrol"
    scontrol.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == show && $2 == hostnames ]]; then printf '%s\\n' ink50-dh02-e02-11; fi\n"
        "exit 0\n"
    )
    scontrol.chmod(0o755)
    sbatch = fake_bin / "sbatch"
    sbatch.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '778900;nscale'\nexit 0\n")
    sbatch.chmod(0o755)
    sacct = fake_bin / "sacct"
    sacct.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' '778900|CANCELLED by 1024|0:0'\n"
    )
    sacct.chmod(0o755)
    proc = subprocess.run(
        [
            _bash4(), str(REPO / "bin/scripts/run-validation-preset.sh"),
            "--preset", "mnubergemm-26.03-v1.17",
            "--nodelist", "ink50-dh02-e02-11",
            "--storage", "local", "--rsync",
            "--local-container-dir", "/opt/containers",
            "--log-dir", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "RACK_SLURM_BIN": str(fake_bin),
            "RACK_SLURM_SKIP_MODULE_LOAD": "1",
        },
        check=False,
    )
    assert proc.returncode == 1
    assert "STAGE_SUBMITTED jobid=778900" in proc.stdout
    assert "state=CANCELLED by 1024 exit=0:0" in proc.stdout
    assert "STAGE_COMPLETE" not in proc.stdout
    assert "STAGE_FAILED jobid=778900" in proc.stderr


def test_cancelled_nemo_is_rejected_even_when_sbatch_wait_returns_zero(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scontrol = fake_bin / "scontrol"
    scontrol.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == show && $2 == hostnames ]]; then\n"
        "  printf '%s\\n' ink50-dh02-e02-11 ink50-dh02-f04-11\n"
        "fi\n"
        "exit 0\n"
    )
    scontrol.chmod(0o755)
    sbatch = fake_bin / "sbatch"
    sbatch.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '778901;nscale'\nexit 0\n")
    sbatch.chmod(0o755)
    sacct = fake_bin / "sacct"
    sacct.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' '778901|CANCELLED by 1024|0:0'\n"
    )
    sacct.chmod(0o755)
    provenance = tmp_path / "provenance" / "runtime" / "bin" / "scripts"
    (provenance / "lib").mkdir(parents=True)
    shutil.copy2(
        REPO / "bin/scripts/nemo-nemotron5h_56b-mbridge-fp8.sh",
        provenance / "nemo-nemotron5h_56b-mbridge-fp8.sh",
    )
    (provenance / "sysinfo.sh").write_text("#!/usr/bin/env bash\n")
    (provenance / "lib/fabric.inc.sh").write_text("#!/usr/bin/env bash\n")
    proc = subprocess.run(
        [
            _bash4(), str(REPO / "bin/scripts/run-validation-preset.sh"),
            "--preset", "nemo-26.04.01",
            "--nodelist", "ink50-dh02-e02-11,ink50-dh02-f04-11",
            "--storage", "local", "--no-rsync",
            "--local-container-dir", "/opt/containers",
            "--log-dir", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "RACK_SLURM_BIN": str(fake_bin),
            "RACK_SLURM_SKIP_MODULE_LOAD": "1",
            "RACK_VALIDATION_PROVENANCE_ROOT": str(tmp_path / "provenance"),
            "NVIS_PRESET_SCOPE_MULTIRACK": "1",
        },
        check=False,
    )
    assert proc.returncode == 1
    assert "STAGE_SUBMITTED jobid=778901" in proc.stdout
    assert "kind=nemo-26.04.01" not in proc.stdout
    assert "state=CANCELLED by 1024 exit=0:0" in proc.stdout
    assert "VALIDATION_PRESET_LAUNCHED" not in proc.stdout
    assert "did not complete successfully" in proc.stderr


@pytest.mark.parametrize(
    ("preset", "node_count", "filename"),
    [
        ("nemo-26.04.01", 2, "nvidia+nemo+26.04.01.sqsh"),
        ("llama31-405b-fp8", 256, "nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"),
    ],
)
def test_requested_dispatcher_dry_runs_use_resolved_known_good_root(
    tmp_path, preset, node_count, filename
):
    nodes = [f"ink50-dh01-d01-{index:03d}" for index in range(1, node_count + 1)]
    root = "/reviewed/nvftp/20260824"
    proc = subprocess.run(
        [
            _bash4(), str(REPO / "bin/scripts/run-validation-preset.sh"),
            "--preset", preset,
            "--nodelist", ",".join(nodes),
            "--storage", "nfs",
            "--log-dir", str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "NVIS_PREPARED_NODE_COUNT": str(node_count),
            "RACK_VALIDATION_CONTAINER_ROOT": root,
            "RACK_VALIDATION_NODE_CONTAINER_DIR": "/opt/containers",
        },
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"{root}/{filename}" in proc.stdout
    assert "/cluster/containers" not in proc.stdout
    assert "/tmp/containers" not in proc.stdout


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        (["ink50-dh01-d01-11", "ink50-dh01-d01-11"], "appears in more than one rack group"),
        (["ink50-dh01-d01-11", "ink50-dh01-d03-11"], "outside the reviewed target"),
    ],
)
def test_mnubergemm_dispatcher_rejects_unsafe_multirack_groups(tmp_path, groups, message):
    script = REPO / "bin/scripts/run-validation-preset.sh"
    args = [
        _bash4(), str(script),
        "--preset", "mnubergemm-26.03-v1.17",
        "--nodelist", "ink50-dh01-d01-11,ink50-dh01-d02-11",
        "--storage", "nfs",
        "--log-dir", str(tmp_path),
        "--dry-run",
    ]
    for group in groups:
        args += ["--group-nodelist", group]
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "NVIS_PREPARED_NODE_COUNT": "2",
            "NVIS_PRESET_SCOPE_MULTIRACK": "1",
        },
        check=False,
    )
    assert proc.returncode != 0
    assert message in proc.stderr


def test_mnubergemm_dispatcher_derives_exact_rack_groups_for_cli(tmp_path):
    script = REPO / "bin/scripts/run-validation-preset.sh"
    proc = subprocess.run(
        [
            _bash4(), str(script),
            "--preset", "mnubergemm-26.03-v1.17",
            "--nodelist", "ink50-dh01-d01-11,ink50-dh01-d01-12,ink50-dh01-d02-11,ink50-dh01-d02-12",
            "--storage", "nfs",
            "--log-dir", str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "NVIS_PREPARED_NODE_COUNT": "4"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("--job-name=mnubergemm-") == 2
    assert "--nodelist=ink50-dh01-d01-11\\,ink50-dh01-d01-12" in proc.stdout
    assert "--nodelist=ink50-dh01-d02-11\\,ink50-dh01-d02-12" in proc.stdout


@pytest.mark.parametrize(
    ("env_update", "message"),
    [
        ({"RACK_MNUBERGEMM_TIME_TO_RUN": "3601"}, "must be 1-3600s"),
        ({"RACK_MNUBERGEMM_TIME": "01:10:01"}, "exceeds the 01:10:00 hard maximum"),
    ],
)
def test_legacy_mnubergemm_driver_rejects_over_one_hour(env_update, message):
    script = REPO / "bin/scripts/run_mnubergemm_stress.sh"
    proc = subprocess.run(
        [_bash4(), str(script), "ink50-dh01-d01"],
        capture_output=True,
        text=True,
        env={**os.environ, **env_update},
        check=False,
    )
    assert proc.returncode != 0
    assert message in proc.stderr


def test_sysinfo_expands_compressed_slurm_hostlist_before_fanout(tmp_path):
    fake_bin = tmp_path / "slurm-bin"
    fake_bin.mkdir()
    scontrol = fake_bin / "scontrol"
    scontrol.write_text(
        "#!/usr/bin/env bash\n"
        "[[ $1 == show && $2 == hostnames ]] || exit 2\n"
        "printf '%s\\n' ink50-dh02-e02-11 ink50-dh02-e02-12 "
        "ink50-dh02-j08-11 ink50-dh02-j08-12\n"
    )
    scontrol.chmod(0o755)
    python3 = fake_bin / "python3"
    python3.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'FANOUT_ARGS=%s\\n' \"$*\"\n"
        "exit 0\n"
    )
    python3.chmod(0o755)

    proc = subprocess.run(
        [
            _bash4(),
            str(REPO / "bin/scripts/sysinfo.sh"),
            "--check",
            "os",
            "ink50-dh02-e02-[11-12],ink50-dh02-j08-[11-12]",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "RACK_SLURM_BIN": str(fake_bin)},
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    expanded = (
        "ink50-dh02-e02-11,ink50-dh02-e02-12,"
        "ink50-dh02-j08-11,ink50-dh02-j08-12"
    )
    assert f"NODELIST={expanded}" in proc.stdout
    assert f"--nodes {expanded}" in proc.stdout
    assert "[11-12]" not in proc.stdout


def test_scripts_encode_runtime_and_result_gates():
    dispatcher = (REPO / "bin/scripts/run-validation-preset.sh").read_text()
    nccl = (REPO / "bin/scripts/nccl_allreduce.sbatch").read_text()
    nccl_launcher = (REPO / "bin/scripts/run_nccl_mpi.sh").read_text()
    llama = (REPO / "bin/scripts/llama405b_fp8_1024.sbatch").read_text()
    llama_preflight = (REPO / "bin/scripts/llama405b_node_preflight.sh").read_text()
    gpt = (REPO / "bin/scripts/gpt-oss-cpu-strict.sbatch").read_text()
    sysinfo = (REPO / "bin/scripts/run-exemplar-system-info.sh").read_text()
    stage = (REPO / "bin/scripts/validation_container_stage.sbatch").read_text()
    imex_prepare = (REPO / "bin/scripts/validation_imex_channel0_prepare.sbatch").read_text()
    nemo = (REPO / "bin/scripts/nemo-nemotron5h_56b-mbridge-fp8.sh").read_text()
    nccl_container = (REPO / "bin/scripts/run-llmb-nccl-container.sh").read_text()
    nccl_legacy_llmb = (REPO / "bin/scripts/run_llmb_nccl.sh").read_text()
    nccl_adapter = (REPO / "bin/scripts/nvftp_nccl_five_collective_20260824.sbatch").read_text()
    mnubergemm_driver = (REPO / "bin/scripts/run_mnubergemm_stress.sh").read_text()
    assert "umask 0002" in mnubergemm_driver
    assert "exactly 256 nodes/1024 GPUs" in dispatcher
    assert "--max_steps=50" in llama and 'NVIS_LLAMA_NVLS_ENABLE:-1' in llama
    assert 'NVIS_LLAMA_MNNVL_ENABLE:-1' in llama
    assert 'NVIS_LLAMA_UB_SKIPMC:-0' in llama
    assert "LLAMA_TRANSPORT_CONTRACT" in llama
    assert "LLAMA_IMEX_HOST_READY" in llama and "LLAMA_IMEX_CONTAINER_READY" in llama
    assert '--container-mounts="/dev:/dev,${NVIS_LLAMA_LOG_DIR}:/nemo_run' in llama
    assert "NCCL_DEBUG=INFO" in llama and "NCCL_DEBUG_SUBSYS=INIT,ENV" in llama
    assert "--tensor_model_parallel_size 4" in llama
    assert "--pipeline_model_parallel_size 8" in llama
    assert "--context_parallel_size 1" in llama
    assert "--virtual_pipeline_model_parallel_size 4" in llama
    assert "--global_batch_size 6144" in llama
    assert llama.count('srun --nodes=256 --ntasks=256 --ntasks-per-node=1 --kill-on-bad-exit=1 --label "$NODE_PREFLIGHT"') == 2
    assert "sleep 20" in llama
    assert "sudo systemd-run --wait --pipe --quiet --collect" in llama_preflight
    assert "max_memory < 1024" in llama_preflight and "process_count == 0" in llama_preflight
    assert "systemctl stop nvidia-dcgm" not in llama_preflight
    assert "systemctl kill" not in llama_preflight
    assert "systemctl start nvidia-dcgm" not in llama_preflight
    assert "torch.cuda.nccl.version()" not in llama
    assert 'ctypes.CDLL("libnccl.so.2")' in llama
    assert "lib.ncclGetVersion(ctypes.byref(version))" in llama
    assert "runtime_nccl=2.30.7" in llama
    assert "NVIS_LLAMA_SCRIPTS_DIR=${node_script_dir}" in dispatcher
    assert '${destination}.nvis-stage.lock' not in llama
    assert 'lock="/tmp/.nvis-stage-${UID}-${IMAGE_SHA256}.lock"' in llama
    assert llama.index('if (( ! ready )); then') < llama.index('exec 9>"$lock"')
    assert "readonly PERF_ROOT=/opt/Megatron-Bridge/scripts/performance" in llama
    assert "/home/cmods/workspace/llmb/.cache/huggingface" not in llama
    assert '${NVIS_LLAMA_LOG_DIR}:/nemo_run' in llama
    assert "HF_CONFIG_SHA256=e85084d7fc0651a483166e959bbeb248e8f74829bcdcef6cb669d9c5e4dc54e7" in llama
    assert "${HF_CACHE_ROOT}:/hf-cache:ro" in llama
    assert "ncclGetVersion" in nccl and "requires=23102" in nccl
    assert "if ! srun --kill-on-bad-exit=1" in nccl and "exit 15" in nccl
    assert "/dev/infiniband/rdma_cm" in nccl and "-type c" in nccl
    assert "NVIS_NCCL_RUNTIME_PREFLIGHT" in nccl_launcher
    assert "NVIS_NCCL_SBATCH_WAIT" in nccl_launcher
    assert "validation_imex_channel0_prepare.sbatch" in nccl_launcher
    assert "--append-verify" in stage and "flock -x" in stage and "sha256sum" in stage
    assert 'NVIS_SLURM_BIN:=/cm/local/apps/slurm/25.05/bin' in stage
    assert '"$NVIS_SLURM_BIN/scontrol"' in stage
    assert stage.count('"$NVIS_SLURM_BIN/srun"') >= 2
    assert 'lock="/tmp/.nvis-stage-${UID}-${NVIS_STAGE_EXPECTED_SHA256}.lock"' in stage
    assert 'sudo -n rsync -a --partial --append-verify' in stage
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0" in stage
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0=${ensure_imex_channel0}" in dispatcher
    assert "mknod" in stage and "os.O_RDWR" in stage
    assert "mknod" in imex_prepare and "os.O_RDWR" in imex_prepare
    assert "systemctl" not in imex_prepare and "udevadm" not in imex_prepare
    assert "GPU_TYPE must be gb300" in gpt and '[[ "$lines" == "$3" ]]' in gpt
    assert "/cluster/llmb/workloads:/cluster/llmb/workloads:ro" in gpt
    assert "System Info Collection Summary" in sysinfo
    assert "squeue -h -j \"$mnubergemm_csv\" -o '%A|%T'" in dispatcher
    assert "squeue -h -j \"$mnubergemm_csv\" 2>/dev/null | grep -q" not in dispatcher
    assert "Failed non-fatal steps: [1-9][0-9]*" in sysinfo
    assert 'cmd+=(--export=' in sysinfo
    assert "GPU_TYPE=gb300" in sysinfo
    assert "squeue -h -j" in sysinfo and "sacct -X -n -j" in sysinfo
    # Dispatcher completion is validation completion, not mere submission.
    assert dispatcher.count("--wait") >= 3
    assert 'mnubergemm_queued="$(squeue -h -j "$mnubergemm_csv"' in dispatcher
    assert 'sacct -X -n -P -j "$mnubergemm_csv"' in dispatcher
    assert "run-llmb-nccl-container.sh" in dispatcher
    assert "NVIS_NCCL_ITERS=500" in dispatcher
    assert "NCCL_DEBUG_SUBSYS=ALL" in dispatcher and "NCCL_DEBUG_SUBSYS=ALL" in nccl_adapter
    assert "/run/enroot/runtime" in nccl_adapter
    assert 'sudo -n install -d -m 0775' in nccl_adapter
    assert 'srun-%t.stderr' in nccl_adapter
    assert 'NVIS_NCCL_MNNVL_MODE="${NVIS_NCCL_MNNVL_ENABLE}"' in nccl_adapter
    assert 'if [ "$NVIS_NCCL_MNNVL_MODE" = auto ]; then unset NCCL_MNNVL_ENABLE' in nccl_adapter
    assert 'direct_contract=RACK_NVLS' in nccl_container
    assert 'RACK_NVLS_AUTO' not in nccl_container
    assert 'NVIS_NCCL_TRANSPORT_CONTRACT=MULTIRACK_HYBRID' in nccl_container
    assert "if ! srun" in nccl_adapter
    assert "enroot runtime setup failed; no NCCL collective was started" in nccl_adapter
    assert '$contract below acceptance floor' in nccl_adapter
    assert "did not prove" not in nccl_adapter
    assert "via P2P/MNNVL" not in nccl_adapter
    assert "RACK_NVLS_AUTO" not in nccl_adapter
    assert 'NVIS_NCCL_MIN_RACK_BUSBW_GBS:-600' in nccl_adapter
    assert 'NCCL_IB_HCA="=mlx5_0,mlx5_1,mlx5_2,mlx5_3"' in nccl_adapter
    assert 'ulimit -l unlimited' in nccl_adapter
    assert 'NCCL_IB_PREFLIGHT_FAIL' in nccl_adapter
    assert 'for hca in mlx5_0 mlx5_1 mlx5_2 mlx5_3' in nccl_adapter
    assert 'ERROR: NCCL IB preflight failed; no collective was started' in nccl_adapter
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0=1" in nccl_container
    assert "validation_imex_channel0_prepare.sbatch" in nccl_legacy_llmb
    assert "--parsable --wait --no-requeue" in nccl_legacy_llmb
    assert "systemctl" not in nccl_legacy_llmb and "udevadm" not in nccl_legacy_llmb
    assert "STAGE_SKIPPED_PREVERIFIED" not in nccl_container
    assert "--job-name=nccl-8k32g-500" in nccl_container
    assert "NVIS_SLURM_SUBMIT_UID" in dispatcher
    assert "NVIS_SLURM_SUBMIT_GID" in dispatcher
    assert "NVIS_SLURM_SUBMIT_HOME" in dispatcher
    assert "NVIS_VALIDATION_NODE_SCRIPTS_DIR" in dispatcher
    assert "compute-visible NeMo script is stale" in dispatcher
    assert "NVIS_NEMO_XRACK=${nemo_xrack}" in dispatcher
    assert "NVIS_NCCL_MNNVL=${nemo_mnnvl}" in dispatcher
    assert "NVIS_NEMO_UB_SKIPMC=${nemo_ub_skipmc}" in dispatcher
    assert 'nemo_mnnvl="${NVIS_NCCL_MNNVL:-1}"' in dispatcher
    assert 'nemo_nvls="${NVIS_NEMO_NVLS_ENABLE:-$nemo_default_nvls}"' in dispatcher
    assert 'nemo_ub_skipmc="${NVIS_NEMO_UB_SKIPMC:-$nemo_default_ub_skipmc}"' in dispatcher
    assert "nemo_default_nvls=1" in dispatcher and "nemo_default_nvls=0" in dispatcher
    assert "nemo_default_ub_skipmc=0" in dispatcher and "nemo_default_ub_skipmc=1" in dispatcher
    assert "NEMO_TRANSPORT_CONTRACT" in nemo
    assert "imex_channel.inc.sh" not in nemo
    assert "nvis_imex_channel_gate" not in nemo
    assert "os.O_RDWR" not in nemo
    assert 'slurm_submit_as=(--uid="$submit_uid" --gid="$submit_gid")' in dispatcher
    assert dispatcher.count('sbatch "${slurm_submit_as[@]}"') >= 6
    assert "NVIS_SLURM_SUBMIT_UID" in nccl_container
    assert "NVIS_SLURM_SUBMIT_GID" in nccl_container
    assert "NVIS_SLURM_SUBMIT_HOME" in nccl_container
    assert 'slurm_submit_as=(--uid="$submit_uid" --gid="$submit_gid")' in nccl_container
    assert "SUBMITTED jobid=$direct_id kind=direct-five-collective" in nccl_container
    assert "train_rc=$?" in nemo and "NEMO_VALIDATION_PASS" in nemo
    assert dispatcher.count("NVIS_LOCAL_STAGE_VERIFIED_SHA256=e9a68d31832f") >= 2
    assert "mnubergemm_runtime_seconds=1800" in dispatcher
    assert "mnubergemm_slurm_time_limit=00:40:00" in dispatcher
    assert '--time="$mnubergemm_slurm_time_limit"' in dispatcher
    assert "MAX_WALLTIME_SECONDS=4200" in mnubergemm_driver
    assert "MAX_PAYLOAD_SECONDS=3600" in mnubergemm_driver
    assert "validation_imex_channel0_prepare.sbatch" in mnubergemm_driver
    assert 'extra=(-t "${WALLTIME}")' in mnubergemm_driver
    assert 'VERIFY_CONT="${IMAGE_DIR}/$(basename "${CONT}")"' in mnubergemm_driver
    assert '"${NODE_CONTAINER_DIR}/"*)' in mnubergemm_driver


def test_live_wrapper_provenance_is_fail_closed(tmp_path):
    approved = REPO / "bin/scripts/run-exemplar-llama.sh"
    copied = tmp_path / approved.name
    shutil.copyfile(approved, copied)
    receipt = validation_provenance.verify("exemplar-llama", tmp_path)
    assert receipt["snapshot"] == "20260824"
    assert receipt["bundles"][0]["sha256"].startswith("e7c1b4fe")
    copied.write_text(copied.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provenance drift"):
        validation_provenance.verify("exemplar-llama", tmp_path)


def test_provenance_manifest_covers_direct_nccl_snapshot_and_all_live_files():
    direct = (
        "working_HLT_NEMO_NCCL_LLAMA/20260824/NCCL/recipes/"
        "single-node/nccl_five_collective.sbatch"
    )
    assert validation_provenance.APPROVED_SNAPSHOT_SHA256[direct] == (
        "b923a98e913230f1a0d0d208aa411b72a373147fdfba05fd6e0fe0915a5138d1"
    )
    for _profile, (_bundles, filenames) in validation_provenance.PROFILES.items():
        assert set(filenames) <= set(validation_provenance.APPROVED_LIVE_SHA256)


def test_changed_provenance_manifest_is_refreshed_without_worker_restart(monkeypatch, tmp_path):
    source = tmp_path / "validation_provenance.py"
    source.write_text("PROFILES = {'old': ((), ())}\n", encoding="utf-8")
    loaded_mtime = source.stat().st_mtime_ns
    old_module = SimpleNamespace(PROFILES={"old": ((), ())}, verify=lambda *_args: {"version": "old"})
    monkeypatch.setattr(app_module, "validation_provenance", old_module)
    monkeypatch.setattr(app_module, "_VALIDATION_PROVENANCE_SOURCE", source)
    monkeypatch.setattr(app_module, "_VALIDATION_PROVENANCE_MTIME_NS", loaded_mtime)

    source.write_text(
        "PROFILES = {'new': ((), ())}\n"
        "def verify(profile, scripts_dir, provenance_root=None):\n"
        "    return {'version': 'new', 'profile': profile}\n",
        encoding="utf-8",
    )
    os.utime(source, ns=(loaded_mtime + 1_000_000, loaded_mtime + 1_000_000))

    app_module._refresh_validation_provenance()

    assert app_module.validation_provenance is not old_module
    assert app_module.validation_provenance.PROFILES == {"new": ((), ())}
    assert app_module.validation_provenance.verify("new", tmp_path) == {
        "version": "new",
        "profile": "new",
    }


def test_invalid_changed_provenance_manifest_fails_closed(monkeypatch, tmp_path):
    source = tmp_path / "validation_provenance.py"
    source.write_text("PROFILES = {'old': ((), ())}\n", encoding="utf-8")
    loaded_mtime = source.stat().st_mtime_ns
    old_module = SimpleNamespace(PROFILES={"old": ((), ())}, verify=lambda *_args: {"version": "old"})
    monkeypatch.setattr(app_module, "validation_provenance", old_module)
    monkeypatch.setattr(app_module, "_VALIDATION_PROVENANCE_SOURCE", source)
    monkeypatch.setattr(app_module, "_VALIDATION_PROVENANCE_MTIME_NS", loaded_mtime)

    source.write_text("this is not valid Python\n", encoding="utf-8")
    os.utime(source, ns=(loaded_mtime + 1_000_000, loaded_mtime + 1_000_000))

    with pytest.raises(ValueError, match="manifest reload failed"):
        app_module._refresh_validation_provenance()

    assert app_module.validation_provenance is old_module
    assert app_module._VALIDATION_PROVENANCE_MTIME_NS == loaded_mtime
