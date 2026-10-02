# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Sphinx configuration for the NVIS Greenlight documentation.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Ported from nvis-software-factory, which established the house style: MyST for content,
nvidia_sphinx_theme for chrome, sphinx-external-toc so navigation lives in one reviewable file
rather than scattered across toctree directives, and a per-page Diataxis declaration that is
both rendered and CI-checked.

Two things are specific to this repo:

* **The version comes from ``version.py``**, parsed rather than imported. That file is the single
  place the version actually lives (the ci-toolkit release job greps it), and importing it would
  pull in the application. ``backend/app_version.py`` makes the same choice for the same reason.
* **The legacy flat docs are excluded.** ``external_toc_exclude_missing = False`` makes a page
  that exists but appears in no toc a *build failure*, which is the check that stops a site
  accumulating pages reachable only by search. This repo still has ~44 hand-written
  ``SCREAMING_CASE.md`` files that predate the site. They were originally all at ``docs/`` root;
  they have since been filed into topic subfolders (``clusters/``, ``networking/``, ``auth/``,
  ``releases/``, ``handovers/``, ``integrations/``, ``dsx/``, …), so both ``[A-Z]*.md`` and
  ``**/[A-Z]*.md`` exclude them until they are migrated. The boundary is deliberate and documented
  in ``README.md``: anything in ``index.md`` or one of the four mode directories is part of the
  site; a ``SCREAMING_CASE.md`` file — at root or in a topic subfolder — is not yet.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# -- Project information -----------------------------------------------------

project = "NVIS Greenlight"
copyright = "2026, NVIDIA Corporation"
author = "NVIDIA Corporation"
root_doc = "index"


def _version() -> str:
    """The product version, read out of ``version.py`` without importing it.

    Parsed, not imported, for the reason ``backend/app_version.py`` documents: importing pulls
    in the application, and a docs build should not need a working app. A missing or unreadable
    file yields a placeholder rather than failing the build — a docs site that will not build
    because it cannot find a version number is a worse outcome than one labelled "dev".
    """
    try:
        text = (REPO_ROOT / "version.py").read_text(encoding="utf-8")
    except OSError:
        return "dev"
    match = re.search(r"""^VERSION\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    return match.group(1) if match else "dev"


def _release() -> str:
    """Version plus the commit, when git can tell us.

    A reader who found a discrepancy needs to be able to say which build they were reading.
    Without this the only answer available to them is "the one on the OVA last Tuesday".
    """
    version_ = _version()
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return version_
    return f"{version_} ({sha})" if sha else version_


version = _version()
release = _release()

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx_external_toc",
    "sphinx_design",
    "sphinx_copybutton",
    "sphinxcontrib.mermaid",
]

external_toc_path = "_toc.yml"
# False: a page that exists but is in no toc is unreachable except by search, which is how a docs
# site accumulates orphans. This makes that a build failure.
external_toc_exclude_missing = False

templates_path = ["_templates"]
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    "README.md",
    # The pre-site flat docs. See the module docstring: these are excluded rather than listed,
    # so adding a page to the site means putting it in a mode directory and in `_toc.yml`.
    # `*` does not cross `/`, so the second pattern keeps the same SCREAMING_CASE.md files
    # excluded after they were filed into topic subfolders (clusters/, networking/, auth/, …).
    "[A-Z]*.md",
    "**/[A-Z]*.md",
    "screenshots",
    # Agent-authored plan/spec artifacts. Internal working notes, not reader-facing pages.
    "superpowers",
]

# A broken reference is a 404 for a reader, and `-W` turns this into a build failure.
nitpicky = True

# -- MyST --------------------------------------------------------------------

source_suffix = {".md": "markdown"}

myst_heading_anchors = 4
myst_enable_extensions = [
    "colon_fence",      # ::: directives, which nest more legibly than backtick fences
    "deflist",          # the natural shape for a field or setting reference
    "fieldlist",        # per-page Diataxis frontmatter renders as a field list
    "linkify",          # bare URLs in prose become links
    "substitution",
    "tasklist",
]

myst_substitutions = {
    "version": version,
    "release": release,
}

# -- HTML output -------------------------------------------------------------

html_theme = "nvidia_sphinx_theme"
html_title = "NVIS Greenlight"
html_static_path = ["_static"]
# `local-fonts.css` first: it re-declares the theme's twelve `@font-face` rules against files in
# `_static/fonts/` instead of `images.nvidia.com`. Without it a browser refuses all twelve under
# the app's `font-src 'self'` policy, and an air-gapped cluster could never have fetched them
# anyway — see the file's own header.
html_css_files = ["local-fonts.css", "custom.css"]

html_theme_options = {
    # Operators reach this over an SSH tunnel on a customer cluster, not from the internet.
    "public_docs_features": False,
    "show_toc_level": 2,
}

html_show_sourcelink = False
html_show_sphinx = False

# Served from /docs by the same Flask app that serves the API, so links must be relative.
html_baseurl = os.environ.get("NVIS_DOCS_BASEURL", "")

# -- Extension configuration -------------------------------------------------

copybutton_prompt_text = r">>> |\$ |# "
copybutton_prompt_is_regexp = True
copybutton_exclude = ".linenos, .gp, .go"
copybutton_line_continuation_character = "\\"

# -- Diataxis page type ------------------------------------------------------

#: Frontmatter value -> what the reader is told the page is for. The second half of each label is
#: the part that does the work: someone who arrived from a search engine needs to know whether
#: this page will teach them or get them unblocked, and those are different needs.
DIATAXIS_LABELS = {
    "tutorial": "Tutorial — a lesson. Follow it start to finish.",
    "how-to": "How-to guide — steps to a goal. Assumes you know the basics.",
    "reference": "Reference — a description of the machinery. Look things up here.",
    "explanation": "Explanation — background and reasoning. No instructions.",
}


def _diataxis_banner(app, docname, source):
    """Render each page's ``diataxis`` frontmatter as a visible banner.

    A page's mode is already implied by the section it sits in, which works for a reader browsing
    the sidebar and not at all for the far more common one who arrived from a search engine
    directly onto a page. Diataxis only helps if the reader can tell what kind of page they are
    on before they invest in reading it, so the classification is stated rather than implied.

    Sourced from the frontmatter rather than the file path so that the docs rule, the CI check and
    the rendered page are all reading one declaration.
    """
    text = source[0]
    if not text.startswith("---"):
        return

    end = text.find("\n---", 3)
    if end == -1:
        return
    # Read the key out of the raw frontmatter rather than from ``env.metadata``: this event fires
    # before the parser runs, so the metadata for this page does not exist yet.
    body_start = text.index("\n", end + 1) + 1
    match = re.search(r"^diataxis:\s*(\S+)\s*$", text[:body_start], re.MULTILINE)
    label = DIATAXIS_LABELS.get(match.group(1)) if match else None
    if not label:
        return

    mode = match.group(1)
    banner = f"\n:::{{div}} nvis-diataxis nvis-diataxis-{mode}\n{label}\n:::\n"

    # Below the page title, not above it. Above the H1 the banner reads as a site-wide element
    # and pushes the title -- the thing the reader is looking for -- down the page.
    title = re.search(r"^# .+$", text[body_start:], re.MULTILINE)
    at = body_start + title.end() + 1 if title else body_start
    source[0] = text[:at] + banner + text[at:]


#: An `@font-face` block that fetches from the network.
_REMOTE_FACE = re.compile(r"@font-face\s*\{[^}]*?//[^}]*?\}", re.IGNORECASE)


def _strip_remote_fonts(app, exception):
    """Delete the theme's network `@font-face` rules from the built stylesheet.

    `nvidia-sphinx-theme` declares twelve faces against `images.nvidia.com`. Operators read these
    pages over an SSH tunnel to an air-gapped cluster, which cannot reach it: the request hangs,
    then the page renders in a fallback font, and the only evidence is a console nobody is
    watching. `_static/local-fonts.css` supplies the same faces from files we ship.

    **Overriding alone is not enough, which is why this exists.** Two of the theme's rules are
    *variable* faces spanning `font-weight: 100 1000`; a static override at 400 does not shadow a
    range, so any weight outside the four we declare would still be fetched from the CDN. Removing
    the remote rules outright means there is no cascade subtlety to get wrong — and it lets
    `bin/check-docs-offline.py` verify the result by looking for network URLs, rather than having
    to model CSS font matching to decide whether a URL is reachable in practice.

    Same treatment the vendored KUI stylesheet gets (`frontend/src/styles/kaizen/README.md`),
    applied at build time because this stylesheet arrives from a pip package rather than the tree.
    """
    if exception is not None or app.builder.name != "html":
        return
    css = Path(app.outdir) / "_static" / "styles" / "nvidia-sphinx-theme.css"
    if not css.is_file():
        return
    before = css.read_text(errors="replace")
    after = _REMOTE_FACE.sub("", before)
    if after != before:
        css.write_text(after)
        removed = len(_REMOTE_FACE.findall(before))
        print(f"[local-fonts] removed {removed} network @font-face rule(s) from the theme CSS")


def setup(app):
    # source-read rather than a template override: the banner is content, it should appear in
    # any builder's output, and overriding the theme's page template to inject it would couple
    # this site to pydata's block structure.
    app.connect("source-read", _diataxis_banner)
    # build-finished, because the theme's stylesheet is copied into the output by the theme
    # itself: there is nothing to rewrite until it is there.
    app.connect("build-finished", _strip_remote_fonts)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
