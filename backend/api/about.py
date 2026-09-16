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
"""`GET /api/about` — what build this is, answerable without a credential.

Exists because the documentation needed the version and the only endpoint carrying it was
`/api/app-stats`, which is gated. `/docs/` is public, so a public page was making an
authenticated call: it worked, because that view only rendered for a signed-in operator, but it
coupled a public surface to the auth system for one string. The version is worth having; the
coupling is not.

**The version comes from `app_version.app_version()`**, the same function `/api/app-stats`
reads — not a second derivation. That module is already the single answer to "what build is
this": `backend/build-info.json` first, then root `version.py` parsed (never imported) as
`<version>-dev`, then `"unknown"`. A second path to the same fact is how two screens come to
disagree about which build is running.

**Why it is safe to serve anonymously**, against the bar `web_auth.AUTH_BYPASS_ENDPOINTS` sets
("safe because", not "convenient because"):

* It reads **no request body and no query parameters** — there is no input to validate.
* It touches **no store**: not the catalog, not the identity file, not the cluster. It calls one
  pure function and returns its string.
* It reads **no credential** and sets **no cookie**.
* Every field it returns is already public knowledge on any deployment that serves the
  documentation: the product name is on the login page, and the docs are the same text anybody
  can read in the repository.

**What it deliberately does not carry**, and this is the line to hold if anyone extends it: no
cluster identity, no customer, no project, no hostnames, no node counts, no configuration, no
paths. Those are the things an unauthenticated caller must not learn, and every one of them is
available to the modules next door — which is exactly why the exclusion is written down rather
than assumed. `test_about_is_public.py` asserts the response carries nothing but these keys, so
a field added carelessly fails the build rather than shipping.

A version string does tell a caller which advisories might apply. That is a real, accepted cost:
it is inferable from the served documentation anyway, and the alternative — a deployment whose
operators cannot tell which build they are running — has caused more incidents here than
version disclosure ever has.
"""

from __future__ import annotations

import app_version
import throttle
from audit.actions import action
from flask import Blueprint, jsonify

#: Full paths, because `url_prefix=""` keeps the URL literal and greppable. The loader would
#: otherwise derive `/api/about` from the filename, which is the same thing — but this module has
#: exactly one route and spelling it out beats a convention a reader has to know.
bp = Blueprint("about", __name__, url_prefix="")

PRODUCT = "NVIS Greenlight"


@bp.route("/api/about", methods=["GET"])
@throttle.throttle(
    60,
    rationale=(
        "anonymous, but a pure function and a small JSON body -- no store read, no subprocess, "
        "no crypto. The limit is here because the route is unauthenticated and therefore a free "
        "request for anyone who can reach the port, not because serving it costs anything."
    ),
)
@action(get="about:read")
def api_about():
    """Product and build metadata. See the module docstring for what is excluded, and why."""
    return jsonify(
        {
            "ok": True,
            "product": PRODUCT,
            "version": app_version.app_version(),
        }
    )
