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
# check-ci-lint.sh — run exactly what CI's `python-lint` job runs, with its pinned versions.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY THIS EXISTS
# ---------------
# `python-lint` is the FIRST stage, so when it fails every later job — python-test, frontend-test,
# docker-build — is *skipped*, not run. On 2026-09-02 one finding took the pipeline red and kept it
# there for 10 consecutive runs over 27 hours, during which **no test ran in CI at all**. The
# finding itself was a one-line duplicate import.
#
# It stayed hidden because the job runs TWO linters and only one of them is habitually installed:
#
#     ruff check backend/ --exclude tests     # usually present, usually clean
#     pylint --recursive=y backend            # pinned in requirements-dev.txt, often not installed
#
# Running `ruff` alone and concluding "lint is clean" is the exact mistake. This script removes the
# choice: one command, both linters, the pinned versions.
#
# VERSIONS ARE PART OF THE CONTRACT. `.gitlab-ci.yml` pins both deliberately — unpinned, a new ruff
# release widens the default rule set under us, which is how that job once sat red at ~310 findings
# while `allow_failure` hid it. A locally installed *different* version therefore does not answer
# the question CI asks, so a mismatch is reported rather than ignored.
#
# Usage:
#   bin/check-ci-lint.sh            # check; non-zero if CI would fail
#   bin/check-ci-lint.sh --install  # install/repair the pinned versions first

set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi

# The single source of truth for both pins, so this script cannot drift from the job.
# bash 3.2 on macOS has no associative arrays -- see local-vs-ci-tree.mdc.
want_ruff=$(sed -n 's/^ruff==\(.*\)$/\1/p' backend/requirements-dev.txt | head -1)
want_pylint=$(sed -n 's/^pylint==\(.*\)$/\1/p' backend/requirements-dev.txt | head -1)

if [ "${1:-}" = "--install" ]; then
  echo "[ci-lint] installing ruff==$want_ruff pylint==$want_pylint"
  "$PY" -m pip install -q "ruff==$want_ruff" "pylint==$want_pylint" || exit 1
fi

have() { "$PY" -m "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1; }

missing=0
for tool in ruff pylint; do
  eval "want=\$want_$tool"
  got=$(have "$tool")
  if [ -z "$got" ]; then
    echo "[ci-lint] MISSING: $tool (CI uses $want). Run: bin/check-ci-lint.sh --install"
    missing=1
  elif [ "$got" != "$want" ]; then
    # Not fatal: a mismatched version still finds most things. But it is NOT the answer CI gives,
    # so say so rather than let a clean run here imply a green pipeline.
    echo "[ci-lint] WARNING: $tool $got installed, CI pins $want — findings may differ"
  fi
done
[ "$missing" -eq 1 ] && exit 1

rc=0
echo "[ci-lint] \$ ruff check backend/ --exclude tests"
"$PY" -m ruff check backend/ --exclude tests || rc=1

# Pylint runs only for what ruff structurally cannot see (cross-module duplication, reimports).
# `--recursive=y backend` verbatim from the job: narrowing the path here would be the same class of
# mistake as running only one of the two linters.
echo "[ci-lint] \$ pylint --recursive=y backend"
"$PY" -m pylint --recursive=y backend || rc=1

if [ "$rc" -eq 0 ]; then
  echo "[ci-lint] PASS — CI's python-lint stage would succeed"
else
  echo "[ci-lint] FAIL — CI's python-lint would fail, and every later stage would be SKIPPED"
fi
exit "$rc"
