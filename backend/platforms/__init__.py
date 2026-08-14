# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Platform provider registry.

Select a provider with ``RACK_PLATFORM`` (default ``bcm``).  Call
``get_platform()`` to obtain the singleton instance.
"""
from __future__ import annotations

import os
import threading

from platforms.base import PlatformProvider

_instance: PlatformProvider | None = None
_lock = threading.Lock()


def get_platform() -> PlatformProvider:
    """Return the active :class:`PlatformProvider` (created once per process)."""
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is not None:
            return _instance
        name = (os.environ.get("RACK_PLATFORM") or "bcm").strip().lower()
        if name == "maas":
            from platforms.maas import MAASProvider
            _instance = MAASProvider()
        else:
            from platforms.bcm import BCMProvider
            _instance = BCMProvider()
    return _instance
