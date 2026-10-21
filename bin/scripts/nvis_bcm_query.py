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
``{"ok": false, "error": ..., "hint": ...}``. The caller falls back to cmsh on ``ok: false``.

Usage:
  nvis_bcm_query.py --query nodes --cert ~/.cm/cert.pem --key ~/.cm/cert.key \
                    --ca /cm/local/apps/cmd/etc/cacert.pem
  nvis_bcm_query.py --query attrs --type PhysicalNode      # discover real attribute names
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

#: Where pythoncm normally lives. A site with per-cluster copies passes --module-path instead.
DEFAULT_MODULE_PATHS = (
    "/cm/local/apps/cmd/pythoncm",
    "/cm/local/apps/python3/lib/python3*/site-packages",
)

#: Interface names/classes that denote the out-of-band controller. BCM names it rf0 on Dell
#: (Redfish) and ipmi0 elsewhere; the class name is the reliable signal when present.
_BMC_NAMES = {"rf0", "ipmi0", "bmc", "ipmi"}


def resolve_module_path(candidates: list[str]) -> str | None:
    """Add the first importable pythoncm location to ``sys.path``; return the one used.

    Accepts either the package's parent directory or the BCM install root, because BCM ships it
    under ``<root>/lib/python3.N/site-packages`` and only its own bundled interpreter has that
    wired in via ``sites.d``. Expanding it here means any python3.N on the box can run this.
    """
    for raw in candidates:
        for base in sorted(glob.glob(os.path.expanduser(raw)), reverse=True):
            for path in (base, *sorted(glob.glob(f"{base}/lib/python3*/site-packages"), reverse=True)):
                if os.path.isdir(os.path.join(path, "pythoncm")):
                    if path not in sys.path:
                        sys.path.insert(0, path)
                    return path
    return None


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
    return {
        "name": _attr(iface, "name", default=""),
        "kind": type(iface).__name__,
        "ip": _attr(iface, "ip", default=""),
        "mac": _attr(iface, "mac", "macAddress", default=""),
        "network": str(_attr(iface, "network", default="") or ""),
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
    if isinstance(value, (str, int, float)):
        return str(value)
    return ""


def shape_status(status: object) -> dict[str, object]:
    """A ``DeviceStatus`` as the fields the app reads, or {} when there is none.

    ``status`` is BCM's own word (UP, DOWN, INSTALLING, ...) -- the enum member's name, the same
    word ``cmsh ds`` prints in brackets. The health-check flag and the message are separate
    fields here, so a node that is UP with a failed health check stays UP, and a message that
    names some other host is that device's message, attached to nothing else.
    """
    if status is None:
        return {}
    value = getattr(status, "status", None)
    word = str(getattr(value, "name", "") or _scalar(value)).upper()
    if not word or word == "NONE":
        return {}
    return {
        "status": word,
        "health_check_failed": bool(getattr(status, "healthCheckFailed", False)),
        "status_message": _scalar(_attr(status, "infoMessage", "userMessage")),
    }


def device_statuses(cluster: object, devices: list[object]) -> dict[str, dict[str, object]]:
    """Device uuid -> :func:`shape_status`, for every device that has a status, in one RPC.

    One ``cmstatus.getStatusForDevices`` call answers for the whole fleet (2,287 devices in
    0.5 s on Nscale NC), where ``cmsh -c ds`` takes 12-23 s and returns free text.
    """
    out: dict[str, dict[str, object]] = {}
    for status in cluster.parallel.device_status(devices) or []:
        uuid = str(getattr(status, "ref_device_uuid", "") or "")
        shaped = shape_status(status)
        if uuid and shaped:
            out[uuid] = shaped
    return out


def shape_device(device: object, racks: dict[str, str] | None = None,
                 status: dict[str, object] | None = None) -> dict[str, object]:
    """One BCM device as plain JSON, including the BMC address the report needs.

    Rack placement takes some unpicking. ``device.rackPosition`` is a ``RackPosition`` entity
    carrying ``position`` (the U) and ``rack`` (a *uuid*, not a name), so *racks* maps rack uuid
    to rack name. Falling back to the cmsh-style "WH1D1:18" fused string keeps this working
    against releases that return a plain string instead. *status* is the device's
    :func:`shape_status`; without one the record carries no status fields at all.
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
        **(status or {}),
    }


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
    except Exception:  # noqa: BLE001 - openssl is the fallback for exactly this
        pass

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
    return Cluster(settings)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Query BCM CMDaemon via pythoncm; emit JSON.")
    ap.add_argument("--query", default="nodes",
                    choices=["nodes", "switches", "devices", "version", "attrs"])
    ap.add_argument("--type", default="PhysicalNode", help="entity type for --query attrs/devices")
    ap.add_argument("--module-path", action="append", default=[],
                    help="pythoncm location; repeatable. Per-cluster copies go here.")
    ap.add_argument("--host", default=os.environ.get("NVIS_BCM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("NVIS_BCM_PORT", "8081")))
    ap.add_argument("--cert", default=os.environ.get("NVIS_BCM_CERT", "~/.cm/cert.pem"))
    ap.add_argument("--key", default=os.environ.get("NVIS_BCM_KEY", "~/.cm/cert.key"))
    ap.add_argument("--ca", default=os.environ.get("NVIS_BCM_CA", "/cm/local/apps/cmd/etc/cacert.pem"))
    ap.add_argument("--ca-out", default=os.environ.get("NVIS_BCM_CA_OUT", "~/.cm/cluster-ca.pem"),
                    help="where --bootstrap-ca writes the pinned chain")
    ap.add_argument("--bootstrap-ca", action="store_true",
                    help="pin the CA chain the head presents, then use it")
    args = ap.parse_args(argv)

    # Pin on request, or automatically when the configured CA is unreadable — which is the
    # default state on a stock BCM, where cacert.pem is root-only. Set NVIS_BCM_CA_BOOTSTRAP=0
    # to require an out-of-band CA instead. The pinned fingerprints are reported every time so
    # the trust decision stays auditable rather than silent.
    ca_readable = bool(args.ca) and os.access(os.path.expanduser(args.ca), os.R_OK)
    pinned: dict[str, object] | None = None
    if args.bootstrap_ca or (
        not ca_readable and os.environ.get("NVIS_BCM_CA_BOOTSTRAP", "1") != "0"
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
    except Exception as exc:  # noqa: BLE001 - the caller falls back to cmsh on any failure
        detail = f"{type(exc).__name__}: {exc}"
        hint = None
        if "CERTIFICATE_VERIFY_FAILED" in detail:
            hint = ("the head presents a self-signed cluster cert; --ca must point at a CA file "
                    "this user can read (/cm/local/apps/cmd/etc/cacert.pem is root-only by default)")
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

    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _run_query(cluster, args: argparse.Namespace, used: str, started: float) -> dict[str, object]:
    status_error = ""
    if args.query == "version":
        data: object = {"cmdaemon": str(getattr(cluster, "version", "") or "")}
    elif args.query == "attrs":
        # Discovery: the one honest way to learn a given BCM release's attribute spellings.
        sample = (cluster.get_by_type(args.type) or [None])[0]
        data = sorted(a for a in dir(sample) if not a.startswith("_")) if sample else []
    else:
        wanted = {"nodes": ["PhysicalNode"], "switches": ["Switch"],
                  "devices": [args.type]}[args.query]
        # Rack placement is a uuid reference on the device, so resolve rack names once for the
        # whole fleet rather than per device.
        racks = {}
        for rack in cluster.get_by_type("Rack") or []:
            uuid = _scalar(_attr(rack, "uuid"))
            name = _scalar(_attr(rack, "name", "hostname"))
            if uuid and name:
                racks[uuid] = name
        devices = []
        for type_name in wanted:
            devices.extend(cluster.get_by_type(type_name) or [])
        # Status is enrichment: a release without the call, or a failed one, still answers with
        # the inventory, and the caller falls back to cmsh ds for state.
        try:
            statuses = device_statuses(cluster, devices)
        except Exception as exc:  # noqa: BLE001 - reported in the payload, never fatal
            statuses, status_error = {}, f"{type(exc).__name__}: {exc}"
        data = [shape_device(d, racks, statuses.get(str(getattr(d, "uuid", "") or ""))) for d in devices]
        data.sort(key=lambda d: d["hostname"])

    payload: dict[str, object] = {
        "ok": True,
        "query": args.query,
        "count": len(data) if isinstance(data, list) else 1,
        "elapsed_seconds": round(time.time() - started, 3),
        "pythoncm_path": used,
        "data": data,
    }
    if status_error:
        payload["status_error"] = status_error
    return payload


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
