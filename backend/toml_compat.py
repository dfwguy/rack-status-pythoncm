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
"""The one place in the tree that decides where TOML parsing comes from (FR-4, DC-1).

`tomllib` entered the standard library in **Python 3.11**. The runtime image does not have it:

    $ docker run --rm ubuntu:22.04 ... python3 -V
    Python 3.10.12
    $ ... python3 -c 'import tomllib'
    ModuleNotFoundError: No module named 'tomllib'

Every place the project actually *runs tests* has 3.12 — the three CI jobs are `python:3.12-slim`
(`.gitlab-ci.yml:61,109,144`) and local development is 3.12. So a bare `import tomllib` passes the
entire suite, passes CI, and fails only inside the shipped container.

**That is `local-vs-ci-tree.mdc` with the image as a third tree nobody tests**, and it had already
happened before this module existed. `catalog/npi_import.py` imported `tomllib` at module level;
`catalog/seed.py` wrapped its importer in `except Exception` logging at *debug* level, so the NPI
reference catalog silently failed to import on every container deployment with no visible trace,
while `catalog/api.py` imported it unguarded and answered 500 on
`POST /api/catalog/npi-tools/import`. Neither symptom can occur in CI.

WHY A MODULE RATHER THAN THE TWO-LINE IDIOM AT EACH SITE
---------------------------------------------------------
The `try`/`except ImportError` idiom is short enough to inline, which is exactly the problem: it
gets inlined at some sites and forgotten at others, and the forgotten one is invisible until a
container starts. One import site is checkable — `test_toml_compat.py` asserts that no other module
in the tree imports `tomllib` or `tomli` directly, so a new call site cannot reintroduce the split.

`ModuleNotFoundError`, not `ImportError`
----------------------------------------
`ModuleNotFoundError` is a *subclass* of `ImportError`, so catching the broader one would also
swallow a `tomllib` that exists but is **broken** — a partial install, a shadowing local file, a
compiled-extension mismatch. Those should fail loudly rather than fall back silently to a different
parser, because a fallback that hides a damaged interpreter is how you end up debugging the wrong
component. Catching the narrow error means "this Python has no tomllib" is the only condition that
reaches `tomli`.
"""

from __future__ import annotations

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10, which is what ubuntu:22.04 ships
    import tomli as _toml  # type: ignore[no-redef]

#: `load(fp)` and `loads(s)`, with `fp` opened in **binary** mode — the stdlib signature, which
#: `tomli` matches exactly because it is the same codebase.
load = _toml.load
loads = _toml.loads

#: Re-exported so call sites can catch a parse error without repeating the import decision. A site
#: that reached for `tomllib.TOMLDecodeError` itself would need the conditional import back.
TOMLDecodeError = _toml.TOMLDecodeError

#: Which implementation bound. Diagnostic only — the two are API-identical, so nothing should
#: branch on this. It exists so a support bundle can answer "which parser was this?" without
#: re-deriving it from the interpreter version.
IMPLEMENTATION = _toml.__name__

__all__ = ["IMPLEMENTATION", "TOMLDecodeError", "load", "loads"]
