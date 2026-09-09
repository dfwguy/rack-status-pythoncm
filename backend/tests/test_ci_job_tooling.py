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
"""A CI job may not invoke a tool its image does not have.

`python:3.12-slim` and `node:24-slim` ship **no `git`** — the runner clones the repository in a
separate helper container, so the checkout exists while the binary does not. This is written down
in `local-vs-ci-tree.mdc` §4, and it has now cost this repo twice:

* a script that shelled out to `git archive` died with `git: command not found`;
* the `sources-of-truth` gate ran `git diff --exit-code` to show a contributor *which* action had
  changed. It never passed once. Worse than failing, it failed **dishonestly**: the non-zero exit
  from the missing binary took the same branch as a real difference, so every run printed "a
  generated source of truth is stale" and blamed the documents for a missing tool. Six pipelines,
  and because it sits in the `lint` stage every downstream job was skipped — the whole pipeline
  red on a gate that could not run.

The generalisable point, and the reason this is a test rather than a line in a document: **an
absent tool and a real failure must not be indistinguishable.** A guard that reads the pipeline is
the only thing that catches it before a runner does, because the failure is invisible in a
developer's shell, where git is always on the PATH.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_FILE = REPO_ROOT / ".gitlab-ci.yml"

# Images that carry no `git`. Both official `-slim` variants drop it; the GitLab runner does the
# clone itself, which is why a job can see the tree and still not have the binary.
GITLESS_IMAGES = ("python:3.12-slim", "node:24-slim")

# Tools absent from those images. `git` is the one that has bitten us; the others are here because
# they are the reflexes a shell script reaches for and are equally absent from a slim image.
ABSENT_TOOLS = ("git", "curl", "make")

# Keys that are pipeline configuration rather than jobs.
NOT_A_JOB = {"include", "default", "stages", "variables", "workflow"}


#: `bin/check-tracked-tree.sh` exports tracked files *excluding dotfiles*, so `.gitlab-ci.yml` is
#: absent in that tree and every assertion here would fail on a file that is simply not there —
#: a false alarm about CI raised by the tool that checks CI. A real runner does a full clone and
#: always has it, so skipping is the honest answer in the one tree that does not.
pytestmark = pytest.mark.skipif(
    not CI_FILE.is_file(), reason=f"{CI_FILE.name} absent (dotfile-stripped tree)"
)


def _pipeline() -> dict:
    """`yaml.safe_load` chokes on GitLab's `!reference` tag, which is not standard YAML."""
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor(
        "!reference", lambda loader, node: loader.construct_sequence(node)
    )
    return yaml.load(CI_FILE.read_text(encoding="utf-8"), Loader=_Loader) or {}


def _command_lines(job: dict) -> list[str]:
    """Every line a shell would actually execute, with comments removed.

    Stripping comments is the whole difficulty. The first version of this check matched the word
    `git` anywhere in the job body and flagged both `sources-of-truth` and `python-test` — whose
    only mentions were prose explaining *why* they avoid git. A guard that cannot tell a command
    from a comment fails on the very notes written to prevent the bug.
    """
    lines: list[str] = []
    for key in ("before_script", "script", "after_script"):
        for entry in job.get(key) or []:
            if not isinstance(entry, str):
                continue
            for line in entry.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                lines.append(stripped.split(" #")[0])
    return lines


def _jobs_on_gitless_images() -> dict[str, dict]:
    pipeline = _pipeline()
    return {
        name: job
        for name, job in pipeline.items()
        if name not in NOT_A_JOB
        and isinstance(job, dict)
        and str(job.get("image", "")) in GITLESS_IMAGES
    }


class TestNoJobCallsAToolItsImageLacks:
    def test_the_pipeline_parses_and_has_jobs_to_check(self):
        """Guards the guard. Every assertion below is vacuously true over an empty mapping, so a
        `!reference` tag or a restructured file would silently disable the whole file."""
        jobs = _jobs_on_gitless_images()
        assert len(jobs) >= 3, f"expected several slim-image jobs, found {sorted(jobs)}"

    @pytest.mark.parametrize("tool", ABSENT_TOOLS)
    def test_no_slim_image_job_invokes_it(self, tool):
        offenders: list[str] = []
        # Word-boundary, and not preceded by `-` or `/`, so `--git-dir`, `gitlab-ci` and a path
        # like `/usr/bin/gitfoo` do not count as an invocation of the bare command.
        pattern = re.compile(rf"(?<![\w./-])\b{tool}\b")
        for name, job in _jobs_on_gitless_images().items():
            for line in _command_lines(job):
                if pattern.search(line):
                    offenders.append(f"{name}: {line}")

        assert not offenders, (
            f"a CI job invokes `{tool}`, which its slim image does not have. It will fail with "
            f"`{tool}: command not found` — and if the call sits inside an `if !` or a `||`, the "
            f"missing binary is indistinguishable from a real failure, so the job reports the "
            f"wrong cause. Use a Python/stdlib equivalent, or install the tool explicitly.\n  "
            + "\n  ".join(offenders)
        )

    def test_the_gate_uses_the_generators_own_check_mode(self):
        """The specific repair, pinned. `--check` prints a unified diff, which is the only reason
        `git diff` was reached for; going back to git would reintroduce the failure exactly."""
        job = _pipeline()["sources-of-truth"]
        lines = " ".join(_command_lines(job))
        assert "--check" in lines, (
            "the sources-of-truth gate no longer runs the generators' --check mode"
        )

    def test_the_gate_reports_both_documents_rather_than_short_circuiting(self):
        """`! A || ! B` stops at the first failure, so a stale vocabulary would hide a stale
        snapshot and the second fault would only appear on the next push. Same 'report every fault
        at once' rule the startup gate and config preflight follow."""
        job = _pipeline()["sources-of-truth"]
        lines = _command_lines(job)
        checks = [line for line in lines if "--check" in line]
        assert len(checks) == 2, f"expected both generators checked, got {checks}"
        assert all("|| stale=1" in line for line in checks), (
            f"the checks short-circuit instead of collecting both verdicts: {checks}"
        )
