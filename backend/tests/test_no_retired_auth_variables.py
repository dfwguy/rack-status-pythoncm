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
"""No script, config or compose file may set a retired authentication variable.

**Why a test and not a cleanup.** A retired variable that is still *set* is worse than one that is
merely mentioned, because nothing fails: the file reads as though it configures authentication, the
product ignores it, and the two disagree silently. `build-docker.sh` passed
`-e RACK_WEB_AUTH_DISABLED=1` into a container that enforces authentication regardless, while
announcing "no auth" — so a developer would have met a login screen the script told them would not
be there, and reasonably concluded the container was broken.

Mentioning one in prose is fine and often necessary: a comment saying the switch was removed,
which is exactly what an operator upgrading from an older deployment needs to read. So this checks
for *assignment*, not for the name.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Removed by FR-18 (the shared token and PAM) and by the bypass removal on 2026-09-08. Each of
#: these is read by nothing; setting one configures a name the product does not consult.
RETIRED = (
    "RACK_WEB_AUTH_DISABLED",
    "RACK_WEB_AUTH_USER",
    "RACK_WEB_AUTH_MODE",
    "RACK_WEB_AUTH_TOKEN",
    "RACK_WEB_PAM_SERVICE",
)

#: Where a variable gets *set*: `export X=`, `X=` at the start of a line or after a shell
#: continuation, `-e X=` for docker, and `- X=` in a compose environment list.
_ASSIGNMENT = re.compile(
    r"(?:^|\s)(?:export\s+|-e\s+|-\s+|')?(" + "|".join(RETIRED) + r")=",
)

#: `unset` lines are the opposite of the problem — they clear a stale value out of a developer's
#: shell — and `${VAR:-...}` reads are matched by the pattern only when followed by `=`.
_EXEMPT_LINE = re.compile(r"^\s*#|unset\s")

SEARCH = ("*.sh", "*.env", "*.yml", "*.yaml", "*.toml")
SKIP_DIRS = {".git", "node_modules", ".venv", ".scratch", "out", "release", "dist", "__pycache__"}


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SEARCH:
        for path in REPO.rglob(pattern):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            files.append(path)
    return files


def _offenders() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_candidate_files()):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = [
            f"{n}: {line.strip()[:100]}"
            for n, line in enumerate(lines, 1)
            if _ASSIGNMENT.search(line) and not _EXEMPT_LINE.match(line)
        ]
        if hits:
            found[str(path.relative_to(REPO))] = hits
    return found


def test_no_file_sets_a_retired_auth_variable():
    offenders = _offenders()
    assert not offenders, (
        "these files assign a retired authentication variable:\n"
        + "\n".join(f"  {name}\n    " + "\n    ".join(hits) for name, hits in offenders.items())
        + "\n\nNothing reads them. Authentication cannot be disabled and there is no shared token; "
        "a deployment authenticates against the identity store, and a fresh one prints a one-time "
        "administrator password at startup. Remove the assignment (prose that explains the removal "
        "is fine)."
    )


def test_no_backend_module_reads_a_retired_variable():
    """AC-28f, the half about Python: the names are read nowhere under `backend/`.

    Separate from the assignment sweep above because the failure is different in kind. A *script*
    that sets one is misleading; a *module* that reads one is a live code path keyed to a variable
    nobody sets, which is the shape of a bypass that still works if the variable comes back.

    Prose is again allowed and again necessary — `web_auth.py` explains at length why the switch was
    removed, and `identity/testing.py` explains what replaced it. Both are worth keeping. So this
    looks for a *read*: `environ.get`, `environ[...]`, `getenv`, or the repo's own `truthy_env`.
    """
    reads = re.compile(
        r"""(environ\.get\(\s*["'](?:"""
        + "|".join(RETIRED)
        + r""")["']|environ\[\s*["'](?:"""
        + "|".join(RETIRED)
        + r""")["']\s*\]|getenv\(\s*["'](?:"""
        + "|".join(RETIRED)
        + r""")["']|truthy_env\(\s*["'](?:"""
        + "|".join(RETIRED)
        + r""")["'])""",
    )
    backend = REPO / "backend"
    offenders: dict[str, list[str]] = {}
    for path in sorted(backend.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        # A test may legitimately assert the *absence* of a read, and this file builds the patterns.
        if path.name in {"test_no_retired_auth_variables.py"}:
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = [
            f"{n}: {line.strip()[:100]}" for n, line in enumerate(lines, 1) if reads.search(line)
        ]
        if hits:
            offenders[str(path.relative_to(REPO))] = hits

    assert not offenders, (
        "these modules read a retired authentication variable:\n"
        + "\n".join(f"  {name}\n    " + "\n    ".join(h) for name, h in offenders.items())
        + "\n\nNothing should. Authentication cannot be disabled; a read here is a code path keyed "
        "to a variable no deployment sets, which becomes a live bypass if the name returns."
    )


def test_the_check_actually_looks_at_the_shipped_scripts():
    """Guard the guard: a glob that matched nothing would pass identically.

    Names the files rather than counting, because a rename that silently dropped `build-docker.sh`
    from the sweep is the failure this is here to notice — that script is the one that had a live
    assignment, so it is the one most worth pinning.
    """
    seen = {str(p.relative_to(REPO)) for p in _candidate_files()}
    for expected in ("build-docker.sh", "docker-compose.yml", "start.sh"):
        assert expected in seen, f"{expected} is not being scanned"


def test_it_would_catch_an_assignment(tmp_path):
    """Mutation check on the pattern itself, in every form these files actually use."""
    for line in (
        'export RACK_WEB_AUTH_DISABLED=1',
        'RACK_WEB_AUTH_DISABLED=1 \\',
        '  -e RACK_WEB_AUTH_DISABLED=1 \\',
        '      - RACK_WEB_AUTH_TOKEN=nvis1234',
        "      - 'RACK_WEB_AUTH_TOKEN=${RACK_WEB_AUTH_TOKEN}'",
    ):
        assert _ASSIGNMENT.search(line), f"pattern missed: {line!r}"

    # And does not fire on the two legitimate shapes.
    for line in (
        '# RACK_WEB_AUTH_DISABLED=1 turns the gate off entirely',
        'unset RACK_WEB_AUTH_TOKEN RACK_WEB_AUTH_MODE RACK_WEB_AUTH_DISABLED \\',
    ):
        assert _EXEMPT_LINE.match(line), f"exemption missed: {line!r}"
