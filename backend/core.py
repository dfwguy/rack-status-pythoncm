"""Helpers shared by the Flask app and every API module under ``backend/api/``.

Author: Rafael M. Koike & Cursor Agent ("Ada")

**This module must never import Flask, and must never import ``app``.** That is the whole
point of it. ``app.py`` imports the API modules in order to register them, so an API module
that reached back into ``app`` for a helper would form an import cycle. Putting the shared
helpers here means the dependency only ever points one way:

    app.py  ->  api/<domain>.py  ->  core.py

The cycle is not hypothetical: ``workflow_engine.py`` and ``bcm_query.py`` already work around
it today with ``import app`` *inside* function bodies, which defers the problem to call time
rather than solving it. Anything they need should migrate here instead.

Scope: small, dependency-light utilities with no request context and no application state.
Configuration resolution (``_eget`` and the job-env override store) is a larger subsystem and
still lives in ``app.py``; it belongs here eventually, but it carries a file-locked cache and a
settings registry that deserve their own move.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

import host_exec


def truthy_env(name: str, default: bool = False) -> bool:
    """Read an environment variable as a boolean.

    Accepts the spellings operators actually use in ``rack-config.env`` and compose files.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    s = raw.strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def truthy_arg(val: object, *, default: bool = False) -> bool:
    """Query-string / JSON-body boolean. Absent or blank -> *default*."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if not s:
        return default
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def rpath(p: object) -> PurePosixPath:
    """The single path resolver: canonicalise *p* against the execution host.

    Returns a :class:`PurePosixPath` deliberately. The path names a file on the execution host,
    which is not necessarily this machine, so local filesystem calls on it are a category error --
    and an inert path type makes that a loud ``AttributeError`` at the call site instead of a
    silently wrong answer. Ask the connection instead: ``host_exec.is_file`` / ``stat`` /
    ``read_text``.
    """
    return PurePosixPath(host_exec.resolve(p))


#: A Slurm job id as ``scancel``/``sacct`` will accept it, optionally an array task.
#:
#: Guards every place a job id reaches a command line. Shared because both the app and the
#: history API validate ids before use, and two copies of a security-relevant pattern is one
#: too many.
SLURM_JOB_ID_RE = re.compile(r"^[1-9][0-9]*(_[0-9]+)?$")


def history_enabled() -> bool:
    """True when the SQLite run-history store is importable and not disabled by env.

    Lives here rather than with the history API because ``app.py`` gates six separate code paths
    on it -- an availability predicate is configuration, not an endpoint.
    """
    try:
        import results_history
    except ImportError:
        return False
    return results_history is not None and not truthy_env("RACK_RESULTS_HISTORY_DISABLED")
