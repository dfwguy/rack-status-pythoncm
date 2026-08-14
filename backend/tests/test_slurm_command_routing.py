# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Architecture guard: production Slurm execution goes through PlatformProvider."""

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SLURM_CLI = r"(?:sbatch|srun|sinfo|squeue|sacct|scontrol|scancel)"


def _production_python_files():
    yield from (
        path for path in BACKEND.rglob("*.py")
        if "tests" not in path.parts and "platforms" not in path.parts
    )


def test_no_production_module_hardcodes_a_versioned_slurm_executable():
    offenders = []
    for path in _production_python_files():
        if "/cm/local/apps/slurm/" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], (
        "Slurm binary paths belong in RACK_SLURM_* + PlatformProvider, not callers: "
        f"{offenders}"
    )


def test_host_execution_calls_do_not_embed_a_bare_slurm_cli():
    """Catch a direct ``run_on_host('sacct ...')`` regression.

    PlatformProvider itself is excluded because it is the one layer allowed to render
    these executable names. Parser strings such as ``srun: error`` are not commands and
    are intentionally ignored.
    """
    command = re.compile(rf"(?:^|[;&|]\s*)(?:sudo\s+)?{SLURM_CLI}\s+(?:-|show\b|update\b)")
    offenders = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else ""
            if name not in {"run_on_host", "run_slurm_on_host", "run"}:
                continue
            literals = [
                part.value for part in ast.walk(node.args[0])
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            if any(command.search(value) for value in literals):
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert offenders == [], (
        "Render Slurm commands with PlatformProvider before executing them: "
        f"{offenders}"
    )
