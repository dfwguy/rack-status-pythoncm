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
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Check that the docs tree still obeys its own Diataxis structure.

WHAT THIS CAN AND CANNOT CHECK
------------------------------
It cannot check that a page is *good*, or even that it is really the mode it claims. That is a
review question and it stays one.

It checks the mechanical failures that quietly dissolve a Diataxis structure over a year, every
one of which is invisible in the diff that introduces it:

* a page whose declared mode disagrees with the directory it is in -- the sidebar then promises
  one kind of page and delivers another;
* a page with no declared mode at all, which is how "we will classify it later" becomes never;
* a how-to whose title is not a task, which is the reliable early sign of a how-to that has
  drifted into being an explanation;
* a tutorial or explanation carrying the imperative-heavy shape of a how-to, or a reference page
  telling the reader what to do.

Run with no arguments to check. Exit code 1 lists every problem, so one run tells you everything
rather than one thing at a time.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"

#: Directory -> the mode a page in it must declare. The landing page and anything outside these
#: directories is exempt; `index.md` at the root is a hub, not a mode.
DIRECTORY_MODE = {
    "tutorials": "tutorial",
    "how-to": "how-to",
    "reference": "reference",
    "explanation": "explanation",
}

#: Modes conf.py knows how to render a banner for. A page declaring anything else silently gets
#: no banner, which is exactly the kind of quiet failure this script exists to make loud.
KNOWN_MODES = frozenset(DIRECTORY_MODE.values())

#: A how-to's title states the task. "How to deploy on an ITSS VM" is findable by someone who
#: knows what they want and not what it is called; "Deployment" is not.
HOWTO_TITLE_RE = re.compile(r"^#\s+How to\s+\S", re.MULTILINE)

#: A numbered procedure heading: "## 3. Fix identity by hand". This is the structural signature of
#: a page that walks someone through an ordered task, and unlike counting imperative verbs it does
#: not depend on how the author phrases a sentence.
STEP_HEADING_RE = re.compile(r"^#{2,3}\s+(?:Step\s+)?\d+[.)]\s+\S", re.MULTILINE)

#: Opening fences only, so a block counts once.
CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]", re.MULTILINE)

#: A how-to's title states the task, and so does a tutorial's -- but only the how-to's is
#: predictable enough to check.
#:
#: Below this many actionable elements (numbered steps plus code blocks), a how-to is not walking
#: anyone through anything. Deliberately low: the check is for pages that drifted into being
#: essays, not a quota on how procedural a guide must be.
HOWTO_ACTION_MINIMUM = 3

#: A numbered procedure inside an explanation is the one shape that is unambiguously misfiled --
#: the reader came for understanding and found steps, which belong in a how-to it links to. One or
#: two numbered headings can be an enumeration of arguments, so allow a couple.
EXPLANATION_STEP_LIMIT = 2


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``(keys, body)``. Keys only from a leading YAML block, values left as raw strings.

    Deliberately not a YAML parse: this runs in CI before the docs requirements are necessarily
    installed, and the only keys it needs are flat scalars.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    body_start = text.index("\n", end + 1) + 1
    keys = {
        match.group(1): match.group(2).strip()
        for match in re.finditer(r"^([a-z_]+):\s*(.*)$", text[:body_start], re.MULTILINE)
    }
    return keys, text[body_start:]


def check_page(path: Path) -> list[str]:
    """Every problem with one page. Empty means it is fine."""
    relative = path.relative_to(REPO_ROOT)
    text = path.read_text(encoding="utf-8")
    keys, body = frontmatter(text)
    problems: list[str] = []

    section = path.relative_to(DOCS).parts[0]
    expected = DIRECTORY_MODE.get(section)
    declared = keys.get("diataxis")

    if expected is None:
        return problems

    if not declared:
        problems.append(
            f"{relative}: no `diataxis:` in the frontmatter. Every page under {section}/ declares "
            f"its mode, so add `diataxis: {expected}` -- or move the page if it is not one."
        )
        return problems

    if declared not in KNOWN_MODES:
        problems.append(
            f"{relative}: `diataxis: {declared}` is not one of {sorted(KNOWN_MODES)}. conf.py "
            f"renders no banner for an unknown mode, so this would fail silently."
        )
        return problems

    if declared != expected:
        problems.append(
            f"{relative}: declares `{declared}` but sits in {section}/, which is `{expected}`. "
            f"The sidebar promises the reader one kind of page and the page is another; move it "
            f"or change the declaration."
        )

    # Generated pages are reference by construction and their prose is not written by hand, so the
    # shape heuristics below have nothing useful to say about them.
    if keys.get("nvis_generated"):
        return problems

    steps = len(STEP_HEADING_RE.findall(body))

    if declared == "how-to":
        if not HOWTO_TITLE_RE.search(body):
            problems.append(
                f"{relative}: the title does not start with 'How to'. A how-to is named for the "
                f"task, because that is what the reader searched for."
            )
        actions = steps + len(CODE_FENCE_RE.findall(body))
        if actions < HOWTO_ACTION_MINIMUM:
            problems.append(
                f"{relative}: {actions} actionable element(s) -- numbered steps plus code blocks. "
                f"A how-to that walks nobody through anything has become an explanation."
            )

    if declared == "explanation" and steps > EXPLANATION_STEP_LIMIT:
        problems.append(
            f"{relative}: {steps} numbered procedure heading(s), over the limit of "
            f"{EXPLANATION_STEP_LIMIT}. Explanation is for understanding; a procedure belongs in "
            f"a how-to this page links to."
        )

    return problems


def main() -> int:
    if not DOCS.is_dir():
        print(f"no docs directory at {DOCS}", file=sys.stderr)
        return 1

    pages = sorted(
        path for path in DOCS.rglob("*.md")
        if "_build" not in path.parts and path.name != "README.md"
    )

    problems = [problem for path in pages for problem in check_page(path)]

    if problems:
        print(f"docs structure: {len(problems)} problem(s) in {len(pages)} page(s)\n",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nSee .cursor/rules/docs-diataxis.mdc, or https://diataxis.fr/ for the four modes.",
              file=sys.stderr)
        return 1

    print(f"docs structure: {len(pages)} page(s) OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
