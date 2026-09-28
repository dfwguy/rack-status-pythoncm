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
"""Reads `config/rackstatus.toml` at launch and materializes it into the process environment.

DC-3 of `.plc/briefs/launch-configuration-brief.md`. The file is the **baseline**; a real
environment variable or a `docker run -e` flag still wins.

WHY IT MATERIALIZES INTO `os.environ` RATHER THAN BECOMING A LADDER RUNG
------------------------------------------------------------------------
238 direct environment reads across 70 non-test `backend/` modules never touch `config_env._eget`.
A file that fed only the accessor would be invisible to all of them — the classic works-on-one-path
split. Writing into `os.environ` with `setdefault` semantics means every one of those reads sees it
and none of them changes, and the precedence falls out for free: a variable that is already set was
set by the launch, so declining to overwrite it *is* "launch overrides file".

`setdefault`, and specifically not `if not os.environ.get(k)`. The latter treats `""` as absent and
would overwrite a deliberately-empty variable — which is a real configuration choice, since several
readers here distinguish empty from unset.

**THIS MODULE MUST RUN BEFORE THE FIRST FIRST-PARTY IMPORT, AND THAT IS NOT STYLISTIC.**
`config_env.py:737` snapshots `os.environ` at *import time* for the 95 Settings-managed keys, and
`:773` **pops** any of those keys that is absent from that snapshot. `_bootstrap_nvftp_sync_at_startup`
reaches that path at `app.py:708` on every deployment that has never opened the Settings screen. So a
value this module materializes *after* `config_env` is imported is **deleted during startup itself**
— present, then gone, no error anywhere. Measured, not theorised.

The invariant is broader than "do not import `config_env`", and the broader form is the one that
matters: **nothing reachable from this module may read the environment at import time.** An
independent review found the live near-miss — the obvious way to locate the repo root is
`paths.REPO_ROOT`, and `paths.py:30` is `from config_env import _eget`, so reaching for the obvious
helper would violate the rule *transitively* and freeze `paths._REMOTE_BASE` before the file is
applied. That is why `REPO_ROOT` below is computed from `__file__` rather than imported, and why
this module's imports are stdlib plus `config_registry` and `toml_compat`, both of which read
nothing at import.

Diagnostics are **deferred**, not logged. This runs before `logging_config.configure()` (`app.py:102`)
so that `rackstatus.toml` can set `NVIS_LOG_LEVEL` — which means there is no configured handler yet.
Records are buffered and replayed by DC-6 once logging exists; fatal paths write to `sys.stderr`,
which is the only sink guaranteed to exist this early.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import config_registry

#: Computed, never imported. See the module docstring: `paths.REPO_ROOT` would drag `config_env`
#: onto this module's import graph and defeat the whole ordering invariant.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Overridable so a test can point at a fixture without monkeypatching `Path`.
CONFIG_FILENAME = "rackstatus.toml"

#: Operator configuration lives in one directory: this file, and `tls/` beside it for a certificate
#: the operator supplies. The container mounts the host's `config/` here read-only, so the default
#: path is the same inside and outside Docker.
CONFIG_DIRNAME = "config"

#: Where an operator may point the loader. Its own value can only ever come from the real
#: environment — a file cannot usefully name the path to itself, which is why FR-28 lists it among
#: the keys whose file value can never take effect.
CONFIG_PATH_ENV = "NVIS_RACKSTATUS_CONFIG"

#: A `.example` file is documentation, never configuration (FR-1d). Loading one would give a
#: deployment the placeholder values it was meant to be told to replace.
EXAMPLE_SUFFIX = ".example"


class ConfigError(Exception):
    """The file exists and cannot be trusted. Fatal — see `load`."""


@dataclass(frozen=True)
class Record:
    """One deferred diagnostic. Replayed by DC-6 once logging is configured."""

    level: str
    message: str


@dataclass
class Result:
    """What the loader did, for the provenance block and for the preflight."""

    path: Path | None = None
    #: key -> where the value came from. `file` when this module set it, `env` when it declined
    #: because the launch already supplied one. Both sit at the process-env *rung*; they are two
    #: origins at one rung, which is why `_eget` does not learn a sixth (FR-25, FR-11).
    origins: dict[str, str] = field(default_factory=dict)
    records: list[Record] = field(default_factory=list)

    @property
    def materialized(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, v in self.origins.items() if v == "file"))

    @property
    def declined(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, v in self.origins.items() if v == "env"))


def resolve_path() -> Path:
    """Where to look. `NVIS_RACKSTATUS_CONFIG` wins over `<repo>/config/rackstatus.toml`."""
    raw = (os.environ.get(CONFIG_PATH_ENV) or "").strip()
    return Path(raw).expanduser() if raw else REPO_ROOT / CONFIG_DIRNAME / CONFIG_FILENAME


def config_dir() -> Path:
    """The directory holding the config file, and therefore the operator's `tls/`."""
    return resolve_path().parent


def render(value: object, key: str) -> str:
    """One TOML value as the string `os.environ` requires (FR-12..FR-15).

    Booleans become `"1"` / `"0"` rather than `"True"` / `"False"`, because that is what
    `core.truthy_env` and every shell script in `bin/scripts` accept. Arrays join on `","` with no
    spaces, matching `logging_config._csv`, which strips but would otherwise carry them into a
    value comparison.

    Floats are **refused**, not rendered. Both float-shaped settings in this tree are version
    strings: `nccl_allreduce.sbatch:114` compares one in `awk` by splitting on `.`, and
    `tls_server.py:60-66` matches the other as a string and silently keeps the TLS 1.2 floor on
    anything unrecognised. Even a round-tripping `repr()` turns `13.30` into `"13.3"`, a different
    value than the operator typed, applied without comment.
    """
    if isinstance(value, bool):  # before int -- bool IS an int in Python
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join(render(v, key) for v in value)
    if isinstance(value, float):
        raise ConfigError(
            f"{key} is a TOML float ({value!r}). Floats are not accepted: the settings shaped like "
            f"one here are version strings, where rendering changes the value. Quote it: "
            f'{key} = "{value}"'
        )
    raise ConfigError(f"{key} has unsupported TOML type {type(value).__name__}")


def flatten(document: dict, _prefix: str = "") -> dict[str, object]:
    """Tables are grouping only: a leaf key is the environment variable name, never a path.

    So `[auth]\\nRACK_WEB_OIDC_ISSUER = "..."` sets `RACK_WEB_OIDC_ISSUER`, not
    `AUTH_RACK_WEB_OIDC_ISSUER`. Concatenating would invent a second name for every knob and put a
    translation table between the file and the 238 readers that name their variables literally.

    The same leaf key in two tables is refused (FR-7b). It is valid TOML and means one setting
    declared twice, with file order silently deciding which wins.
    """
    out: dict[str, object] = {}
    for key, value in document.items():
        if isinstance(value, dict):
            for leaf, inner in flatten(value, f"{_prefix}{key}.").items():
                if leaf in out:
                    raise ConfigError(
                        f"{leaf} appears in more than one table; a setting has one declaration, "
                        f"and file order must not decide its value"
                    )
                out[leaf] = inner
        else:
            if key in out:
                raise ConfigError(f"{key} appears more than once")
            out[key] = value
    return out


def load(path: Path | None = None) -> Result:
    """Read the file and materialize it. Idempotent (NFR-5).

    An **absent** file completes normally and is the ordinary case, not an edge case: the real file
    is gitignored, so every fresh clone and every deployment that predates this feature has none.
    An **unreadable or malformed** file is fatal, because a deployment configured by a file nobody
    can parse is not a deployment anyone should trust — and a partial load is worse, since the
    operator would see some of their settings applied.
    """
    result = Result()
    target = path or resolve_path()
    result.path = target

    if target.name.endswith(EXAMPLE_SUFFIX):
        raise ConfigError(f"{target} is an example file; it documents settings, it is not one")

    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        result.records.append(Record("info", f"config: no {target}; environment only"))
        return result
    except OSError as exc:
        raise ConfigError(f"{target} cannot be read: {exc}") from exc

    # Imported here, not at module scope, so `cli.py init-config` -- which only needs the path --
    # runs on a host Python older than 3.11, where `tomllib` does not exist and `tomli` is not
    # installed. Reading a file needs a parser; writing one does not.
    import toml_compat

    try:
        document = toml_compat.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        # A third case, distinct from "cannot open" and "is not TOML". TOML v1.0.0 requires UTF-8,
        # and without this branch the operator gets an unhandled traceback out of a module that
        # runs before any log handler exists.
        raise ConfigError(
            f"{target} is not valid UTF-8 (byte {exc.start}); TOML v1.0.0 requires UTF-8"
        ) from exc
    except toml_compat.TOMLDecodeError as exc:
        raise ConfigError(f"{target} is not valid TOML: {exc}") from exc

    for key, value in flatten(document).items():
        if not config_registry.is_declared(key):
            # Warned, not refused. An operator carrying a key this build does not know about is
            # usually mid-upgrade, and refusing the boot over it would make the file a rollback
            # hazard. The warning names the key so a typo is findable.
            result.records.append(
                Record("warning", f"config: {target} sets {key}, which is not a declared setting")
            )

        rendered = render(value, key)
        if key in os.environ:
            result.origins[key] = "env"
            continue
        os.environ[key] = rendered
        result.origins[key] = "file"

    result.records.append(
        Record(
            "info",
            f"config: {target}: {len(result.materialized)} applied, "
            f"{len(result.declined)} already set in the environment",
        )
    )
    return result


def fatal(exc: ConfigError) -> None:
    """Refuse to start, to stderr.

    `sys.stderr` because this runs before `logging_config.configure()`, so it is the only sink that
    certainly exists. Exit 78 (`EX_CONFIG`) matches `bin/scripts/lib/nvswitch_creds.inc.sh:26`,
    which uses it for a refused credential — though note gunicorn masks the value, so the message
    is what an operator will actually have.
    """
    print(f"rackstatus: refusing to start: {exc}", file=sys.stderr, flush=True)
    raise SystemExit(78)
