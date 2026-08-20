# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""gunicorn configuration — exists only to enforce the TLS floor.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Every other setting stays on the ENTRYPOINT command line, where it is visible in
``docker inspect`` and in the process list. This file holds the one thing that *cannot* be
expressed as a flag: ``ssl_context`` is a hook (``cli=None``), so a minimum TLS version can only
be set from a config file.

That matters because gunicorn 23 still defaults ``ssl_version`` to ``PROTOCOL_TLS``, the
permissive selector -- so without this hook the floor would be whatever OpenSSL and the Python
build happen to allow, which is not something to leave to chance on a customer deployment.
"""

import os
import sys

# The backend directory is on gunicorn's --pythonpath at runtime, but a config file is read
# before that is applied, so resolve it here rather than relying on ordering.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))


def ssl_context(conf, default_ssl_context_factory):
    """Apply the shared TLS policy to gunicorn's own context.

    ``default_ssl_context_factory()`` returns a context already loaded with the certificate, key
    and ciphers from the CLI flags; we only tighten the negotiable versions. Sharing
    ``tls_server.harden`` with the Werkzeug path is the point -- a floor that differs between
    "how it runs in production" and "how it runs locally" is a floor nobody can verify.
    """
    from tls_server import describe, harden

    context = harden(default_ssl_context_factory())
    print(f"[gunicorn] TLS policy: {describe(context)}", file=sys.stderr)
    return context
