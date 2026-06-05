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
