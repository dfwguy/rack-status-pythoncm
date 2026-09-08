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
"""The Greenlight launcher — `serve` to run, `check` to find out what a run would do.

**Why this exists.** Runtime decisions were spread across `start.sh` and `bin/run-local-auth.sh`:
which paths to read, whether to serve TLS, which mode to run in, and — until this file — whether an
administrator and a policy get created. A decision made in bash is invisible to the app that
depends on it, untestable by the suite that verifies the app, and duplicated in every other launcher.
This module owns those decisions; the scripts keep the ones that genuinely precede Python, such as
building a virtualenv and compiling the frontend.

**Two rules shape the whole file.**

*It sets environment variables and nothing else.* Precedence already has an owner — the
`config_registry` / `config_launch` / `config_env` ladder, whose contract includes an empty string
being a deliberate value rather than an absence. A CLI that resolved its own precedence would fork
that contract, and the fork would show up as one setting behaving differently depending on whether
it arrived by flag or by file. So a flag writes `os.environ` and the ladder decides the rest. It is
also why this uses `argparse` rather than `click`: `click`'s `envvar=` support is the reason to
reach for it, and measured here it treats an empty variable as absent — the opposite of our rule.

*It imports no first-party module at module scope.* `config_launch.load()` runs during `import app`
and must see the flags already in the environment, and `config_env` snapshots `os.environ` when it
is first imported. Anything imported here before the flags are applied would read the wrong
environment. Every first-party import is therefore inside a function, after `_apply`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

#: Flag → environment variable. The single table, so a flag cannot drift from the setting it feeds
#: and `check` can print provenance without a second list to maintain.
PATH_SETTINGS: dict[str, str] = {
    "config": "NVIS_GREENLIGHT_CONFIG",
    "identity_file": "NVIS_IDENTITY_FILE",
    "policy_file": "RACK_WEB_POLICY_FILE",
    "catalog_file": "NVIS_CATALOG_FILE",
    "signing_key_dir": "RACK_WEB_SIGNING_KEY_DIR",
    "tls_cert": "RACK_WEB_TLS_CERT",
    "tls_key": "RACK_WEB_TLS_KEY",
}

EXIT_OK = 0
EXIT_FAULT = 1
#: Same code `config_preflight` uses, and for the same reason: `EX_CONFIG` from sysexits.h tells an
#: init system the configuration is wrong rather than the program having crashed, so a supervisor
#: does not restart-loop a deployment that will never come up.
EXIT_CONFIG = 78


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="greenlight",
        description="Run NVIS Greenlight, or report what running it would do.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("serve", "Bootstrap what is missing and start the server."),
        ("check", "Report what `serve` would do, without creating an admin or a policy."),
    ):
        cmd = sub.add_parser(name, help=help_text, description=help_text)
        _add_path_options(cmd)
        cmd.add_argument(
            "--host",
            metavar="ADDR",
            help="Bind address. Sets RACK_WEB_BIND_ADDR.",
        )
        cmd.add_argument(
            "--port",
            type=int,
            metavar="N",
            help="Bind port. Sets RACK_WEB_PORT.",
        )

    serve = sub.choices["serve"]
    serve.add_argument(
        "--no-bootstrap",
        action="store_true",
        help=(
            "Refuse to start rather than creating a missing administrator or policy. For a "
            "deployment that provisions both out of band and wants a missing one to be an error."
        ),
    )

    check = sub.choices["check"]
    check.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the report as JSON for a deploy gate to parse.",
    )
    return parser


def _add_path_options(cmd: argparse.ArgumentParser) -> None:
    """The path overrides, shared by both subcommands.

    Every one of these already has an environment variable, and the flag exists because a path is
    the thing an operator most often needs to move for one run — a second deployment on one host, a
    dry run against a copy of the store, a scratch policy file — and editing the environment to do
    it is how the wrong file gets written.
    """
    group = cmd.add_argument_group(
        "paths",
        "Where to read configuration and state. A flag wins over the matching environment "
        "variable; both win over the built-in default.",
    )
    for flag, env in (
        ("--config", "NVIS_GREENLIGHT_CONFIG"),
        ("--identity-file", "NVIS_IDENTITY_FILE"),
        ("--policy-file", "RACK_WEB_POLICY_FILE"),
        ("--catalog-file", "NVIS_CATALOG_FILE"),
        ("--signing-key-dir", "RACK_WEB_SIGNING_KEY_DIR"),
        ("--tls-cert", "RACK_WEB_TLS_CERT"),
        ("--tls-key", "RACK_WEB_TLS_KEY"),
    ):
        group.add_argument(flag, metavar="PATH", help=f"Sets {env}.")


#: Defaults `start.sh` used to export, moved here so the app owns its own runtime decisions.
#:
#: They were invisible to the app that depends on them, untestable by the suite that verifies it,
#: and re-derived by every other launcher. `setdefault` semantics throughout: a real environment
#: variable, a `greenlight.toml` entry or a flag still wins, exactly as before.
CLUSTER_DEFAULTS: dict[str, str] = {
    "VALIDATION_TEST": "nccl_allreduce,llm",
    "NEMO_SQSH_SRC": "/home/cmsupport/nemo-25.11.01.sqsh",
    "RACK_STATUS_LOG_DIR": "/home/cmsupport/aihpc_val-main/validation/nemo",
}


def apply_runtime_defaults(platform: str | None = None) -> dict[str, str]:
    """Demo-vs-cluster mode and the cluster-only defaults. Returns what it set.

    **Mode is inferred from the platform, which is what `start.sh` did and why it belonged here.**
    macOS is a developer's laptop with no head node to talk to, so demo; anything else is assumed to
    be a cluster host. Inference only sets a *default* — `RACK_STATUS_DEMO=0` on a Mac is honoured,
    which is how someone tests against a real cluster over SSH from their desk.

    The three cluster defaults are applied only outside demo mode, because they name absolute paths
    on a head node's filesystem and a demo run has no head node for them to be wrong about.
    """
    applied: dict[str, str] = {}
    system = platform or sys.platform
    demo_default = "1" if system == "darwin" else "0"
    if not (os.environ.get("RACK_STATUS_DEMO") or "").strip():
        os.environ["RACK_STATUS_DEMO"] = demo_default
        applied["RACK_STATUS_DEMO"] = demo_default

    if (os.environ.get("RACK_STATUS_DEMO") or "").strip() != "1":
        for name, value in CLUSTER_DEFAULTS.items():
            if not (os.environ.get(name) or "").strip():
                os.environ[name] = value
                applied[name] = value
    return applied


def _apply(args: argparse.Namespace) -> dict[str, str]:
    """Put the supplied flags into the environment. Returns what it set, for the report.

    Absolute paths, because the server may be started from anywhere and several of these are read
    long after startup by code with no memory of the original working directory.
    """
    applied: dict[str, str] = dict(apply_runtime_defaults())
    for attr, env in PATH_SETTINGS.items():
        value = getattr(args, attr, None)
        if value:
            resolved = str(Path(value).expanduser().resolve())
            os.environ[env] = resolved
            applied[env] = resolved
    if getattr(args, "host", None):
        os.environ["RACK_WEB_BIND_ADDR"] = args.host
        applied["RACK_WEB_BIND_ADDR"] = args.host
    if getattr(args, "port", None):
        os.environ["RACK_WEB_PORT"] = str(args.port)
        applied["RACK_WEB_PORT"] = str(args.port)
    return applied


def _plan() -> dict:
    """What a launch would create, and whether it could.

    **Creates no identity store, no credential and no policy** — the three things it reports on,
    and the reason it is safe to point at a live deployment. It reaches that by testing for the
    identity file's existence rather than opening it: `IdentityStore.__init__` calls `touch()`, so
    constructing one to ask "is there an administrator" would create the store and then truthfully
    report it as empty.

    It is **not** side-effect free, and the exception is worth naming rather than glossing. The
    required-setting preflight resolves the active cluster, and the catalog seeds itself on first
    access, so a `check` against a path with no catalog will write one. That is the same idempotent
    seed any start performs, it holds no secret, and avoiding it would mean the preflight could not
    consult the cluster overlay — which is precisely what makes the `RACK_SSH_HOST` verdict correct
    on a site that supplies the host through its cluster record.
    """
    import config_launch
    import config_preflight
    from authz import store as policy_store
    from identity import bootstrap as identity_bootstrap
    from identity import store as identity_store_module
    from identity.models import IdentityError
    from identity.store import IdentityStore

    config = config_launch.load()

    faults: list[str] = []
    admin_exists: bool | None = None
    policy_count: int | None = None

    # Existence first, and opening only if it is already there. `IdentityStore.__init__` calls
    # `touch()`, so merely constructing one *creates* the store — which would make `check` write
    # the very file it is reporting on, and report "administrator absent" about a store it had
    # just brought into being. Caught by running it: the first version left an `identity.json`
    # behind in a directory it was only supposed to inspect.
    identity_path = Path(identity_store_module.identity_file())
    if not identity_path.is_file():
        admin_exists = False
    else:
        store = None
        try:
            store = IdentityStore(str(identity_path))
            admin_exists = not identity_bootstrap.needed(store)
        except (OSError, ValueError, IdentityError) as exc:
            # OSError: permissions or a bad path. ValueError: TinyDB's JSON parse. IdentityError: a
            # record the current schema refuses. Anything else is a defect and should surface as a
            # traceback rather than as advice about the file.
            faults.append(f"identity store unreadable: {exc}")
        finally:
            if store is not None:
                store.close()

    try:
        policy_count = len(policy_store.read_all())
    except (policy_store.PolicyStoreError, OSError) as exc:
        # `PolicyStoreError` already wraps the unreadable and unparseable cases with the path in
        # the message; `OSError` covers a directory we cannot traverse to reach it.
        faults.append(f"policy set unreadable: {exc}")

    # Reported rather than raised: `check` exists to list everything wrong at once, and a
    # required-setting fault should not hide a missing administrator. `collect()` returns
    # `(faults, warnings)`; only the first refuses a boot, so only the first is a fault here.
    required_faults, required_warnings = config_preflight.collect()
    faults.extend(_describe(f) for f in required_faults)

    loaded = config.path if config.path and config.path.is_file() else None
    return {
        "config_file": str(loaded) if loaded else None,
        "config_settings_from_file": len(config.materialized),
        "warnings": [_describe(w) for w in required_warnings],
        "identity_file": str(identity_path),
        "policy_file": str(policy_store.path()),
        "would_create_admin": admin_exists is False,
        "would_create_policy": admin_exists is False and policy_count == 0,
        "admin_exists": admin_exists,
        "policy_count": policy_count,
        "enforcing": bool(policy_count),
        "tls": bool(os.environ.get("RACK_WEB_TLS_CERT") and os.environ.get("RACK_WEB_TLS_KEY")),
        "faults": faults,
    }


def _describe(fault: object) -> str:
    """One line per fault, naming the setting and why it is required."""
    name = getattr(fault, "name", None)
    condition = getattr(fault, "condition", "") or ""
    detail = getattr(fault, "detail", "") or ""
    if not name:
        return str(fault)
    return f"{name}: {condition}{f' ({detail})' if detail else ''}"


def _report(plan: dict, applied: dict[str, str], as_json: bool) -> int:
    if as_json:
        print(json.dumps({"applied": applied, **plan}, indent=2, sort_keys=True))
        return EXIT_CONFIG if plan["faults"] else EXIT_OK

    def line(label: str, value: object) -> None:
        print(f"  {label:22} {value}")

    print("greenlight check — no administrator, policy or credential has been created")
    print()
    line(
        "config file",
        f"{plan['config_file']} ({plan['config_settings_from_file']} setting(s))"
        if plan["config_file"]
        else "(none; environment only)",
    )
    line("identity store", plan["identity_file"])
    line("policy file", plan["policy_file"])
    line("TLS", "configured" if plan["tls"] else "NOT configured")
    print()
    line("administrator", "exists" if plan["admin_exists"] else "absent")
    line("policies", plan["policy_count"] if plan["policy_count"] is not None else "unreadable")
    line(
        "authorization",
        "ENFORCING (default-deny)" if plan["enforcing"] else "permissive (allow-all)",
    )
    print()
    print("  a `serve` from here would:")
    if plan["would_create_admin"]:
        print("    - create the `admin` account and print a one-time password")
    if plan["would_create_policy"]:
        print("    - write the `administrators` policy and attach it, switching to default-deny")
    if not plan["would_create_admin"] and not plan["would_create_policy"]:
        print("    - create nothing; the administrator and policy set are already in place")

    if plan["warnings"]:
        print()
        print(f"  {len(plan['warnings'])} setting(s) could not be determined:")
        for warning in plan["warnings"]:
            print(f"    - {warning}")

    if plan["faults"]:
        print()
        print(f"  {len(plan['faults'])} fault(s) would refuse the launch:")
        for fault in plan["faults"]:
            print(f"    - {fault}")
        return EXIT_CONFIG
    return EXIT_OK


def _serve(args: argparse.Namespace) -> int:
    """Hand off to gunicorn, which is what the container runs.

    Not the Werkzeug development server: over TLS it races its own shutdown against chunked
    responses and Chrome reports a truncated stream (`start.sh` carries the full account). Reusing
    `gunicorn.conf.py` is what keeps a local run on the same TLS floor as production.
    """
    # Before exec, so the refusal comes from the command the operator ran. Gunicorn's own
    # `on_starting` hook checks the same thing for the container and `start.sh`, which do not come
    # through here — one gate, two entry points, and neither trusting the other to have run it.
    import startup_gate

    startup_gate.prepare(bootstrap=not args.no_bootstrap)

    port = os.environ.get("RACK_WEB_PORT") or "5050"
    bind = f"{os.environ.get('RACK_WEB_BIND_ADDR') or '0.0.0.0'}:{port}"
    argv = [
        sys.executable, "-m", "gunicorn", "-c", "gunicorn.conf.py",
        "--workers", "1",
        # One worker is load-bearing rather than conservative: SRP challenge state is in-process,
        # and a second worker splits the table so roughly half of all logins fail.
        "--threads", os.environ.get("GUNICORN_THREADS") or "24",
        "--worker-class", "gthread",
        "--timeout", os.environ.get("GUNICORN_TIMEOUT") or "600",
        "--graceful-timeout", "30", "--keep-alive", "5",
        "--access-logfile", "-", "--pythonpath", "backend",
        "--bind", bind,
    ]
    cert, key = os.environ.get("RACK_WEB_TLS_CERT"), os.environ.get("RACK_WEB_TLS_KEY")
    if cert and key:
        argv += ["--certfile", cert, "--keyfile", key]
    argv.append("app:app")

    os.execv(sys.executable, argv)
    return EXIT_FAULT  # unreachable: execv replaces this process


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    applied = _apply(args)

    if args.command == "check":
        return _report(_plan(), applied, args.as_json)
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
