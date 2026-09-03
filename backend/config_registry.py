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
"""What `greenlight.toml` is allowed to declare, and which settings a launch cannot do without.

DC-2 of `.plc/briefs/launch-configuration-brief.md`. Three consumers: the loader (DC-3) asks what a
key's TOML type is and whether it may be materialized, the preflight (DC-4) asks which settings are
required *right now*, and the example generator (DC-5) renders this file into
`greenlight.toml.example` so the documentation cannot drift from the code that reads it.

DELIBERATELY THE SAME SHAPE AS `logging_config.Setting`
-------------------------------------------------------
`logging_config.py:84-115` already has a frozen `Setting(name, default, parse)` with
`register_setting()` and a module-level dict, built so other modules register into it
(`backend/audit` does). Reusing that shape is the point — a second, differently-shaped settings
record would drift from the first, and "which registry declares this knob" becomes a question
nobody can answer. This module adds the fields this feature needs and nothing else.

**No name translation, anywhere.** A leaf key in the TOML file *is* the environment variable name
(FR-7). A mapping layer would be the natural thing to build and would be a mistake: every one of
the 238 direct `os.environ` reads in `backend/` names its variable literally, so a translation
table becomes a second place to look up every knob, and a stale entry silently stops materializing
a setting that looks declared.

WHY `secret` IS TRI-STATE AND NOT A BOOLEAN
--------------------------------------------
`None` means "derive from `logging_config.SENSITIVE_NAME_PATTERN`". The pattern is right about the
six credential-bearing keys it was written for, and wrong about at least one: `RACK_PROJECT_TOKEN`
matches on `TOKEN` but is an **NVFTP folder name** (`config_env.py:116-123`). Under NFR-3 a
false positive is not an over-redaction — it is a **refusal to boot on a legitimate setting**, so
the pattern needs an explicit override rather than a tweak. Note also what the pattern deliberately
omits: a bare `KEY` would match `RACK_SSH_KEY` and `RACK_WEB_TLS_KEY`, which hold file paths.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from logging_config import SENSITIVE_NAME_PATTERN

#: The TOML types the loader knows how to render into the string-shaped environment (FR-12..FR-15).
#: Anything else is refused at load rather than coerced, because a silent coercion of a type nobody
#: designed for is how a setting comes to mean something different in the file than in the process.
TOML_STR = "string"
TOML_BOOL = "boolean"
TOML_INT = "integer"
TOML_FLOAT = "float"
TOML_ARRAY = "array"
TOML_TYPES = (TOML_STR, TOML_BOOL, TOML_INT, TOML_FLOAT, TOML_ARRAY)


class RegistryError(Exception):
    """A malformed declaration. Raised at import, never logged and continued."""


@dataclass(frozen=True)
class Setting:
    """One declared setting.

    Frozen, like its `logging_config` sibling: a registry a caller can mutate in place is a registry
    whose contents depend on import order, and the preflight's answer would then depend on who
    asked first.
    """

    name: str
    toml_type: str = TOML_STR
    #: `True` / `False` / `None` = derive from the sensitive-name pattern. See the module docstring.
    secret: bool | None = None
    #: Whether the dashboard Settings screen (`job_env_overrides.json`) may carry this key. Where it
    #: may, Settings outranks the file — which is not a special case but the existing citizen-5
    #: ladder working as designed, since overrides sit above the process-env rung.
    ui_managed: bool = False
    #: Zero-argument predicate over the *resolved* environment. `None` means never required.
    #: Evaluated at preflight time, not at declaration time: several predicates depend on the active
    #: cluster overlay, which does not exist until the catalog bootstrap has run.
    required_when: Callable[[], bool] | None = None
    #: Optional value validator. Returns an error string, or None when the value is acceptable.
    validate: Callable[[str], str | None] | None = None
    example: str = ""
    section: str = "general"
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise RegistryError("a setting needs a name")
        if self.toml_type not in TOML_TYPES:
            raise RegistryError(
                f"{self.name} declares toml_type {self.toml_type!r}; known: {list(TOML_TYPES)}"
            )

    @property
    def is_secret(self) -> bool:
        """Resolved secrecy: the explicit flag where given, the name pattern otherwise (FR-26a)."""
        if self.secret is not None:
            return self.secret
        return bool(SENSITIVE_NAME_PATTERN.search(self.name))

    def is_required(self) -> bool:
        """Whether this launch needs it. Never raises — a predicate that throws is not a reason to
        refuse a boot, so it is treated as "not required" and the preflight logs it.

        The alternative would let an unrelated bug in one predicate take the whole deployment down,
        which is a worse failure than the one the preflight exists to prevent.
        """
        if self.required_when is None:
            return False
        try:
            return bool(self.required_when())
        except Exception:  # noqa: BLE001 - see the docstring; narrowing this would defeat it
            # Broad on purpose. A predicate can fail for reasons that have nothing to do with this
            # setting -- an unavailable catalog, a half-written overrides file -- and the correct
            # response to "we cannot tell whether this is required" is not to refuse the boot.
            # Enumerating the exception types would mean the next unanticipated one takes the
            # deployment down, which is the failure this whole feature exists to prevent.
            return False


_SETTINGS: dict[str, Setting] = {}


def register(setting: Setting) -> Setting:
    """Declare a setting. A duplicate name is an error, not a last-writer-wins overwrite.

    Silently replacing a declaration would make the registry's contents depend on import order, and
    the symptom — one of two differing declarations winning at random — is the kind of thing that
    reproduces on one machine and not another.
    """
    existing = _SETTINGS.get(setting.name)
    if existing is not None and existing != setting:
        raise RegistryError(
            f"{setting.name} is already declared differently; a setting has exactly one declaration"
        )
    _SETTINGS[setting.name] = setting
    return setting


def declared() -> dict[str, Setting]:
    """Every declared setting. A copy, so a caller cannot mutate the registry by accident."""
    return dict(_SETTINGS)


def get(name: str) -> Setting | None:
    return _SETTINGS.get(name)


def is_declared(name: str) -> bool:
    return name in _SETTINGS


def is_secret(name: str) -> bool:
    """Resolved secrecy for a name that may not be declared at all.

    An undeclared name still gets the pattern applied. That matters because the loader warns about
    undeclared keys (FR-8) and the provenance block names keys it did not materialize: refusing to
    redact something merely because nobody declared it would leak exactly the value a typo'd secret
    key holds.
    """
    setting = _SETTINGS.get(name)
    if setting is not None:
        return setting.is_secret
    return bool(SENSITIVE_NAME_PATTERN.search(name))


def required_now() -> tuple[Setting, ...]:
    """The settings this launch requires, evaluated against the environment as it stands."""
    return tuple(s for s in _SETTINGS.values() if s.is_required())


def reset_for_tests() -> None:
    _SETTINGS.clear()


# --- Declarations ------------------------------------------------------------------------------
#
# The initial required set is deliberately SMALL. Every entry here is a setting whose absence
# breaks the deployment in a way the operator cannot diagnose from the symptom; a setting with a
# sensible default does not belong, because "missing" and "defaulted" are different states and only
# the first is a fault.


def _oidc_provider_enabled() -> bool:
    """Imported lazily: `identity.oidc` must not be on this module's import graph.

    DC-3 loads before the first first-party import (DD-1), and this module is on that path, so a
    module-level import here would drag `identity/` into the pre-load phase.
    """
    from identity import oidc

    return oidc.enabled()


def _ssh_control_connection_in_use() -> bool:
    """SSH is the default transport (`transport/factory.py:36`), so this fires unless the
    deployment is in demo mode.

    The demo conjunct is decision 10 and it is not a convenience. Without it, `./start.sh` on a
    macOS laptop — which is demo mode — and every genuinely-fresh first boot at all five sites
    would be refused, because `catalog/seed.py:577` stores an empty `ssh_host` when the variable is
    unset. That would make the preflight worse than the defect it exists to prevent.
    """
    if (os.environ.get("RACK_STATUS_DEMO") or "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    mode = (os.environ.get("RACK_EXEC_MODE") or "").strip().lower()
    return mode != "local"


RACK_WEB_OIDC_ISSUER = register(
    Setting(
        name="RACK_WEB_OIDC_ISSUER",
        secret=False,
        required_when=_oidc_provider_enabled,
        example="https://greenlight.example.com",
        section="auth",
        description=(
            "This deployment's own base URL, used as the OIDC `iss` claim. Required when the OIDC "
            "provider is enabled. Cannot be derived from the request: OIDC Discovery 1.0 §4.3 "
            "requires it to be the URL discovery is served from, and a Host header is "
            "attacker-controlled. http is accepted only for localhost, which is how an operator "
            "reaches a tunnelled deployment."
        ),
    )
)

RACK_SSH_HOST = register(
    Setting(
        name="RACK_SSH_HOST",
        secret=False,
        required_when=_ssh_control_connection_in_use,
        example="10.0.0.1",
        section="connection",
        description=(
            "Head node for the SSH control connection. Required unless the deployment is in demo "
            "mode or RACK_EXEC_MODE=local."
        ),
    )
)

#: Declared for exactly one reason: to say it is NOT a secret.
#:
#: It matches `SENSITIVE_NAME_PATTERN` on `TOKEN` but is the NVFTP *folder name*
#: (`config_env.py:116-123`). Verified before adding this: `is_secret("RACK_PROJECT_TOKEN")`
#: returned `True` from the pattern alone. Under NFR-3 that is not a harmless over-redaction — it
#: is a refusal to boot on a legitimate setting, so the override is load-bearing rather than
#: cosmetic. This is the concrete case the tri-state `secret` field exists for; leaving it out
#: would have shipped the field with nothing exercising its middle state.
RACK_PROJECT_TOKEN = register(
    Setting(
        name="RACK_PROJECT_TOKEN",
        secret=False,
        example="my-project",
        section="nvftp",
        description=(
            "NVFTP project folder name. Not a credential, despite matching the sensitive-name "
            "pattern on 'TOKEN'."
        ),
    )
)

RACK_REMOTE_BASE_PATH = register(
    Setting(
        name="RACK_REMOTE_BASE_PATH",
        secret=False,
        required_when=_ssh_control_connection_in_use,
        example="/home/cmsupport/nvis-rack-status",
        section="connection",
        description=(
            "Where the tooling is deployed on the head node. Every script path and the default log "
            "directory derive from it, so a wrong value fails at first use rather than at launch."
        ),
    )
)
