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
"""nvis_bcm_query — query BCM's CMDaemon through pythoncm and print JSON on stdout.

Runs as a short-lived process on the machine whose pythoncm matches its own cmdaemon — normally
the head node, reached through the app's existing transport. That isolation is the whole design,
not an implementation detail:

* **Version skew cannot happen.** pythoncm is version-locked to cmdaemon and is not on PyPI, so
  one long-lived multi-cluster process cannot hold the right library for every cluster at once.
  Here the library is chosen per invocation by ``--module-path``, and the interpreter exits
  afterwards, so "unloading" is process exit rather than an attempt to purge ``sys.modules``
  (which cannot really unload a module: live objects keep their classes, and two versions in one
  interpreter yield same-named classes that fail ``isinstance`` against each other).
* **No global mutable state.** The module path is an argument, not a symlink flipped per request,
  so two clusters queried concurrently cannot race and silently answer from the wrong library.
* **No secret leaves the head.** The client cert, key and CA stay where they already are.
* **Nothing of BCM's is redistributed** into an image we ship to other customers.

The cost is one interpreter start (~0.3 s) per call, against the 12-23 s a ``cmsh -c`` invocation
costs on this cluster — and a whole-fleet query is a single call.

Output is always a single JSON object: ``{"ok": true, "query": ..., "data": [...]}`` or
``{"ok": false, "error": ..., "hint": ...}``. A failure is explicit and machine-readable; the
PythonCM deployment does not silently switch to a different control plane.

Usage:
  nvis_bcm_query.py --query nodes --cert ~/.cm/cert.pem --key ~/.cm/cert.key \
                    --ca /cm/local/apps/cmd/etc/cacert.pem
  nvis_bcm_query.py --query attrs --type PhysicalNode      # discover real attribute names
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import sys
import time
from uuid import UUID

#: Where pythoncm normally lives. A site with per-cluster copies passes --module-path instead.
DEFAULT_MODULE_PATHS = (
    "/cm/local/apps/cmd/pythoncm",
    "/cm/local/apps/python3/lib/python3*/site-packages",
)

#: Interface names/classes that denote the out-of-band controller. BCM names it rf0 on Dell
#: (Redfish) and ipmi0 elsewhere; the class name is the reliable signal when present.
_BMC_NAMES = {"rf0", "ipmi0", "bmc", "ipmi"}

# Mutating queries remain gated on a BCM web/API login cookie.  BCM 11 permits the
# device-status and drain-status read RPCs with the client certificate on this cluster;
# pre-emptively rejecting those reads made every rack look unknown/down even though the
# controller would return the physical state.
_AUTHENTICATED_QUERIES = {
    "power", "drain", "undrain",
}


def _natural_path_key(path: str) -> tuple[object, ...]:
    """Sort versioned install paths numerically (``pythoncm-11`` after ``pythoncm-9``)."""
    return tuple(int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path))


def resolve_module_path(candidates: list[str]) -> str | None:
    """Add the first importable pythoncm location to ``sys.path``; return the one used.

    Accepts either the package's parent directory or the BCM install root, because BCM ships it
    under ``<root>/lib/python3.N/site-packages`` and only its own bundled interpreter has that
    wired in via ``sites.d``. Expanding it here means any python3.N on the box can run this.
    """
    for raw in candidates:
        for base in sorted(
            glob.glob(os.path.expanduser(raw)), key=_natural_path_key, reverse=True
        ):
            site_paths = sorted(
                glob.glob(f"{base}/lib/python3*/site-packages"),
                key=_natural_path_key,
                reverse=True,
            )
            for path in (base, *site_paths):
                if os.path.isdir(os.path.join(path, "pythoncm")):
                    if path not in sys.path:
                        sys.path.insert(0, path)
                    return path
    # A BCM-bundled interpreter may already have the matching package on its site path. Do not
    # declare it missing merely because a site installed it somewhere outside our known layouts.
    found = importlib.util.find_spec("pythoncm")
    if found and found.submodule_search_locations:
        return os.path.dirname(next(iter(found.submodule_search_locations)))
    return None


def default_ca_path() -> str:
    """Return pythoncm's shipped public CA bundle, which is readable on stock BCM installs."""
    candidates = sorted(
        glob.glob(
            "/cm/local/apps/cmd/pythoncm/lib/python3*/site-packages/pythoncm/etc/cacert.pem"
        ),
        key=_natural_path_key,
        reverse=True,
    )
    return candidates[0] if candidates else "/cm/local/apps/cmd/etc/cacert.pem"


def _attr(obj: object, *names: str, default: object = None) -> object:
    """First present, non-empty attribute among *names*.

    pythoncm's entity attribute spellings vary across BCM releases (``rackPosition`` vs
    ``rackposition``), and this runs against clusters we cannot all test on. Trying the known
    spellings beats pinning one and returning null on every other version; ``--query attrs``
    exists to settle it for a given cluster.
    """
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return default


def _iface_dict(iface: object) -> dict[str, object]:
    network = _attr(iface, "network")
    return {
        "name": _attr(iface, "name", default=""),
        "kind": type(iface).__name__,
        "ip": _attr(iface, "ip", default=""),
        "mac": _attr(iface, "mac", "macAddress", default=""),
        "network": _scalar(network) or _scalar(_attr(network, "name")),
    }


def _is_bmc(iface: dict[str, object]) -> bool:
    return "bmc" in str(iface["kind"]).lower() or str(iface["name"]).lower() in _BMC_NAMES


def _scalar(value: object) -> str:
    """A plain string, or "" for a pythoncm entity.

    Entity-valued attributes stringify to a multi-line JSON dump of the whole object, so a naive
    ``str()`` silently lands a blob in the report. ``rack`` and ``rackPosition`` are both
    entities on BCM 11.0 — this is how that was found.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, UUID)):
        return str(value)
    return ""


def shape_device(device: object, racks: dict[str, str] | None = None) -> dict[str, object]:
    """One BCM device as plain JSON, including the BMC address the report needs.

    Rack placement takes some unpicking. ``device.rackPosition`` is a ``RackPosition`` entity
    carrying ``position`` (the U) and ``rack`` (a *uuid*, not a name), so *racks* maps rack uuid
    to rack name. Falling back to the cmsh-style "WH1D1:18" fused string keeps this working
    against releases that return a plain string instead.
    """
    racks = racks or {}
    interfaces = [_iface_dict(i) for i in (_attr(device, "interfaces", default=[]) or [])]
    bmc = next((i for i in interfaces if _is_bmc(i)), None)

    rack_id = rack_unit = ""
    position = _attr(device, "rackPosition", "rackposition")
    if position is not None:
        # BCM 11.0 hands back a RackPosition entity; older releases hand back a bare number.
        rack_unit = _scalar(position) or _scalar(_attr(position, "position"))
        rack_id = racks.get(_scalar(_attr(position, "rack")), "")
    rack = _attr(device, "rack")
    if not rack_id:
        # A plain string here is cmsh's fused "<rack>:<unit>" form; an entity has a name.
        raw = _scalar(rack)
        if raw:
            rack_id, _, fused_unit = raw.partition(":")
            rack_unit = rack_unit or fused_unit
        else:
            rack_id = _scalar(_attr(rack, "name")) or racks.get(_scalar(_attr(rack, "uuid")), "")

    return {
        "hostname": _scalar(_attr(device, "hostname")),
        "type": type(device).__name__,
        "rack_id": rack_id,
        "rack_unit": rack_unit,
        "mac": _scalar(_attr(device, "mac", "macAddress")),
        "serial": _scalar(_attr(device, "serialNumber", "serialnumber", "serial")),
        "category": _scalar(_attr(device, "category")),
        "bmc_ip": str((bmc or {}).get("ip") or ""),
        "bmc_mac": str((bmc or {}).get("mac") or ""),
        "interfaces": interfaces,
    }


def _enum_text(value: object) -> str:
    """Return a stable enum token across pythoncm versions, or a plain scalar string."""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    text = _scalar(value)
    if "." in text and text.rsplit(".", 1)[-1].isupper():
        return text.rsplit(".", 1)[-1]
    return text


def shape_status(status: object) -> dict[str, object]:
    """One :class:`pythoncm.entity.DeviceStatus` without entity-valued fields."""
    device = _attr(status, "parent", "device")
    return {
        "hostname": _scalar(_attr(device, "hostname", "name")),
        "device_uuid": _scalar(_attr(status, "ref_device_uuid")),
        "status": _enum_text(_attr(status, "final_status", "status", default="UNDEFINED")),
        "reported_status": _enum_text(_attr(status, "reportedStatus", "reported_status")),
        "closed": bool(_attr(status, "closed", default=False)),
        "muted": bool(_attr(status, "muted", default=False)),
        "burning": bool(_attr(status, "burning", default=False)),
        "message": _scalar(_attr(status, "userMessage", "user_message", "message")),
    }


def _read_secret(path: str | None) -> str:
    if not path:
        return ""
    expanded = os.path.expanduser(path)
    if not os.access(expanded, os.R_OK):
        raise FileNotFoundError(f"secret file not readable: {path}")
    with open(expanded, encoding="utf-8") as handle:
        return handle.read().strip()


def _configure_auth(settings: object, args: argparse.Namespace) -> str:
    """Apply a cookie or obtain one without ever putting the secret on the command line."""
    cookie = os.environ.get("NVIS_BCM_COOKIE", "") or _read_secret(args.cookie_file)
    if cookie:
        settings.cookie = cookie
        return "cookie"

    username = args.username or os.environ.get("NVIS_BCM_USERNAME", "")
    password = os.environ.get("NVIS_BCM_PASSWORD", "") or _read_secret(args.password_file)
    if username or password:
        if not username or not password:
            raise PermissionError("both BCM username and password are required")
        if not settings.get_cookie(username, password):
            raise PermissionError("BCM username/password authentication was rejected")
        return "login"
    return "certificate"


def fetch_ca_chain(host: str, port: int, timeout: int = 8) -> list[str]:
    """The CA certificates the head presents, leaf excluded, newest anchor last.

    A CA *certificate* is public — only its private key is secret — and cmdaemon hands the whole
    chain over during the handshake. That matters because BCM ships the cluster CA as
    ``/cm/local/apps/cmd/etc/cacert.pem`` mode 600 root-only, so a service account otherwise
    cannot verify the head at all. Reading it off the wire needs no root and no admin ticket,
    and is strictly better than the alternative of disabling verification (which would present
    our client certificate to any peer that could intercept the port).

    Python's ``ssl`` only exposes the full chain from 3.13, so this falls back to openssl.
    """
    import ssl
    import subprocess

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # we are collecting the chain, not trusting it yet
    try:
        import socket

        with socket.create_connection((host, port), timeout=timeout) as raw, \
                context.wrap_socket(raw, server_hostname=host) as tls:
            if hasattr(tls, "get_unverified_chain"):
                chain = [ssl.DER_cert_to_PEM_cert(c.public_bytes(1)) if hasattr(c, "public_bytes")
                         else c for c in tls.get_unverified_chain()]
                if chain:
                    return chain[1:]
    except Exception as exc:  # noqa: BLE001 - openssl is the fallback for exactly this
        print(f"warning: ssl chain collection failed, trying openssl: {exc}", file=sys.stderr)

    out = subprocess.run(
        ["openssl", "s_client", "-showcerts", "-connect", f"{host}:{port}"],
        input="", capture_output=True, text=True, timeout=timeout + 10, check=False,
    ).stdout
    certs, current = [], []
    for line in out.splitlines():
        if "BEGIN CERTIFICATE" in line:
            current = [line]
        elif "END CERTIFICATE" in line and current:
            current.append(line)
            certs.append("\n".join(current) + "\n")
            current = []
        elif current:
            current.append(line)
    return certs[1:]


def bootstrap_ca(host: str, port: int, out_path: str) -> dict[str, object]:
    """Write the head's CA chain to *out_path* and report what was pinned.

    Both the cluster CA and its anchor are written: pinning the cluster's own intermediate alone
    fails verification with "unable to get issuer certificate", since OpenSSL still has to build
    a path to a trusted root.
    """
    chain = fetch_ca_chain(host, port)
    if not chain:
        raise RuntimeError(f"{host}:{port} presented no CA chain to pin")

    target = os.path.expanduser(out_path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("".join(chain))
    os.chmod(target, 0o600)

    return {"ca_file": target, "certificates": len(chain),
            "subjects": [_cert_subject(pem) for pem in chain],
            "fingerprints_sha256": [_cert_fingerprint(pem) for pem in chain]}


def _cert_field(pem: str, *args: str) -> str:
    import subprocess

    try:
        out = subprocess.run(["openssl", "x509", "-noout", *args], input=pem,
                             capture_output=True, text=True, timeout=15, check=False)
        return out.stdout.strip().split("=", 1)[-1].strip()
    except Exception:  # noqa: BLE001 - provenance is descriptive; a missing openssl is not fatal
        return ""


def _cert_subject(pem: str) -> str:
    return _cert_field(pem, "-subject")


def _cert_fingerprint(pem: str) -> str:
    return _cert_field(pem, "-fingerprint", "-sha256")


def _connect(args: argparse.Namespace):
    # Credentials first, before importing anything: readability is a cheap local fact, and it is
    # the failure this cluster actually hits. Checking it up front also means a broken library
    # install cannot mask a missing CA (or the reverse) in the message the operator reads.
    #
    # Readability, not existence: the cluster CA ships root-owned mode 600, so a service account
    # sees the path and then dies deep inside the TLS handshake with a bare PermissionError.
    unreadable = [
        p for p in (args.cert, args.key, args.ca)
        if p and not os.access(os.path.expanduser(p), os.R_OK)
    ]
    if unreadable:
        raise FileNotFoundError(f"credential file(s) not readable: {', '.join(unreadable)}")

    from pythoncm.cluster import Cluster
    from pythoncm.settings import Settings

    settings = Settings(
        host=args.host,
        port=args.port,
        cert_file=os.path.expanduser(args.cert),
        key_file=os.path.expanduser(args.key),
        ca_file=os.path.expanduser(args.ca) if args.ca else None,
        check_hostname=False,
    )
    auth_mode = _configure_auth(settings, args)
    if args.query in _AUTHENTICATED_QUERIES and auth_mode == "certificate":
        raise PermissionError(f"{args.query} requires a BCM login cookie")
    cluster = Cluster(settings)
    # Cluster.check_api() runs during construction. It compares pythoncm's generated API hash
    # with CMDaemon and deliberately rejects version skew before any entity is interpreted.
    cluster._nvis_auth_mode = auth_mode
    return cluster


def build_parser() -> argparse.ArgumentParser:
    """Return the shared query parser used by the CLI and persistent broker."""
    ap = argparse.ArgumentParser(description="Query BCM CMDaemon via pythoncm; emit JSON.")
    ap.add_argument(
        "--query",
        default="nodes",
        choices=[
            "nodes", "switches", "devices", "racks", "topology", "status", "node-states", "rf0", "version",
            "capabilities", "attrs", "power", "drain", "undrain", "drain-status",
        ],
    )
    ap.add_argument("--type", default="PhysicalNode", help="entity type for --query attrs/devices")
    ap.add_argument("--module-path", action="append", default=[],
                    help="pythoncm location; repeatable. Per-cluster copies go here.")
    ap.add_argument("--host", default=os.environ.get("NVIS_BCM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("NVIS_BCM_PORT", "8081")))
    ap.add_argument("--cert", default=os.environ.get("NVIS_BCM_CERT", "~/.cm/cert.pem"))
    ap.add_argument("--key", default=os.environ.get("NVIS_BCM_KEY", "~/.cm/cert.key"))
    ap.add_argument("--ca", default=os.environ.get("NVIS_BCM_CA") or default_ca_path())
    ap.add_argument("--ca-out", default=os.environ.get("NVIS_BCM_CA_OUT", "~/.cm/cluster-ca.pem"),
                    help="where --bootstrap-ca writes the pinned chain")
    ap.add_argument("--username", default=os.environ.get("NVIS_BCM_USERNAME", ""),
                    help="BCM login name; password is read only from environment/file")
    ap.add_argument("--password-file", default=os.environ.get("NVIS_BCM_PASSWORD_FILE", ""),
                    help="read BCM password from this head-node file")
    ap.add_argument("--cookie-file", default=os.environ.get("NVIS_BCM_COOKIE_FILE", ""),
                    help="read a pre-issued BCM Cookie header value from this head-node file")
    ap.add_argument("--target", default="", help="BCM device/node name range")
    ap.add_argument("--action", choices=["on", "off", "reset", "status"], default="status")
    ap.add_argument("--reason", default="", help="drain reason")
    ap.add_argument("--force", action="store_true", help="include closed devices in power operations")
    ap.add_argument("--operation-timeout", type=float, default=120.0,
                    help="seconds to wait for a parallel power operation")
    ap.add_argument("--format", choices=["json", "cmsh"], default="json",
                    help="JSON API output or compatibility text for existing Rack Status parsers")
    ap.add_argument("--bootstrap-ca", action="store_true",
                    help="pin the CA chain the head presents, then use it")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.format == "cmsh" and args.query not in {"racks", "status", "rf0"}:
        return _fail("unsupported output format", f"--format cmsh is not supported for {args.query}")

    # CA collection is explicit by default. Automatically trusting whichever endpoint currently
    # answers on port 8081 would turn a missing readable CA into a silent trust-on-first-use
    # decision. A site that deliberately accepts TOFU can set NVIS_BCM_CA_BOOTSTRAP=1.
    ca_readable = bool(args.ca) and os.access(os.path.expanduser(args.ca), os.R_OK)
    pinned: dict[str, object] | None = None
    if args.bootstrap_ca or (
        not ca_readable and os.environ.get("NVIS_BCM_CA_BOOTSTRAP", "0") == "1"
    ):
        try:
            pinned = bootstrap_ca(args.host, args.port, args.ca_out)
            args.ca = str(pinned["ca_file"])
        except Exception as exc:  # noqa: BLE001 - fall through to the normal credential error
            if args.bootstrap_ca:
                return _fail("ca bootstrap failed", f"{type(exc).__name__}: {exc}")
            print(f"warning: CA bootstrap failed: {exc}", file=sys.stderr)

    if args.bootstrap_ca and args.query == "nodes":
        # Bootstrapping alone is a legitimate one-shot: prove the pin verifies and stop.
        probe = _probe_after_bootstrap(args)
        json.dump({"ok": probe[0], "query": "bootstrap-ca", "pinned": pinned,
                   **({} if probe[0] else {"error": probe[1]})}, sys.stdout)
        sys.stdout.write("\n")
        return 0 if probe[0] else 2

    started = time.time()
    candidates = args.module_path or list(DEFAULT_MODULE_PATHS)
    if os.environ.get("NVIS_PYTHONCM_PATH"):
        candidates.insert(0, os.environ["NVIS_PYTHONCM_PATH"])

    used = resolve_module_path(candidates)
    if not used:
        return _fail("pythoncm not found", f"searched: {', '.join(candidates)}",
                     hint="install cmdaemon-pythoncm, or pass --module-path")

    try:
        cluster = _connect(args)
    except FileNotFoundError as exc:
        return _fail("credentials unavailable", str(exc),
                     hint="the cluster CA is often root-only; ask the BCM admin for a readable copy")
    except PermissionError as exc:
        return _fail(
            "authentication failed",
            str(exc),
            hint=("certificate authentication can read entities, but status/power/workload RPCs "
                  "require a BCM login cookie; configure a head-node password/cookie file"),
        )
    except Exception as exc:  # noqa: BLE001 - always return structured connection failure
        detail = f"{type(exc).__name__}: {exc}"
        hint = None
        if "CERTIFICATE_VERIFY_FAILED" in detail:
            hint = ("the head presents a self-signed cluster cert; --ca must point at a CA file "
                    "this user can read (/cm/local/apps/cmd/etc/cacert.pem is root-only by default)")
        elif "Invalid API hash" in detail or "verifyAPI" in detail:
            hint = ("pythoncm and CMDaemon APIs do not match; run the pythoncm shipped by this "
                    "head node or set --module-path to its matching install")
            return _fail("pythoncm/cmdaemon version mismatch", detail, hint=hint)
        return _fail("cmdaemon connection failed", detail, hint=hint)

    try:
        payload = _run_query(cluster, args, used, started)
    except Exception as exc:  # noqa: BLE001 - always answer with JSON so the caller can decide
        return _fail("query failed", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            cluster.disconnect()
        except Exception as exc:  # noqa: BLE001 - must not mask a good result on the way out
            # stderr, never stdout: the caller parses stdout as a single JSON document.
            print(f"warning: disconnect failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    if args.format == "cmsh":
        _write_cmsh(payload, args.query)
    else:
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    return 0 if payload.get("ok") else 2


def _racks_by_uuid(cluster) -> dict[str, str]:
    racks: dict[str, str] = {}
    for rack in cluster.get_by_type("Rack") or []:
        uuid = _scalar(_attr(rack, "uuid"))
        name = _scalar(_attr(rack, "name", "hostname"))
        if uuid and name:
            racks[uuid] = name
    return racks


def _shape_node_groups(cluster) -> list[dict[str, object]]:
    """BCM NodeGroup names with their resolved physical-node hostnames.

    ``NodeGroup.nodes`` is a vector of resolved entities on BCM 11, but older
    generated clients can expose UUID values. Resolve both forms without ever
    serializing an entity's multi-line JSON representation.
    """
    groups: list[dict[str, object]] = []
    for group in cluster.get_by_type("NodeGroup") or []:
        name = _scalar(_attr(group, "name"))
        if not name:
            continue
        nodes: set[str] = set()
        for node in _attr(group, "nodes", default=[]) or []:
            hostname = _scalar(_attr(node, "hostname", "name")) or _name_for_uuid(
                cluster, node
            )
            if hostname:
                nodes.add(hostname)
        groups.append({"name": name, "nodes": sorted(nodes, key=_natural_path_key)})
    groups.sort(key=lambda group: _natural_path_key(str(group["name"])))
    return groups


def _shape_topology(cluster) -> dict[str, object]:
    """Slow-changing rack and NodeGroup topology in one CMDaemon session."""
    racks = [
        {
            "name": _scalar(_attr(rack, "name", "hostname")),
            "uuid": _scalar(_attr(rack, "uuid")),
            "serial": _scalar(_attr(rack, "serialNumber", "serial")),
        }
        for rack in cluster.get_by_type("Rack") or []
    ]
    racks.sort(key=lambda item: _natural_path_key(str(item["name"])))
    # NodeGroup membership is operator-maintained and can temporarily omit a
    # rack even though its PhysicalNodes still exist. Include the hostname
    # universe so consumers do not silently drop those racks from inventory.
    nodes = sorted(
        {
            hostname
            for node in cluster.get_by_type("PhysicalNode") or []
            if (hostname := _scalar(_attr(node, "hostname", "name")))
        },
        key=_natural_path_key,
    )
    return {"racks": racks, "node_groups": _shape_node_groups(cluster), "nodes": nodes}


def _select_devices(cluster, target: str, *, nodes_only: bool = False) -> list[object]:
    """Resolve BCM's supported name-range syntax and reject partial/ambiguous selections."""
    if not target.strip():
        raise ValueError("--target is required")
    from pythoncm.device_selection import DeviceSelection

    selection = DeviceSelection(cluster)
    selection.add_devices_in_text_range(target)
    devices = selection.get_sorted_by_name()
    if nodes_only:
        invalid = [d for d in devices if not callable(getattr(d, "drain", None))]
        if invalid:
            names = ", ".join(_scalar(_attr(d, "hostname", "name")) for d in invalid[:5])
            raise ValueError(f"target includes non-node device(s): {names}")
    if not devices:
        raise ValueError(f"target matched no devices: {target}")
    return devices


def _name_for_uuid(cluster, value: object) -> str:
    try:
        entity = cluster.get_by_uuid(value)
    except (TypeError, ValueError):
        entity = None
    return _scalar(_attr(entity, "hostname", "name")) or _scalar(value)


def _shape_operation_state(cluster, state: object) -> dict[str, object]:
    device = _attr(state, "device", "ref_device_uuid")
    return {
        "hostname": _name_for_uuid(cluster, device),
        "state": _enum_text(_attr(state, "state")),
        "operation": _enum_text(_attr(state, "operation")),
        "info": _scalar(_attr(state, "info")),
    }


def _shape_drain_result(cluster, result: object) -> dict[str, object]:
    node_uuids = list(_attr(result, "ref_node_uuids", default=[]) or [])
    states = list(_attr(result, "result", default=[]) or [])
    reasons = list(_attr(result, "reason", default=[]) or [])
    return {
        "success": bool(_attr(result, "success", default=False)),
        "nodes": [
            {
                "hostname": _name_for_uuid(cluster, uuid),
                "state": _enum_text(states[index]) if index < len(states) else "UNKNOWN",
                "reason": _scalar(reasons[index]) if index < len(reasons) else "",
            }
            for index, uuid in enumerate(node_uuids)
        ],
    }


def _power(cluster, args: argparse.Namespace) -> dict[str, object]:
    devices = _select_devices(cluster, args.target)
    operation = getattr(cluster.parallel, f"power_{args.action}")
    result = operation(
        devices,
        force=args.force,
        wait=True,
        timeout=max(1.0, args.operation_timeout),
    )
    if isinstance(result, tuple):
        completed, states = result
        shaped_states = [
            _shape_operation_state(cluster, state) for state in states or [] if state is not None
        ]
        accepted = True
    else:
        completed = accepted = bool(result)
        shaped_states = []
    return {
        "accepted": accepted,
        "completed": bool(completed),
        "action": args.action,
        "target": args.target,
        "devices": [_scalar(_attr(d, "hostname", "name")) for d in devices],
        "states": shaped_states,
    }


def _drain(cluster, args: argparse.Namespace, *, drain: bool) -> dict[str, object]:
    nodes = _select_devices(cluster, args.target, nodes_only=True)
    if drain:
        results = cluster.parallel.drain(nodes, reason=args.reason)
    else:
        results = cluster.parallel.undrain(nodes)
    shaped = [_shape_drain_result(cluster, result) for result in results or []]
    return {
        "target": args.target,
        "action": "drain" if drain else "undrain",
        "accepted": bool(shaped) and all(item["success"] for item in shaped),
        "results": shaped,
    }


def _scheduler_nodes(cluster, target: str = "") -> list[dict[str, object]]:
    """Build PythonCM eligibility from BCM device and drain-status RPCs.

    ProgramRunner allocation ownership is overlaid by Rack Status from its durable
    PythonCM activity registry. This helper deliberately does not query WlmCluster
    or ``cluster.workload`` because Nscale's WLM is Slurm.
    """
    devices = (
        _select_devices(cluster, target, nodes_only=True)
        if target.strip()
        else list(cluster.get_by_type("PhysicalNode") or [])
    )
    by_uuid = {_scalar(_attr(device, "uuid")): device for device in devices}
    device_states: dict[str, dict[str, object]] = {}
    for status in cluster.parallel.device_status(devices):
        device_states[_scalar(_attr(status, "ref_device_uuid"))] = shape_status(status)

    drain_states: dict[str, dict[str, str]] = {}
    for result in cluster.parallel.drain_status(devices) or []:
        shaped = _shape_drain_result(cluster, result)
        for row in shaped["nodes"]:
            hostname = str(row.get("hostname") or "")
            current = drain_states.get(hostname)
            state = str(row.get("state") or "UNKNOWN").upper()
            # One node can appear once per WLM. Any draining/drained result wins.
            if current is None or state in {"DRAINING", "DRAINED"}:
                drain_states[hostname] = {
                    "state": state,
                    "reason": str(row.get("reason") or ""),
                }

    rows: list[dict[str, object]] = []
    for uuid, device in by_uuid.items():
        hostname = _scalar(_attr(device, "hostname", "name"))
        cm_state = str(device_states.get(uuid, {}).get("status") or "UNDEFINED").upper()
        has_drain_status = hostname in drain_states
        drain = drain_states.get(hostname, {"state": "UNKNOWN", "reason": ""})
        drain_state = drain["state"]
        running_jobs: list[str] = []
        if cm_state in {"DOWN", "INSTALLER_FAILED", "INSTALLER_UNREACHABLE"}:
            scheduler_state = "down"
        elif drain_state == "DRAINED":
            scheduler_state = "drain"
        elif drain_state == "DRAINING":
            scheduler_state = "draining"
        elif cm_state == "UP" and drain_state == "UNDRAINED":
            scheduler_state = "idle"
        elif cm_state == "UP" and not has_drain_status:
            # Physical health is known even when this BCM/Slurm integration does
            # not publish a per-node drain result.  "up" is intentionally not
            # "idle": scheduler_runtime only admits idle nodes to operations.
            scheduler_state = "up"
        else:
            scheduler_state = "unknown"
        rows.append({
            "hostname": hostname,
            "device_status": cm_state,
            "drain_state": drain_state,
            "scheduler_state": scheduler_state,
            "reason": drain["reason"],
            "jobs": running_jobs,
        })
    rows.sort(key=lambda row: row["hostname"])
    return rows


def _run_query(cluster, args: argparse.Namespace, used: str, started: float) -> dict[str, object]:
    query = args.query
    if query == "version":
        try:
            cmdaemon = str(cluster.settings.get_version() or "")
        except Exception:  # noqa: BLE001 - API hash was already checked during Cluster()
            cmdaemon = ""
        try:
            from importlib.metadata import version

            pythoncm_version = version("pythoncm")
        except Exception:  # noqa: BLE001 - older bundled installs omit dist-info
            pythoncm_version = ""
        data: object = {"cmdaemon": cmdaemon, "pythoncm": pythoncm_version, "api_compatible": True}
    elif query == "capabilities":
        auth_mode = str(getattr(cluster, "_nvis_auth_mode", "certificate"))
        data = {
            "auth_mode": auth_mode,
            "authenticated_operations": auth_mode != "certificate",
            "api_compatible": True,
            "entity_reads": True,
            "device_status": callable(getattr(cluster.parallel, "device_status", None)),
            "power": all(callable(getattr(cluster.parallel, f"power_{op}", None))
                         for op in ("on", "off", "reset", "status")),
            "drain": callable(getattr(cluster.parallel, "drain", None)),
            "undrain": callable(getattr(cluster.parallel, "undrain", None)),
            "programrunner_execute": callable(getattr(cluster.parallel, "execute", None)),
            "programrunner_status": callable(
                getattr(cluster.parallel, "active_command_status", None)
            ),
        }
    elif query == "attrs":
        sample = (cluster.get_by_type(args.type) or [None])[0]
        data = sorted(a for a in dir(sample) if not a.startswith("_")) if sample else []
    elif query == "topology":
        data = _shape_topology(cluster)
    elif query == "racks":
        data = [
            {"name": _scalar(_attr(rack, "name", "hostname")),
             "uuid": _scalar(_attr(rack, "uuid")),
             "serial": _scalar(_attr(rack, "serialNumber", "serial"))}
            for rack in cluster.get_by_type("Rack") or []
        ]
        data.sort(key=lambda item: item["name"])
    elif query == "status":
        devices = list(cluster.get_by_type(args.type or "PhysicalNode") or [])
        data = [shape_status(status) for status in cluster.parallel.device_status(devices)]
        data.sort(key=lambda item: item["hostname"])
    elif query == "node-states":
        data = _scheduler_nodes(cluster, args.target)
    elif query == "rf0":
        racks = _racks_by_uuid(cluster)
        data = [shape_device(d, racks) for d in cluster.get_by_type("PhysicalNode") or []]
        data = [{"hostname": item["hostname"], "ip": item["bmc_ip"], "mac": item["bmc_mac"]}
                for item in data if item["hostname"] and item["bmc_ip"]]
        data.sort(key=lambda item: item["hostname"])
    elif query in {"nodes", "switches", "devices"}:
        wanted = {"nodes": ["PhysicalNode"], "switches": ["Switch"],
                  "devices": [args.type]}[query]
        racks = _racks_by_uuid(cluster)
        devices = []
        for type_name in wanted:
            devices.extend(cluster.get_by_type(type_name) or [])
        data = [shape_device(d, racks) for d in devices]
        data.sort(key=lambda item: item["hostname"])
    elif query == "power":
        data = _power(cluster, args)
    elif query == "drain":
        data = _drain(cluster, args, drain=True)
    elif query == "undrain":
        data = _drain(cluster, args, drain=False)
    elif query == "drain-status":
        nodes = _select_devices(cluster, args.target, nodes_only=True)
        data = [_shape_drain_result(cluster, result)
                for result in cluster.parallel.drain_status(nodes) or []]
    else:  # argparse makes this unreachable; keep refusal explicit for imported use.
        raise ValueError(f"unsupported query: {query}")

    operation_ok = True
    if query in {"power", "drain", "undrain"} and isinstance(data, dict):
        operation_ok = bool(data.get("accepted"))
        if query == "power":
            operation_ok = operation_ok and bool(data.get("completed"))

    return {
        "ok": operation_ok,
        "query": query,
        "count": len(data) if isinstance(data, list) else 1,
        "elapsed_seconds": round(time.time() - started, 3),
        "pythoncm_path": used,
        "data": data,
    }


def _write_cmsh(payload: dict[str, object], query: str) -> None:
    """Render only the legacy text shapes still consumed by existing Rack Status parsers."""
    data = payload.get("data")
    if not isinstance(data, list):
        raise TypeError(f"--format cmsh is not supported for {query}")
    if query == "racks":
        for rack in data:
            print(str(rack.get("name") or ""))
    elif query == "status":
        for status in data:
            print(f"{status.get('hostname', '')} [ {status.get('status', 'UNDEFINED')} ]")
    elif query == "rf0":
        for interface in data:
            print(str(interface.get("hostname") or ""))
            print(str(interface.get("ip") or ""))
    else:
        raise ValueError(f"--format cmsh is not supported for {query}")


def _probe_after_bootstrap(args: argparse.Namespace) -> tuple[bool, str]:
    """Confirm a freshly pinned CA actually verifies, so --bootstrap-ca cannot report success
    on a chain that does not work."""
    try:
        cluster = _connect(args)
    except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
        return False, f"{type(exc).__name__}: {exc}"
    try:
        cluster.disconnect()
    except Exception as exc:  # noqa: BLE001 - verified already; teardown is cosmetic
        print(f"warning: disconnect failed: {exc}", file=sys.stderr)
    return True, ""


def _fail(error: str, detail: str = "", *, hint: str | None = None) -> int:
    body: dict[str, object] = {"ok": False, "error": error, "detail": detail}
    if hint:
        body["hint"] = hint
    json.dump(body, sys.stdout)
    sys.stdout.write("\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
