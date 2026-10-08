# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Which copy of a script runs -- the project's, the platform's, or the shared default.

Author: Rafael M. Koike & Cursor Agent ("Ada")

One server runs several customers' projects. Until now it ran them all from one directory, so
editing a script to fix one site edited it for every site, and the blast radius of a typo was
every project on the box. The layered tree fixes the isolation:

    {base}/scripts/project-<id>/          one project's copy, plus its own edits   (writable)
    {base}/scripts/custom/                scripts operators added, shared           (writable)
    {base}/scripts/gb200/  gb300/ ...     platform-specific overrides, shipped      (read-only)
    {base}/scripts/multi-platform/        the shared default set, shipped           (read-only)
    {base}/var/                           unchanged

A script resolves through those **most specific first**, so a project directory need only hold
what it overrides -- and `bootstrap-project-scripts.sh` fills it with a full copy anyway, which
is what makes a project's execution independent of edits made for another.

Two things this module is deliberately not:

**It does not import the catalog.** The active project and platform arrive as environment
(`NVIS_PROJECT_ID`, `NVIS_PLATFORM_ID`, written by `catalog.context.high_overlay`), the same way
every other per-cluster value reaches the code that needs it. Importing `catalog` here would put
a store read under every script-path resolution and invert the dependency direction that
`core-designs.mdc` §5 exists to protect.

**It does not assume the tree exists.** Every deployment today has a flat `{base}/bin/scripts`,
and a resolver that silently pointed at a directory nobody created would break all of them. The
layout is detected (`_has_tree`), and without it every answer is exactly what `paths` answered
before -- so the migration is "create the tree on the head node", not "upgrade and hope".
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath

import host_exec
from config_env import _eget
from core import rpath as _rpath

#: The shared default set: what every project starts from, and where a script with no platform
#: or project opinion lives. Named rather than "default" because `multi-platform` is what it
#: means -- a script that runs anywhere -- and the deployed directory is the operator's to read.
MULTI_PLATFORM = "multi-platform"

#: Where a script that did not ship with the app lands.
#:
#: This layer exists to resolve a contradiction between two rules that are each correct. The
#: platform layers are the product's and must not be edited in place, because they are shared by
#: every project on the server. But registering a Tool only records a *basename* -- somebody has
#: to put the file on the head node, and the only writable place was a project directory, which
#: would make a genuinely cross-platform script belong to one project by accident.
#:
#: So: `custom/` is operator-owned and shared, `multi-platform/` and `<platform>/` are the
#: product's. A script that should ship with the app goes through a merge request instead, and
#: then it arrives in the default set like everything else.
#:
#: It sits *above* the product layers in the search order, so an operator can shadow a shipped
#: script without editing it -- which is the same relationship a project directory has, one
#: scope wider.
CUSTOM = "custom"

#: Prefix for a project's own directory. **Flat, not `project/<id>/`**, and that is load-bearing:
#: scripts reach sibling trees relatively (`rack_status.sh` reads
#: `"${_RACK_SCRIPTS_DIR}/../../var/cache/cmsh_rack_list.txt"`), which resolves to `{base}/var`
#: only while the script directory sits exactly two levels under the base. `scripts/gb200/` keeps
#: that depth; `scripts/project/<id>/` would be three and would read `{base}/scripts/var`
#: instead -- silently, on a cache file. `NVIS_VAR_DIR` (below) is the real fix and the scripts
#: are moving to it, but the layout should not depend on every script having been converted.
PROJECT_PREFIX = "project-"

#: Directory-probe results are cached for this long. A layer either exists or it does not, and
#: for a remote target each miss is an SSH round trip on a path resolved on every launch.
_CACHE_TTL_SEC = 30.0

_cache: dict[tuple, tuple[float, object]] = {}


def clear_cache() -> None:
    """Forget the layout probes.

    Registered as a catalog change hook: switching the active project changes which layers
    apply, and a stale answer would run the previous project's copy of a script.
    """
    _cache.clear()


def _cached(key: tuple, produce):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL_SEC:
        return hit[1]
    value = produce()
    _cache[key] = (now, value)
    return value


def _repo_root() -> PurePosixPath:
    # Imported lazily: `paths` imports this module, so importing it back at module scope would
    # be a cycle. Only the local-development fallback needs it.
    from paths import REPO_ROOT

    return PurePosixPath(str(REPO_ROOT))


def remote_base() -> str:
    """The deployment root on the execution host, or "" when running from the repo."""
    return (_eget("RACK_REMOTE_BASE_PATH", "") or "").strip()


def var_dir() -> str:
    """The `var/` tree that scripts read caches and write logs under.

    Exported to scripts as `NVIS_VAR_DIR` so they stop deriving it as `../..` from their own
    directory. That derivation is correct for exactly one layout and wrong for any other, and it
    fails by reading the wrong file rather than by erroring.
    """
    override = (_eget("NVIS_VAR_DIR", "") or "").strip()
    if override:
        return override
    base = remote_base()
    return f"{base}/var" if base else str(_repo_root() / "var")


def tree_root() -> str:
    """Root of the layered tree, whether or not it has been created yet."""
    override = (_eget("NVIS_SCRIPTS_TREE", "") or "").strip()
    if override:
        return override
    base = remote_base()
    return f"{base}/scripts" if base else str(_repo_root() / "bin" / "scripts-tree")


def legacy_dir() -> str:
    """Today's single flat directory: the answer for every deployment not yet migrated.

    Honours the topology `scripts` role first, so a deployment that keeps its script tree on a
    different host from the app keeps working -- that is the behaviour `paths._scripts_dir()`
    had, and the reason `workflow_engine` having its own copy that ignored roles was a bug.
    """
    role = host_exec.role_root("scripts")
    if role:
        return role
    base = remote_base()
    return f"{base}/bin/scripts" if base else str(_repo_root() / "bin" / "scripts")


def _has_tree() -> bool:
    """Whether the layered tree has been bootstrapped on the execution host."""

    def probe() -> bool:
        return host_exec.is_dir(f"{tree_root()}/{MULTI_PLATFORM}", role="scripts")

    return bool(_cached(("has_tree", tree_root()), probe))


def active_project_id() -> str:
    return (_eget("NVIS_PROJECT_ID", "") or "").strip()


def active_platform_id() -> str:
    return (_eget("NVIS_PLATFORM_ID", "") or "").strip()


def project_dir(project_id: str = "") -> str:
    """Where a project's own copy of the scripts lives."""
    pid = (project_id or active_project_id()).strip()
    if not pid:
        return ""
    return f"{tree_root()}/{PROJECT_PREFIX}{pid}"


def platform_dir(platform_id: str = "") -> str:
    plat = (platform_id or active_platform_id()).strip()
    if not plat:
        return ""
    return f"{tree_root()}/{plat}"


def multi_platform_dir() -> str:
    return f"{tree_root()}/{MULTI_PLATFORM}"


def custom_dir() -> str:
    """Where operator-added scripts live: shared across projects, not shipped with the app."""
    return f"{tree_root()}/{CUSTOM}"


def layers(project_id: str = "", platform_id: str = "") -> list[str]:
    """Directories to search, most specific first.

    In legacy mode this is the single flat directory, which is what makes every existing
    deployment behave exactly as it did.
    """
    if not _has_tree():
        return [legacy_dir()]
    out = [d for d in (project_dir(project_id), custom_dir(), platform_dir(platform_id)) if d]
    out.append(multi_platform_dir())
    return out


def scripts_dir(project_id: str = "", platform_id: str = "") -> str:
    """The directory a launcher should treat as "the scripts directory".

    The most specific layer that exists, because that is the one whose `lib/` a batch script
    will source once this is exported as `NVIS_SCRIPTS_DIR`. A project directory holds a full
    copy, so in practice this is the project's own tree.
    """
    for layer in layers(project_id, platform_id):
        if _cached(("dir", layer), lambda layer=layer: host_exec.is_dir(layer, role="scripts")):
            return layer
    return layers(project_id, platform_id)[-1]


#: Inside a scripts directory: settings Rack Status publishes for the cluster, as files
#: (``node_config``). Shipped empty in the default set (``bin/scripts/config/README.md``) so every
#: layer and every bootstrapped project copy has one.
CONFIG = "config"


def config_dir(project_id: str = "", platform_id: str = "") -> str:
    """The ``config/`` of the scripts a project actually runs.

    Follows :func:`scripts_dir` -- the most specific layer that exists -- because that is the copy
    whose ``lib/`` reads it: ``project-<id>/config`` once the project is bootstrapped, the platform
    layer's when one is deployed, else ``multi-platform/config``. A project that is never
    bootstrapped (NC today: project ``default`` on ``gb300-nvl72``, no platform layer) runs from the
    shared default set, so that is where its settings must be.
    """
    return f"{scripts_dir(project_id, platform_id).rstrip('/')}/{CONFIG}"


def resolve(basename: str, project_id: str = "", platform_id: str = "") -> str:
    """Absolute path to *basename*, from the most specific layer that has it.

    Falls back to the least specific layer when no layer has the file, so the path in an error
    message names where the script was expected rather than an empty string. Callers that care
    check `paths._script_exists` separately, exactly as they did before.
    """
    name = (basename or "").strip().lstrip("/")
    if not name:
        return ""
    searched = layers(project_id, platform_id)
    for layer in searched:
        candidate = f"{layer}/{name}"
        if _cached(
            ("file", candidate),
            lambda candidate=candidate: host_exec.is_file(candidate, role="scripts"),
        ):
            return candidate
    return f"{searched[-1]}/{name}"


def resolve_path(basename: str, project_id: str = "", platform_id: str = ""):
    """`resolve`, as the `PurePosixPath` the path resolvers hand out."""
    return _rpath(resolve(basename, project_id, platform_id))


def script_env(project_id: str = "", platform_id: str = "") -> dict[str, str]:
    """What a launcher must export so a batch script can find its siblings.

    Slurm copies a batch script to a spool directory, so `${BASH_SOURCE[0]}` inside it points at
    the spool and not at the script tree -- `NVIS_SCRIPTS_DIR` is the only way `lib/*.inc.sh` is
    found. `NVIS_VAR_DIR` is here for the same class of reason: relative escapes out of the tree
    are correct for one layout only.
    """
    root = scripts_dir(project_id, platform_id)
    return {
        "NVIS_SCRIPTS_DIR": root,
        "NVIS_BIN_SCRIPTS": root,
        "NVIS_VAR_DIR": var_dir(),
    }


class ImmutableLayer(PermissionError):
    """A write was aimed at a layer the product owns. The message names what to do instead."""


def layer_of(path: str) -> str:
    """Which layer *path* belongs to: ``project``, ``platform``, ``multi-platform``, ``legacy``
    or ``""`` when it is outside the tree entirely.
    """
    p = str(path or "").rstrip("/")
    if not p:
        return ""
    root = tree_root().rstrip("/")
    if p == legacy_dir().rstrip("/") or p.startswith(legacy_dir().rstrip("/") + "/"):
        return "legacy"
    if not (p == root or p.startswith(root + "/")):
        return ""
    rest = p[len(root) :].lstrip("/")
    first = rest.split("/", 1)[0] if rest else ""
    if not first:
        return ""
    if first.startswith(PROJECT_PREFIX):
        return "project"
    if first == CUSTOM:
        return "custom"
    if first == MULTI_PLATFORM:
        return "multi-platform"
    return "platform"


def is_writable(path: str) -> bool:
    """Whether the **app** may write *path*.

    A project's own directory, and `custom/`. The shared default set and the platform overrides
    are the product's: they are shipped, they are upgradeable, and they are shared by every
    project on the box, so editing one in place changes another customer's run -- which is the
    whole reason the per-project copy exists.

    `custom/` is writable because it is the answer to "where does a script somebody registers
    go?". Registering a Tool records a basename; the file has to exist somewhere, and before
    this layer the only writable place was a project directory -- which would make a genuinely
    cross-platform script the property of whichever project happened to be active. A script that
    should ship with the app goes through a merge request instead.

    **This is a property of the layer, not a flag on a record.** A `protected` field on the
    Platform document would let an operator clear it and start editing the shared files, which
    is precisely the outcome being prevented; there is nothing here to toggle. Platform
    *records* stay editable, because tuning `tool_param_defaults` per site is a legitimate and
    common thing to do.

    The legacy flat directory counts as product-owned too, and that is the safe direction: on an
    unmigrated deployment it is shared by every project, so a write there has the widest blast
    radius of all. The fix is to create the tree, which `bootstrap-project-scripts.sh` does.
    """
    return layer_of(path) in {"project", "custom"}


def assert_writable(path: str) -> None:
    """Raise :class:`ImmutableLayer` unless the app may write *path*.

    The chokepoint. Nothing in the app writes into the script tree today except the bootstrap,
    which composes a project directory -- so this exists to be called by whatever comes next (an
    in-UI script editor, an upload, a generated preset) rather than to guard code that is
    already here. `test_script_immutability.py` pins the rule so the next writer inherits it.
    """
    layer = layer_of(path)
    if layer in {"project", "custom"}:
        return
    if not layer:
        raise ImmutableLayer(
            f"{path} is outside the script tree ({tree_root()}); the app only writes a "
            "project's own script directory"
        )
    where = "the shared default set" if layer == "multi-platform" else f"the {layer} layer"
    raise ImmutableLayer(
        f"refusing to write {path}: {where} ships with the app and is shared by every project "
        f"on this server. Put an operator-added script in {custom_dir()}, or copy this one into "
        f"{project_dir() or 'the project directory'} and change it there -- 'Update scripts' on "
        "the Projects screen does exactly that, and will not overwrite what you change. To "
        "change the shipped copy for everyone, open a merge request."
    )


def describe() -> dict[str, object]:
    """The resolved layout, for the settings page and the bootstrap API."""
    return {
        "tree_root": tree_root(),
        "layered": _has_tree(),
        "layers": layers(),
        "custom_dir": custom_dir(),
        "scripts_dir": scripts_dir(),
        "var_dir": var_dir(),
        "project": active_project_id(),
        "platform": active_platform_id(),
        "legacy_dir": legacy_dir(),
        # The one directory the app may write, so a UI can say which files are editable rather
        # than discovering it from a refusal.
        "writable_dir": project_dir(),
    }
