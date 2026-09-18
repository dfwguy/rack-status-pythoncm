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

# The launch component moved to validation_launch.py; patch/call the owning module.
import app as app_module
import pytest
import validation_launch
import validation_presets

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
        validation_launch,
        "_get_platform",
        lambda: SimpleNamespace(
            sinfo_cmd=lambda args: f"/site/slurm/bin/sinfo {args}"
        ),
    )
    monkeypatch.setattr(validation_launch.host_exec, "run_slurm_on_host", run)
    states, reasons = validation_launch._validation_node_eligibility([node])
    assert captured["cmd"].startswith("/site/slurm/bin/sinfo ")
    assert states == {node: "idle"}
    assert reasons == []


def test_catalogue_is_honest_and_version_pins_are_immutable():
    rows = {row["id"]: row for row in validation_presets.catalogue()}
    assert rows["llama31-405b-fp8"]["requirements"]["exact_nodes"] == 256
    assert "1024 GPUs" in rows["llama31-405b-fp8"]["label"]
    assert rows["nccl-2.31.2"]["defaults"]["cuda_version"] == "13.3+"
    assert rows["nccl-2.31.2"]["defaults"]["nccl_version"] == "2.31.2"
    assert rows["llama31-405b-fp8"]["defaults"]["storage"] == "local"
    assert rows["llama31-405b-fp8"]["defaults"]["rsync"] is True
    assert "mnubergemm-26.03-v1.17" in rows
    assert "multirack" in rows["mnubergemm-26.03-v1.17"]["requirements"]["scopes"]
    assert rows["mnubergemm-26.03-v1.17"]["defaults"]["max_runtime_minutes"] == 60
    assert "mnubergemm-26.04.1" not in rows
    with pytest.raises(ValueError, match="unknown validation preset"):
        validation_presets.resolve_preset("mnubergemm-26.04.1")
    llama = validation_presets.resolve_preset("llama31-405b-fp8")
    with pytest.raises(ValueError, match="max_steps=50"):
        validation_presets.normalize_options({"scope": "multirack", "max_steps": 13}, llama)
    mnubergemm = validation_presets.resolve_preset("mnubergemm-26.03-v1.17")
    with pytest.raises(ValueError, match="max_runtime_minutes=60"):
        validation_presets.normalize_options(
            {"scope": "multirack", "max_runtime_minutes": 61}, mnubergemm
        )


def test_prepare_resolves_exact_nodes_for_gpt_fanout(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d01-12"]
    monkeypatch.setattr(validation_launch, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_launch, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_launch, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(validation_launch, "_validation_node_eligibility", lambda raw: ({node: "idle" for node in raw}, []))
    with app_module.app.app_context():
        plan, err = validation_launch._prepare_validation_preset({
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
    monkeypatch.setattr(validation_launch, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_launch, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_launch, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(validation_launch, "_validation_node_eligibility", lambda raw: ({node: "idle" for node in raw}, []))
    with app_module.app.app_context():
        plan, err = validation_launch._prepare_validation_preset({
            "preset_id": "llama31-405b-fp8", "scope": "multirack", "nodes": nodes,
            "storage": "local", "rsync": True, "max_steps": 50,
        })
    assert err is None
    assert plan is not None
    assert plan["node_count"] == 256
    assert plan["max_steps"] == 50
    assert plan["nccl_version"] == "2.30.7"
    assert plan["artifact"]["sha256"] == "0829d0c77f38db54848afd095862093a7c62e98778b0b8664b8edff69e273747"
    assert plan["env_updates"] == {"NVIS_PREPARED_NODE_COUNT": "256", "NVIS_PRESET_SCOPE_MULTIRACK": "1"}
    assert len(plan["plan_hash"]) == 64


def test_backend_routes_publish_prepare_contract(monkeypatch, tmp_path):
    node = "ink50-dh01-d01-11"
    monkeypatch.setattr(validation_launch, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_launch, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_launch, "_all_slurm_nodenames", lambda: [node])
    monkeypatch.setattr(validation_launch, "_validation_node_eligibility", lambda raw: ({item: "idle" for item in raw}, []))
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


def test_mnubergemm_multirack_plan_is_exact_and_time_capped(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d02-11"]
    rack_nodes = {
        "ink50-dh01-d01": [nodes[0]],
        "ink50-dh01-d02": [nodes[1]],
    }
    monkeypatch.setattr(validation_launch, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_launch, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_launch, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(validation_launch, "_normalize_rack_id", lambda rack: rack)
    monkeypatch.setattr(validation_launch, "_multirack_campaign_snapshot", lambda: {"active": False})
    monkeypatch.setattr(validation_launch, "_rack_node_hostnames_from_inventory", lambda rack: rack_nodes[rack])
    monkeypatch.setattr(
        validation_launch,
        "_validation_node_eligibility",
        lambda raw: ({node: "idle" for node in raw}, []),
    )
    with app_module.app.app_context():
        plan, err = validation_launch._prepare_validation_preset({
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
    assert plan["max_runtime_minutes"] == 60
    assert "Slurm TimeLimit=01:00:00" in plan["checks"]
    assert plan["env_updates"]["NVIS_PRESET_SCOPE_MULTIRACK"] == "1"
    assert plan["cmd"].count("--group-nodelist") == 2
    assert nodes[0] in plan["command_display"] and nodes[1] in plan["command_display"]

    with app_module.app.app_context():
        unsafe_plan, unsafe_err = validation_launch._prepare_validation_preset({
            "preset_id": "mnubergemm-26.03-v1.17",
            "scope": "multirack",
            "nodes": nodes,
            "storage": "nfs",
        })
    assert unsafe_plan is None
    assert unsafe_err is not None
    assert unsafe_err[1] == 409
    assert "one resolvable exact-node group" in unsafe_err[0].get_json()["error"]


def test_mnubergemm_dispatcher_dry_run_prints_bounded_slurm_command(tmp_path):
    script = REPO / "bin/scripts/run-validation-preset.sh"
    env = {
        **os.environ,
        "NVIS_PREPARED_NODE_COUNT": "2",
        "NVIS_PRESET_SCOPE_MULTIRACK": "1",
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
    assert "--time=01:00:00" in proc.stdout
    assert "MNUBERGEMM_TIME_TO_RUN=3300" in proc.stdout
    assert proc.stdout.count("PLAN: sbatch --parsable --no-requeue") == 2
    assert "--nodes=1 --nodelist=ink50-dh01-d01-11" in proc.stdout
    assert "--nodes=1 --nodelist=ink50-dh01-d02-11" in proc.stdout
    assert "VALIDATION_PRESET_DRY_RUN" in proc.stdout


@pytest.mark.parametrize(
    ("groups", "message"),
    [
        ([], "requires at least two exact --group-nodelist values"),
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


@pytest.mark.parametrize(
    ("env_update", "message"),
    [
        ({"RACK_MNUBERGEMM_TIME_TO_RUN": "3301"}, "must be 1-3300s"),
        ({"RACK_MNUBERGEMM_TIME": "01:00:01"}, "exceeds the 01:00:00 hard maximum"),
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


def test_scripts_encode_runtime_and_result_gates():
    dispatcher = (REPO / "bin/scripts/run-validation-preset.sh").read_text()
    nccl = (REPO / "bin/scripts/nccl_allreduce.sbatch").read_text()
    nccl_launcher = (REPO / "bin/scripts/run_nccl_mpi.sh").read_text()
    llama = (REPO / "bin/scripts/llama405b_fp8_1024.sbatch").read_text()
    llama_preflight = (REPO / "bin/scripts/llama405b_node_preflight.sh").read_text()
    gpt = (REPO / "bin/scripts/gpt-oss-cpu-strict.sbatch").read_text()
    sysinfo = (REPO / "bin/scripts/run-exemplar-system-info.sh").read_text()
    stage = (REPO / "bin/scripts/validation_container_stage.sbatch").read_text()
    nemo = (REPO / "bin/scripts/nemo-nemotron5h_56b-mbridge-fp8.sh").read_text()
    mnubergemm_driver = (REPO / "bin/scripts/run_mnubergemm_stress.sh").read_text()
    assert "exactly 256 nodes/1024 GPUs" in dispatcher
    assert "--max_steps=50" in llama and "NCCL_NVLS_ENABLE=0" in llama
    assert "--tensor_model_parallel_size 4" in llama
    assert "--pipeline_model_parallel_size 8" in llama
    assert "--context_parallel_size 1" in llama
    assert "--virtual_pipeline_model_parallel_size 4" in llama
    assert "--global_batch_size 6144" in llama
    assert llama.count('srun --nodes=256 --ntasks=256 --ntasks-per-node=1 --kill-on-bad-exit=1 --label "$NODE_PREFLIGHT"') == 2
    assert "sleep 20" in llama
    assert "sudo systemd-run --wait --pipe --quiet --collect" in llama_preflight
    assert "max_memory < 1024" in llama_preflight and "process_count == 0" in llama_preflight
    assert "torch.cuda.nccl.version()" not in llama
    assert 'ctypes.CDLL("libnccl.so.2")' in llama
    assert "lib.ncclGetVersion(ctypes.byref(version))" in llama
    assert "runtime_nccl=2.30.7" in llama
    assert "ncclGetVersion" in nccl and "requires=23102" in nccl
    assert "if ! srun --kill-on-bad-exit=1" in nccl and "exit 15" in nccl
    assert "/dev/infiniband/rdma_cm" in nccl and "-type c" in nccl
    assert "NVIS_NCCL_RUNTIME_PREFLIGHT" in nccl_launcher
    assert "NVIS_NCCL_SBATCH_WAIT" in nccl_launcher
    assert "--append-verify" in stage and "flock -x" in stage and "sha256sum" in stage
    assert "GPU_TYPE must be gb300" in gpt and '[[ "$lines" == "$3" ]]' in gpt
    assert "/cluster/llmb/workloads:/cluster/llmb/workloads:ro" in gpt
    assert "System Info Collection Summary" in sysinfo
    assert "Failed non-fatal steps: [1-9][0-9]*" in sysinfo
    assert 'cmd+=(--export=' in sysinfo
    assert "GPU_TYPE=gb300" in sysinfo
    assert "squeue -h -j" in sysinfo and "sacct -X -n -j" in sysinfo
    # Dispatcher completion is validation completion, not mere submission.
    assert dispatcher.count("sbatch --wait") >= 3
    assert 'while squeue -h -j "$mnubergemm_csv"' in dispatcher
    assert 'sacct -X -n -P -j "$mnubergemm_csv"' in dispatcher
    assert "NVIS_NCCL_SBATCH_WAIT=1" in dispatcher
    assert "train_rc=$?" in nemo and "NEMO_VALIDATION_PASS" in nemo
    assert "mnubergemm_runtime_seconds=3300" in dispatcher
    assert "mnubergemm_slurm_time_limit=01:00:00" in dispatcher
    assert '--time="$mnubergemm_slurm_time_limit"' in dispatcher
    assert "MAX_WALLTIME_SECONDS=3600" in mnubergemm_driver
    assert "MAX_PAYLOAD_SECONDS=3300" in mnubergemm_driver
    assert 'extra=(-t "${WALLTIME}")' in mnubergemm_driver
