# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Unix-socket client shared by PythonCM query and scheduler providers."""
from __future__ import annotations

import json
import socket
from typing import Any

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def request(
    socket_path: str,
    service: str,
    argv: list[str],
    *,
    timeout: int,
) -> dict[str, Any]:
    body = json.dumps(
        {"service": service, "argv": argv}, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    chunks: list[bytes] = []
    size = 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(1, timeout))
        client.connect(socket_path)
        client.sendall(body)
        client.shutdown(socket.SHUT_WR)
        while True:
            chunk = client.recv(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("PythonCM broker response exceeds 64 MiB")
            chunks.append(chunk)
    response = json.loads(b"".join(chunks))
    if not isinstance(response, dict):
        raise TypeError("PythonCM broker returned non-object JSON")
    return response
