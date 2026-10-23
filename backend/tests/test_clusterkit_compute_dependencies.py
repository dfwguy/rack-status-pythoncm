# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""The actual compute entrypoint must work with Python's standard library alone."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import clusterkit_execution as execution
import pytest

from .test_clusterkit_execution import SCRIPTS, make_plan

ROOT = Path(__file__).resolve().parents[2]


def test_compute_entrypoint_help_without_backend_site_packages():
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPTS / "clusterkit_execute.py"), "--help"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "batch-preflight" in result.stdout and "batch-pairwise" in result.stdout


def test_cold_compute_import_does_not_load_application_or_provider_factory():
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'backend')!r}); "
        "import clusterkit_execution; "
        "forbidden = {'schedulers.factory', 'schedulers.pythoncm', 'platforms', "
        "'host_exec', 'tinydb', 'flask'}; "
        "assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_prepared_attempt_rejects_modified_scheduler_package_snapshot(tmp_path):
    (tmp_path / "hpcx-init.sh").write_text("# fixture only\n")
    plan = make_plan(tmp_path)
    execution.prepare_attempt(plan, SCRIPTS)
    root = Path(plan["results_directory"])
    package = root / "source/backend/schedulers/__init__.py"
    assert package.is_file()
    package.write_text(package.read_text() + "\n# changed fixture\n")
    with pytest.raises(ValueError, match="source changed"):
        execution.load_attempt(root / "plan.json", SCRIPTS)
