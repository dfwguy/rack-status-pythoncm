#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# check-tracked-tree.sh — prove the committed tree boots without help from your working directory.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY THIS EXISTS
#
# On 2026-08-21 four commits were pushed in which backend/app.py did
# `from audit import request_hook`, while backend/audit/request_hook.py was never `git add`ed. Every
# local check passed -- the app booted, 1686 tests were green, ruff and pylint were clean -- because
# the untracked file was sitting in the working tree the whole time. A fresh clone or a CI runner
# would have died at import with ModuleNotFoundError.
#
# The cause is mundane and will happen again: staging a whole file to fix its whitespace also stages
# whatever unrelated edit is in it, and an import can arrive one commit before the module it names.
# `git status` shows the untracked file, but next to twenty other lines nobody reads twice.
#
# So this script asks the only question that actually settles it: extract ONLY what git tracks at a
# given ref, into a directory with no relationship to this checkout, and import the app there.
#
# It also answers a second question, with --with-tests, and that one has now bitten three times in
# one day. The export contains only tracked files, so it has no `static/spa/` -- a gitignored build
# artifact that exists in every developer's tree and in no CI checkout. Tests that walk HTTP
# routes therefore behave DIFFERENTLY here than locally: the four SPA shell routes answer 200 with
# a built frontend and 503 without one. A local `pytest` cannot see that class of failure at all,
# which is exactly how eight endpoint tests reached the pipeline green from this workstation.
# Running the suite inside the export reproduces the runner's tree without needing the runner.
#
# Usage:
#   bin/check-tracked-tree.sh                    # imports only, ~3s
#   bin/check-tracked-tree.sh --with-tests       # imports, then the suite in a build-artifact-free tree
#   bin/check-tracked-tree.sh <ref>              # any ref (origin/main, a tag, a SHA)
#
# Exits non-zero on failure, so it can gate a push or a CI job.

set -euo pipefail

WITH_TESTS=0
if [[ "${1:-}" == "--with-tests" ]]; then
    WITH_TESTS=1
    shift
fi

REF="${1:-HEAD}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if [[ -x .venv/bin/python ]]; then PYTHON="$REPO_ROOT/.venv/bin/python"; else PYTHON=python3; fi
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nvis-tracked-XXXXXX")"
# shellcheck disable=SC2064  # expand WORK now, while it is still set
trap "rm -rf '$WORK'" EXIT

echo "[tracked-tree] ref=$REF  python=$PYTHON"

# Two ways to get a tree containing ONLY tracked files, and which one applies is decided by whether
# git exists here rather than by a flag.
#
# GitLab's `python:3.12-slim` job image has no git binary -- the runner clones in a separate helper
# container -- so `git archive` is simply unavailable in CI. That is fine, because a CI job starts
# from a fresh clone: the checkout ALREADY is the tracked tree, and there is no working-tree drift
# for the export to strip. Locally the opposite holds and the export is the entire point.
if command -v git >/dev/null 2>&1; then
    echo "[tracked-tree] mode: export (git available) -> $WORK"
    # Every tracked top-level entry except dot-entries and frontend/.
    #
    # Wider than it first needs to be, and each exclusion has a reason. The import check alone only
    # needs `backend`, but --with-tests runs the suite, and two test modules read bin/scripts/ --
    # exporting only backend made them fail with FileNotFoundError, which is a broken harness
    # rather than the divergence being hunted. Dot-entries are dropped because some sandboxes
    # refuse to create them under a temp dir and none of them affect a pytest run (there is no
    # pytest ini in this repo). frontend/ is dropped because it is large and the Python suite never
    # reads it -- and crucially, NOT exporting it is faithful: static/spa/ is gitignored, so a real
    # CI checkout has no built frontend either. That absence is the whole point.
    # Filtering happens at EXTRACTION, via tar, rather than by building an argument list.
    #
    # Two portability traps avoided. `mapfile` would read the path list into an array, but it is
    # bash 4+ and macOS ships bash 3.2 as /bin/bash -- the divergence this whole file is about,
    # cutting the other way. Word-splitting an unquoted variable of paths works today only because
    # no tracked top-level name contains a space. `tar --exclude` needs neither: it is supported by
    # both bsdtar (macOS) and GNU tar (Linux, and the CI image).
    git archive "$REF" | tar -x -C "$WORK" \
        --exclude='.*' --exclude='frontend' --exclude='frontend/*'
    ROOT="$WORK"
else
    echo "[tracked-tree] mode: in-place (no git -- assuming a clean CI checkout)"
    if [[ "$REF" != "HEAD" ]]; then
        echo "[tracked-tree] FAIL: ref '$REF' was requested but git is not available here." >&2
        echo "[tracked-tree]       Only the checked-out tree can be verified without git." >&2
        exit 1
    fi
    ROOT="$REPO_ROOT"
fi

if [[ ! -f "$ROOT/backend/app.py" ]]; then
    echo "[tracked-tree] FAIL: backend/app.py is not present at $REF" >&2
    exit 1
fi

# Import every backend module, not just app.py. app.py alone would miss a module that is tracked but
# whose own import is broken, since nothing imports it at startup -- which is the one thing this
# adds over the test suite, and it holds in both modes above.
cd "$ROOT"
RACK_STATUS_DEMO=1 \
NVIS_LOG_LEVEL=ERROR \
"$PYTHON" - <<'PY'
import importlib
import pathlib
import sys
import traceback

sys.path.insert(0, "backend")
sys.dont_write_bytecode = True

root = pathlib.Path("backend")
skip_dirs = {"tests", "__pycache__", "rack_web"}


def module_names():
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(p in skip_dirs for p in parts):
            continue
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
            if not parts:
                continue
        else:
            parts = parts[:-1] + (parts[-1][:-3],)
        yield ".".join(parts)


failures = []
# `app` is imported explicitly below, as the composition root. Skipping it here keeps a failure in
# it from being reported twice, which is confusing in the one moment someone is reading this output.
names = [n for n in module_names() if n != "app"]
for name in names:
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - reporting every failure is the whole point
        failures.append((name, exc))

# The composition root last: it wires everything and is where a missing module actually bites.
try:
    import app

    routes = len(list(app.app.url_map.iter_rules()))
except Exception as exc:  # noqa: BLE001
    failures.append(("app (composition root)", exc))
    routes = 0

if failures:
    print(f"[tracked-tree] FAIL: {len(failures)} of {len(names) + 1} modules did not import")
    for name, exc in failures:
        print(f"  {name}: {type(exc).__name__}: {exc}")
    print()
    # Two causes, and the hint used to name only the first — which sent the reader looking for an
    # untracked file when the real answer was the interpreter. That matters now that this script
    # runs on the image's Python (the `runtime-python-import` job) as well as on 3.12, because the
    # second cause is the *only* one that job exists to catch.
    print("Two things produce this:")
    print()
    print("  1. An import committed without the module it names. Check for an untracked file:")
    print("       git status --short -- 'backend/**/*.py'")
    print()
    print(f"  2. A name this interpreter does not have. Running Python {sys.version.split()[0]};")
    print("     the shipped container runs the `python3` of the Dockerfile's final stage, which is")
    print("     older. `StrEnum` and `tomllib` are 3.11+, and both have shipped broken this way.")
    print("     See backend/toml_compat.py for how the tree handles that split.")
    traceback.print_exception(type(failures[0][1]), failures[0][1], failures[0][1].__traceback__)
    sys.exit(1)

print(f"[tracked-tree] OK: {len(names)} modules imported, app exposes {routes} routes")
PY

if [[ "$WITH_TESTS" == "1" ]]; then
    if [[ ! -d "$ROOT/backend/tests" ]]; then
        echo "[tracked-tree] FAIL: backend/tests is not tracked, so --with-tests cannot run" >&2
        exit 1
    fi
    # No static/spa/ here, by construction. That is the point: the SPA shell routes answer 503
    # rather than 200, exactly as they do on a runner, so a route-walking test that only ever ran
    # against a built frontend gets its first honest result.
    echo "[tracked-tree] running the suite in a build-artifact-free tree..."
    ( cd "$ROOT" && "$PYTHON" -m pytest backend/tests -q --no-header ) || {
        echo "[tracked-tree] FAIL: the suite passes in your tree and fails without build artifacts." >&2
        echo "[tracked-tree]       This is the local/CI gap -- see .cursor/rules/local-vs-ci-tree.mdc" >&2
        exit 1
    }
fi

echo "[tracked-tree] PASS: the committed tree stands on its own"
