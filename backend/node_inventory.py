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
"""Fleet hardware/firmware inventory: raw probe output -> the report's ``firmware_inventory``.

``node_report.py`` used to fill this block from :data:`node_report.ASSUMPTIONS` because nothing
measured it, so every shipped report carried all-zero MACs, ``TBD`` firmware, and a single
placeholder node. This module is the measurement.

Split on purpose: everything here is a pure function over probe text, so the parsing, the
normalisation and the baseline derivation are testable without a cluster. Orchestration — the
fan-out, cmsh, Redfish — lives in the workflow step that calls this.

Two schema mismatches are handled here rather than at the edges, because both are cases where
the honest value cannot be represented and the substitution must be *recorded*, not hidden:

* ``pcieBdf`` allows a 4-hex-digit PCI domain. GB300 GPUs live at ``00000008:06:00.0``. The
  domain is narrowed to 4 digits, which is lossless while the high half is zero.
* ``macAddress`` demands 6 octets. The compute rails are InfiniBand, whose port identity is a
  64-bit GUID (and whose IPoIB link address is 20 bytes). The EUI-48 embedded in the GUID is
  the port's real hardware MAC, so it is recovered rather than substituting an unrelated
  Ethernet MAC from the management bond.

Every such substitution appends to a deviations list that the report publishes, so a reader can
tell a measured value from a reshaped one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

_log = logging.getLogger(__name__)

#: Marker for a field the probe did not return. Mirrors ``node_report.TBD`` without importing
#: it, so this module stays usable on its own.
TBD = "TBD"

_BDF_RE = re.compile(r"^([0-9a-fA-F]+):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-9a-fA-F])$")

#: Deviation notices. Deliberately value-free: the same reshape applies to every GPU on every
#: node, so a per-value message would put thousands of near-identical lines in a customer
#: report. The report says what was reshaped and why, once.
DEVIATION_BDF_NARROWED = (
    "GPU pcie_bdf values were narrowed to a 4-hex-digit PCI domain to satisfy the schema's "
    "pcieBdf pattern; GB300 GPUs enumerate in a wide domain (e.g. 00000008:06:00.0 -> "
    "0008:06:00.0). The high half is zero, so no device is misidentified."
)
DEVIATION_IB_MAC = (
    "nics[].mac carries the EUI-48 recovered from each InfiniBand port GUID. The schema's "
    "macAddress requires 6 octets and has no field for the 64-bit GUID that actually identifies "
    "an IB port; the rails' IPoIB link address is 20 bytes and would not validate."
)


#: Concurrent SSH hops for the sweep, bounded by the head's sshd, not by our patience.
#:
#: Every hop is a session channel multiplexed over one ControlMaster connection to the head, so
#: the real ceiling is that host's ``MaxSessions`` — unset on Nscale TX, therefore the OpenSSH
#: default of 10. Running 32 workers was three times over it: sessions past the limit came back
#: "Session open refused by peer", and because the whole fleet was queued behind the same master
#: the two built-in retries could not drain fast enough. A sweep silently lost 655 of 2013 nodes
#: that way, and the earlier run that lost only 4 was luck, not correctness.
#:
#: 8 leaves headroom for the app's other traffic on the same master. It costs time — roughly
#: 8 minutes for 2000 nodes at ~2 s each — which is the right trade for a customer deliverable
#: that now reports its own progress. Raise it only alongside the head's MaxSessions.
DEFAULT_MAX_WORKERS = 8


def collect_fleet(
    nodes: list[str],
    *,
    progress: Callable[[int, int, str], None] | None = None,
    max_workers: int | None = None,
    timeout: int = 120,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Probe every node in parallel; return ``(records, errors)``.

    Nodes that fail are collected as errors rather than aborting the sweep — one unreachable
    tray must not cost the other 2015 their inventory. The report says how many answered.
    """
    import host_exec
    from transport import fanout

    if not nodes:
        return [], []

    # role_root("scripts") is already the scripts directory, not the deployment root.
    base_dir = (host_exec.role_root("scripts") or "").rstrip("/")
    script = f"{base_dir}/node_inventory.sh" if base_dir else "node_inventory.sh"

    def _tick(result: Any, done: int, total: int) -> None:
        if progress:
            progress(done, total, getattr(result, "node", ""))

    results = fanout(
        base, nodes, f"bash {script}",
        timeout=timeout,
        max_workers=max_workers or int(os.environ.get("NVIS_INVENTORY_WORKERS", DEFAULT_MAX_WORKERS)),
        on_result=_tick,
    ) if (base := host_exec.connection("control")) else []

    records, errors = [], []
    for result in results:
        text = getattr(result, "stdout", "") or ""
        if getattr(result, "returncode", 1) == 0 and "\t" in text:
            record = parse_record(text)
            record.setdefault("host", {}).setdefault("hostname", result.node)
            records.append(record)
        else:
            # Say something useful even when the hop reports nothing. A direct ssh to a node we
            # hold no allocation on fails with rc=255 and "Login not allowed: no running jobs
            # and no WLM allocations" on stderr, but through the hop both streams arrive empty,
            # so the bare rc is all we get. Naming the usual cause beats "no inventory output",
            # which is what sent me chasing sshd MaxSessions for an afternoon.
            rc = getattr(result, "returncode", 1)
            detail = (getattr(result, "stderr", "") or "").strip() or text.strip()
            if not detail:
                detail = (
                    f"unreachable (rc={rc}, no diagnostic returned). On this cluster that is "
                    f"usually pam_slurm_adopt refusing a node we hold no Slurm allocation on."
                )
            errors.append(f"{result.node}: {detail[:200]}")
    return records, errors


#: Where measured records persist between runs. Inventory changes only when hardware or
#: firmware does, so yesterday's measurement of a node that is busy today is still the truth —
#: it just has to be labelled as carried forward rather than passed off as fresh.
CACHE_PATH = Path(os.environ.get("NVIS_INVENTORY_CACHE")
                  or REPO_ROOT / "var" / "cache" / "node_inventory.json")

#: Marker written into a node's ``firmware_overrides`` when its data came from the cache. The
#: schema fixes every other per-node field, but ``firmware_overrides`` is an open string map,
#: so this is the one place a provenance note fits without inventing a key the contract forbids.
CARRY_FORWARD_KEY = "* carried forward from"


def load_cache(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Previously measured records, keyed by hostname. Missing or corrupt cache -> empty."""
    target = Path(path or CACHE_PATH)
    try:
        blob = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    nodes = blob.get("nodes") if isinstance(blob, dict) else None
    return nodes if isinstance(nodes, dict) else {}


def save_cache(records: list[dict[str, Any]], *, captured_at: str,
               path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Merge *records* into the cache and persist it; returns the merged map.

    Merged rather than replaced: a sweep only ever reaches the nodes Slurm currently lets us
    into, so overwriting would throw away every node that happened to be busy this time.
    """
    target = Path(path or CACHE_PATH)
    merged = load_cache(target)
    for record in records:
        hostname = (record.get("host") or {}).get("hostname")
        if hostname:
            merged[str(hostname)] = {**record, "_captured_at": captured_at}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"nodes": merged}), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        _log.warning("could not persist the inventory cache at %s", target, exc_info=True)
    return merged


def merge_with_cache(
    records: list[dict[str, Any]],
    cache: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fresh records, plus cached ones for nodes this run could not reach.

    Returns ``(records, carried)`` where *carried* maps hostname to the timestamp its data was
    measured, so the caller can mark those entries instead of presenting them as current.
    """
    fresh = {(r.get("host") or {}).get("hostname"): r for r in records}
    fresh.pop(None, None)
    out = list(records)
    carried: dict[str, str] = {}
    for hostname, cached in cache.items():
        if hostname in fresh:
            continue
        when = str(cached.get("_captured_at") or "")
        record = {k: v for k, v in cached.items() if k != "_captured_at"}
        out.append(record)
        carried[hostname] = when
    out.sort(key=lambda r: str((r.get("host") or {}).get("hostname") or ""))
    return out, carried


def parse_srun_labeled(text: str) -> list[str]:
    """Split ``srun --label`` output back into one blob per task.

    Tasks write concurrently and srun interleaves them line by line, so the ``N: `` prefix is
    the only reliable way to reassemble each node's record. Without ``--label`` the fields of
    two thousand nodes arrive shuffled into each other and nothing can be parsed.
    """
    by_task: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        task, sep, rest = line.partition(": ")
        if not sep or not task.strip().isdigit():
            continue
        by_task.setdefault(task.strip(), []).append(rest)
    return ["\n".join(lines) for _, lines in sorted(by_task.items(), key=lambda kv: int(kv[0]))]


#: Nodes per allocation. Small bites on purpose: a shared cluster running other teams' work
#: should never see a two-thousand-node request for an inventory sweep, and a small chunk can
#: slot into whatever happens to be free instead of waiting for a big contiguous window.
SRUN_CHUNK = 64

#: Seconds ``srun`` may wait for resources before giving up. Inventory is never worth queueing
#: for: a request that waits is a request sitting in front of somebody's validation run, and it
#: would also strand report generation behind a queue with no way to show progress.
SRUN_IMMEDIATE_SECONDS = 20

#: Positive nice = lower priority. Whatever else is in the queue should win.
SRUN_NICE = 10000


def build_srun_command(
    nodes: list[str],
    script: str,
    *,
    minutes: int = 5,
    immediate: int = SRUN_IMMEDIATE_SECONDS,
    nice: int = SRUN_NICE,
) -> str:
    """The ``srun`` line that sweeps *nodes*, one task each.

    SSH to a compute node is refused by ``pam_slurm_adopt`` unless the user already holds an
    allocation there — "Login not allowed: no running jobs and no WLM allocations" — so a
    fan-out only reaches whatever the queue happens to be running for us. Going through Slurm
    asks for access the supported way and can reach idle nodes too.

    The flags matter more than the mechanism on a shared cluster:

    * ``--immediate`` makes this yield instead of queue. Inventory must never sit in front of
      another team's validation run, and a queued step would also hang report generation.
    * ``--nice`` puts it below everything else competing for the same nodes.
    * ``--no-kill`` keeps one unresponsive tray from taking the step down and losing the rest.
    * ``--label`` is what makes the interleaved per-task output separable again.
    """
    joined = ",".join(nodes)
    args = (
        "--job-name=nvis-inventory --ntasks-per-node=1 "
        f"--nodes={len(nodes)} --nodelist={shlex.quote(joined)} "
        f"--time=00:{minutes:02d}:00 --immediate={immediate} --nice={nice} "
        f"--label --no-kill --overlap bash {shlex.quote(script)}"
    )
    from platforms import get_platform

    return get_platform().srun_cmd(args)


def collect_via_srun(
    nodes: list[str],
    *,
    timeout: int = 300,
    minutes: int = 5,
    chunk: int | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Sweep *nodes* through Slurm in small chunks; returns ``(records, errors)``.

    Stops as soon as a chunk cannot get resources. On a busy cluster that is the correct
    behaviour: take whatever is free right now, leave the rest for the cache to carry, and do
    not keep asking a queue that is already full of other people's work.
    """
    import host_exec
    import scheduler_runtime

    if not nodes:
        return [], []
    base_dir = (host_exec.role_root("scripts") or "").rstrip("/")
    script = f"{base_dir}/node_inventory.sh" if base_dir else "node_inventory.sh"
    size = max(1, int(chunk or os.environ.get("NVIS_INVENTORY_SRUN_CHUNK", SRUN_CHUNK)))

    records: list[dict[str, Any]] = []
    for start in range(0, len(nodes), size):
        batch = nodes[start:start + size]
        try:
            if scheduler_runtime.is_pythoncm():
                from schedulers import JobSpec, build_scheduler

                scheduler = build_scheduler(
                    "pythoncm", connection=host_exec.connection("control"), profile="control"
                )
                result = scheduler.run(JobSpec(
                    name="nvis-inventory",
                    nodelist=",".join(batch),
                    ntasks_per_node=1,
                    command=f"bash {shlex.quote(script)}",
                    timeout=min(timeout, max(60, minutes * 60)),
                ), poll_interval=1)
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                returncode = result.returncode
                blobs = [
                    block for block in re.split(r"(?m)^===== [^=]+ =====\n", stdout)
                    if "\t" in block
                ]
            else:
                proc = host_exec.run_slurm_on_host(
                    build_srun_command(batch, script, minutes=minutes), timeout=timeout
                )
                stdout = getattr(proc, "stdout", "") or ""
                stderr = getattr(proc, "stderr", "") or ""
                returncode = int(getattr(proc, "returncode", 1))
                blobs = parse_srun_labeled(stdout)
        except Exception as exc:  # noqa: BLE001 - a busy cluster is an expected outcome here
            _log.info("scheduler inventory stopped after %d node(s): %s", len(records), exc)
            break

        before = len(records)
        for blob in blobs:
            if "\t" in blob:
                records.append(parse_record(blob))
        if progress:
            progress(len(records), len(nodes), batch[0])

        if len(records) == before:
            # Nothing came back: almost always --immediate declining because the nodes are
            # busy. Later chunks will meet the same queue, so stop rather than hammer it.
            _log.info("scheduler inventory returned no records after %d node(s): %s",
                      len(records), stderr.strip()[:200] or f"rc={returncode}")
            break

    seen = {(r.get("host") or {}).get("hostname") for r in records}
    mechanism = "PythonCM execution returned no inventory" if scheduler_runtime.is_pythoncm() \
        else "not reachable by SSH and no Slurm allocation was free"
    errors = [f"{n}: {mechanism}" for n in nodes
              if n not in seen]
    return records, errors


def parse_record(text: str) -> dict[str, Any]:
    """Parse one node's ``key<TAB>value`` probe output into a nested record.

    Indexed keys (``gpu.0.uuid``) become ordered lists. Unknown keys are kept rather than
    dropped: a probe that learns a new field should not need a change here to be visible.
    """
    flat: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "\t" not in line:
            continue
        key, _, value = line.partition("\t")
        key = key.strip()
        value = value.strip()
        if key and value:
            flat[key] = value

    out: dict[str, Any] = {"host": {}, "gpu": {}, "nic": {}, "software": {}, "gpus": [], "nics": []}
    indexed: dict[str, dict[int, dict[str, str]]] = {"gpu": {}, "nic": {}}

    for key, value in flat.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[0] in indexed and parts[1].isdigit():
            indexed[parts[0]].setdefault(int(parts[1]), {})[parts[2]] = value
        elif len(parts) == 2:
            out.setdefault(parts[0], {})[parts[1]] = value
        else:
            out[key] = value

    for kind, plural in (("gpu", "gpus"), ("nic", "nics")):
        out[plural] = [indexed[kind][i] for i in sorted(indexed[kind])]
    return out


def normalize_pcie_bdf(bdf: str) -> tuple[str, str | None]:
    """Return ``(schema_valid_bdf, deviation_or_None)``.

    The schema fixes the PCI domain at 4 hex digits. Grace-Blackwell GPUs enumerate in wide
    domains (``00000008:06:00.0``), so the leading zeros are dropped to fit. That is lossless
    while the high half is zero; if it ever is not, the value is returned unchanged so the
    report fails validation loudly rather than silently reporting the wrong device.
    """
    raw = (bdf or "").strip()
    m = _BDF_RE.match(raw)
    if not m:
        return raw, None
    domain, bus, dev, func = m.groups()
    if len(domain) <= 4:
        return f"{domain.zfill(4)}:{bus}:{dev}.{func}", None
    high, low = domain[:-4], domain[-4:]
    if high.strip("0"):
        return raw, f"PCIe domain {domain} exceeds the schema's 4-hex-digit field and cannot be narrowed"
    return f"{low}:{bus}:{dev}.{func}", DEVIATION_BDF_NARROWED


def mac_from_port_guid(guid: str) -> str | None:
    """Recover the EUI-48 MAC embedded in an InfiniBand port GUID.

    A Mellanox port GUID is the EUI-64 form of the port's MAC with a 2-byte marker (``03:00``,
    sometimes ``ff:fe``) inserted in the middle. Removing it yields the actual hardware address,
    which is what the schema's ``macAddress`` field is asking for. Returns ``None`` when the GUID
    is not in that form, so the caller can fall back rather than invent an address.
    """
    raw = (guid or "").strip().lower().removeprefix("0x")
    if len(raw) != 16 or not all(c in "0123456789abcdef" for c in raw):
        return None
    octets = [raw[i:i + 2] for i in range(0, 16, 2)]
    if octets[3:5] not in (["03", "00"], ["ff", "fe"]):
        return None
    return ":".join(octets[:3] + octets[5:]).upper()


def _mode(values: Iterable[Any]) -> Any:
    """The most common non-empty value, which is what "shared baseline" means in the schema."""
    counts = Counter(v for v in values if v not in (None, "", TBD))
    return counts.most_common(1)[0][0] if counts else None


def _first_gpu(record: dict[str, Any], field: str) -> str | None:
    gpus = record.get("gpus") or []
    return (gpus[0].get(field) if gpus else None) or None


def _first_nic(record: dict[str, Any], field: str) -> str | None:
    nics = record.get("nics") or []
    return (nics[0].get(field) if nics else None) or None


#: ``shared_baseline`` path -> how to read it out of one node record. Kept as data so the set of
#: baseline fields is readable in one place and a new field is one line.
_BASELINE_READERS: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {
    ("host", "bios_version"): lambda r: r["host"].get("bios_version"),
    ("gpu", "vbios_version"): lambda r: _first_gpu(r, "vbios_version"),
    ("gpu", "driver_version"): lambda r: r["gpu"].get("driver_version"),
    ("gpu", "fabric_manager_version"): lambda r: r["gpu"].get("fabric_manager_version"),
    ("gpu", "imex_version"): lambda r: r["gpu"].get("imex_version"),
    ("gpu", "cuda_runtime"): lambda r: r["gpu"].get("cuda_runtime"),
    ("nic", "model"): lambda r: r["nic"].get("model"),
    ("nic", "firmware"): lambda r: _first_nic(r, "firmware"),
    ("nic", "ofed_version"): lambda r: r["nic"].get("ofed_version"),
    ("bmc", "firmware"): lambda r: (r.get("bmc") or {}).get("firmware"),
    ("software", "os"): lambda r: r["host"].get("os"),
    ("software", "kernel"): lambda r: r["host"].get("kernel"),
    ("software", "dcgm_version"): lambda r: r["software"].get("dcgm_version"),
}


def derive_baseline(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Modal value per field across the fleet — the schema's ``shared_baseline``.

    Modal rather than first-node so one freshly reimaged outlier does not redefine the fleet's
    baseline and make every other node look like it has an override.
    """
    baseline: dict[str, dict[str, Any]] = {}
    for (section, field), read in _BASELINE_READERS.items():
        value = _mode(_safe_read(read, r) for r in records)
        if value is not None:
            baseline.setdefault(section, {})[field] = value

    gpu_counts = [len(r.get("gpus") or []) for r in records]
    if any(gpu_counts):
        baseline.setdefault("gpu", {})["count_per_node"] = _mode(c for c in gpu_counts if c)
    nic_counts = [len(r.get("nics") or []) for r in records]
    if any(nic_counts):
        baseline.setdefault("nic", {})["ports_per_node"] = _mode(c for c in nic_counts if c)
    rate = _mode(_safe_read(lambda r: _first_nic(r, "rate_gbps"), r) for r in records)
    if rate:
        baseline.setdefault("nic", {})["link_speed"] = f"{rate}Gb/s"
    model = _mode(_safe_read(lambda r: _first_gpu(r, "model"), r) for r in records)
    if model:
        baseline.setdefault("gpu", {})["model"] = model
    return baseline


def _safe_read(read: Callable[[dict[str, Any]], Any], record: dict[str, Any]) -> Any:
    # A truncated probe (node rebooted mid-sweep) yields a record missing whole sections. That
    # is one bad node, not a bad fleet, so the reader degrades to None and the baseline is drawn
    # from everyone else. Narrow on purpose: these are the shapes a malformed record produces.
    try:
        return read(record)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        host = (record.get("host") or {}).get("hostname")
        _log.debug("baseline reader failed for %s", host, exc_info=True)
        return None


def build_node_entry(
    record: dict[str, Any],
    bcm: dict[str, Any] | None,
    baseline: dict[str, dict[str, Any]],
    deviations: set[str],
) -> dict[str, Any]:
    """One ``firmware_inventory.nodes[]`` entry, with per-node deltas as ``firmware_overrides``.

    *deviations* is mutated with any value this had to reshape to satisfy the schema; it is a
    set because the same reshape applies to thousands of nodes and the report wants the reason
    once, not once per node.
    """
    host = record.get("host") or {}
    bcm = bcm or {}
    bmc_self = record.get("bmc") or {}
    hostname = host.get("hostname") or bcm.get("hostname") or TBD

    gpus = []
    for i, g in enumerate(record.get("gpus") or []):
        bdf, dev = normalize_pcie_bdf(g.get("pcie_bdf", ""))
        if dev:
            deviations.add(dev)
        gpus.append({
            "index": int(g.get("index") or i),
            "uuid": g.get("uuid") or TBD,
            "serial": g.get("serial") or TBD,
            "model": g.get("model") or TBD,
            "pcie_bdf": bdf or TBD,
            "vbios_version": g.get("vbios_version") or TBD,
        })

    nics = []
    for n in record.get("nics") or []:
        bdf, dev = normalize_pcie_bdf(n.get("pcie_bdf", ""))
        if dev:
            deviations.add(dev)
        mac = mac_from_port_guid(n.get("port_guid", ""))
        if mac:
            deviations.add(DEVIATION_IB_MAC)
        entry = {
            "device": n.get("device") or TBD,
            "port": int(n.get("port") or 1),
            "pcie_bdf": bdf or TBD,
            "mac": mac or TBD,
            "firmware": n.get("firmware") or TBD,
        }
        nics.append(entry)

    node: dict[str, Any] = {
        "hostname": hostname,
        "serial": host.get("serial") or bcm.get("serial") or TBD,
        "rack_id": bcm.get("rack_id") or TBD,
        # The node's own ipmitool answer wins over BCM's: it is measured from the controller
        # itself (and carries firmware, which BCM does not), while BCM's is configuration. They
        # agree in practice — verified on ink50-dh01-d01-18, both 10.40.128.9 — so BCM stays as
        # the fallback for a node whose ipmitool is missing or needs a password.
        "bmc": {
            "ip": bmc_self.get("ip") or bcm.get("bmc_ip") or "0.0.0.0",
            "mac": bmc_self.get("mac") or bcm.get("bmc_mac") or "00:00:00:00:00:00",
            "firmware": bmc_self.get("firmware") or bcm.get("bmc_firmware") or TBD,
        },
        "nics": nics,
        "gpus": gpus,
        "firmware_overrides": _overrides(record, baseline),
    }
    if bcm.get("rack_unit"):
        node["rack_unit"] = str(bcm["rack_unit"])
    return node


def _overrides(record: dict[str, Any], baseline: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Fields where this node disagrees with the fleet baseline.

    This is the part of the report an operator actually reads: on a healthy fleet it is empty,
    and a node with a stale BIOS or a mismatched NIC firmware stands out by itself.
    """
    out: dict[str, str] = {}
    for (section, field), read in _BASELINE_READERS.items():
        want = (baseline.get(section) or {}).get(field)
        got = _safe_read(read, record)
        if want is not None and got not in (None, "") and got != want:
            out[f"{section}.{field}"] = str(got)
    return out


def build_inventory(
    records: list[dict[str, Any]],
    bcm_by_host: dict[str, dict[str, Any]] | None = None,
    fabric: dict[str, Any] | None = None,
    carried: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Assemble ``firmware_inventory`` (minus ``captured_at``) plus the deviations to publish.

    *carried* names nodes whose data came from a previous run because Slurm would not admit us
    this time; each is marked in place and summarised in the deviations, so a reader can tell a
    measurement taken today from one taken last week.
    """
    bcm_by_host = bcm_by_host or {}
    carried = carried or {}
    baseline = derive_baseline(records)
    deviations: set[str] = set()

    nodes = []
    for record in records:
        hostname = (record.get("host") or {}).get("hostname") or ""
        entry = build_node_entry(record, bcm_by_host.get(hostname), baseline, deviations)
        if hostname in carried:
            entry["firmware_overrides"][CARRY_FORWARD_KEY] = carried[hostname] or "an earlier run"
        nodes.append(entry)
    nodes.sort(key=lambda n: n["hostname"])
    if carried:
        deviations.add(
            f"{len(carried)} node(s) could not be measured in this run and carry data from an "
            f"earlier one; each is marked '{CARRY_FORWARD_KEY} <timestamp>' in its "
            f"firmware_overrides. Slurm refuses login to a node we hold no allocation on, so "
            f"coverage depends on what the queue was running at the time."
        )

    shared = {
        "host": {"bios_version": (baseline.get("host") or {}).get("bios_version") or TBD},
        "bmc": {
            "model": (fabric or {}).get("bmc_model") or TBD,
            # The fleet's modal controller firmware, measured per node by ipmitool. Nodes that
            # disagree with it surface individually in their firmware_overrides.
            "firmware": (baseline.get("bmc") or {}).get("firmware")
            or (fabric or {}).get("bmc_firmware") or TBD,
        },
        "nic": {
            "model": (baseline.get("nic") or {}).get("model") or TBD,
            "firmware": (baseline.get("nic") or {}).get("firmware") or TBD,
            "ofed_version": (baseline.get("nic") or {}).get("ofed_version") or TBD,
            "ports_per_node": (baseline.get("nic") or {}).get("ports_per_node") or 1,
            "link_speed": (baseline.get("nic") or {}).get("link_speed") or TBD,
        },
        "gpu": {
            "model": (baseline.get("gpu") or {}).get("model") or TBD,
            "count_per_node": (baseline.get("gpu") or {}).get("count_per_node") or 1,
            "vbios_version": (baseline.get("gpu") or {}).get("vbios_version") or TBD,
            "driver_version": (baseline.get("gpu") or {}).get("driver_version") or TBD,
        },
        "fabric": {
            "switch_model": (fabric or {}).get("switch_model") or TBD,
            "switch_firmware": (fabric or {}).get("switch_firmware") or TBD,
            "ufm_version": (fabric or {}).get("ufm_version") or TBD,
        },
        "software": {
            "os": (baseline.get("software") or {}).get("os") or TBD,
            "kernel": (baseline.get("software") or {}).get("kernel") or TBD,
        },
    }
    for section, field in (
        ("gpu", "fabric_manager_version"), ("gpu", "imex_version"), ("gpu", "cuda_runtime"),
        ("software", "dcgm_version"),
    ):
        value = (baseline.get(section) or {}).get(field)
        if value:
            shared[section][field] = value
    if (fabric or {}).get("nvswitch_firmware"):
        shared["fabric"]["nvswitch_firmware"] = fabric["nvswitch_firmware"]

    return {"shared_baseline": shared, "nodes": nodes}, sorted(deviations)
