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
"""What `rackstatus.toml` is allowed to declare, and which settings a launch cannot do without.

DC-2 of `.plc/briefs/launch-configuration-brief.md`. Three consumers: the loader (DC-3) asks what a
key's TOML type is and whether it may be materialized, the preflight (DC-4) asks which settings are
required *right now*, and the example generator (DC-5) renders this file into
`config/rackstatus.toml.example` so the documentation cannot drift from the code that reads it.

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

from collections.abc import Callable
from dataclasses import dataclass

from logging_config import SENSITIVE_NAME_PATTERN

#: The TOML types the loader knows how to render into the string-shaped environment (FR-12..FR-15).
#: Anything else is refused at load rather than coerced, because a silent coercion of a type nobody
#: designed for is how a setting comes to mean something different in the file than in the process.
TOML_STR = "string"
TOML_BOOL = "boolean"
TOML_INT = "integer"
TOML_ARRAY = "array"
TOML_TYPES = (TOML_STR, TOML_BOOL, TOML_INT, TOML_ARRAY)

#: Floats are REFUSED, not rendered, and the review that found this had better evidence than my
#: reasoning did. I had settled on `repr()` because it round-trips. It does -- but both float-shaped
#: settings in this tree are **version strings**, and for those, round-tripping is not the property
#: that matters:
#:
#:   * `bin/scripts/nccl_allreduce.sbatch:107,114` reads NVIS_NCCL_MIN_CUDA_VERSION and compares it
#:     in awk by splitting on `.`; under `format(v,"f")` "13.3" becomes "13.300000" and the
#:     fail-closed gate evaluates `3 >= 300000`, refusing every allocated node.
#:   * `tls_server.py:60-66` matches RACK_WEB_TLS_MIN_VERSION as a STRING against 1.2/1.3, and an
#:     unrecognised value warns and keeps the 1.2 floor -- so a rendering rule produces a silent
#:     TLS downgrade.
#:
#: `repr()` survives both, but `13.30` still renormalises to `"13.3"` -- a different value than the
#: operator typed, applied without comment. Refusing is free (nothing declares a float) and the
#: diagnostic lands where the mistake was made: declare it as a string.


class RegistryError(Exception):
    """A malformed declaration. Raised at import, never logged and continued."""


@dataclass(frozen=True)
class Requirement:
    """Whether a setting is required, with room for "could not tell".

    `required is None` is the third state, and it exists so a failing predicate is *reported*
    rather than silently resolved in either direction — see `Setting.requirement`.
    """

    required: bool | None
    error: str | None = None

    @property
    def undetermined(self) -> bool:
        return self.required is None


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

    def requirement(self) -> Requirement:
        """Whether this launch needs it — as a **tri-state**, not a boolean.

        The first version of this returned `bool` and answered `False` when the predicate raised.
        That was fail-open on a fail-closed control, and `identity-auth-traps.mdc` trap 7 names
        exactly that mistake: a predicate that throws means the preflight *cannot tell* whether the
        setting is required, and resolving "cannot tell" to "not required" boots a deployment with a
        possibly-missing required setting. That is the green-but-unusable outcome this whole feature
        exists to prevent. Its docstring also claimed "the preflight logs it" while this module
        imports no logger and the method returned a bare bool — so the stated mitigation could not
        exist.

        Three answers, so DC-4 can report a third fault class ("could not evaluate X's predicate")
        in the same report-all diagnostic FR-17 already requires. That keeps one unrelated predicate
        bug from refusing a boot *and* stops the failure being silent, which the boolean form could
        not do at the same time.
        """
        if self.required_when is None:
            return Requirement(required=False)
        try:
            return Requirement(required=bool(self.required_when()))
        except Exception as exc:  # noqa: BLE001 - any predicate failure is reportable, not fatal
            # Broad on purpose: a predicate can fail for reasons unrelated to its setting (an
            # unavailable catalog, a half-written overrides file). Enumerating the types would mean
            # the next unanticipated one takes the deployment down. Unlike the previous version this
            # does not swallow it — the error text reaches the operator through DC-4.
            return Requirement(required=None, error=f"{type(exc).__name__}: {exc}")


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


def unregister(name: str) -> None:
    """Remove a declaration. **For tests only.**

    The registry is module state, so a test that registers a setting to provoke a fault would leave
    it behind for every later test in the session — and a stray always-required setting makes the
    preflight refuse a launch it should permit. `register` deliberately refuses a differing
    duplicate, which means a leaked declaration also breaks re-registration.

    Silent on an unknown name: a fixture tearing down should not have to know whether its `register`
    got as far as running.
    """
    _SETTINGS.pop(name, None)


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
    """The settings this launch definitely requires, against the environment as it stands.

    Settings whose predicate could not be evaluated are **not** here — they are in
    `undetermined_now()`. Folding them in either direction is what made the first version
    fail-open; keeping them separate is what lets DC-4 report all three outcomes.
    """
    return tuple(s for s in _SETTINGS.values() if s.requirement().required is True)


def undetermined_now() -> tuple[tuple[Setting, str], ...]:
    """Settings whose `required_when` raised, with the error text.

    DC-4's third fault class. A caller that ignores this is back to fail-open, so the preflight
    reports these alongside genuine faults rather than treating an unanswerable question as a no.
    """
    out: list[tuple[Setting, str]] = []
    for setting in _SETTINGS.values():
        result = setting.requirement()
        if result.undetermined:
            out.append((setting, result.error or "predicate failed"))
    return tuple(out)


def reset_for_tests() -> None:
    _SETTINGS.clear()


# --- Declarations ------------------------------------------------------------------------------
#
# The initial required set is deliberately SMALL. Every entry here is a setting whose absence
# breaks the deployment in a way the operator cannot diagnose from the symptom; a setting with a
# sensible default does not belong, because "missing" and "defaulted" are different states and only
# the first is a fault.


def _ssh_control_connection_in_use() -> bool:
    """Whether this deployment actually reaches its cluster over SSH.

    Two things here were wrong in the first version and both mattered.

    **It read `RACK_EXEC_MODE` directly.** FR-21 forbids that explicitly: the variable is
    *deprecated* (`transport/factory.py:45-55`), and `TransportConfig.from_env` is not a synonym for
    reading it — that function warns when it is set and coerces an unrecognised value to
    `DEFAULT_EXEC_MODE`, neither of which a bare read does. The predicate must key off the
    **resolved** connection.

    **It read raw `os.environ`, so the active cluster's high overlay was never consulted.** That is
    the citizen-5 mistake `core-designs.mdc` names in as many words — "a bare `os.environ.get` opts
    out of the per-cluster overlay and produces a feature that works on one cluster and not
    another" — and it defeats the entire reason FR-22 places the preflight *after* the catalog
    bootstrap. A cluster supplying `RACK_SSH_HOST` through its overlay would have been refused.

    The demo conjunct is decision 10 and is separately load-bearing: without it, `./start.sh` on a
    macOS laptop (demo mode) and every genuinely-fresh first boot at all five sites is refused,
    because `catalog/seed.py:577` stores an empty `ssh_host` when the variable is unset.

    Both imports are function-level: DC-3 loads before the
    first first-party import, this module sits on that path, and `config_env` is precisely what
    DD-1 rule 2 forbids on it at module scope.
    """
    from core import truthy_env

    if truthy_env("RACK_STATUS_DEMO"):
        return False

    from config_env import _subprocess_env
    from transport import factory

    return factory.TransportConfig.from_env(_subprocess_env()).exec_mode == "ssh"


RACK_WEB_OIDC_ISSUER = register(
    Setting(
        name="RACK_WEB_OIDC_ISSUER",
        secret=False,
        # Not required: `startup_gate._ensure_oidc_issuer()` derives one when unset, the same
        # way it generates a self-signed certificate rather than refusing to start for want of
        # something the product can make. The predicate this replaced asked whether the OIDC
        # provider was enabled — a question with only one answer since 2026-09-08.
        required_when=None,
        example="https://rackstatus.example.com",
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

#: Declared but NOT required, and the correction is worth recording.
#:
#: I first registered this with the same predicate as `RACK_SSH_HOST`, which made it required on
#: every non-demo deployment. An independent design review caught it: `paths.py:37-43` falls back to
#: `REPO_ROOT/bin/scripts` when it is unset, `ENVIRONMENT.md:632` documents its default as unset,
#: and `paths.py:56` prefers a declared topology's `scripts` role root over it entirely. So a
#: topology-declared deployment that legitimately sets no base path would have been **refused
#: startup** — decision 10's failure mode reintroduced on a different key, and a direct violation of
#: the rule stated in this module's own docstring: a setting with a sensible default does not belong
#: in the required set, because "missing" and "defaulted" are different states and only the first is
#: a fault.
RACK_REMOTE_BASE_PATH = register(
    Setting(
        name="RACK_REMOTE_BASE_PATH",
        secret=False,
        example="/home/cmsupport/nvis-rack-status",
        section="connection",
        description=(
            "Where the tooling is deployed on the head node. Script paths and the default log "
            "directory derive from it. Optional: unset falls back to the repo root, and a declared "
            "topology's scripts role root supersedes it."
        ),
    )
)


# -- The settings a real deployment actually sets ------------------------------------------------
register(Setting(name="RACK_CLUSTERKIT_SITE_PROFILE", secret=False, ui_managed=True,
                 example='{"enabled":false}', section="clusterkit",
                 description="Atomic modular ClusterKit site policy as JSON; paths, approved nodes/groups and HCA belong to this cluster profile. Disabled until configured; cluster.clusterkit_profile takes precedence."))
register(Setting(name="RACK_CLUSTERKIT_PREVIEW_TTL_SEC", secret=False, ui_managed=True,
                 example="300", section="clusterkit",
                 description="Signed preview lifetime in seconds, clamped to 30-900. An expired preview cannot submit work."))
register(Setting(name="RACK_CLUSTERKIT_COLLECT_GRACE_SEC", secret=False, ui_managed=True,
                 example="300", section="clusterkit",
                 description="Collection grace after scheduler completion before a missing normalized summary becomes a failure."))

#
# The settings deployed sites carried in `rack-config.env`, now in `rackstatus.toml`. None of these is
# *required* — each has a working default — but they are declared so that `rackstatus.toml.example`
# documents them, so the loader stops warning "not a declared setting" about the ordinary case, and
# so their secrecy is resolved explicitly rather than by pattern.

register(
    Setting(
        name="RACK_EXEC_MODE",
        secret=False,
        example="ssh",
        section="connection",
        description=(
            "How commands reach the cluster: 'ssh' or 'local'. Deprecated in favour of the "
            "topology-based transport config, and 'ssh' is the default; kept because deployed "
            "sites still set it."
        ),
    )
)

register(
    Setting(
        name="RACK_STATUS_LOG_DIR",
        secret=False,
        example="/home/cmsupport/aihpc_val-main/validation/nemo",
        section="connection",
        description=(
            "Deprecated. The directory Rack Status ingests node logs from. This is one global "
            "value for every cluster at once, so a multi-tenant deployment would read a project's "
            "logs from a cluster it may not operate; set the log directory per cluster instead "
            "(the cluster's connection.log_dir). Kept only as a fallback for deployments still "
            "migrating, and it warns when used."
        ),
    )
)

register(
    Setting(
        name="RACK_BUG_REPORT_REMOTE_DIR",
        secret=False,
        example="/home/cmods/bringup-data/reports",
        section="connection",
        description=(
            "Base directory the bug-report and NVLMapper outputs are written to on each node "
            "(pdsh) and read from on the dashboard host (the Reports page). Point it at the "
            "project's own prefix -- a directory the service account can create and the "
            "dashboard host can see over the shared mount. Default /home/cmsupport/reports "
            "exists on only one appliance, so most deployments set this. NVLMapper's base "
            "(RACK_NVLMAPPER_REPORTS_BASE) defaults to it, so one value moves both."
        ),
    )
)

register(
    Setting(
        name="RACK_SSH_USER",
        secret=False,
        example="root",
        section="connection",
        description="Account for the SSH control connection to the head node.",
    )
)

register(
    Setting(
        name="RACK_SSH_PORT",
        toml_type=TOML_INT,
        secret=False,
        example="22",
        section="connection",
        description="Port for the SSH control connection.",
    )
)

register(
    Setting(
        name="RACK_SSH_KEY",
        # A path, not key material. `SENSITIVE_NAME_PATTERN` omits a bare `KEY` for exactly this
        # reason, but declaring it explicitly means the answer does not depend on that omission
        # surviving a future edit to the pattern.
        secret=False,
        example="/run/secrets/ssh_key",
        section="connection",
        description=(
            "Path to the private key for the control connection. The path, not the key: the key "
            "itself must never appear in this file."
        ),
    )
)

register(
    Setting(
        name="RACK_PLATFORM",
        secret=False,
        example="bcm",
        section="cluster",
        description="Cluster manager provider: 'bcm' (Base Command Manager) or 'maas'.",
    )
)

register(
    Setting(
        name="NVIS_FORCE_SETUP_WIZARD",
        toml_type=TOML_BOOL,
        secret=False,
        example="false",
        section="general",
        description=(
            "Re-open the first-run setup wizard even though a project is already configured. "
            "What `cli.py serve --setup-wizard` sets; normally left unset, since the wizard "
            "offers itself when no operator-created project exists."
        ),
    )
)


register(
    Setting(
        name="RACK_STATUS_DEMO",
        toml_type=TOML_BOOL,
        secret=False,
        example="false",
        section="general",
        description=(
            "Synthetic data, no cluster contact. True on a developer laptop, and it also exempts "
            "the connection settings from the required-setting preflight."
        ),
    )
)

register(
    Setting(
        name="NVIS_LOG_LEVEL",
        secret=False,
        example="INFO",
        section="general",
        description=(
            "Log level. Settable here because the config file is read before logging is "
            "configured, which is the reason the loader's own records appear slightly late."
        ),
    )
)

register(
    Setting(
        name="RACK_SLURM_SKIP_MODULE_LOAD",
        toml_type=TOML_BOOL,
        secret=False,
        example="true",
        section="cluster",
        description=(
            "Skip 'module load slurm' before scheduler commands, for sites where Slurm is already "
            "on PATH."
        ),
    )
)


def _data_dir_problem(value: str) -> str | None:
    from catalog.models import valid_data_dir

    if not value.strip() or valid_data_dir(value.strip()):
        return None
    return "must be an absolute path of plain path characters (no spaces, quotes, $, commas or '..')"


register(
    Setting(
        name="RACK_VALIDATION_CONTAINER_ROOT",
        secret=False,
        validate=_data_dir_problem,
        example="/cluster/containers",
        section="validation",
        description=(
            "Fallback for the cluster's shared container image store (the cluster's "
            "connection.shared_container_dir wins). Default /cluster/containers."
        ),
    )
)

register(
    Setting(
        name="NVIS_NODE_CONTAINER_DIR",
        secret=False,
        validate=_data_dir_problem,
        example="/opt/containers",
        section="validation",
        description=(
            "Fallback for where each compute node caches container images (the cluster's "
            "connection.node_container_dir wins). Staging copies images here and jobs run them "
            "from here. Default /tmp/containers."
        ),
    )
)

register(
    Setting(
        name="NEMO_SQSH_SRC",
        secret=False,
        example="/cluster/containers/nvidia+nemo+26.04.01-arm.sqsh",
        section="validation",
        description="Squashed NeMo container image used by the NeMo validation launchers.",
    )
)


register(
    Setting(
        name="LLMB_IMAGE_ROOT",
        secret=False,
        validate=_data_dir_problem,
        example="/cluster/llmb/images",
        section="validation",
        description=(
            "LLMB's own image store on the launch host, for presets that stage from it (GPT-OSS). "
            "Which file and its digest are the cluster's image catalog's. Default /cluster/llmb/images."
        ),
    )
)


def _tls_cert_configured() -> bool:
    """A certificate without its key is the asymmetric failure this row exists for.

    `tls_server.py` needs both. With only the cert, one path downgrades silently and the other
    crashes — so the key is required exactly when the cert is present, and not otherwise.
    """
    from config_env import _eget

    return bool((_eget("RACK_WEB_TLS_CERT") or "").strip())


RACK_WEB_TLS_KEY = register(
    Setting(
        name="RACK_WEB_TLS_KEY",
        secret=False,  # a path, not the key material; see SENSITIVE_NAME_PATTERN's omission of KEY
        required_when=_tls_cert_configured,
        example="/app/config/tls/server.key",
        section="tls",
        description=(
            "Private key for the TLS certificate. Required whenever RACK_WEB_TLS_CERT is set: a "
            "cert with no key downgrades one path silently and crashes the other."
        ),
    )
)
