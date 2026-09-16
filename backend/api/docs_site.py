# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Serve the Sphinx documentation site at ``/docs/``.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Served by the app rather than published separately so **the documentation a deployment shows is
always the documentation for the code running there**. An operator on a customer cluster reaches
Greenlight through an SSH tunnel and often cannot reach a docs site on the internet at all, which
is the same constraint that made the Kaizen CSS vendored.

Built by ``sphinx-build -W --keep-going -b html docs static/docs`` — in CI, and into the image by
the Dockerfile. ``docs/README.md`` has the local command.

Three things worth knowing:

* **It declares an action.** The authorization gate refuses any route that declares none, so
  without ``@action("docs:read")`` the docs would be a 403 for everybody, administrators
  included. ``docs:read`` is a read verb, so an existing read-only role
  (``["*:read", "*:list"]``) already covers it rather than needing a new grant.
* **A missing build is a 503 with instructions**, not a 404 and not a crash. ``static/docs`` is a
  build artifact and gitignored, so a developer tree and a CI checkout genuinely differ here —
  the same divergence that makes the SPA shell answer 503 (see
  ``api/_common.serve_spa_asset``). The audience is a person in a browser, so the body is text
  they can act on.
* **Directory URLs get their ``index.html``.** Sphinx writes ``reference/identity/index.html``
  and its own links point at the directory, so resolving that here is what makes in-page
  navigation work.
"""

from __future__ import annotations

import logging
import pathlib
import re
import secrets

import throttle
from audit.actions import action
from flask import Blueprint, Response, g, send_from_directory
from paths import DOCS_DIR

_logger = logging.getLogger(__name__)

#: An explicit prefix, not the filename convention: this module serves `/docs`, and the loader
#: would otherwise derive `/api/docs-site` from its name.
bp = Blueprint("docs_site", __name__, url_prefix="/docs")

_NOT_BUILT = (
    "The documentation is not built into this deployment.\n\n"
    "Build it with:\n"
    "  pip install -r docs/requirements.txt\n"
    "  sphinx-build -W --keep-going -b html docs static/docs\n\n"
    "See docs/README.md. CI builds it into the image, so a released build has it.\n"
)

#: An opening `<script>` tag that is **not** loading a file.
#:
#: Sphinx emits exactly two per page — the theme's light/dark restore, which reads `localStorage`
#: before first paint, and `DOCUMENTATION_OPTIONS.pagename`. Under `script-src 'self'` both are
#: refused, which is why the documentation reported "invalid theme mode: undefined" and reset to
#: auto instead of honouring a stored choice.
#:
#: Matching on the *absence* of `src=` is the whole point: an external script needs no nonce (it is
#: already covered by `'self'`), and noncing one indiscriminately would grant the attribute to
#: whatever a future theme decides to pull in. The negative lookahead keeps this to script tags
#: whose body ships in the page we just built.
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)([^>]*)>", re.IGNORECASE)


def _with_nonce(html: bytes, nonce: str) -> bytes:
    """Add `nonce="…"` to every inline `<script>` in *html*."""
    attr = f' nonce="{nonce}"'.encode()
    return _INLINE_SCRIPT.sub(lambda m: b"<script" + m.group(1) + attr + b">", html)


def _html_with_nonce(path: pathlib.Path) -> Response:
    """One documentation page, its inline scripts authorised by a per-response nonce.

    Read and rewritten rather than `send_from_directory`, which is a real cost and a deliberate
    one: the alternative to a nonce is `'unsafe-inline'`, and that would apply to the whole origin
    for anything served under `/docs`. A page is tens of kilobytes and this is a read plus one
    regex substitution.

    **`no-store` is not optional.** The nonce is embedded in this body and named in this
    response's CSP header; a cached copy replayed against a later response's policy is a page
    whose scripts are all refused. Caching the HTML would make the theme flicker come back
    intermittently, which is the hardest version of this bug to find. The subresources — every
    one of them a hashed or versioned static file — are untouched and still cacheable.
    """
    # Containment, stated rather than inherited.
    #
    # `send_from_directory` — which still serves every non-HTML file — refuses a path that escapes
    # its root. This branch reads the file itself, so it has to say so itself. Werkzeug does
    # normalise `..` out of the URL before routing, and every traversal shape tried here already
    # returns 404 because of it; that is a property of the framework's URL handling, not of this
    # function, and a reader cannot see it from here. One `resolve()` makes the guarantee local.
    resolved = path.resolve()
    if not resolved.is_relative_to(DOCS_DIR.resolve()):
        _logger.warning("docs: refusing a path outside the docs tree: %r", str(path))
        return Response("No such documentation page\n", status=404, mimetype="text/plain")

    nonce = secrets.token_urlsafe(16)
    # Read by the `after_request` hook that owns every security header, so the policy is still
    # built in one place. Setting the header here instead would have it overwritten by that hook.
    g.docs_script_nonce = nonce
    resp = Response(_with_nonce(path.read_bytes(), nonce), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/", defaults={"filename": "index.html"})
@bp.route("/<path:filename>")
@throttle.throttle(
    300,
    rationale=(
        "static files, anonymous. Generous on purpose: index.html alone references 30 "
        "subresources (measured), so a first visit is ~30 requests and browsing a few pages is "
        "a few hundred -- a tight limit would break reading the documentation rather than "
        "protect anything. Each request is a sendfile of a build artifact, not compute, so the "
        "limit is here to bound an anonymous flood, not to ration real use."
    ),
)
@action(get="docs:read")
def serve_docs(filename: str):
    """Serve one file from the built site."""
    if not DOCS_DIR.is_dir():
        return Response(_NOT_BUILT, status=503, mimetype="text/plain")

    target = DOCS_DIR / filename
    if target.is_file():
        # Only the pages are rewritten. `send_from_directory` still serves the CSS, the scripts,
        # the fonts and the images -- it handles ranges, conditional requests and caching, none of
        # which a hand-rolled read would, and none of which an HTML page with a per-response nonce
        # may have anyway.
        if target.suffix.lower() in (".html", ".htm"):
            return _html_with_nonce(target)
        return send_from_directory(DOCS_DIR, filename)
    # `/docs/reference/identity/` and `/docs/reference/identity` both mean its index.
    index = DOCS_DIR / filename / "index.html"
    if index.is_file():
        return _html_with_nonce(index)

    _logger.info("docs: no such page %r", filename)
    return Response(
        f"No such documentation page: {filename}\n\nStart at /docs/\n",
        status=404,
        mimetype="text/plain",
    )
