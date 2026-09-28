# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""gunicorn configuration — owns TLS, so every serving path is HTTPS.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Every other setting stays on the command line, where it is visible in ``docker inspect`` and in the
process list. This file holds the TLS decisions, because they cannot be made anywhere else:

* **Which certificate.** ``certfile`` / ``keyfile`` are settled here, when gunicorn reads this file,
  by ``startup_gate.ensure_tls()``. gunicorn fixes its listener from the config *before*
  ``on_starting`` runs, so a certificate chosen in that hook — or a shell deciding from an
  environment variable — arrives too late. That ordering is how a first boot used to generate a
  certificate and then serve plain HTTP beside it. There is no plain-HTTP path.
* **Which protocol versions.** ``ssl_context`` is a hook (``cli=None``), so a minimum TLS version
  can only be set from a config file.

That matters because gunicorn 23 still defaults ``ssl_version`` to ``PROTOCOL_TLS``, the
permissive selector -- so without this hook the floor would be whatever OpenSSL and the Python
build happen to allow, which is not something to leave to chance on a customer deployment.
"""

import os
import sys

# The backend directory is on gunicorn's --pythonpath at runtime, but a config file is read
# before that is applied, so resolve it here rather than relying on ordering.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

# After the sys.path insert above, which is what makes the backend importable here.
import startup_gate

certfile, keyfile = startup_gate.ensure_tls()


def on_starting(server):  # noqa: ARG001 - gunicorn passes the arbiter; we need none of it
    """Refuse to serve without an administrator, a policy and TLS.

    Here because this hook is the narrowest place that means *starting*. The container's
    ENTRYPOINT and `cli.py serve` (which `start.sh` and `bin/run-local-auth.sh` use) all reach
    gunicorn through this config, so one hook covers every real serving path — while the tools
    that merely *import* `app` to enumerate routes or prove the tree imports keep working, since
    importing serves nothing.

    Runs in the arbiter before any worker is forked, so a refusal is a single clear message rather
    than one per worker.
    """
    startup_gate.prepare()


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
