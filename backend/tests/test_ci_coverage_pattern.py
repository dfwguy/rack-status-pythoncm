# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A coverage badge reads its figure from the job log, through the job's ``coverage:`` pattern.

The pattern and the report that prints the figure live in different files, and nothing failed when
they drifted apart: ``.coveragerc`` moved to ``precision = 1``, the backend total became ``71.4%``,
the whole-number pattern stopped matching, and the Backend Coverage badge read "unknown" while every
pipeline stayed green. These tests render each summary line the way its tool prints it -- the backend
one at the precision ``.coveragerc`` asks for -- and require the job's pattern to read the number.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_FILE = REPO_ROOT / ".gitlab-ci.yml"
COVERAGERC = REPO_ROOT / ".coveragerc"


def _pipeline() -> dict:
    """``yaml.safe_load`` chokes on GitLab's ``!reference`` tag, which is not standard YAML."""
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))
    return yaml.load(CI_FILE.read_text(encoding="utf-8"), Loader=_Loader) or {}


def _coverage_pattern(job: str) -> re.Pattern[str]:
    """The job's ``coverage: '/.../'`` as a regex. GitLab's RE2 and Python agree on what it uses."""
    raw = _pipeline()[job]["coverage"]
    assert raw.startswith("/") and raw.endswith("/"), f"{job}: coverage must be a /regex/, got {raw!r}"
    return re.compile(raw[1:-1])


def _backend_precision() -> int:
    config = configparser.ConfigParser()
    config.read(COVERAGERC, encoding="utf-8")
    return config.getint("report", "precision", fallback=0)


@pytest.mark.parametrize("precision", sorted({0, 1, 2, _backend_precision()}))
def test_the_backend_pattern_reads_coverage_py_total_at_any_precision(precision):
    # coverage.py's term-missing TOTAL row with branch coverage, as python-test prints it.
    percent = f"{71.4:.{precision}f}%"
    line = f"TOTAL                              33290   8439  10430   1819  {percent}"
    match = _coverage_pattern("python-test").search(line)
    assert match and match.group(1) == percent


def test_the_backend_pattern_reads_the_precision_the_repo_actually_uses():
    # The case that broke: the pattern has to read the total .coveragerc makes coverage.py print.
    percent = f"{71.4:.{_backend_precision()}f}%"
    match = _coverage_pattern("python-test").search(f"TOTAL   33290   8439  10430   1819  {percent}")
    assert match and match.group(1) == percent


@pytest.mark.parametrize("percent", ["56.39%", "100%", "0%"])
def test_the_frontend_pattern_reads_the_istanbul_statements_line(percent):
    # vitest's text-summary reporter, as frontend-test prints it.
    line = f"Statements   : {percent} ( 1234/2188 )"
    match = _coverage_pattern("frontend-test").search(line)
    assert match and match.group(1) == percent
