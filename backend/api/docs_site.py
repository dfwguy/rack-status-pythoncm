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

import throttle
from audit.actions import action
from flask import Blueprint, Response, send_from_directory
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
        return send_from_directory(DOCS_DIR, filename)
    # `/docs/reference/identity/` and `/docs/reference/identity` both mean its index.
    index = DOCS_DIR / filename / "index.html"
    if index.is_file():
        return send_from_directory(DOCS_DIR, f"{filename.rstrip('/')}/index.html")

    _logger.info("docs: no such page %r", filename)
    return Response(
        f"No such documentation page: {filename}\n\nStart at /docs/\n",
        status=404,
        mimetype="text/plain",
    )
