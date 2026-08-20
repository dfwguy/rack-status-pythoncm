# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The TLS policy for whichever server is running.

Author: Rafael M. Koike & Cursor Agent ("Ada")

One policy, two servers: gunicorn in the container (via the ``ssl_context`` hook in
``gunicorn.conf.py``) and Werkzeug for a local run (via ``app.run(ssl_context=...)``). Both call
:func:`harden`, so the floor cannot differ between how a deployment runs and how it is tested.

**Why this module exists rather than trusting the defaults.** Adding TLS adds a negotiation, and a
negotiation that can be talked down to TLS 1.0/1.1 is a downgrade surface we did not have while
serving plain HTTP behind a tunnel. The two servers disagree about the default, too:

* Python's ``SSLContext`` floor is TLS 1.2 on 3.12 -- but that is a *language version* default,
  not a promise, and it has moved twice.
* gunicorn 23's ``ssl_version`` still defaults to ``PROTOCOL_TLS`` (the permissive selector).

So the minimum is set explicitly, in one place, and asserted by tests. TLS 1.3 is available with
``RACK_WEB_TLS_MIN_VERSION=1.3`` for a site that can require it -- worth checking against the
oldest browser your operators actually use before turning it on.

Ciphers are left to OpenSSL's own defaults deliberately. A hand-pinned suite list is a
maintenance burden that ages badly: it silently blocks browsers as they move, and OpenSSL's
default already excludes the suites whose weaknesses motivate pinning.
"""

from __future__ import annotations

import logging
import os
import ssl

_logger = logging.getLogger(__name__)

#: The floor. Nothing below TLS 1.2 negotiates, on either server.
DEFAULT_MIN_TLS = ssl.TLSVersion.TLSv1_2

_MIN_BY_NAME = {
    "1.2": ssl.TLSVersion.TLSv1_2,
    "tlsv1.2": ssl.TLSVersion.TLSv1_2,
    "1.3": ssl.TLSVersion.TLSv1_3,
    "tlsv1.3": ssl.TLSVersion.TLSv1_3,
}


def configured_min_version(raw: str | None = None) -> ssl.TLSVersion:
    """Resolve ``RACK_WEB_TLS_MIN_VERSION``, falling back to :data:`DEFAULT_MIN_TLS`.

    An unrecognised value is a configuration mistake, and the safe reading of a mistake here is
    the stricter one -- so it warns and keeps the floor rather than quietly widening it.
    """
    name = (raw if raw is not None else os.environ.get("RACK_WEB_TLS_MIN_VERSION", "")).strip().lower()
    if not name:
        return DEFAULT_MIN_TLS
    chosen = _MIN_BY_NAME.get(name)
    if chosen is None:
        _logger.warning(
            "RACK_WEB_TLS_MIN_VERSION=%r is not one of 1.2/1.3; keeping the TLS 1.2 floor", name
        )
        return DEFAULT_MIN_TLS
    return chosen


def harden(context: ssl.SSLContext, *, min_version: ssl.TLSVersion | None = None) -> ssl.SSLContext:
    """Apply the policy to a context another layer built (gunicorn's factory, or ours).

    Kept separate from :func:`build_ssl_context` because gunicorn hands us a context it has
    already loaded certificates into; the policy must be applicable to that object rather than
    only to one we construct.
    """
    context.minimum_version = min_version or configured_min_version()
    # TLS-level compression is the CRIME oracle and buys nothing for JSON over a fast link.
    context.options |= ssl.OP_NO_COMPRESSION
    # Client-initiated renegotiation is a DoS lever on TLS 1.2 and is gone in 1.3. Guarded
    # because the flag is OpenSSL-version dependent and its absence must not break startup.
    no_reneg = getattr(ssl, "OP_NO_RENEGOTIATION", None)
    if no_reneg is not None:
        context.options |= no_reneg
    return context


def build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """A server context for *certfile*/*keyfile*, hardened.

    Raises whatever ``load_cert_chain`` raises: a deployment that configured TLS and cannot load
    its own certificate must fail loudly at startup, not fall back to plaintext while the
    operator believes the dashboard is encrypted.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    return harden(context)


def describe(context: ssl.SSLContext) -> str:
    """One-line summary for the startup log, so the floor is visible in the journal."""
    return f"min={context.minimum_version.name} max={context.maximum_version.name}"
