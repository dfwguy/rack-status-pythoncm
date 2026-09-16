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
"""Force-refresh the deployed Post Run Node Report through its authenticated API."""

from __future__ import annotations

import fcntl
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_CONTAINER = "nvis-rack-status-v3"
DEFAULT_URL = "http://127.0.0.1:5056"
LOCK_FILE = "/run/lock/nvis-post-run-report-refresh.lock"


def _container_environment(container: str) -> dict[str, str]:
    output = subprocess.check_output(
        [
            "docker",
            "inspect",
            container,
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
        ],
        text=True,
        timeout=30,
    )
    environment: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            environment[name] = value
    return environment


def _request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: dict[str, str] | None = None,
    timeout: int,
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with opener.open(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} returned a non-object JSON response")
    return payload


def refresh() -> dict:
    container = os.environ.get("NVIS_CONTAINER", DEFAULT_CONTAINER).strip() or DEFAULT_CONTAINER
    base_url = os.environ.get("RACK_POST_RUN_REFRESH_URL", DEFAULT_URL).strip().rstrip("/")
    timeout = max(30, int(os.environ.get("RACK_POST_RUN_REFRESH_TIMEOUT", "180")))
    environment = _container_environment(container)

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    auth_mode = environment.get("RACK_WEB_AUTH_MODE", "").strip().lower()
    if auth_mode == "token":
        # Prefer an explicitly supplied host-side secret file.  Never copy or
        # print the in-container Compose secret.  The inspected environment is
        # retained only for rolling-upgrade compatibility.
        token_path = os.environ.get("RACK_WEB_AUTH_TOKEN_FILE", "").strip()
        token = ""
        if token_path:
            path = Path(token_path)
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("RACK_WEB_AUTH_TOKEN_FILE must be a regular non-symlink file")
            token = path.read_text(encoding="utf-8").strip()
        if not token:
            token = environment.get("RACK_WEB_AUTH_TOKEN", "")
        if not token:
            raise RuntimeError(
                f"{container} uses token auth; set host-side RACK_WEB_AUTH_TOKEN_FILE"
            )
        login = _request_json(
            opener,
            f"{base_url}/api/login",
            data={"password": token},
            timeout=30,
        )
        if login.get("ok") is False:
            raise RuntimeError(f"dashboard login failed: {login.get('error') or 'unknown error'}")

    payload = _request_json(
        opener,
        f"{base_url}/api/post-run-node-report?refresh=1",
        timeout=timeout,
    )
    if payload.get("ok") is not True:
        raise RuntimeError(f"post-run refresh failed: {payload.get('error') or 'unknown error'}")
    refresh_status = payload.get("refresh") or {}
    errors = [
        str(refresh_status.get(name) or "").strip()
        for name in ("accounting_error", "inventory_error")
    ]
    errors = [error for error in errors if error]
    if errors:
        raise RuntimeError("post-run refresh was incomplete: " + "; ".join(errors))
    return payload


def main() -> int:
    with open(LOCK_FILE, "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("post-run refresh skipped: previous refresh is still running", flush=True)
            return 0
        try:
            payload = refresh()
        except Exception as error:  # noqa: BLE001 - cron must report every failure
            now = datetime.now(timezone.utc).isoformat()
            print(f"{now} ERROR {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            return 1

    summary = payload.get("summary") or {}
    merge = (payload.get("refresh") or {}).get("merge") or {}
    now = datetime.now(timezone.utc).isoformat()
    print(
        f"{now} OK generated_at={payload.get('generated_at') or '-'} "
        f"activities={(payload.get('refresh') or {}).get('activities', '-')} "
        f"events={merge.get('after', '-')} nodes={summary.get('nodes', '-')} "
        f"failures={summary.get('attributed_failures', '-')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
