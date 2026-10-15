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
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Materialise the configuration surface as a document — every setting the code actually reads.

Written because the question "what can I configure?" had no complete answer. Measured on
2026-09-15: the backend reads **246 distinct environment variables**; `docs/ENVIRONMENT.md`
describes 114 of them with prose, and 132 appear in no document at all. Fifteen are *declared*
in `backend/config_registry.py`, which is the only set with preflight, TOML support and a
validator.

So there are three populations, and conflating them is what made the gap invisible:

* **declared** — in the registry. `rackstatus.toml` can set it, the preflight can require it, and
  `rackstatus.toml.example` already lists it.
* **described** — has prose in `docs/ENVIRONMENT.md`, whether or not it is declared.
* **read** — appears in an `_eget(...)` or `os.environ.get(...)` call. This is the honest
  denominator, and it is the one nobody had.

This generator produces the *read* set with whatever is known about each member, so the
inventory cannot drift: a new `os.environ.get("RACK_NEW_THING")` shows up here on the next
build, and `--check` fails the pipeline until the document is regenerated. It does **not**
invent descriptions — an undescribed setting is listed as undescribed, which is a to-do a
reader can act on rather than a sentence a generator guessed.

Deliberately a static scan (`ast`) rather than an import-and-introspect: a variable read inside
a function body is never reached by importing the module, so only the source knows the full set.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import os
import pathlib
import re
import sys
from typing import NamedTuple

import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "docs" / "reference" / "configuration" / "settings.md"
ENVIRONMENT_DOC = REPO_ROOT / "docs" / "ENVIRONMENT.md"
HINTS = REPO_ROOT / "docs" / "settings-hints.toml"

#: The readers that mean "this is a configuration setting".
#:
#: `_eget` and `_eget_int` go through the whole resolution ladder (cluster overlay, job-env
#: overrides, process environment, platform defaults), so they are the preferred form and the
#: only one the dashboard Settings screen can influence. A bare `os.environ.get` opts out of the
#: overlay, which is why `core-designs.mdc` §5 asks new code not to use it — counted separately
#: so the document can say how much of that is left.
_LADDER_READERS = {"_eget", "_eget_int", "config_env._eget", "config_env._eget_int"}
_RAW_READERS = {"getenv"}
#: `config_env.env_int` and the module-local `_env_int` wrappers that delegate to it read one
#: variable by key straight from the process environment -- raw, not the ladder. Their callers pass
#: the name as a literal, so the scan has to know them by name: before it did, 99 of the 100
#: settings read through this family were missing from a document whose one claim is completeness.
#: Every `_env_int` in the backend delegates to `config_env.env_int`; `_unclassified_readers` fails
#: the build if a new one reads through the ladder instead, since this set would call it raw.
_RAW_HELPERS = {"env_int", "_env_int"}
#: `config_env._job_env_int` checks Settings -> job environment first and only then the process
#: environment, so the Settings screen CAN change it -- which is what `ladder` tells a reader, even
#: though it skips the cluster overlay `_eget` consults. Thirteen settings are read only this way.
_LADDER_HELPERS = {"_job_env_int"}


class _Reader(NamedTuple):
    """A bespoke reader that takes the setting's name as a parameter, and where its arguments sit.

    ``key`` and ``default`` are ``(position, keyword)``: either may be ``None`` where the argument
    is only ever passed the other way.
    """

    kind: str
    key: tuple[int | None, str | None] = (0, None)
    default: tuple[int | None, str | None] | None = (1, None)


#: Module-local readers that take the setting's name as a parameter, each classified by reading its
#: body. Keyed by **(module, function)** and matched by resolving each call through the caller's
#: imports, never by bare name: `_env` here is four unrelated functions in four modules, and
#: `transport/topology.py` has a `_truthy` that parses a dict value, not the environment. Before these
#: were known, 100+ settings -- every Grafana knob, every `RACK_*_SCRIPT` override, every flag read
#: through `core.truthy_env` -- were missing from a document whose one claim is completeness.
#:
#: `kind` is what a reader can rely on: `ladder` where the Settings screen can change the value.
#: `native_status._native_env_ds` reads `os.environ` directly, but only after consulting its own
#: subset of the job-env overrides file -- the Settings screen wins, so it is `ladder`.
#:
#: `_unclassified_readers` finds every function that forwards a parameter into an environment read,
#: so a new one fails the build until it is added here (or to `_NOT_SETTING_READERS`).
_PARAM_READERS: dict[tuple[str, str], _Reader] = {
    ("app", "_validation_activities_refresh_number"): _Reader("ladder"),
    ("config_preflight", "_is_set"): _Reader("raw", (0, None), None),
    ("core", "truthy_env"): _Reader("raw"),
    ("grafana_embeds", "_env"): _Reader("raw"),
    ("grafana_embeds", "_env_truthy"): _Reader("raw", (0, None), None),
    ("grafana_embeds", "_env_float"): _Reader("raw"),
    ("identity.tokens", "_configured_ttl"): _Reader("raw"),
    ("lake_sink", "_env"): _Reader("raw"),
    ("metrics_query", "_env"): _Reader("raw"),
    ("native_status", "_native_env_ds"): _Reader("ladder"),
    ("native_status", "_env_truthy"): _Reader("raw", (0, None), None),
    ("nvftp_cache_sync", "_truthy"): _Reader("raw", (0, None), None),
    ("nvis_id_templates", "_env_marker_list"): _Reader("raw", (0, None), None),
    # `_script(basename, env_key)`: the override goes through `_eget`.
    ("paths", "_script"): _Reader("ladder", (1, "env_key"), None),
    ("stream_exec", "_merged_stdout_stream_generator"): _Reader(
        "ladder", (None, "linebuffer_env_var"), (None, "linebuffer_default")
    ),
    # A dataclass field, not a parameter the audit can see: `AgentTool.is_available` reads
    # `os.environ.get(self.disabled_env)`, and the name is given at construction.
    ("agent.tools", "AgentTool"): _Reader("raw", (None, "disabled_env"), None),
}

#: Functions that take an environment variable's name as a parameter but do not read a setting.
_NOT_SETTING_READERS = {
    # Materialises the catalog overlay into `os.environ`; it reads only to remember the original.
    ("app", "_catalog_set_environ"),
}

#: Names that are not configuration: loop variables, test scaffolding, and the handful of
#: all-caps locals that happen to be read from the environment by a helper taking a key.
_NOT_A_SETTING = re.compile(r"^(?:TRUE|FALSE|NONE|PATH|HOME|USER|LANG|LC_[A-Z]+|TZ|TERM|SHELL)$")


def _is_env_read(node: ast.Call) -> str | None:
    """`"ladder"`, `"raw"` or `None` for one call node."""
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in {"_eget", "_eget_int"} or func.id in _LADDER_HELPERS:
            return "ladder"
        if func.id == "getenv" or func.id in _RAW_HELPERS:
            return "raw"
        return None
    if isinstance(func, ast.Attribute):
        # `os.environ.get(...)` only.
        #
        # A bare `environ.get(...)` is deliberately NOT accepted, and the first version of this
        # scan did accept it. Every such call in this codebase reads the **WSGI environ** — the
        # request dict handed to middleware — so the inventory listed `HTTP_HOST`, `PATH_INFO`,
        # `QUERY_STRING`, `REMOTE_ADDR` and four more CGI keys as undescribed configuration
        # settings. Eight entries in a document whose whole claim is completeness, each one an
        # invitation to go documenting a setting that does not exist. No module here does
        # `from os import environ`, so requiring the attribute chain loses nothing.
        if func.attr == "get":
            target = func.value
            if isinstance(target, ast.Attribute) and target.attr == "environ":
                return "raw"
        if func.attr in {"_eget", "_eget_int"} or func.attr in _LADDER_HELPERS:
            return "ladder"
        if func.attr == "getenv" or func.attr in _RAW_HELPERS:
            return "raw"
    return None


def _arg(node: ast.Call, where: tuple[int | None, str | None]) -> ast.expr | None:
    """The argument at ``(position, keyword)``, whichever way the call passed it."""
    index, keyword = where
    if index is not None and len(node.args) > index:
        return node.args[index]
    for kw in node.keywords:
        if keyword is not None and kw.arg == keyword:
            return kw.value
    return None


def _literal_default(
    node: ast.Call, where: tuple[int | None, str | None] | None = (1, None)
) -> str:
    """The default argument (the second positional, unless a reader says otherwise) when it is a
    literal, else ``""``.

    Only literals: a default computed from another setting is not a value a reader can copy, and
    printing the expression would be noise.
    """
    arg = _arg(node, where) if where is not None else None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, (str, int, float, bool)):
        return str(arg.value)
    return ""


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "LITERAL"`` bindings.

    Needed because the key is frequently a constant rather than a literal:
    ``os.environ.get(ISSUER_ENV)`` in `identity/oidc.py` is how `RACK_WEB_OIDC_ISSUER` is
    actually read, and a scan that only accepted literals reported it as never read at all.
    That is the difference between an inventory and an inventory that is *complete*, which is
    the only property this document claims.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[target.id] = node.value.value
    return out


def _key_of(
    node: ast.Call, constants: dict[str, str], where: tuple[int | None, str | None] = (0, None)
) -> str:
    """The setting name a call reads: a literal, or a module constant holding one."""
    first = _arg(node, where)
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.Name):
        return constants.get(first.id, "")
    return ""


def _module_name(path: pathlib.Path) -> str:
    """``backend/identity/tokens.py`` -> ``identity.tokens``, as the backend imports it."""
    parts = list(path.relative_to(REPO_ROOT / "backend").with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _Module(NamedTuple):
    """One parsed backend module and what its names refer to."""

    name: str
    rel: str
    tree: ast.Module
    constants: dict[str, str]
    #: local name -> (module, attribute): top-level definitions, `from X import f [as g]`, and
    #: module-level re-bindings such as `app._truthy_env = core.truthy_env`.
    names: dict[str, tuple[str, str]]
    #: local spelling -> module, for `import X [as y]` so `y.f(...)` resolves.
    modules: dict[str, str]


def _index_module(path: pathlib.Path, tree: ast.Module) -> _Module:
    name = _module_name(path)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    names: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}

    def bind_import(node: ast.Import | ast.ImportFrom, *, override: bool) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.setdefault(alias.asname or alias.name, alias.name)
            return
        source = node.module or ""
        if node.level:
            base = package.split(".") if package else []
            base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
            source = ".".join([*base, source] if source else base)
        for alias in node.names:
            local = alias.asname or alias.name
            if override or local not in names:
                names[local] = (source, alias.name)

    # Top level in source order, so a later definition or re-binding wins as it does at runtime.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] = (name, node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bind_import(node, override=True)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            bound = _resolve_expr(node.value, names, modules)
            if bound is not None:
                names[node.targets[0].id] = bound
    # Imports deferred into function bodies (to dodge import cycles) only fill gaps: they must not
    # shadow a module-level name of the same spelling.
    top = set(map(id, tree.body))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in top:
            bind_import(node, override=False)
    return _Module(
        name, str(path.relative_to(REPO_ROOT)), tree, _module_constants(tree), names, modules
    )


def _resolve_expr(
    func: ast.expr, names: dict[str, tuple[str, str]], modules: dict[str, str]
) -> tuple[str, str] | None:
    if isinstance(func, ast.Name):
        return names.get(func.id)
    if isinstance(func, ast.Attribute):
        target = modules.get(ast.unparse(func.value))
        if target is not None:
            return target, func.attr
    return None


def _resolve(
    node: ast.Call,
    module: _Module,
    index: dict[str, _Module],
    readers: dict[tuple[str, str], _Reader] = _PARAM_READERS,
) -> tuple[str, str] | None:
    """The (module, function) a call lands on, following imports and re-bindings to the definition."""
    where = _resolve_expr(node.func, module.names, module.modules)
    for _ in range(8):  # a re-export chain longer than this is not a pattern this codebase has
        if where is None or where in readers:
            return where
        owner = index.get(where[0])
        nxt = owner.names.get(where[1]) if owner is not None else None
        if nxt is None or nxt == where:
            return where
        where = nxt
    return where


def _modules() -> dict[str, _Module]:
    """Every backend module outside `tests/`, parsed once."""
    out: dict[str, _Module] = {}
    for path in sorted((REPO_ROOT / "backend").rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            continue
        module = _index_module(path, tree)
        out[module.name] = module
    return out


def _reads(
    module: _Module,
    index: dict[str, _Module],
    readers: dict[tuple[str, str], _Reader] = _PARAM_READERS,
):
    """Each environment read in a module: ``(call, kind, key position, default position)``."""
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _is_env_read(node)
        if kind is not None:
            yield node, kind, (0, None), (1, None)
            continue
        reader = readers.get(_resolve(node, module, index, readers) or ("", ""))
        if reader is not None:
            yield node, reader.kind, reader.key, reader.default


def scan() -> dict[str, dict]:
    """Every environment variable the backend reads, with where and how."""
    found: dict[str, dict] = {}
    index = _modules()
    for module in index.values():
        for node, kind, key_at, default_at in _reads(module, index):
            name = _key_of(node, module.constants, key_at)
            if not name:
                continue
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or _NOT_A_SETTING.match(name):
                continue
            entry = found.setdefault(
                name, {"name": name, "modules": set(), "ladder": False, "raw": False, "default": ""}
            )
            entry["modules"].add(module.rel)
            entry[kind if kind == "ladder" else "raw"] = True
            if not entry["default"]:
                entry["default"] = _literal_default(node, default_at)
    return found


def _parameterised_readers(index: dict[str, _Module]) -> dict[tuple[str, str], str]:
    """Every function that hands one of its own parameters to an environment read, with the kind
    of read it hands it to (``ladder`` if any path does).

    This is how the scan stays complete without anyone remembering to extend it: a new
    `def _env(name)` in some module is found here whether or not anything calls it yet. Iterated to
    a fixed point, each find standing in as a reader for the next round, so a wrapper over a new
    reader (`_env_float` over `_env`) is reported together with it rather than one build later.
    """
    out: dict[tuple[str, str], str] = {}
    readers = dict(_PARAM_READERS)
    while True:
        found = False
        for module in index.values():
            reads = list(_reads(module, index, readers))
            for fn in ast.walk(module.tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                a = fn.args
                params = [p.arg for p in [*a.posonlyargs, *a.args]]
                kwonly = {p.arg for p in a.kwonlyargs}
                end = fn.end_lineno or fn.lineno
                for node, kind, key_at, _ in reads:
                    key = _arg(node, key_at)
                    if not (isinstance(key, ast.Name) and (key.id in params or key.id in kwonly)):
                        continue
                    if not fn.lineno <= node.lineno <= end:
                        continue
                    where = (module.name, fn.name)
                    if out.get(where) == "ladder" or out.get(where) == kind:
                        continue
                    out[where] = kind
                    found = True
                    if where not in _PARAM_READERS:
                        position = params.index(key.id) if key.id in params else None
                        readers[where] = _Reader(kind, (position, key.id), None)
        if not found:
            return out


def _unclassified_readers(index: dict[str, _Module] | None = None) -> list[str]:
    """Problems that would make the scan silently incomplete or wrong, one sentence each.

    * a function forwards a parameter into an environment read but is in neither
      `_PARAM_READERS` nor `_NOT_SETTING_READERS` -- its callers' settings are invisible;
    * one recognised only by a generic helper name (`_env_int`, ...) reads a different way than
      that name promises;
    * a `_PARAM_READERS` entry names a function that no longer exists -- renamed, so its callers
      have quietly dropped out of the document.
    """
    index = index if index is not None else _modules()
    problems: list[str] = []
    # Only one direction of the kind check is meaningful: `_eget` and `_job_env_int` *are* the
    # ladder, and their own bottom rung is an `os.environ` read, so a ladder name with a raw body
    # is expected. A raw name whose body reaches the ladder is the mistake worth catching.
    generic = _RAW_HELPERS | _LADDER_HELPERS | {"_eget", "_eget_int"}
    for (mod, fn), kind in sorted(_parameterised_readers(index).items()):
        if (mod, fn) in _PARAM_READERS or (mod, fn) in _NOT_SETTING_READERS:
            continue
        if fn in generic:
            if fn in _RAW_HELPERS and kind == "ladder":
                problems.append(
                    f"{mod}.{fn} is recognised by name as a raw read, but its body goes through "
                    "the ladder; add it to _PARAM_READERS as a ladder reader"
                )
            continue
        problems.append(
            f"{mod}.{fn} reads the environment by a parameter; classify it in _PARAM_READERS "
            "(or _NOT_SETTING_READERS) so the settings its callers read are documented"
        )
    for mod, fn in sorted(set(_PARAM_READERS) | _NOT_SETTING_READERS):
        owner = index.get(mod)
        if owner is None or owner.names.get(fn) != (mod, fn):
            problems.append(f"{mod}.{fn} is classified but no longer defined; update the table")
    return problems


def registry_metadata() -> dict[str, dict]:
    """What `config_registry` declares, for the fifteen settings that have a declaration."""
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    os.environ.setdefault("RACK_STATUS_DEMO", "1")
    os.environ.setdefault("NVIS_LOG_SINKS", "file")
    os.environ.setdefault("NVIS_LOG_FILE", "/tmp/gen-settings-reference.log")

    import app  # noqa: F401  (the registry is populated at import)
    import config_registry

    out: dict[str, dict] = {}
    for name, setting in config_registry.declared().items():
        requirement = setting.requirement()
        out[name] = {
            "section": setting.section,
            "toml_type": setting.toml_type,
            "secret": setting.is_secret,
            "ui_managed": setting.ui_managed,
            "example": setting.example,
            "description": setting.description,
            "required": bool(getattr(requirement, "required", False)),
        }
    return out


def hints() -> dict[str, str]:
    """Reviewed one-line hints for settings with no declaration of their own.

    A separate, hand-maintained file rather than anything derived, because a generator cannot
    know what a setting is *for* — and one that guessed would emit confident sentences nobody
    checked, which is the failure mode this whole document exists to avoid. Unhinted settings
    stay on the to-do list.
    """
    if not HINTS.is_file():
        return {}
    with HINTS.open("rb") as fh:
        return dict(tomllib.load(fh).get("hints") or {})


def anchor(name: str) -> str:
    """The id a setting's row carries, so the to-do list can link into the table."""
    return f"s-{name.lower().replace('_', '-')}"


def described_names() -> set[str]:
    """Settings that already have prose in `docs/ENVIRONMENT.md`."""
    if not ENVIRONMENT_DOC.is_file():
        return set()
    text = ENVIRONMENT_DOC.read_text(errors="replace")
    return set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text))


def render(
    read: dict[str, dict],
    declared: dict[str, dict],
    described: set[str],
    hint: dict[str, str],
) -> str:
    # The population is the **union**, and that matters. Five declared settings never appear in a
    # scannable call: `RACK_SSH_USER`, `RACK_SSH_KEY`, `RACK_SSH_PORT` and `RACK_EXEC_MODE` are
    # consumed by the connection profile from a list of names, and `NVIS_LOG_LEVEL` through
    # `logging_config`'s own registry. Listing only what the scan sees would tell a reader that
    # `RACK_SSH_USER` is unused, which is worse than an incomplete document -- it is a confident
    # wrong answer about the setting most deployments must set.
    indirect = sorted(set(declared) - set(read))
    for name in indirect:
        read[name] = {
            "name": name,
            "modules": set(),
            "ladder": False,
            "raw": False,
            "default": "",
            "indirect": True,
        }
    total = len(read)
    n_declared = sum(1 for n in read if n in declared)
    n_described = sum(1 for n in read if n in described)
    undocumented = sorted(
        n for n in read if n not in described and n not in declared and n not in hint
    )
    raw_only = sorted(n for n, e in read.items() if e["raw"] and not e["ladder"])

    lines = [
        "---",
        "diataxis: reference",
        "description: Every environment variable this product reads, and which of them a config file can set.",
        "---",
        "",
        "<!-- GENERATED by bin/gen-settings-reference.py. Do not edit. -->",
        "",
        "# Configuration settings",
        "",
        "Every environment variable the backend reads, found by scanning the source rather than by",
        "importing it — a variable read inside a function body is never reached by an import, so only",
        "the source knows the whole set.",
        "",
        f"**{total} settings.** {n_declared} are *declared* in",
        "`backend/config_registry.py`, which is the set `rackstatus.toml` can carry, the required-",
        f"setting preflight can demand, and `rackstatus.toml.example` lists. {n_described} are named in",
        "`docs/ENVIRONMENT.md` — named, which is a weaker claim than explained: the check is",
        "whether that document mentions the setting at all. It is not in this site yet; see",
        "`docs/README.md` for why the migration is page-at-a-time.",
        "",
        "## How a value is resolved",
        "",
        "Highest wins (`core-designs.mdc` §5):",
        "",
        "1. the active cluster's **high overlay** — identity, naming and connection from the catalog",
        "2. **job env overrides** — `var/cache/job_env_overrides.json`, written by the Settings screen",
        "3. the **process environment** — `rackstatus.toml`, the shell, the container",
        "4. the platform's **low overlay** — geometry and per-tool defaults",
        "",
        "`Reader` below says which rung a setting starts at. `ladder` means it is read through",
        "`_eget` and therefore honours all four; `environment` means it is read straight from the",
        "process environment and cannot be set per cluster or from the Settings screen.",
        "",
    ]

    if not undocumented:
        lines += [
            "## Undescribed settings",
            "",
            "None: every setting read has either a declaration, prose in `docs/ENVIRONMENT.md` or a",
            "hint in `docs/settings-hints.toml`. A newly added setting appears here on the next",
            "build until somebody writes one.",
            "",
        ]

    if undocumented:
        lines += [
            "## Undescribed settings",
            "",
            f"{len(undocumented)} settings have no declaration, no prose in `docs/ENVIRONMENT.md`",
            "and no hint in `docs/settings-hints.toml`. Each name links to its row in the table",
            "below, which still gives you its reader, its default and the module that reads it.",
            "",
            "They are listed because an inventory that hid them would be the reason nobody knew. A",
            "generator must not invent a description, so each of these is a real to-do — write one",
            "into `docs/settings-hints.toml` and it moves out of this list on the next build.",
            "",
            ", ".join(
                f'<a href="#{anchor(n)}"><code>{n}</code></a>' for n in undocumented
            ),
            "",
        ]

    if indirect:
        lines += [
            "## Declared, read indirectly",
            "",
            f"{len(indirect)} settings are declared but never appear in a call this scan can see:",
            "the connection profile reads its four by name from a list, and `NVIS_LOG_LEVEL` goes",
            "through `logging_config`'s own registry. They are configuration like any other — the",
            "scan simply cannot point at a call site.",
            "",
            ", ".join(f"`{n}`" for n in indirect),
            "",
        ]

    if raw_only:
        lines += [
            "## Read outside the resolution ladder",
            "",
            f"{len(raw_only)} of them are read only via `os.environ`, so a cluster overlay and the",
            "Settings screen cannot influence them — the setting is process-wide. New code should",
            "prefer `_eget` (`core-designs.mdc` §5); these are the ones that predate the rule or",
            "genuinely belong to the process rather than to a cluster.",
            "",
            f"They are not listed here — {len(raw_only)} names in a paragraph is not something",
            "anyone reads.",
            "The table below marks each one `environment` in its `Reader` column, which answers the",
            "same question setting by setting, where you are already looking.",
            "",
        ]

    lines += [
        "## Declared settings",
        "",
        "These have a declaration, so `rackstatus.toml` can set them and the preflight can require",
        "them. `UI` marks the ones the dashboard Settings screen may override.",
        "",
        "| Setting | Section | Type | Required | UI | Example | Purpose |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in sorted(n for n in read if n in declared):
        d = declared[name]
        lines.append(
            f"| `{name}` | {d['section']} | {d['toml_type']} | "
            f"{'yes' if d['required'] else 'no'} | {'yes' if d['ui_managed'] else ''} | "
            f"`{d['example']}` | {d['description'] or '—'} |"
        )
    lines.append("")

    lines += [
        "## Every setting",
        "",
        "`What it controls` is the registry's own description where a setting has a declaration,",
        "then a reviewed hint from `docs/settings-hints.toml`, then a pointer to the prose in",
        "`docs/ENVIRONMENT.md`. A dash means nobody has written one yet.",
        "",
        "`Reader` is `ladder` where the value goes through `_eget`, `environment` where it is read",
        "straight from `os.environ`, and `indirect` for the five consumed by name from a list (the",
        "connection profile) or through another registry (`logging_config`) rather than by a call",
        "this scan can see. `Default` is shown only where the call site supplies a literal.",
        "",
        "| Setting | Reader | Default | What it controls | Read in |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(read):
        entry = read[name]
        reader = (
            "indirect" if entry.get("indirect")
            else "ladder" if entry["ladder"]
            else "environment"
        )
        default = f"`{entry['default']}`" if entry["default"] else ""
        modules = sorted(entry["modules"])
        shown = ", ".join(f"`{m}`" for m in modules[:2])
        if len(modules) > 2:
            shown += f" +{len(modules) - 2}"
        purpose = (
            declared.get(name, {}).get("description")
            or hint.get(name)
            or ("See `docs/ENVIRONMENT.md`" if name in described else "—")
        )
        lines.append(
            f'| <a id="{anchor(name)}"></a>`{name}` | {reader} | {default} '
            f"| {purpose} | {shown} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def build() -> str:
    return render(scan(), registry_metadata(), described_names(), hints())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the tracked document differs from what the source would produce",
    )
    args = parser.parse_args()

    problems = _unclassified_readers()
    if problems:
        for problem in problems:
            print(f"unclassified: {problem}")
        return 1

    rendered = build()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != rendered:
            # A unified diff, not just "stale": the pipeline has no git binary
            # (`local-vs-ci-tree.mdc` §4), so printing the difference here is the only way a
            # contributor sees *which* setting changed.
            print(f"stale: {OUT.relative_to(REPO_ROOT)}")
            for line in difflib.unified_diff(
                current.splitlines(), rendered.splitlines(),
                fromfile="tracked", tofile="generated", lineterm="", n=2,
            ):
                print(line)
            print("\nA setting was added, removed or renamed without regenerating.")
            print("Run: bin/gen-settings-reference.py")
            return 1
        print("settings reference is current")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
