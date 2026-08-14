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
"""One-shot PAM check for NVIS Rack Status: JSON on stdin, one JSON line on stdout.

Used when the Flask process cannot load ctypes (e.g. venv Python from a different
build than its stdlib). Run with RACK_WEB_PAM_HELPER_PYTHON (default /usr/bin/python3).
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

_logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
_rs = str(ROOT)
if _rs not in sys.path:
    sys.path.insert(0, _rs)


def main() -> None:
    err: str | None = None
    ok = False
    try:
        raw = sys.stdin.read() or "{}"
        d = json.loads(raw)
        user = d.get("user")
        password = d.get("password")
        service = d.get("service") or "login"
        if not isinstance(user, str) or not isinstance(password, str):
            err = "expected user and password strings"
        elif not isinstance(service, str):
            err = "service must be a string"
        else:
            from rack_web.pam_ctypes import authenticate

            ok = bool(authenticate(user, password, service=service))
    except Exception as e:
        _logger.debug("main: ignoring error", exc_info=True)
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
    if err:
        print(json.dumps({"ok": False, "error": err}))
    else:
        print(json.dumps({"ok": ok}))


if __name__ == "__main__":
    main()
