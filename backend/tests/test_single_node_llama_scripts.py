#!/usr/bin/env python3
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


SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"


def test_llama_launcher_stages_and_exports_the_same_image_path():
    body = (SCRIPTS / "run-llama.sh").read_text()

    assert 'pre_staged_image="/opt/containers/${_sqsh_b}"' in body
    assert 'llama_run_image="$pre_staged_image"' in body
    assert 'llama_run_image="/raid/data/${_sqsh_b}"' not in body
    assert 'export NVIS_LLAMA_CONT="$llama_run_image"' in body
    assert '--export=ALL,NVIS_LLAMA_CONT="$NVIS_LLAMA_CONT"' in body


def test_llama_batch_is_fail_closed_and_emits_terminal_marker():
    body = (SCRIPTS / "nemo-llma3_8b.sh").read_text()

    assert 'CONT="${NVIS_LLAMA_CONT:-/raid/data/nemo-25.11.01.sqsh}"' in body
    assert 'LLAMA_VALIDATION_FAIL stage=image' in body
    assert 'LLAMA_VALIDATION_FAIL stage=training' in body
    assert 'LLAMA_VALIDATION_PASS node=${SLURM_JOB_NODELIST}' in body


def test_single_node_56b_uses_the_proven_four_gpu_memory_recipe():
    wrapper = (SCRIPTS / "nemo-single-56b-30steps.sh").read_text()
    engine = (SCRIPTS / "nemo-nemotron5h_56b-mbridge-fp8.sh").read_text()

    assert "export NVIS_NEMO_MAX_STEPS=30" in wrapper
    assert "export NVIS_NEMO_NVLS_ENABLE=0" in wrapper
    assert '"${SLURM_NNODES}" -eq 1' in engine
    assert "-tp 4 -pp 1 -cp 1 -ep 1 -mb 1" in engine
    assert "train.manual_gc_interval=1" in engine
    assert "model.tensor_model_parallel_size=4" in engine
    assert "model.cuda_graph_impl=none" in engine
    assert "comm_overlap.tp_comm_overlap=false" in engine


def test_nemo_trace_mount_is_shared_log_backed_and_fail_closed():
    engine = (SCRIPTS / "nemo-nemotron5h_56b-mbridge-fp8.sh").read_text()

    assert "/cluster/OVA/logs/nscaletx-16k-validation" in engine
    assert 'NVIS_NCCL_TRACE_HOST_DIR="${NVIS_NCCL_TRACE_HOST_DIR:-${NVIS_NCCL_TRACE_ROOT%/}/' in engine
    assert 'if ! mkdir -p "$NVIS_NCCL_TRACE_HOST_DIR"' in engine
    assert "cannot create NCCL trace directory" in engine
    assert '$(pwd)/nccl_traces' not in engine
    assert 'mkdir -p "${NVIS_NCCL_TRACE_HOST_DIR}" 2>/dev/null || true' not in engine


def test_nemo_defaults_require_nvls_contract_without_custom_imex_gate():
    engine = (SCRIPTS / "nemo-nemotron5h_56b-mbridge-fp8.sh").read_text()

    assert "export NCCL_MNNVL_ENABLE=${NVIS_NCCL_MNNVL:-1}" in engine
    assert engine.count("export NCCL_NVLS_ENABLE=${NVIS_NEMO_NVLS_ENABLE:-1}") >= 3
    assert "NEMO_TRANSPORT_CONTRACT" in engine
    assert "imex_channel.inc.sh" not in engine
    assert "nvis_imex_channel_gate" not in engine
    assert "os.O_RDWR" not in engine
    assert "0) single_rack=0" in engine
    assert "elif [ $single_rack -eq 1 ]; then\n  export NCCL_MNNVL_ENABLE=1" not in engine


def test_single_rack_compatibility_wrapper_uses_reviewed_shared_runtime(tmp_path):
    wrapper = SCRIPTS / "run_singlerack.sh"
    copied_wrapper = tmp_path / wrapper.name
    shutil.copyfile(wrapper, copied_wrapper)
    copied_wrapper.chmod(0o755)
    shutil.copyfile(SCRIPTS / "slurm-init.inc.sh", tmp_path / "slurm-init.inc.sh")
    fake_dispatcher = tmp_path / "run-validation-preset.sh"
    fake_dispatcher.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'ARG=%s\\n' \"$@\"\n"
        "printf 'RUNTIME=%s\\n' \"$NVIS_VALIDATION_NODE_SCRIPTS_DIR\"\n"
        "printf 'MNNVL=%s NVLS=%s\\n' \"$NVIS_NCCL_MNNVL\" \"$NVIS_NEMO_NVLS_ENABLE\"\n",
        encoding="utf-8",
    )
    fake_dispatcher.chmod(0o755)
    log_dir = tmp_path / "shared-logs"
    proc = subprocess.run(
        [str(copied_wrapper), "ink50-dh01-a01-11,ink50-dh01-a01-12", "ink50-dh01-a01", str(log_dir)],
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "RACK_VALIDATION_PROVENANCE_ROOT": "/cluster/OVA/scripts/nscaletx-16k-validation",
            "RACK_VALIDATION_NODE_CONTAINER_DIR": "/opt/containers",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert log_dir.is_dir()
    assert "ARG=--preset\nARG=nemo-26.04.01" in proc.stdout
    assert "ARG=--storage\nARG=local" in proc.stdout
    assert "ARG=--no-rsync" in proc.stdout
    assert f"ARG=--log-dir\nARG={log_dir}" in proc.stdout
    assert "RUNTIME=/cluster/OVA/scripts/nscaletx-16k-validation/runtime/bin/scripts" in proc.stdout
    assert "MNNVL=1 NVLS=1" in proc.stdout
    assert "/app" not in proc.stdout


def test_single_rack_compatibility_wrapper_has_no_legacy_raid_or_pwd_log_path():
    wrapper = (SCRIPTS / "run_singlerack.sh").read_text()

    assert "NEMO_SQSH_SRC" not in wrapper
    assert "/raid" not in wrapper
    assert "${RACK_STATUS_LOG_DIR:-$PWD}" not in wrapper
    assert "/cluster/OVA/logs/nscaletx-16k-validation" in wrapper
    assert "--preset nemo-26.04.01" in wrapper
    assert "--no-rsync" in wrapper


def test_nemo_preset_dry_run_keeps_app_paths_out_of_slurm_child():
    runtime = "/cluster/OVA/scripts/nscaletx-16k-validation/runtime/bin/scripts"
    log_dir = "/cluster/OVA/logs/nscaletx-16k-validation"
    env = {
        **os.environ,
        "NVIS_PREPARED_NODE_COUNT": "2",
        "NVIS_SLURM_SUBMIT_UID": "1003",
        "NVIS_SLURM_SUBMIT_GID": "1003",
        "NVIS_SLURM_SUBMIT_HOME": "/home/cmods",
        "NVIS_VALIDATION_NODE_SCRIPTS_DIR": runtime,
    }
    env.pop("RACK_STATUS_LOG_DIR", None)
    env.pop("RACK_JOB_LOG_DIR", None)
    proc = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "run-validation-preset.sh"),
            "--preset",
            "nemo-26.04.01",
            "--nodelist",
            "ink50-dh01-a01-11,ink50-dh01-a01-12",
            "--storage",
            "local",
            "--no-rsync",
            "--local-container-dir",
            "/opt/containers",
            "--log-dir",
            log_dir,
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"--chdir={runtime}" in proc.stdout
    assert f"NVIS_SCRIPTS_DIR={runtime}" in proc.stdout
    assert f"{runtime}/nemo-nemotron5h_56b-mbridge-fp8.sh" in proc.stdout
    assert "/app" not in proc.stdout
    assert "NVIS_NCCL_MNNVL=1" in proc.stdout
    assert "NVIS_NEMO_NVLS_ENABLE=1" in proc.stdout
    assert "NVIS_NEMO_UB_SKIPMC=0" in proc.stdout
