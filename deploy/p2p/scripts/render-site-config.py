#!/usr/bin/env python3
"""Render a deterministic, secret-safe NVIS-P2P site overlay."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ROOT = "/home/hlakshminara/NVIS-P2P"
DEFAULT_IP = "10.41.112.3"

REQUIRED = (
    "NVIS_INSTALL_ROOT", "NVIS_SERVICE_USER", "NVIS_SERVICE_GROUP",
    "NVIS_SERVICE_UID", "NVIS_SERVICE_GID",
    "NVIS_MANAGER_IP", "NVIS_APPSERVER_IP", "NVIS_CONSOLE_BIND",
    "NVIS_INVENTORY_SOURCE", "NVIS_INVENTORY_PATTERN",
    "NVIS_CONTAINER_PATH", "NVIS_CATALOG_PATH", "NVIS_PEER_ROOT",
    "NVIS_OBJECT_STORE_DATA_PATH", "NVIS_SEED_CACHE_PATH",
    "NVIS_POSTGRES_PASSWORD_FILE", "NVIS_MANAGER_JWT_KEY_FILE",
    "NVIS_MINIO_ROOT_PASSWORD_FILE", "NVIS_DFSTORE_READONLY_ENV_FILE",
    "NVIS_SYSTEMD_DIR",
)


def die(message: str) -> "NoReturn":
    raise ValueError(message)


def parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            die(f"{path}:{number}: expected NAME=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            die(f"{path}:{number}: invalid variable name")
        parts = shlex.split(value, comments=True, posix=True)
        if len(parts) > 1:
            die(f"{path}:{number}: quote values containing whitespace")
        values[key] = parts[0] if parts else ""
    for key in REQUIRED:
        if not values.get(key):
            die(f"missing required setting: {key}")
    return values


def validate(values: dict[str, str]) -> tuple[str, int]:
    for key in ("NVIS_MANAGER_IP", "NVIS_APPSERVER_IP"):
        ipaddress.ip_address(values[key])
    try:
        bind_host, bind_port_text = values["NVIS_CONSOLE_BIND"].rsplit(":", 1)
        ipaddress.ip_address(bind_host)
        bind_port = int(bind_port_text)
    except Exception as exc:
        raise ValueError("NVIS_CONSOLE_BIND must be an IP:port") from exc
    if not 1 <= bind_port <= 65535:
        die("NVIS_CONSOLE_BIND port must be in 1..65535")
    for key in ("NVIS_SERVICE_USER", "NVIS_SERVICE_GROUP"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", values[key]):
            die(f"invalid account name in {key}")
    for key in ("NVIS_SERVICE_UID", "NVIS_SERVICE_GID"):
        if not values[key].isdigit() or not 0 < int(values[key]) < 2**31:
            die(f"{key} must be a positive numeric ID")
    for key in (
        "NVIS_INSTALL_ROOT", "NVIS_INVENTORY_SOURCE", "NVIS_CONTAINER_PATH",
        "NVIS_CATALOG_PATH", "NVIS_PEER_ROOT", "NVIS_OBJECT_STORE_DATA_PATH",
        "NVIS_SEED_CACHE_PATH", "NVIS_SYSTEMD_DIR",
    ):
        value = pathlib.Path(values[key])
        if not value.is_absolute() or any(char.isspace() for char in values[key]):
            die(f"{key} must be an absolute path without whitespace")
    install_root = pathlib.Path(values["NVIS_INSTALL_ROOT"])
    if install_root in {pathlib.Path("/"), pathlib.Path("/opt"), pathlib.Path("/home"), pathlib.Path("/tmp"), pathlib.Path("/var")}:
        die("NVIS_INSTALL_ROOT is too broad")
    if pathlib.Path(values["NVIS_CATALOG_PATH"]).suffix != ".json":
        die("NVIS_CATALOG_PATH must name a JSON file")
    for key in (
        "NVIS_POSTGRES_PASSWORD_FILE", "NVIS_MANAGER_JWT_KEY_FILE",
        "NVIS_MINIO_ROOT_PASSWORD_FILE", "NVIS_DFSTORE_READONLY_ENV_FILE",
    ):
        path = pathlib.Path(values[key])
        if not path.is_file():
            die(f"credential file is not readable: {key}")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            die(f"credential file must not be group/world accessible: {key}")
    dfstore_keys: set[str] = set()
    for line in pathlib.Path(values["NVIS_DFSTORE_READONLY_ENV_FILE"]).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            die("DFSTORE readonly environment contains a malformed line")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"DFSTORE_EXPORT_STORAGE_[A-Z0-9_]+", name) or not value:
            die("DFSTORE readonly environment contains an invalid assignment")
        dfstore_keys.add(name)
    required_dfstore = {
        "DFSTORE_EXPORT_STORAGE_ENDPOINT", "DFSTORE_EXPORT_STORAGE_ACCESS_KEY_ID",
        "DFSTORE_EXPORT_STORAGE_ACCESS_KEY_SECRET",
    }
    if not required_dfstore <= dfstore_keys:
        die("DFSTORE readonly environment file is missing required export settings")
    allowed_dfstore = required_dfstore | {
        "DFSTORE_EXPORT_STORAGE_REGION", "DFSTORE_EXPORT_STORAGE_INSECURE_SKIP_VERIFY",
    }
    if not dfstore_keys <= allowed_dfstore:
        die("DFSTORE readonly environment contains unsupported variables")
    cert = values.get("NVIS_OBJECT_STORE_CERT_FILE", "")
    key = values.get("NVIS_OBJECT_STORE_KEY_FILE", "")
    if bool(cert) != bool(key):
        die("NVIS_OBJECT_STORE_CERT_FILE and NVIS_OBJECT_STORE_KEY_FILE must be set together")
    for item in (cert, key):
        if item and not pathlib.Path(item).is_file():
            die("configured TLS file is not readable")
    if key and stat.S_IMODE(pathlib.Path(key).stat().st_mode) & 0o077:
        die("NVIS_OBJECT_STORE_KEY_FILE must not be group/world accessible")
    return bind_host, bind_port


def secret_text(path: str, label: str) -> str:
    value = pathlib.Path(path).read_text().rstrip("\r\n")
    if not value or "\n" in value or "\r" in value:
        die(f"{label} must contain exactly one non-empty line")
    return value


def inventory(values: dict[str, str]) -> list[str]:
    source = pathlib.Path(values["NVIS_INVENTORY_SOURCE"])
    paths = [source] if source.is_file() else sorted(source.glob(values["NVIS_INVENTORY_PATTERN"]))
    if not paths:
        die("inventory source/pattern matched no files")
    nodes: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            node = raw.strip()
            if node and not node.startswith("#"):
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", node):
                    die(f"invalid inventory hostname in {path}")
                nodes.add(node)
    if not nodes:
        die("inventory contains no nodes")
    return sorted(nodes)


def write(path: pathlib.Path, content: str | bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content if isinstance(content, bytes) else content.encode()
    path.write_bytes(data)
    path.chmod(mode)


def copy_replaced(source: pathlib.Path, destination: pathlib.Path, replacements: list[tuple[str, str]]) -> None:
    text = source.read_text()
    for old, new in replacements:
        text = text.replace(old, new)
    write(destination, text, stat.S_IMODE(source.stat().st_mode))


def render(values: dict[str, str], output: pathlib.Path) -> None:
    bind_host, bind_port = validate(values)
    current = f'{values["NVIS_INSTALL_ROOT"]}/current'
    manager_ip = values["NVIS_MANAGER_IP"]
    app_ip = values["NVIS_APPSERVER_IP"]
    user = values["NVIS_SERVICE_USER"]
    group = values["NVIS_SERVICE_GROUP"]
    peer_root = values["NVIS_PEER_ROOT"]

    output.mkdir(parents=True, exist_ok=False)
    manager = (ROOT / "configs/manager.example.yaml").read_text()
    manager = manager.replace("/opt/nvis-p2p", current)
    manager = manager.replace("REPLACE_WITH_CONTROL_PLANE_IP", manager_ip)
    manager = manager.replace("REPLACE_WITH_POSTGRES_PASSWORD", json.dumps(secret_text(values["NVIS_POSTGRES_PASSWORD_FILE"], "PostgreSQL password")))
    manager = manager.replace("REPLACE_WITH_RANDOM_JWT_SIGNING_KEY", json.dumps(secret_text(values["NVIS_MANAGER_JWT_KEY_FILE"], "manager JWT key")))
    write(output / "configs/manager.yaml", manager, 0o600)

    common = [(DEFAULT_ROOT, current), (DEFAULT_IP, app_ip)]
    copy_replaced(ROOT / "configs/scheduler.yaml", output / "configs/scheduler.yaml", common + [(f"addr: {app_ip}:12003", f"addr: {manager_ip}:12003")])
    for name in ("seed.yaml", "seed2.yaml"):
        text = (ROOT / "configs" / name).read_text().replace(DEFAULT_ROOT, current).replace(DEFAULT_IP, app_ip)
        text = text.replace(f"addr: http://{app_ip}:12003", f"addr: http://{manager_ip}:12003")
        text = text.replace("/cluster/OVA/NVIS-P2P/seed-cache", values["NVIS_SEED_CACHE_PATH"] + "/seed1")
        text = text.replace("/cluster/OVA/NVIS-P2P/seed2-cache", values["NVIS_SEED_CACHE_PATH"] + "/seed2")
        write(output / "configs" / name, text)
    copy_replaced(ROOT / "configs/peer.template.yaml", output / "configs/peer.template.yaml", [
        ("/opt/containers/.nvis-p2p", peer_root),
        (f"http://{DEFAULT_IP}:12003", f"http://{manager_ip}:12003"),
    ])
    copy_replaced(ROOT / "configs/seed-dynconfig.yaml", output / "configs/seed-dynconfig.yaml", [(DEFAULT_IP, app_ip)])
    for name in ("scheduler-dynconfig.yaml", "seed-fast-origin.conf"):
        shutil.copy2(ROOT / "configs" / name, output / "configs" / name)

    systemd_replacements = [
        (DEFAULT_ROOT, current), ("User=hlakshminara", f"User={user}"),
        ("Group=cmods", f"Group={group}"), ("/opt/containers/.nvis-p2p", peer_root),
    ]
    for source in sorted((ROOT / "deploy/systemd").glob("*.service")):
        copy_replaced(source, output / "systemd" / source.name, systemd_replacements)
    console_unit = output / "systemd/nvis-p2p-console-api.service"
    if console_unit.exists():
        text = console_unit.read_text()
        text = re.sub(r"^Environment=NVIS_P2P_ELIGIBLE_NODES=.*$", f"Environment=NVIS_P2P_ELIGIBLE_NODES={current}/site/inventory/eligible.nodes", text, flags=re.M)
        text = re.sub(r"^Environment=NVIS_P2P_OBJECT_STORE_HEALTH_URL=.*$", f"Environment=NVIS_P2P_OBJECT_STORE_HEALTH_URL=https://{app_ip}:19000/minio/health/live", text, flags=re.M)
        write(console_unit, text, 0o644)

    compose_sources = {
        "control-plane": ROOT / "deploy/control-plane/compose.yaml",
        "object-store": ROOT / "deploy/object-store/compose.yaml",
        "postgres": ROOT / "deploy/postgres/compose.yaml",
        "redis": ROOT / "deploy/redis/compose.yaml",
        "seed-origin": ROOT / "deploy/seed-origin/compose.yaml",
    }
    for name, source in compose_sources.items():
        replacements = common + [
            ("/cluster/containers", values["NVIS_CONTAINER_PATH"]),
            ("/cluster/OVA/NVIS-P2P/object-store/data", values["NVIS_OBJECT_STORE_DATA_PATH"]),
            ('user: "1024:1003"', f'user: "{values["NVIS_SERVICE_UID"]}:{values["NVIS_SERVICE_GID"]}"'),
        ]
        copy_replaced(source, output / "compose" / name / "compose.yaml", replacements)
    copy_replaced(ROOT / "deploy/seed-origin/nginx.conf", output / "compose/seed-origin/nginx.conf", [(DEFAULT_IP, app_ip)])
    for name in ("dfstore-policy.json", "dfstore-readonly-policy.json"):
        shutil.copy2(ROOT / "deploy/object-store" / name, output / "compose/object-store" / name)
    console_compose = ROOT / "deploy/console/compose.yaml"
    console_nginx = ROOT / "upstream/console/deploy/nginx.nvis.conf"
    if console_compose.is_file() and console_nginx.is_file():
        text = console_compose.read_text()
        text = text.replace("context: ../../upstream/console", f"context: {current}/upstream/console")
        text = re.sub(r"http://[^/]+:4300/nvis-p2p-health", f"http://{bind_host}:{bind_port}/nvis-p2p-health", text)
        write(output / "compose/console/compose.yaml", text)
        nginx = console_nginx.read_text()
        nginx = re.sub(r"(?:[ ]+listen [^;]+;\n){2}", f"    listen {bind_host}:{bind_port};\n", nginx, count=1)
        write(output / "console/nginx.nvis.conf", nginx)

    write(output / "inventory/eligible.nodes", "\n".join(inventory(values)) + "\n")
    (output / "compose/object-store/secrets").mkdir(parents=True, exist_ok=True)
    (output / "compose/postgres/secrets").mkdir(parents=True, exist_ok=True)
    (output / "secrets").mkdir(parents=True, exist_ok=True)
    shutil.copy2(values["NVIS_MINIO_ROOT_PASSWORD_FILE"], output / "compose/object-store/secrets/root_password")
    shutil.copy2(values["NVIS_POSTGRES_PASSWORD_FILE"], output / "compose/postgres/secrets/postgres_password")
    shutil.copy2(values["NVIS_DFSTORE_READONLY_ENV_FILE"], output / "secrets/dfstore-readonly.env")
    for path in (output / "compose/object-store/secrets/root_password", output / "compose/postgres/secrets/postgres_password", output / "secrets/dfstore-readonly.env"):
        path.chmod(0o600)
    cert = values.get("NVIS_OBJECT_STORE_CERT_FILE", "")
    if cert:
        shutil.copy2(cert, output / "compose/object-store/certs/public.crt")
        shutil.copy2(values["NVIS_OBJECT_STORE_KEY_FILE"], output / "compose/object-store/certs/private.key")
        (output / "compose/object-store/certs/private.key").chmod(0o600)
    else:
        (output / "compose/object-store/certs").mkdir(parents=True, exist_ok=True)
    write(output / "catalog-path", values["NVIS_CATALOG_PATH"] + "\n")


def manifest(root: pathlib.Path) -> None:
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if "secrets" in relative.parts or path.name in {"manager.yaml", "private.key"}:
            digest = "SECRET"
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"{relative}\t{path.stat().st_size}\t{digest}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True, type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    values = parse_env(args.env)
    if args.check or args.dry_run:
        with tempfile.TemporaryDirectory(prefix="nvis-p2p-render-check-") as temp:
            rendered = pathlib.Path(temp) / "site"
            render(values, rendered)
            if args.dry_run:
                manifest(rendered)
        print("site configuration check passed", file=sys.stderr)
        return 0
    if not args.output:
        parser.error("--output is required unless --check or --dry-run is used")
    render(values, args.output.resolve())
    manifest(args.output.resolve())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"render error: {exc}", file=sys.stderr)
        raise SystemExit(1)
