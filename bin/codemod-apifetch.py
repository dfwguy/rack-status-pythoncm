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
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""One-shot codemod: route every SPA call to ``/api/*`` through ``apiFetch``.

A bare ``fetch("/api/...")`` gets no silent access-token renewal, so once the access cookie
expires (one hour by default) that call site simply starts returning 401 and its panel breaks
until some *other* call happens to refresh the cookie first. ``apiFetch`` is the wrapper that
refreshes once and replays; half the call sites were not using it.

Run from the repo root::

    bin/codemod-apifetch.py --dry-run   # unified diff, changes nothing
    bin/codemod-apifetch.py             # apply

Kept as a script rather than done by hand so the transformation is reviewable as a rule instead
of as 26 separate diffs, and so it can be re-run to catch call sites added since.

**Two files are excluded on purpose** — see ``EXCLUDE``. Everything else is mechanical.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "frontend" / "src"
API_FETCH_MODULE = SRC / "lib" / "apiFetch.ts"

#: Deliberate exceptions, both of which would be wrong to convert.
EXCLUDE = {
    # The wrapper itself. Its own `fetch("/api/auth/refresh")` must stay raw: routing the renewal
    # through the thing that triggers renewals is an infinite regress.
    SRC / "lib" / "apiFetch.ts",
    # Pre-authentication. The SRP exchange is how a session is *obtained*, so there is by
    # definition no access token to renew, and `apiFetch` lists these paths in NO_REFRESH anyway.
    # Converting them would add operator headers to an anonymous handshake and imply a renewal
    # path that cannot exist.
    SRC / "lib" / "srpClient.ts",
}

#: `fetch(` followed by a string literal beginning `/api`.
#:
#: The lookbehind is what stops a second run turning `apiFetch(` into `apiapiFetch(`, and it
#: excludes a `.` so a method call like `client.fetch("/api/x")` is left alone. `\s*` spans
#: newlines, which matters because several call sites wrap the argument onto its own line.
CALL = re.compile(r"(?<![\w$.])fetch\((\s*[\"'`]/api)")

#: A whole import statement, including the multi-line `import {\n  a,\n} from "x";` form.
#:
#: The trailing class is `[ \t]*`, NOT `\s*`: `\s` matches newlines, so the first version's match
#: ran past the blank line separating the import block from the code below it, and the inserted
#: statement landed glued to the first declaration instead of inside the block.
IMPORT_LINE = re.compile(r"^import\s[\s\S]*?;[ \t]*$", re.MULTILINE)
HAS_IMPORT = re.compile(r"""^import\s*\{[^}]*\bapiFetch\b[^}]*\}\s*from\s*["'].*apiFetch["'];""",
                        re.MULTILINE)


def relative_import(from_file: Path) -> str:
    """The specifier `from_file` should use to import the wrapper, without an extension.

    Depth varies from `./lib/apiFetch` in `App.tsx` to `../../lib/apiFetch` under
    `components/rack/`, so it is computed rather than guessed.
    """
    spec = os.path.relpath(API_FETCH_MODULE.with_suffix(""), from_file.parent)
    return spec if spec.startswith(".") else f"./{spec}"


def transform(path: Path) -> str | None:
    """The file's new text, or None when nothing needed changing."""
    original = path.read_text(encoding="utf-8")
    converted, count = CALL.subn(r"apiFetch(\1", original)
    if count == 0:
        return None

    if not HAS_IMPORT.search(converted):
        spec = relative_import(path)
        statement = f'import {{ apiFetch }} from "{spec}";'
        imports = list(IMPORT_LINE.finditer(converted))
        if imports:
            # After the last import, so the new line joins the existing block rather than
            # splitting it -- and after, not before, because the first import may be preceded by
            # the SPDX header and a `"use client"`-style directive.
            at = imports[-1].end()
            converted = f"{converted[:at]}\n{statement}{converted[at:]}"
        else:
            converted = f"{statement}\n{converted}"
    return converted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print a diff and change nothing")
    args = parser.parse_args()

    targets = sorted(
        p for p in SRC.rglob("*.[tj]s*")
        if p not in EXCLUDE and ".test." not in p.name and CALL.search(p.read_text("utf-8"))
    )
    if not targets:
        print("nothing to do: no raw fetch() to /api remains")
        return 0

    changed = 0
    for path in targets:
        new = transform(path)
        if new is None:
            continue
        changed += 1
        rel = path.relative_to(REPO_ROOT)
        if args.dry_run:
            sys.stdout.writelines(
                difflib.unified_diff(
                    path.read_text("utf-8").splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{rel}", tofile=f"b/{rel}",
                )
            )
        else:
            path.write_text(new, encoding="utf-8")
            print(f"updated {rel}")

    print(f"\n{changed} file(s) {'would be' if args.dry_run else ''} changed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
