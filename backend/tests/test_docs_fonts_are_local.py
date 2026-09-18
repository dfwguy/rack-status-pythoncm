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
"""The documentation's fonts come from this deployment, not from the NVIDIA CDN.

`nvidia-sphinx-theme` points all twelve of its faces at `images.nvidia.com`, which fails twice
over: the app's CSP is `font-src 'self'` so a browser refuses every one and logs a violation, and
an air-gapped cluster — where these docs matter most — cannot reach the CDN at all. The page then
renders in a fallback font with nothing but console noise to say why.

`docs/_static/local-fonts.css` re-declares those faces against vendored files. These tests check
the two things that make it work and are easy to undo by accident: that it is loaded **after** the
theme's stylesheet (for `@font-face`, the last declaration of a family/weight/style wins, and the
shadowed one is never fetched), and that every file it names exists.

Deliberately reads the *tracked* sources rather than the built site: `static/docs` is a build
artifact absent from a CI checkout (`local-vs-ci-tree.mdc` §1), so a test against the output would
simply not run where it is needed.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONF = REPO_ROOT / "docs" / "conf.py"
FONT_CSS = REPO_ROOT / "docs" / "_static" / "local-fonts.css"


def _css_files() -> list[str]:
    """The `html_css_files` list, in order."""
    match = re.search(r"html_css_files\s*=\s*\[([^\]]*)\]", CONF.read_text(), re.DOTALL)
    assert match, "docs/conf.py no longer declares html_css_files"
    return re.findall(r"[\"']([^\"']+)[\"']", match.group(1))


def test_the_override_is_loaded_and_ordered_after_the_theme():
    """Sphinx appends `html_css_files` after the theme's own stylesheet, so being in the list is
    what puts these rules last. Being *first within the list* keeps them ahead of `custom.css`,
    which is where a future author would reasonably put a font tweak of their own."""
    files = _css_files()
    assert "local-fonts.css" in files, (
        "docs/conf.py stopped loading local-fonts.css, so the documentation is back to fetching "
        "twelve font faces from images.nvidia.com — refused by the CSP, and unreachable from an "
        "air-gapped cluster"
    )
    assert files.index("local-fonts.css") < files.index("custom.css")


def test_every_font_it_names_is_vendored():
    """A face whose file is missing is worse than the CDN reference it replaced: the browser has
    nothing to fall back *to* within the family, and the 404 is silent."""
    css = FONT_CSS.read_text()
    referenced = re.findall(r"url\(([^)]+)\)", css)
    assert referenced, "local-fonts.css declares no font files"
    missing = [
        ref for ref in referenced if not (FONT_CSS.parent / ref.strip("\"'")).is_file()
    ]
    assert not missing, f"local-fonts.css names files that are not vendored: {missing}"


def test_it_declares_no_remote_source():
    """The whole point. A single absolute URL here reintroduces the defect for that weight only,
    which is the hardest version to notice — most of the page looks right."""
    css = FONT_CSS.read_text()
    remote = re.findall(r"url\(\s*(?:https?:)?//[^)]+\)", css)
    assert not remote, f"local-fonts.css fetches a font from the network: {remote}"


def test_the_build_strips_the_theme_s_own_network_faces():
    """Overriding is not sufficient on its own, and this is the wire that makes up the difference.

    Two of the theme's rules are *variable* faces spanning `font-weight: 100 1000`. A static
    override at 400 does not shadow a range, so any weight outside the four we declare would still
    be fetched from the CDN — on an air-gapped cluster, a hang and then a fallback font.
    `_strip_remote_fonts` removes the remote rules at build time so there is no cascade subtlety
    to get wrong.

    Asserting the *connection*, not just the function: an unregistered hook raises nothing and
    changes nothing visible on a connected laptop. Same reason `test_config_materialize_hook.py`
    exists.
    """
    conf = CONF.read_text()
    assert "def _strip_remote_fonts" in conf
    assert 'app.connect("build-finished", _strip_remote_fonts)' in conf, (
        "conf.py defines the font stripper but no longer connects it, so the theme's network "
        "@font-face rules survive into the build"
    )


def test_it_covers_both_families_the_theme_declares():
    """`NVIDIA` and `RobotoMono`, the two families in the theme. Leaving one out means half the
    page keeps asking the CDN — the sans faces are obvious when they fail, the mono ones are only
    visible inside a code block."""
    css = FONT_CSS.read_text()
    families = set(re.findall(r"font-family:\s*([A-Za-z][A-Za-z0-9-]*)", css))
    assert {"NVIDIA", "RobotoMono"} <= families, (
        f"local-fonts.css covers {sorted(families)}; the theme declares NVIDIA and RobotoMono"
    )
