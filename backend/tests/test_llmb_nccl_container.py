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

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "bin" / "scripts"


def test_container_workflow_prepares_exact_nodes(monkeypatch, tmp_path):
    nodes = ["ink50-dh01-d01-11", "ink50-dh01-d01-12"]
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: nodes)
    monkeypatch.setattr(
        app_module,
        "_verified_validation_provenance",
        lambda _profile: {"snapshot": "20260824", "bundles": [], "live_files": {}},
    )
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda raw, *, require_slurm=False: ({node: "idle" for node in raw}, []),
    )
    with app_module.app.app_context():
        prep, err = app_module._prepare_run_llmb_nccl({
            "workflow": "llmb-container-24test",
            "nodes": nodes,
            "log_dir": str(tmp_path),
            "dry_run": True,
        })
    assert err is None
    assert prep is not None
    assert prep["workflow"] == "nvftp-nccl-five-collective"
    assert prep["adapter"] == "direct-five-collective"
    assert prep["scope"] == "node"
    assert prep["nodes"] == nodes
    assert prep["storage"] == "local"
    assert prep["rsync_to_local"] is True
    assert prep["container_runtime"] == {"nccl": "2.29.3", "cuda": "13.1"}
    assert prep["recipe_contract"].startswith("NVFTP 20260824 five-collective")
    assert prep["image_provenance"]["size"] == 42245001216
    assert prep["image_provenance"]["sha256"].startswith("e9a68d31")
    assert prep["image_provenance"]["source"].endswith("/nvidia+nemo+26.04.01.sqsh")
    assert "canonical NVFTP" in prep["image_provenance"]["status"]
    assert prep["mpi_contract"]["value"] == "PMI2"
    assert "run-llmb-nccl-container.sh" in prep["display_cmd"]
    assert "--nodelist" in prep["cmd"]
    assert prep["env_updates"]["NVIS_PREPARED_NODE_COUNT"] == "2"


def test_container_workflow_rejects_non_idle_exact_target(monkeypatch, tmp_path):
    node = "ink50-dh01-d01-11"
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: [node])
    monkeypatch.setattr(
        app_module,
        "_validation_node_eligibility",
        lambda _raw, *, require_slurm=False: ({node: "drained"}, [f"{node}=drained"]),
    )
    with app_module.app.app_context():
        prep, err = app_module._prepare_run_llmb_nccl({
            "workflow": "container", "nodes": [node], "log_dir": str(tmp_path),
        })
    assert prep is None
    assert err is not None
    assert err[1] == 409
    assert "drained" in err[0].get_json()["error"]


def test_container_dispatcher_dry_run_is_offline_and_non_mutating(tmp_path):
    log_dir = tmp_path / "not-created"
    llmb_install = tmp_path / "llmb"
    llmb_install.mkdir()
    llmb_run = tmp_path / "llmb-run"
    llmb_run.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    llmb_run.chmod(0o755)
    env = {
        **os.environ,
        "NVIS_PREPARED_NODE_COUNT": "2",
        "RACK_STATUS_LOG_DIR": str(log_dir),
        "LLMB_INSTALL": str(llmb_install),
        "LLMB_RUN": str(llmb_run),
        "NVIS_SLURM_SUBMIT_UID": "1024",
        "NVIS_SLURM_SUBMIT_GID": "1003",
        "NVIS_SLURM_SUBMIT_HOME": "/home/hlakshminara",
        "NVIS_VALIDATION_NODE_SCRIPTS_DIR": str(SCRIPTS),
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
    assert "NVIS_NCCL_TRANSPORT_CONTRACT=RACK_NVLS" in proc.stdout
    assert f"NVIS_SCRIPTS_DIR={SCRIPTS}" in proc.stdout
    assert "/app/bin/scripts/nvftp_nccl_five_collective_20260824.sbatch" not in proc.stdout
    assert "adapter=direct-five-collective" in proc.stdout
    assert "recipe_snapshot=20260824" in proc.stdout
    assert "--uid=1024 --gid=1003" in proc.stdout


def test_direct_container_multirack_dry_run_uses_one_hybrid_communicator(tmp_path):
    log_dir = tmp_path / "not-created"
    nodes = (
        "ink50-dh02-j01-11,ink50-dh02-j01-12,"
        "ink50-dh02-j02-11,ink50-dh02-j02-12"
    )
    env = {
        **os.environ,
        "NVIS_PREPARED_NODE_COUNT": "4",
        "RACK_STATUS_LOG_DIR": str(log_dir),
        "LLMB_INSTALL": str(tmp_path / "missing-llmb"),
        "LLMB_RUN": str(tmp_path / "missing-llmb-run"),
        "NVIS_LOCAL_STAGE_VERIFIED_SHA256": (
            "e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca"
        ),
        "NVIS_VALIDATION_NODE_SCRIPTS_DIR": str(SCRIPTS),
    }
    proc = subprocess.run(
        [
            "bash", str(SCRIPTS / "run-llmb-nccl-container.sh"),
            "--nodelist", nodes, "--storage", "local", "--no-rsync",
            "--log-dir", str(log_dir), "--dry-run",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("--job-name=nccl-multirack-hybrid") == 1
    assert "--nodes=4" in proc.stdout
    assert "--nodelist=ink50-dh02-j01-11\\,ink50-dh02-j01-12\\,ink50-dh02-j02-11\\,ink50-dh02-j02-12" in proc.stdout
    assert proc.stdout.count("NVIS_NCCL_TRANSPORT_CONTRACT=MULTIRACK_HYBRID") == 1
    assert "--wait" in proc.stdout
    assert not log_dir.exists()


def test_direct_container_compact_single_rack_dry_run_expands_truthfully(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scontrol = fake_bin / "scontrol"
    scontrol.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' ink50-dh02-k17-{11..18} ink50-dh02-k17-{28..37}\n",
        encoding="utf-8",
    )
    scontrol.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "NVIS_PREPARED_NODE_COUNT": "18",
        "NVIS_LOCAL_STAGE_VERIFIED_SHA256": (
            "e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca"
        ),
        "NVIS_VALIDATION_NODE_SCRIPTS_DIR": str(SCRIPTS),
    }
    proc = subprocess.run(
        [
            "bash", str(SCRIPTS / "run-llmb-nccl-container.sh"),
            "--nodelist", "ink50-dh02-k17-[11-18,28-37]",
            "--storage", "local", "--no-rsync", "--log-dir", str(tmp_path), "--dry-run",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "NVIS_NCCL_TRANSPORT_CONTRACT=RACK_NVLS" in proc.stdout
    assert "MULTIRACK_HYBRID" not in proc.stdout
    assert "--nodes=18" in proc.stdout


def test_direct_container_wait_rejects_cancelled_root_job(tmp_path):
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
    sbatch.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '55590;nscale'\nexit 0\n")
    sbatch.chmod(0o755)
    sacct = fake_bin / "sacct"
    sacct.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' '55590|CANCELLED by 1024|0:0'\n"
    )
    sacct.chmod(0o755)
    proc = subprocess.run(
        [
            "bash", str(SCRIPTS / "run-llmb-nccl-container.sh"),
            "--nodelist", "ink50-dh02-e02-11,ink50-dh02-f04-11",
            "--storage", "local", "--no-rsync",
            "--log-dir", str(tmp_path),
        ],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "RACK_SLURM_SKIP_MODULE_LOAD": "1",
            "LLMB_INSTALL": str(tmp_path / "missing-llmb"),
            "LLMB_RUN": str(tmp_path / "missing-llmb-run"),
            "NVIS_LOCAL_STAGE_VERIFIED_SHA256": (
                "e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca"
            ),
            "NVIS_VALIDATION_NODE_SCRIPTS_DIR": str(SCRIPTS),
        },
        check=False,
    )
    assert proc.returncode == 44
    assert "SUBMITTED jobid=55590" in proc.stdout
    assert "state=CANCELLED by 1024 exit=0:0" in proc.stdout
    assert "LLMB_NCCL_CONTAINER_PASS" not in proc.stdout
    assert "LLMB_NCCL_CONTAINER_FAIL" in proc.stderr


def test_direct_adapter_refuses_one_mnnvl_communicator_across_racks(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_scontrol = fake_bin / "scontrol"
    fake_scontrol.write_text(
        "#!/bin/sh\nprintf '%s\\n' ink50-dh02-j01-11 ink50-dh02-j02-11\n",
        encoding="utf-8",
    )
    fake_scontrol.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "SLURM_JOB_NODELIST": "fake",
        "NVIS_NCCL_TRANSPORT_CONTRACT": "RACK_MNNVL",
    }
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "nvftp_nccl_five_collective_20260824.sbatch")],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 18
    assert "requires multiple nodes in exactly one physical rack" in proc.stderr
    assert "fan out multi-rack selections" in proc.stderr


def test_direct_adapter_accepts_explicit_multirack_hybrid_contract(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_scontrol = fake_bin / "scontrol"
    fake_scontrol.write_text(
        "#!/bin/sh\nprintf '%s\\n' ink50-dh02-j01-11 ink50-dh02-j02-11\n",
        encoding="utf-8",
    )
    fake_scontrol.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "SLURM_JOB_NODELIST": "fake",
        "NVIS_NCCL_TRANSPORT_CONTRACT": "MULTIRACK_HYBRID",
    }
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "nvftp_nccl_five_collective_20260824.sbatch")],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode != 18
    assert "MULTIRACK_HYBRID nodes=2 racks=2 mnnvl=0 nvls=1 hcoll=0" in proc.stdout
    assert "NCCL_IMEX_GATE" not in proc.stdout


def test_direct_adapter_single_rack_nvls_skips_custom_imex_gate(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_scontrol = fake_bin / "scontrol"
    fake_scontrol.write_text(
        "#!/bin/sh\nprintf '%s\\n' ink50-dh02-h20-11 ink50-dh02-h20-12\n",
        encoding="utf-8",
    )
    fake_scontrol.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "SLURM_JOB_NODELIST": "fake",
        "SLURM_JOB_NUM_NODES": "2",
        "SLURM_JOB_ID": "999",
        "AUG21_REPORT_DIR": str(tmp_path),
        "NVIS_IMAGE": "/opt/containers/test.sqsh",
        "NVIS_NCCL_TRANSPORT_CONTRACT": "RACK_NVLS",
    }
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "nvftp_nccl_five_collective_20260824.sbatch")],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode != 18
    assert "RACK_NVLS nodes=2 racks=1 mnnvl=auto nvls=1 hcoll=0" in proc.stdout
    assert "NCCL_IMEX_GATE" not in proc.stdout
    assert "shared IMEX channel gate is missing" not in proc.stderr


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
NVIS_LLMB_AUDITED_CONTAINER=1 RUN_CONF_IMAGE=/opt/containers/nvidia+nemo+26.04.01.sqsh CAPTURE={capture} sbatch {generated}
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
    assert "e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca" in dispatcher
    assert "/opt/containers" in dispatcher
    assert "nvidia+nemo+26.04.01.sqsh" in dispatcher
    assert "direct-five-collective" in dispatcher
    assert "b923a98e913230f1a0d0d208aa411b72a373147fdfba05fd6e0fe0915a5138d1" in dispatcher
    assert dispatcher.count("--wait") >= 2
    assert "LLMB_NCCL_CONTAINER_PASS" in dispatcher
    assert "NVIS_FABRIC_SKIP=1" in dispatcher
    assert "NVIS_LLMB_TESTSET_APPLY=0" in dispatcher
    assert "NVIS_STAGE_EXPECTED_GPUS" in stage
    assert "NVIS_STAGE_EXPECTED_NCCL" in stage
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0=1" in dispatcher
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0" in stage
    assert "IMEX_CHANNEL_READY" in stage
    assert "--container-mounts=/dev/infiniband:/dev/infiniband" in stage
    assert "--parsable --wait --no-requeue --ntasks-per-node=4 --gpus-per-node=4" in shim
    assert "umask 0002" in dispatcher
    assert '${RACK_STATUS_LOG_DIR:-/cluster/OVA/logs/nscaletx-16k-validation}' in dispatcher
    assert '${RACK_STATUS_LOG_DIR:-$PWD}' not in dispatcher
    assert "cannot create shared log directory" in dispatcher
    assert 'node_script_dir="${NVIS_VALIDATION_NODE_SCRIPTS_DIR:-${provenance_root}/runtime/bin/scripts}"' in dispatcher
    assert 'direct_recipe="${node_script_dir}/nvftp_nccl_five_collective_20260824.sbatch"' in dispatcher
    assert "NVIS_SCRIPTS_DIR=${node_script_dir}" in dispatcher
    assert dispatcher.index('source "${script_dir}/slurm-init.inc.sh"') < dispatcher.index(
        'source "${script_dir}/lib/nodes.inc.sh"'
    )
