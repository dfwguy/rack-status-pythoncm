#!/usr/bin/env bash
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
# Usage:
#   bin/check-tracked-tree.sh            # check HEAD
#   bin/check-tracked-tree.sh <ref>      # check any ref (e.g. origin/main, a tag, a SHA)
#
# Exits non-zero on failure, so it can gate a push or a CI job.

set -euo pipefail

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
echo "[tracked-tree] export dir: $WORK"

# Only the paths the backend needs to import. Deliberately NOT the whole tree: dot-directories are
# irrelevant here and some sandboxes refuse to create them under a temp dir.
git archive "$REF" backend version.py help_content.json | tar -x -C "$WORK"

if [[ ! -f "$WORK/backend/app.py" ]]; then
    echo "[tracked-tree] FAIL: backend/app.py is not tracked at $REF" >&2
    exit 1
fi

# Import every tracked backend module, not just app.py. app.py alone would miss a module that is
# tracked but whose own import is broken, since nothing imports it at startup.
cd "$WORK"
RACK_STATUS_DEMO=1 \
RACK_WEB_AUTH_DISABLED=1 \
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
names = list(module_names())
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
    print("Most likely an import was committed without the module it names. Check for an untracked")
    print("file: git status --short -- 'backend/**/*.py'")
    traceback.print_exception(type(failures[0][1]), failures[0][1], failures[0][1].__traceback__)
    sys.exit(1)

print(f"[tracked-tree] OK: {len(names)} modules imported, app exposes {routes} routes")
PY

echo "[tracked-tree] PASS: the committed tree stands on its own"
