# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""TC-21: the documented environment reference states what the audit trail cannot know.

Two documentation obligations are asserted as tests rather than trusted, because a doc statement
that nobody checks decays exactly like an untested branch. `test_https_serving.py` already
established this pattern for the Dockerfile.

Traces to AC-21 (NFR-7, the `src_ip` tunnel limitation) and the documentation half of AC-3a
(FR-2a, syslog is best-effort).
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[2] / "docs" / "ENVIRONMENT.md"
SECTION_HEADING = "### Application logging (`NVIS_LOG_*`)"


#: Subsections that together document this feature. Scoped rather than searching the whole file,
#: so a stray match in a 700-line reference cannot make these tests pass — but spanning both,
#: because the settings and the caveats they need are split across the two.
OUR_SUBSECTIONS = (SECTION_HEADING, "### The audit trail (`NVIS_AUDIT_*`)")


@pytest.fixture(scope="module")
def logging_section() -> str:
    """The logging and audit subsections, concatenated."""
    text = DOC.read_text(encoding="utf-8")
    chunks = []
    for heading in OUR_SUBSECTIONS:
        assert heading in text, f"{DOC.name} has no {heading!r} section"
        after = text.split(heading, 1)[1]
        chunks.append(after.split("\n### ", 1)[0])
    return "\n".join(chunks)


def test_environment_doc_states_the_tunnel_limitation(logging_section):
    """AC-21: operators reach this over an SSH tunnel, so `src_ip` is the tunnel endpoint."""
    assert "tunnel endpoint" in logging_section, (
        "the src_ip limitation must be stated where the audit settings are documented"
    )
    assert "authentication log" in logging_section, (
        "the doc should say where the operator's real address does exist"
    )


def test_environment_doc_states_syslog_is_best_effort(logging_section):
    """Documentation half of AC-3a. The behavioural half is TC-2's syslog-alone refusal."""
    assert "best-effort" in logging_section
    assert "lossy" in logging_section


def test_every_logging_setting_is_documented(logging_section):
    """A knob nobody documented is a knob nobody can use (FR-13's other half)."""
    import logging_config as lc

    undocumented = [name for name in lc.SETTINGS if f"`{name}`" not in logging_section]
    assert not undocumented, f"settings missing from {DOC.name}: {undocumented}"
