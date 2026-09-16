# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Executable contract tests for NeMo's allocation-side image/GPU gate."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[2]
STAGE = REPO / "bin" / "scripts" / "validation_container_stage.sbatch"
LAUNCHER = REPO / "bin" / "scripts" / "run-nemo-xrack.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_gate(tmp_path: Path, *, gpu_mode: str = "ready", destination: bool = True) -> subprocess.CompletedProcess[str]:
    fake_slurm = tmp_path / "slurm"
    fake_slurm.mkdir()
    _executable(fake_slurm / "scontrol", "#!/bin/bash\nprintf 'node01\\n'\n")
    _executable(
        fake_slurm / "srun",
        "#!/bin/bash\nwhile [[ \"$1\" == --* ]]; do shift; done\nexec \"$@\"\n",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "nvidia-smi",
        """#!/bin/bash
if [[ "${FAKE_GPU_MODE:-ready}" == ready ]]; then
  printf 'None\nNone\nNone\nNone\n'
else
  printf 'Reset Required\nNone\nNone\nNone\n'
fi
""",
    )

    source = tmp_path / "reviewed.sqsh"
    target = tmp_path / "node-cache.sqsh"
    payload = b"reviewed-ne-mo-image"
    source.write_bytes(payload)
    if destination:
        target.write_bytes(payload)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "FAKE_GPU_MODE": gpu_mode,
        "SLURM_JOB_NODELIST": "node01",
        "NVIS_SLURM_BIN": str(fake_slurm),
        "NVIS_STAGE_SOURCE": str(source),
        "NVIS_STAGE_DEST": str(target),
        "NVIS_STAGE_RSYNC": "0",
        "NVIS_STAGE_CONTAINER_PREFLIGHT": "0",
        "NVIS_STAGE_GPU_RECOVERY_CHECK": "1",
        "NVIS_STAGE_EXPECTED_GPUS": "4",
        "NVIS_STAGE_EXPECTED_SIZE": str(len(payload)),
        "NVIS_STAGE_EXPECTED_SHA256": hashlib.sha256(payload).hexdigest(),
    }
    return subprocess.run(
        ["bash", str(STAGE)], capture_output=True, text=True, env=env, timeout=30, check=False
    )


def test_ready_image_and_four_healthy_gpus_pass(tmp_path: Path):
    proc = _run_gate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("IMAGE_STAGE_READY ") == 1
    assert proc.stdout.count("GPU_RECOVERY_READY ") == 1
    assert "VALIDATION_CONTAINER_STAGE_COMPLETE nodes=1" in proc.stdout


def test_one_reset_required_gpu_fails_before_completion(tmp_path: Path):
    proc = _run_gate(tmp_path, gpu_mode="blocked")
    assert proc.returncode != 0
    assert "GPU_RECOVERY_FAIL " in proc.stderr
    assert "ready=3 total=4 expected=4" in proc.stderr
    assert "VALIDATION_CONTAINER_STAGE_COMPLETE" not in proc.stdout


def test_missing_node_local_image_fails_before_gpu_probe(tmp_path: Path):
    proc = _run_gate(tmp_path, destination=False)
    assert proc.returncode != 0
    assert "IMAGE_STAGE_FAIL " in proc.stderr
    assert "GPU_RECOVERY_READY " not in proc.stdout
    assert "VALIDATION_CONTAINER_STAGE_COMPLETE" not in proc.stdout


def test_launcher_dry_run_uses_shared_runtime_and_executes_no_sbatch(tmp_path: Path):
    fake_slurm = tmp_path / "slurm"
    fake_slurm.mkdir()
    _executable(fake_slurm / "scontrol", "#!/bin/bash\nprintf 'node01\\nnode02\\n'\n")
    sentinel = tmp_path / "sbatch-was-called"
    _executable(
        fake_slurm / "sbatch",
        "#!/bin/bash\nprintf called >\"$SBATCH_SENTINEL\"\nexit 99\n",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    expected_sha = "0829d0c77f38db54848afd095862093a7c62e98778b0b8664b8edff69e273747"
    _executable(
        fake_bin / "sha256sum",
        f"#!/bin/bash\nprintf '{expected_sha}  %s\\n' \"$1\"\n",
    )
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    image = image_dir / "nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"
    image.touch()
    os.truncate(image, 42477232128)
    log_dir = tmp_path / "logs"
    runtime = "/cluster/OVA/scripts/nscaletx-16k-validation/runtime/bin/scripts"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "SBATCH_SENTINEL": str(sentinel),
        "RACK_SLURM_BIN": str(fake_slurm),
        "RACK_SLURM_SKIP_MODULE_LOAD": "1",
        "RACK_JOB_LOG_DIR": str(log_dir),
        "NEMO_SQSH_SRC": str(image),
        "NVIS_NEMO_VETTED_SQSH": str(image),
        "NVIS_NEMO_SHARED_CACHE_DIR": str(image_dir),
        "NVIS_NEMO_NODE_CACHE_DIR": "/opt/containers",
        "NVIS_NEMO_SUBMIT_DRY_RUN": "1",
        "NVIS_VALIDATION_NODE_SCRIPTS_DIR": runtime,
    }
    proc = subprocess.run(
        ["bash", str(LAUNCHER), "node[01-02]"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "submit-dry-run preflight=allocation" in proc.stdout
    assert f"runtime={runtime}" in proc.stdout
    assert "/app/bin/scripts" not in proc.stdout
    assert "nodes=2" in proc.stdout
    assert not sentinel.exists(), "dry-run executed sbatch"
