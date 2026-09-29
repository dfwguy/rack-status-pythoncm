#!/usr/bin/env python3
"""Atomic, noninteractive NVIS-P2P release installer with rollback records."""

from __future__ import annotations

import argparse
import datetime
import grp
import json
import os
import pathlib
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
RENDERER = ROOT / "scripts/render-site-config.py"


def env_values(path: pathlib.Path, validate: bool = True) -> dict[str, str]:
    namespace: dict[str, str] = {}
    # Reuse the renderer's strict parser without importing a hyphenated module.
    command = [sys.executable, str(RENDERER), "--env", str(path), "--check"]
    if validate:
        subprocess.run(command, check=True)
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        import shlex
        parts = shlex.split(value, comments=True, posix=True)
        namespace[key.strip()] = parts[0] if parts else ""
    return namespace


def release_name() -> str:
    for raw in (ROOT / "release/components.env").read_text().splitlines():
        if raw.startswith("NVIS_P2P_RELEASE="):
            return raw.split("=", 1)[1].strip()
    raise RuntimeError("NVIS_P2P_RELEASE is missing")


def safe_install_root(value: str) -> pathlib.Path:
    root = pathlib.Path(value)
    if not root.is_absolute() or any(char.isspace() for char in value):
        raise RuntimeError("NVIS_INSTALL_ROOT must be an absolute path without whitespace")
    if root in {pathlib.Path("/"), pathlib.Path("/opt"), pathlib.Path("/home"), pathlib.Path("/tmp"), pathlib.Path("/var")}:
        raise RuntimeError("NVIS_INSTALL_ROOT is too broad")
    return root


def release_files() -> list[pathlib.Path]:
    output = subprocess.check_output([str(ROOT / "scripts/list-release-inputs.sh")], text=True)
    return [pathlib.Path(value) for value in output.splitlines() if value]


def copy_release(destination: pathlib.Path) -> None:
    for relative in release_files():
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def atomic_symlink(target: pathlib.Path, link: pathlib.Path) -> None:
    temporary = link.with_name(f".{link.name}.new-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, link)


def install_units(site: pathlib.Path, systemd_dir: pathlib.Path, rollback: pathlib.Path) -> list[dict[str, str | bool]]:
    systemd_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str | bool]] = []
    backup_dir = rollback / "systemd"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((site / "systemd").glob("*.service")):
        target = systemd_dir / source.name
        existed = target.exists() or target.is_symlink()
        record: dict[str, str | bool] = {"target": str(target), "existed": existed}
        if existed:
            backup = backup_dir / source.name
            shutil.copy2(target, backup, follow_symlinks=False)
            record["backup"] = str(backup)
        temporary = systemd_dir / f".{source.name}.new-{os.getpid()}"
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        records.append(record)
    return records


def install_catalog(staged: pathlib.Path, catalog_json: pathlib.Path, rollback: pathlib.Path) -> list[dict[str, str | bool]]:
    catalog_json.parent.mkdir(parents=True, exist_ok=True)
    catalog_tsv = catalog_json.with_name("images.tsv")
    records: list[dict[str, str | bool]] = []
    for source, target in ((staged / "catalog/images.json", catalog_json), (staged / "catalog/images.tsv", catalog_tsv)):
        existed = target.exists() or target.is_symlink()
        record: dict[str, str | bool] = {"target": str(target), "existed": existed}
        if existed:
            backup = rollback / "catalog" / target.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup, follow_symlinks=False)
            record["backup"] = str(backup)
        temporary = target.with_name(f".{target.name}.new-{os.getpid()}")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        source.unlink()
        source.symlink_to(target)
        records.append(record)
    return records


def restore_records(records: list[dict[str, str | bool]]) -> None:
    for item in records:
        target = pathlib.Path(str(item["target"]))
        if item["existed"]:
            shutil.copy2(str(item["backup"]), target, follow_symlinks=False)
        elif target.exists() or target.is_symlink():
            target.unlink()


def rollback_transaction(install_root: pathlib.Path, transaction: str) -> None:
    transaction_dir = install_root / "rollback" / transaction
    record_path = transaction_dir / "transaction.json"
    record = json.loads(record_path.read_text())
    current = install_root / "current"
    previous = record.get("previous_current")
    if previous:
        atomic_symlink(pathlib.Path(previous), current)
    elif current.is_symlink():
        current.unlink()
    restore_records(record.get("units", []))
    restore_records(record.get("catalog", []))
    replaced = record.get("replaced_release")
    release_path = pathlib.Path(record["release_path"])
    if replaced and pathlib.Path(replaced).exists():
        if release_path.exists():
            parked = transaction_dir / "rolled-back-release"
            os.replace(release_path, parked)
        os.replace(pathlib.Path(replaced), release_path)
    print(f"rollback restored transaction={transaction}; no services were reloaded or restarted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=pathlib.Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-systemd", action="store_true")
    parser.add_argument("--systemd-dir", type=pathlib.Path)
    parser.add_argument("--rollback", metavar="TRANSACTION")
    args = parser.parse_args()
    if args.rollback:
        if not args.env:
            parser.error("--rollback requires --env to identify NVIS_INSTALL_ROOT")
        values = env_values(args.env, validate=False)
        if not values.get("NVIS_INSTALL_ROOT"):
            raise RuntimeError("NVIS_INSTALL_ROOT is missing")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9]+", args.rollback):
            raise RuntimeError("invalid rollback transaction identifier")
        rollback_transaction(safe_install_root(values["NVIS_INSTALL_ROOT"]), args.rollback)
        return 0
    if not args.env:
        parser.error("--env is required")

    values = env_values(args.env)
    subprocess.run([str(ROOT / "scripts/verify-release-inputs.sh")], check=True)
    install_root = safe_install_root(values["NVIS_INSTALL_ROOT"])
    systemd_dir = args.systemd_dir or pathlib.Path(values["NVIS_SYSTEMD_DIR"])
    version = release_name()
    release_path = install_root / "releases" / version
    current = install_root / "current"
    if current.exists() and not current.is_symlink():
        raise RuntimeError(f"refusing to replace non-symlink: {current}")

    if args.check:
        print(f"installer check passed: release={version}")
        return 0
    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="nvis-p2p-install-dry-run-") as temp:
            staged = pathlib.Path(temp) / version
            staged.mkdir()
            copy_release(staged)
            subprocess.run([sys.executable, str(RENDERER), "--env", str(args.env), "--output", str(staged / "site")], check=True, stdout=subprocess.DEVNULL)
        print(f"DRY_RUN install release={version} destination={release_path}")
        print(f"DRY_RUN current_symlink={current}")
        print(f"DRY_RUN systemd={'skipped' if args.no_systemd else systemd_dir}")
        print("DRY_RUN no services would be reloaded, enabled, started, stopped, or restarted")
        return 0

    uid = pwd.getpwnam(values["NVIS_SERVICE_USER"]).pw_uid
    gid = grp.getgrnam(values["NVIS_SERVICE_GROUP"]).gr_gid
    if uid != int(values["NVIS_SERVICE_UID"]) or gid != int(values["NVIS_SERVICE_GID"]):
        raise RuntimeError("configured service UID/GID do not match service user/group")
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    transaction = f"{timestamp}-{os.getpid()}"
    rollback = install_root / "rollback" / transaction
    rollback.mkdir(parents=True, exist_ok=False)
    releases = install_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    staged = releases / f".{version}.new-{os.getpid()}"
    staged.mkdir()
    record: dict[str, object] = {
        "transaction": transaction, "release_path": str(release_path),
        "previous_current": str(current.resolve()) if current.is_symlink() else None,
        "units": [], "catalog": [], "replaced_release": None,
    }
    try:
        copy_release(staged)
        subprocess.run([sys.executable, str(RENDERER), "--env", str(args.env), "--output", str(staged / "site")], check=True, stdout=subprocess.DEVNULL)
        secret_source = staged / "site/secrets/dfstore-readonly.env"
        secret_target = staged / "deploy/object-store/secrets/dfstore-readonly.env"
        secret_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(secret_source, secret_target)
        secret_target.chmod(0o600)
        for sensitive in (
            staged / "site/configs/manager.yaml", secret_target,
            staged / "site/secrets/dfstore-readonly.env",
            staged / "site/compose/object-store/secrets/root_password",
            staged / "site/compose/postgres/secrets/postgres_password",
        ):
            os.chown(sensitive, uid, gid)
        catalog_path = pathlib.Path(values["NVIS_CATALOG_PATH"])
        if str(catalog_path).startswith(str(install_root / "current") + os.sep):
            raise RuntimeError("NVIS_CATALOG_PATH must be outside NVIS_INSTALL_ROOT/current")
        record["catalog"] = install_catalog(staged, catalog_path, rollback)
        rendered_nginx = staged / "site/console/nginx.nvis.conf"
        if rendered_nginx.exists():
            nginx_target = staged / "upstream/console/deploy/nginx.nvis.conf"
            nginx_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rendered_nginx, nginx_target)
        for directory in (staged / "state", staged / "state/manager/logs", staged / "state/scheduler/logs", staged / "state/seed/logs", staged / "state/seed2/logs"):
            directory.mkdir(parents=True, exist_ok=True)
            os.chown(directory, uid, gid)
        if release_path.exists():
            replaced = rollback / "replaced-release"
            os.replace(release_path, replaced)
            record["replaced_release"] = str(replaced)
        os.replace(staged, release_path)
        atomic_symlink(release_path, current)
        if not args.no_systemd:
            record["units"] = install_units(release_path / "site", systemd_dir, rollback)
        (rollback / "transaction.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    except Exception:
        if staged.exists():
            shutil.rmtree(staged)
        restore_records(record.get("units", []))
        restore_records(record.get("catalog", []))
        if record.get("previous_current"):
            atomic_symlink(pathlib.Path(str(record["previous_current"])), current)
        elif current.is_symlink():
            current.unlink()
        replaced = record.get("replaced_release")
        if replaced and pathlib.Path(str(replaced)).exists():
            if release_path.exists():
                failed = rollback / "failed-release"
                os.replace(release_path, failed)
            os.replace(pathlib.Path(str(replaced)), release_path)
        raise
    print(f"installed release={version} transaction={transaction}")
    print("configuration and units were installed; no services were reloaded, enabled, started, stopped, or restarted")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"install error: {exc}", file=sys.stderr)
        raise SystemExit(1)
