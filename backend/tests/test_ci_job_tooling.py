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

    def test_the_test_job_provides_rsync_because_the_suite_shells_out_to_it(self):
        """`bin/bootstrap-project-scripts.sh` needs `rsync`, and four tests drive it.

        `python:3.12-slim` does not ship rsync, so those tests failed in CI with exit 127 while
        passing on every developer machine. The script now refuses with a readable message rather
        than a bare `command not found`, but the tests still need the binary — and if somebody
        drops this install to speed the job up, the same four tests go red for a reason the
        failure output does not name. Asserting the install is what stops that being rediscovered.
        """
        job = _pipeline()["python-test"]
        lines = " ".join(_command_lines(job))
        assert "rsync" in lines, (
            "python-test no longer installs rsync, but the suite still drives "
            "bin/bootstrap-project-scripts.sh, which requires it"
        )

    #: What `python3` each runtime base provides. Needed because the Dockerfile names a distro,
    #: not an interpreter, so "which Python does the image run" is knowledge that lives nowhere
    #: else in the tree — which is precisely why two 3.11-only imports reached customers.
    RUNTIME_BASE_PYTHON = {
        "ubuntu:22.04": "3.10",
        "ubuntu:24.04": "3.12",
    }

    def test_a_job_imports_the_tree_on_the_python_the_image_actually_runs(self):
        """The image is a third tree nobody tested, and it cost two shipped defects.

        `tomllib` (stdlib in 3.11) broke the NPI catalog import on every container deployment, and
        `from enum import StrEnum` (3.11) killed `cli.py check` on an air-gapped install at a
        customer site. Both passed CI, because CI is 3.12 and the shipped image is 3.10.

        This asserts the job exists **and** that its interpreter matches the Dockerfile's final
        base, because those drift apart silently in either direction: move the base to 24.04 and
        the job keeps testing 3.10, which is merely wasteful; pin the job to 3.12 and it reports a
        safety it never checked, which is worse than having no job at all.
        """
        job = _pipeline().get("runtime-python-import")
        assert job, (
            "no job imports the tree on the image's Python. `Dockerfile`'s final stage runs an "
            "older interpreter than every job in this pipeline, and two 3.11-only imports have "
            "already reached customers through that gap"
        )

        bases = re.findall(r"^FROM\s+(\S+)", (REPO_ROOT / "Dockerfile").read_text(), re.MULTILINE)
        runtime_base = bases[-1]  # the final stage is the one that ships
        expected = self.RUNTIME_BASE_PYTHON.get(runtime_base)
        assert expected, (
            f"Dockerfile's runtime base is {runtime_base!r} and this test does not know which "
            f"Python it provides. Add it to RUNTIME_BASE_PYTHON so the check below can hold."
        )

        image = job.get("image")
        image_ref = image if isinstance(image, str) else (image or {}).get("name", "")
        assert f"python:{expected}" in image_ref, (
            f"runtime-python-import runs on {image_ref!r}, but the shipped image ({runtime_base}) "
            f"provides Python {expected}."
        )

    def test_the_gate_uses_the_generators_own_check_mode(self):
        """The specific repair, pinned. `--check` prints a unified diff, which is the only reason
        `git diff` was reached for; going back to git would reintroduce the failure exactly."""
        job = _pipeline()["sources-of-truth"]
        lines = " ".join(_command_lines(job))
        assert "--check" in lines, (
            "the sources-of-truth gate no longer runs the generators' --check mode"
        )

    def test_the_gate_reports_every_document_rather_than_short_circuiting(self):
        """`! A || ! B` stops at the first failure, so a stale vocabulary would hide a stale
        snapshot and the second fault would only appear on the next push. Same 'report every fault
        at once' rule the startup gate and config preflight follow.

        The expected count is **derived from the job**, not written here. This assertion used to
        say `== 2` and broke when a third generator landed — which trains the next person to bump
        a number rather than to ask whether the new generator is gated. Every generator the job
        runs must also be checked, and that is what this compares.
        """
        job = _pipeline()["sources-of-truth"]
        lines = _command_lines(job)
        # A regex, because the job's own failure message echoes the generator names back at the
        # contributor and a naive split leaves the closing quote attached.
        name = re.compile(r"bin/(gen-[\w.-]+\.py)")
        generated = {
            m.group(1)
            for line in lines
            if "--check" not in line
            for m in [name.search(line)]
            if m
        }
        checks = [line for line in lines if "--check" in line]
        checked = {m.group(1) for line in checks for m in [name.search(line)] if m}
        assert generated == checked, (
            f"every generator the job runs must also be checked; built but ungated: "
            f"{sorted(generated - checked)}, checked but never built: {sorted(checked - generated)}"
        )
        assert all("|| stale=1" in line for line in checks), (
            f"the checks short-circuit instead of collecting every verdict: {checks}"
        )
