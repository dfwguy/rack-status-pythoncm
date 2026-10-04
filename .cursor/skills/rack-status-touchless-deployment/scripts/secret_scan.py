#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Conservative secret gate for a tree (e.g. the deployment config directory).

Prints only paths and rule names — never the matched value — and exits non-zero on any hit. Run it
over the mounted config directory before a deploy: that file is what ends up in bug reports, and
the loader already refuses to start with a secret in it, so a credential there is a hard stop.
"""
from __future__ import annotations
import pathlib, re, sys

root = pathlib.Path(sys.argv[1]).resolve()
skip = {".git", "node_modules", ".venv", "__pycache__"}
rules = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "authenticated-url": re.compile(rb"(?:https?|ssh)://[^/\s:@]+:[^/\s@]+@"),
    "high-confidence-token": re.compile(rb"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})"),
    "literal-password-assignment": re.compile(
        rb"(?im)^\s*(?:export\s+)?(?:password|passwd|pass|bmc_password|ssh_password|rack_web_auth_token)\s*=\s*['\"]?[A-Za-z0-9!@#$%^&*_.-]{6,}['\"]?\s*$"
    ),
}
bad = []
for path in root.rglob("*"):
    if not path.is_file() or any(part in skip for part in path.parts):
        continue
    try:
        data = path.read_bytes()
    except OSError:
        continue
    if b"\0" in data[:8192] or len(data) > 10 * 1024 * 1024:
        continue
    for name, rule in rules.items():
        match = rule.search(data)
        # A URL assembled from variable references embeds no credential in the artifact.
        if name == "authenticated-url" and match and b"$" in match.group(0):
            match = None
        if match:
            bad.append((path.relative_to(root), name))
for path, name in bad:
    print(f"{path}: {name}", file=sys.stderr)
raise SystemExit(1 if bad else 0)
