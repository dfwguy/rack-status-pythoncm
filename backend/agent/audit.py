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
"""Append-only JSONL audit of every agent tool call.

Every tool invocation (read or action) and every approval decision is recorded so
mutating actions taken "by the AI" are traceable to an operator/session. Best-effort:
audit failures never block a tool call.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_DIR.parent
_AUDIT_FILE = Path(
    os.environ.get("RACK_AI_TOOL_AUDIT_FILE", "")
    or str(_REPO_ROOT / "var" / "logs" / "agent_tool_audit.jsonl")
)
_lock = threading.Lock()


def record(event: str, **fields) -> None:
    """Append one audit line. ``event`` e.g. tool_call, approval, denied, error."""
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **fields,
        }
        line = json.dumps(entry, default=str)
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock, open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # audit is best-effort: it must never block a tool call
        _logger.debug("agent audit write failed for %s", event, exc_info=True)
