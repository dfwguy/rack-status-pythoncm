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
"""Refuses to start when a setting the deployment cannot do without is missing (DC-4).

FR-16..FR-22. Reports **every** fault in one diagnostic and then exits, rather than failing on the
first: an operator fixing five settings one restart at a time is a miserable afternoon, and each
restart of a real deployment costs more than the check saves.

WHY THIS RUNS AFTER THE CATALOG BOOTSTRAP AND NOT BEFORE EVERYTHING
--------------------------------------------------------------------
The obvious placement — first thing, before any bootstrap — is wrong, and the requirement said so
before this module existed (RG-1). Predicates resolve through `_subprocess_env()`, which consults
the active cluster's high overlay, and `_bootstrap_catalog_at_startup()` is what *creates* the
default project on a first boot. Preflight-before-catalog would therefore refuse every genuinely
fresh deployment at all five sites. It runs after the catalog and before the identity bootstrap,
which is the first consumer of a required setting.

THREE OUTCOMES, NOT TWO
-----------------------
A required setting can be present, absent, or *unknown* — a predicate can raise for reasons that
have nothing to do with its setting. The first version of the registry collapsed the third into
"not required", which is fail-open on a fail-closed control (`identity-auth-traps.mdc` trap 7): it
boots a deployment that may be missing something required, silently. So an unevaluable predicate is
reported here as its own fault class rather than resolved in either direction. It does not refuse
the boot — one unrelated bug should not take a deployment down — but it can no longer be invisible.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

import config_registry

_logger = logging.getLogger(__name__)

#: `EX_CONFIG`. Matches `bin/scripts/lib/nvswitch_creds.inc.sh:26`, which uses 78 for a refused
#: credential. Note gunicorn masks the value, so the message is what an operator actually gets.
EXIT_CONFIG = 78


@dataclass(frozen=True)
class Fault:
    """One thing wrong, phrased so the operator can act on it without reading the source."""

    name: str
    condition: str
    example: str = ""
    detail: str = ""

    def render(self) -> str:
        out = f"  {self.name} is required: {self.condition}"
        if self.detail:
            out += f"\n      {self.detail}"
        if self.example:
            out += f'\n      example:  {self.name} = "{self.example}"'
        return out


def _is_set(name: str) -> bool:
    """Present *and* non-blank.

    Whitespace counts as absent here, unlike in the loader — a required setting whose value is
    `"  "` is a configuration mistake, not a deliberate empty value, and treating it as present
    would push the failure to first use, which is the whole thing this module exists to prevent.
    """
    return bool((os.environ.get(name) or "").strip())


def collect() -> tuple[list[Fault], list[Fault]]:
    """`(faults, warnings)` — separated because only the first refuses the boot."""
    faults: list[Fault] = []
    warnings: list[Fault] = []

    for setting in config_registry.declared().values():
        result = setting.requirement()

        if result.undetermined:
            warnings.append(
                Fault(
                    name=setting.name,
                    condition="could not be determined",
                    detail=(
                        f"its required-when predicate failed ({result.error}); the setting is "
                        f"neither confirmed required nor confirmed optional"
                    ),
                )
            )
            continue

        if result.required is not True or _is_set(setting.name):
            continue

        faults.append(
            Fault(
                name=setting.name,
                condition=setting.description or "no description recorded",
                example=setting.example,
            )
        )

    faults.sort(key=lambda f: f.name)
    warnings.sort(key=lambda f: f.name)
    return faults, warnings


def run(*, exit_on_fault: bool = True) -> tuple[list[Fault], list[Fault]]:
    """Check, report everything, and refuse if anything is missing.

    `exit_on_fault=False` exists for tests only. It is a keyword argument on purpose: a positional
    boolean at a call site reads as noise, and this is the one flag that turns a fail-closed control
    off.
    """
    faults, warnings = collect()

    for warning in warnings:
        _logger.warning("config: %s %s — %s", warning.name, warning.condition, warning.detail)

    if not faults:
        if config_registry.required_now():
            _logger.info(
                "config: %d required setting(s) present", len(config_registry.required_now())
            )
        return faults, warnings

    # Every fault at once, to stderr as well as the log: this can run before a deployment's log
    # sink is somewhere the operator will look, and the whole point is that they see all of it.
    lines = [
        "rackstatus: refusing to start — required configuration is missing.",
        "",
        *[f.render() for f in faults],
        "",
        "Set these in config/rackstatus.toml, or in the environment (the environment wins).",
    ]
    report = "\n".join(lines)
    _logger.critical("config: refusing to start; %d required setting(s) missing", len(faults))
    print(report, file=sys.stderr, flush=True)

    if exit_on_fault:
        raise SystemExit(EXIT_CONFIG)
    return faults, warnings
