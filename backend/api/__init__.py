# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Per-domain API modules, discovered and registered automatically.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Adding an endpoint should not mean editing a file someone else is editing. ``app.py`` had grown
to twenty thousand lines and still holds 135 of the app's 199 routes; every feature touched it, so
two people working in parallel collided by construction -- which is exactly how a whole feature
ended up living uncommitted on a customer deployment.

So: drop a module in this package. Nothing else is edited -- not ``app.py``, not this file.

A module qualifies by exposing either of:

``bp``
    A ``flask.Blueprint``. If it declares its own ``url_prefix`` that is honoured; otherwise the
    prefix is derived from the filename, so ``rack_locks.py`` serves ``/api/rack-locks``.
    Declare one explicitly when the URL cannot be spelled as a Python module name (``node-report``
    has a hyphen) or when the module owns several prefixes.

    ``url_prefix=""`` means "my routes already carry their full paths". That is the mode modules
    migrated out of ``app.py`` use: the decorators keep the exact strings they always had, so a
    move cannot silently change a URL through prefix arithmetic. New modules should prefer the
    filename convention instead.

``install(app)``
    An escape hatch for things Flask routing cannot express. The terminal needs it: ``/ws/terminal``
    is WSGI middleware wrapping ``app.wsgi_app``, below Flask's router entirely.

Deliberately strict, in both directions:

* imports are **not** wrapped in ``try``. A module that fails to import takes startup down and
  names itself. We spent an afternoon on a feature that silently did nothing because a lazy import
  inside a ``try`` swallowed a missing module -- silence is the worst possible failure here.
* a module exposing neither ``bp`` nor ``install`` is a mistake, not something to skip quietly.
  Shared helpers inside the package belong in an underscore-prefixed module.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from flask import Flask

_logger = logging.getLogger("rack_web.api")


def module_names() -> list[str]:
    """Every registrable module in this package, in a stable order.

    Sorted so registration order does not depend on the filesystem, and underscore-prefixed
    modules are skipped so the package can hold shared helpers of its own.
    """
    return sorted(
        info.name
        for info in pkgutil.iter_modules(__path__)
        if not info.name.startswith("_")
    )


def default_prefix(module_name: str) -> str:
    """URL prefix implied by a filename: ``rack_locks`` -> ``/api/rack-locks``."""
    return f"/api/{module_name.replace('_', '-')}"


def register(app: Flask) -> list[str]:
    """Register every API module on *app*. Returns what was registered, for logging and tests."""
    registered: list[str] = []
    for name in module_names():
        module = importlib.import_module(f"{__name__}.{name}")
        bp = getattr(module, "bp", None)
        install = getattr(module, "install", None)

        if bp is None and install is None:
            raise RuntimeError(
                f"backend/api/{name}.py exposes neither 'bp' nor 'install(app)'. "
                "Give it a Blueprint, or rename it with a leading underscore if it is a helper."
            )

        if bp is not None:
            # A blueprint that names its own prefix keeps it; otherwise use the filename.
            if bp.url_prefix is None:
                app.register_blueprint(bp, url_prefix=default_prefix(name))
            else:
                app.register_blueprint(bp)
            # "" is a real answer ("my routes carry full paths"), not a missing one.
            where = default_prefix(name) if bp.url_prefix is None else (bp.url_prefix or "full paths")
            registered.append(f"{name} -> {where}")

        if install is not None:
            install(app)
            registered.append(f"{name} -> install()")

    _logger.info("api: registered %d module(s): %s", len(registered), ", ".join(registered))
    return registered
