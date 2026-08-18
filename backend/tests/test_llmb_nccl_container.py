# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import app as app_module
from api import validation_runs

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "bin" / "scripts"


def test_container_workflow_prepares_exact_nodes(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d01-12"]
    monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_runs, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_runs, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(
        validation_runs,
        "_validation_node_eligibility",
        lambda raw: ({node: "idle" for node in raw}, []),
    )
    with app_module.app.app_context():
        prep, err = validation_runs._prepare_run_llmb_nccl({
            "workflow": "llmb-container-24test",
            "nodes": nodes,
            "log_dir": str(tmp_path),
            "dry_run": True,
        })
    assert err is None
    assert prep is not None
    assert prep["workflow"] == "llmb-container-24test"
    assert prep["scope"] == "node"
    assert prep["nodes"] == nodes
    assert prep["storage"] == "local"
    assert prep["rsync_to_local"] is True
    assert prep["container_runtime"] == {"nccl": "2.29.3", "cuda": "13.1"}
    assert prep["recipe_contract"].startswith("job147392-shape")
    assert prep["image_provenance"]["size"] == 42245001216
    assert prep["image_provenance"]["sha256"].startswith("55130500")
    assert "differs" in prep["image_provenance"]["status"]
    assert "not a proven result claim" in prep["pmix_gds"]["status"]
    assert "run-llmb-nccl-container.sh" in prep["display_cmd"]
    assert "--nodelist" in prep["cmd"]
    assert prep["env_updates"]["NVIS_PREPARED_NODE_COUNT"] == "2"


def test_container_workflow_rejects_non_idle_exact_target(monkeypatch, tmp_path):
    node = "ink50-dh01-d01-11"
    monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_runs, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_runs, "_all_slurm_nodenames", lambda: [node])
    monkeypatch.setattr(
        validation_runs,
        "_validation_node_eligibility",
        lambda _raw: ({node: "drained"}, [f"{node}=drained"]),
    )
    with app_module.app.app_context():
        prep, err = validation_runs._prepare_run_llmb_nccl({
            "workflow": "container", "nodes": [node], "log_dir": str(tmp_path),
        })
    assert prep is None
    assert err is not None
    assert err[1] == 409
    assert "drained" in err[0].get_json()["error"]


def test_container_dispatcher_dry_run_is_offline_and_non_mutating(tmp_path):
    log_dir = tmp_path / "not-created"
    env = {
        **os.environ,
        "NVIS_PREPARED_NODE_COUNT": "2",
        "RACK_STATUS_LOG_DIR": str(log_dir),
    }
    proc = subprocess.run(
        [
            "bash", str(SCRIPTS / "run-llmb-nccl-container.sh"),
            "--nodelist", "ink50-dh01-d01-11,ink50-dh01-d01-12",
            "--storage", "local", "--rsync", "--log-dir", str(log_dir), "--dry-run",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert not log_dir.exists()
    assert "validation_container_stage.sbatch" in proc.stdout
    assert "NVIS_STAGE_EXPECTED_GPUS=4" in proc.stdout
    assert "NVIS_STAGE_EXPECTED_NCCL=22903" in proc.stdout
    assert "NVIS_STAGE_EXPECTED_CUDA=13.1" in proc.stdout
    assert "PMIX_MCA_gds=hash" in proc.stdout
    assert "MELLANOX_VISIBLE_DEVICES=all" in proc.stdout
    assert "NVIS_FABRIC_SKIP=1" in proc.stdout
    assert "NVIS_LLMB_TESTSET_APPLY=0" in proc.stdout
    assert "tests=24 ranks_per_node=4" in proc.stdout


def test_audited_recipe_transformer_enforces_contract(tmp_path):
    generated = tmp_path / "generated.sbatch"
    collective_names = (
        ["all_reduce_perf"] * 6
        + ["all_gather_perf"] * 3
        + ["reduce_scatter_perf"] * 3
        + ["alltoall_perf"] * 6
        + ["sendrecv_perf"] * 6
    )
    lines = [
        "#!/bin/bash",
        "export LOG_DIR=$PWD/results",
        "mkdir -p \"$LOG_DIR\"",
        "srun --ntasks=$SLURM_JOB_NUM_NODES --ntasks-per-node=1 --container-image=$RUN_CONF_IMAGE --no-container-mount-home --container-mounts=./:/nvcomms-perf-workspace true",
        "srun --ntasks=1 --nodes=1 --container-image=$RUN_CONF_IMAGE --no-container-mount-home --container-mounts=./:/nvcomms-perf-workspace true",
    ]
    for index, name in enumerate(collective_names, 1):
        lines += [
            f'echo "[1/1] Running test {index}/24"',
            f"srun -N ${{SLURM_JOB_NUM_NODES}} --mpi=pmi2 --cpu-bind=none --container-image=$RUN_CONF_IMAGE --no-container-mount-home --container-mounts=./:/nvcomms-perf-workspace bash -c 'run_test /usr/local/bin/{name}_mpi -t1 -g1 -f2 -n20 -w5 -b8 -e16G -d{'float' if index <= 12 else 'uint8'} -R0 -c1 -G{('None', '2', '4')[(index - 1) % 3]}'",
        ]
    generated.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fake_sbatch = tmp_path / "sbatch"
    fake_sbatch.write_text(
        "#!/bin/bash\nset -eu\nfor arg in \"$@\"; do if [[ -f \"$arg\" && \"$arg\" == *audited-llmb* ]]; then cp \"$arg\" \"$CAPTURE\"; fi; done\necho 999999\n",
        encoding="utf-8",
    )
    fake_sbatch.chmod(0o755)
    capture = tmp_path / "captured.sbatch"
    command = f"""
set -euo pipefail
PATH={tmp_path}:$PATH
source {SCRIPTS / 'lib/sbatch_pin.inc.sh'}
nvis_sbatch_pin_install ink50-dh01-d01-11
NVIS_LLMB_AUDITED_CONTAINER=1 RUN_CONF_IMAGE=/tmp/containers/nvidia+nemo+26.04.01-arm.sqsh CAPTURE={capture} sbatch {generated}
"""
    proc = subprocess.run(["bash", "-c", command], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    patched = capture.read_text(encoding="utf-8")
    assert patched.count("Running test ") == 24
    assert patched.count("--ntasks-per-node=4 --mpi=pmix") == 24
    assert patched.count("--container-mounts=/dev/infiniband:/dev/infiniband,") == 26
    assert patched.count("--container-env=PMIX_MCA_gds,MELLANOX_VISIBLE_DEVICES") == 26
    assert "export PMIX_MCA_gds=hash" in patched
    assert "export MELLANOX_VISIBLE_DEVICES=all" in patched
    assert "LLMB_NCCL_24TEST_PASS completed=24 failed=0" in patched


def test_autocollect_accepts_tagged_and_bare_parsable_job_ids(tmp_path):
    tagged = tmp_path / "tagged.out"
    tagged.write_text("launch jobid=123456 ready\n", encoding="utf-8")
    bare = tmp_path / "bare.out"
    bare.write_text("scale=64\n147392;nscale\n", encoding="utf-8")
    include = SCRIPTS / "lib" / "llmb_autocollect.inc.sh"
    for path, expected in ((tagged, "123456"), (bare, "147392")):
        proc = subprocess.run(
            ["bash", "-c", f"source {include}; llmb_jobid_from_output {path}"],
            text=True, capture_output=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == expected


def test_scripts_pin_image_and_truthful_lifecycle():
    dispatcher = (SCRIPTS / "run-llmb-nccl-container.sh").read_text(encoding="utf-8")
    stage = (SCRIPTS / "validation_container_stage.sbatch").read_text(encoding="utf-8")
    shim = (SCRIPTS / "lib/sbatch_pin.inc.sh").read_text(encoding="utf-8")
    assert "42245001216" in dispatcher
    assert "55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc" in dispatcher
    assert "/tmp/containers/nvidia+nemo+26.04.01-arm.sqsh" in dispatcher
    assert dispatcher.count("--wait") >= 2
    assert "LLMB_NCCL_CONTAINER_PASS" in dispatcher
    assert "NVIS_FABRIC_SKIP=1" in dispatcher
    assert "NVIS_LLMB_TESTSET_APPLY=0" in dispatcher
    assert "NVIS_STAGE_EXPECTED_GPUS" in stage
    assert "NVIS_STAGE_EXPECTED_NCCL" in stage
    assert "--container-mounts=/dev/infiniband:/dev/infiniband" in stage
    assert "--parsable --wait --no-requeue --ntasks-per-node=4 --gpus-per-node=4" in shim
