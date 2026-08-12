from pathlib import Path

import app as app_module
import pytest
import validation_presets

REPO = Path(__file__).resolve().parents[2]


def test_catalogue_is_honest_and_version_pins_are_immutable():
    rows = {row["id"]: row for row in validation_presets.catalogue()}
    assert rows["llama31-405b-fp8"]["requirements"]["exact_nodes"] == 256
    assert "1024 GPUs" in rows["llama31-405b-fp8"]["label"]
    assert rows["nccl-2.31.2"]["defaults"]["cuda_version"] == "13.3+"
    assert rows["nccl-2.31.2"]["defaults"]["nccl_version"] == "2.31.2"
    assert rows["llama31-405b-fp8"]["defaults"]["storage"] == "local"
    assert rows["llama31-405b-fp8"]["defaults"]["rsync"] is True
    assert "mnubergemm-26.03-v1.17" in rows
    assert "mnubergemm-26.04.1" not in rows
    with pytest.raises(ValueError, match="unknown validation preset"):
        validation_presets.resolve_preset("mnubergemm-26.04.1")
    llama = validation_presets.resolve_preset("llama31-405b-fp8")
    with pytest.raises(ValueError, match="max_steps=50"):
        validation_presets.normalize_options({"scope": "multirack", "max_steps": 13}, llama)


def test_prepare_resolves_exact_nodes_and_enforces_single_node_gpt(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d01-12"]
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(app_module, "_validation_node_eligibility", lambda raw: ({node: "idle" for node in raw}, []))
    with app_module.app.app_context():
        plan, err = app_module._prepare_validation_preset({
            "preset_id": "gpt-oss-cpu", "scope": "node", "nodes": nodes,
            "storage": "nfs",
        })
    assert plan is None
    assert err is not None
    assert err[1] == 400
    assert "at most 1" in err[0].get_json()["error"]


def test_llama_prepare_is_exact_1024_gpu_contract(monkeypatch, tmp_path):
    nodes = [f"ink50-dh01-d{i // 16 + 1:02d}-{i % 16 + 1}" for i in range(256)]
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(app_module, "_validation_node_eligibility", lambda raw: ({node: "idle" for node in raw}, []))
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
    assert plan["env_updates"] == {"NVIS_PREPARED_NODE_COUNT": "256", "NVIS_PRESET_SCOPE_MULTIRACK": "1"}
    assert len(plan["plan_hash"]) == 64


def test_backend_routes_publish_prepare_contract(monkeypatch, tmp_path):
    node = "ink50-dh01-d01-11"
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: [node])
    monkeypatch.setattr(app_module, "_validation_node_eligibility", lambda raw: ({item: "idle" for item in raw}, []))
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
    assert "GPU_TYPE must be b300" in gpt and '[[ "$lines" == "$3" ]]' in gpt
    assert "/cluster/llmb/workloads:/cluster/llmb/workloads:ro" in gpt
    assert "System Info Collection Summary" in sysinfo
    assert "Failed non-fatal steps: [1-9][0-9]*" in sysinfo
    assert 'cmd+=(--export=' in sysinfo
    assert "GPU_TYPE=gb300" in sysinfo
    assert "squeue -h -j" in sysinfo and "sacct -X -n -j" in sysinfo
    # Dispatcher completion is validation completion, not mere submission.
    assert dispatcher.count("sbatch --wait") >= 4
    assert "NVIS_NCCL_SBATCH_WAIT=1" in dispatcher
    assert "train_rc=$?" in nemo and "NEMO_VALIDATION_PASS" in nemo
