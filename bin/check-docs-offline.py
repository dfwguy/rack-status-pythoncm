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
"""Fail if the built documentation would fetch anything from the network.

Operators read these pages over an SSH tunnel to an air-gapped cluster. A subresource hosted
somewhere else does not degrade gracefully there — it hangs until it times out, then the page
renders without it, and the only evidence is a console the operator is not looking at. That is
how the documentation shipped for weeks in a fallback font: `nvidia-sphinx-theme` points twelve
`@font-face` rules at `images.nvidia.com`, and on a connected laptop it looked perfect.

**Fonts were the instance; the class is "anything fetched".** A theme bump can add a stylesheet
from a CDN, an analytics snippet or a hosted icon set, and each would fail the same silent way.
So this checks every attribute a browser resolves on its own — `src` and `href` in the HTML, and
`url(...)` in the CSS — rather than grepping for the one hostname that caused the last incident.

A link a *human* clicks is fine and is not flagged: `<a href="https://…">` is a reference, not a
subresource, and a documentation page that cannot cite an upstream URL would be worse. Only tags
that fetch without being asked are errors.

Run against a build:

    python bin/check-docs-offline.py                 # static/docs
    python bin/check-docs-offline.py --build DIR
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_BUILD = REPO_ROOT / "static" / "docs"

#: `src` on any tag, and `href` on everything except `<a>`. The negative lookbehind is the whole
#: subtlety: `<a href="https://docs.nvidia.com/…">` is a citation and must pass, while
#: `<link href="https://fonts.example/…">` is a fetch and must not.
_HTML_FETCH = re.compile(
    r"""<(?!a[\s>])(?:[^>]*?\s)?(?:src|href)\s*=\s*["'](https?:)?//([^"']+)["']""",
    re.IGNORECASE,
)

#: `url(…)` in a stylesheet is always a fetch — a font, an image, an imported sheet.
_CSS_FETCH = re.compile(r"""url\(\s*["']?(?:https?:)?//([^)"'\s]+)""", re.IGNORECASE)


def offenders(build: pathlib.Path) -> list[tuple[str, str]]:
    """`(file, url)` for every subresource the build would fetch from the network."""
    found: list[tuple[str, str]] = []
    for path in sorted(build.rglob("*")):
        if path.suffix.lower() not in (".html", ".htm", ".css"):
            continue
        text = path.read_text(errors="replace")
        rel = str(path.relative_to(build))
        if path.suffix.lower() == ".css":
            found += [(rel, f"//{m}") for m in _CSS_FETCH.findall(text)]
        else:
            found += [(rel, f"//{m[1]}") for m in _HTML_FETCH.findall(text)]
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", default=str(DEFAULT_BUILD), help="the built documentation")
    args = parser.parse_args()

    build = pathlib.Path(args.build)
    if not build.is_dir():
        print(f"no build at {build}; run sphinx-build first", file=sys.stderr)
        return 2

    found = offenders(build)
    if not found:
        print(f"docs are self-contained: no network subresource in {build}")
        return 0

    print(f"{len(found)} subresource(s) would be fetched from the network:\n")
    for rel, url in found[:40]:
        print(f"  {rel}: {url}")
    if len(found) > 40:
        print(f"  ... and {len(found) - 40} more")
    print(
        "\nAn air-gapped cluster cannot reach these, and the page will render without them "
        "rather than fail visibly.\nVendor the file under docs/_static/ and override the rule "
        "(see docs/_static/local-fonts.css)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
