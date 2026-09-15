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

* **declared** — in the registry. `greenlight.toml` can set it, the preflight can require it, and
  `greenlight.toml.example` already lists it.
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

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "docs" / "reference" / "configuration" / "settings.md"
ENVIRONMENT_DOC = REPO_ROOT / "docs" / "ENVIRONMENT.md"

#: The readers that mean "this is a configuration setting".
#:
#: `_eget` and `_eget_int` go through the whole resolution ladder (cluster overlay, job-env
#: overrides, process environment, platform defaults), so they are the preferred form and the
#: only one the dashboard Settings screen can influence. A bare `os.environ.get` opts out of the
#: overlay, which is why `core-designs.mdc` §5 asks new code not to use it — counted separately
#: so the document can say how much of that is left.
_LADDER_READERS = {"_eget", "_eget_int", "config_env._eget", "config_env._eget_int"}
_RAW_READERS = {"getenv"}

#: Names that are not configuration: loop variables, test scaffolding, and the handful of
#: all-caps locals that happen to be read from the environment by a helper taking a key.
_NOT_A_SETTING = re.compile(r"^(?:TRUE|FALSE|NONE|PATH|HOME|USER|LANG|LC_[A-Z]+|TZ|TERM|SHELL)$")


def _is_env_read(node: ast.Call) -> str | None:
    """`"ladder"`, `"raw"` or `None` for one call node."""
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in {"_eget", "_eget_int"}:
            return "ladder"
        if func.id == "getenv":
            return "raw"
        return None
    if isinstance(func, ast.Attribute):
        # `os.environ.get(...)` / `environ.get(...)`
        if func.attr == "get":
            target = func.value
            if isinstance(target, ast.Attribute) and target.attr == "environ":
                return "raw"
            if isinstance(target, ast.Name) and target.id == "environ":
                return "raw"
        if func.attr in {"_eget", "_eget_int"}:
            return "ladder"
        if func.attr == "getenv":
            return "raw"
    return None


def _literal_default(node: ast.Call) -> str:
    """The second positional argument when it is a literal, else ``""``.

    Only literals: a default computed from another setting is not a value a reader can copy, and
    printing the expression would be noise.
    """
    if len(node.args) < 2:
        return ""
    arg = node.args[1]
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
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out[target.id] = node.value.value
    return out


def _key_of(node: ast.Call, constants: dict[str, str]) -> str:
    """The setting name a call reads: a literal, or a module constant holding one."""
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.Name):
        return constants.get(first.id, "")
    return ""


def scan() -> dict[str, dict]:
    """Every environment variable the backend reads, with where and how."""
    found: dict[str, dict] = {}
    for path in sorted((REPO_ROOT / "backend").rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            kind = _is_env_read(node)
            if kind is None:
                continue
            name = _key_of(node, constants)
            if not name:
                continue
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or _NOT_A_SETTING.match(name):
                continue
            entry = found.setdefault(
                name, {"name": name, "modules": set(), "ladder": False, "raw": False, "default": ""}
            )
            entry["modules"].add(rel)
            entry[kind if kind == "ladder" else "raw"] = True
            if not entry["default"]:
                entry["default"] = _literal_default(node)
    return found


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


def described_names() -> set[str]:
    """Settings that already have prose in `docs/ENVIRONMENT.md`."""
    if not ENVIRONMENT_DOC.is_file():
        return set()
    text = ENVIRONMENT_DOC.read_text(errors="replace")
    return set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text))


def render(read: dict[str, dict], declared: dict[str, dict], described: set[str]) -> str:
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
    undocumented = sorted(n for n in read if n not in described and n not in declared)
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
        "`backend/config_registry.py`, which is the set `greenlight.toml` can carry, the required-",
        f"setting preflight can demand, and `greenlight.toml.example` lists. {n_described} are named in",
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
        "3. the **process environment** — `greenlight.toml`, the shell, the container",
        "4. the platform's **low overlay** — geometry and per-tool defaults",
        "",
        "`Reader` below says which rung a setting starts at. `ladder` means it is read through",
        "`_eget` and therefore honours all four; `environment` means it is read straight from the",
        "process environment and cannot be set per cluster or from the Settings screen.",
        "",
    ]

    if undocumented:
        lines += [
            "## Undescribed settings",
            "",
            f"{len(undocumented)} settings are read by the code and are not named in any document. They",
            "are listed here",
            "because an inventory that hid them would be the reason nobody knew: a generator must not",
            "invent a description, so each of these is a real to-do rather than a sentence somebody",
            "guessed.",
            "",
            ", ".join(f"`{n}`" for n in undocumented),
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
            f"{len(raw_only)} settings are read only via `os.environ`, so a cluster overlay and the",
            "Settings screen cannot influence them — the setting is process-wide. New code should",
            "prefer `_eget` (`core-designs.mdc` §5); these are the ones that predate the rule or",
            "genuinely belong to the process rather than to a cluster.",
            "",
            ", ".join(f"`{n}`" for n in raw_only),
            "",
        ]

    lines += [
        "## Declared settings",
        "",
        "These have a declaration, so `greenlight.toml` can set them and the preflight can require",
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
        "`Reader` is `ladder` where the value goes through `_eget`, `environment` where it is read",
        "straight from `os.environ`, and `indirect` for the five consumed by name from a list (the",
        "connection profile) or through another registry (`logging_config`) rather than by a call",
        "this scan can see. `Default` is shown only where the call site supplies a literal.",
        "",
        "| Setting | Reader | Default | Declared | Described | Read in |",
        "|---|---|---|---|---|---|",
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
        lines.append(
            f"| `{name}` | {reader} | {default} | {'yes' if name in declared else ''} "
            f"| {'yes' if name in described else ''} | {shown} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def build() -> str:
    return render(scan(), registry_metadata(), described_names())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the tracked document differs from what the source would produce",
    )
    args = parser.parse_args()

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
