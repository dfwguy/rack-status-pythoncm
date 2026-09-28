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
"""Container health: is this process answering HTTPS? Exit 0 if so, 1 if not.

Author: Rafael M. Koike & Claude

The Dockerfile's HEALTHCHECK runs this inside the container. It asks the public `/api/about` over
HTTPS on loopback, and any answer below 500 counts: a 4xx still proves the listener is up, speaking
TLS and routing into the app, which is the question. Plain HTTP, a refused connection, a TLS
failure or a 5xx is unhealthy.

The certificate is deliberately not verified — it may be the self-signed one generated on first
boot. `bin/verify-deploy.sh` verifies the chain the way a browser will; this only proves liveness.
Standard library only, since it runs in the image with nothing else on the path.
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib.error
import urllib.request


def probe(port: str, timeout: float = 8.0) -> bool:
    url = f"https://127.0.0.1:{port}/api/about"
    context = ssl._create_unverified_context()  # noqa: S323 - liveness only; see module docstring
    try:
        with urllib.request.urlopen(url, context=context, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    return 0 if probe(os.environ.get("PORT") or "5000") else 1


if __name__ == "__main__":
    raise SystemExit(main())
