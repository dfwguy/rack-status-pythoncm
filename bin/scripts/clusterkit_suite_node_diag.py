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
"""Bounded observations and process configuration for an allocated two-node test."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys


def command(args, seconds=3):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=seconds)
        return {"exit": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit": None, "stdout": "", "stderr": str(exc)}


def text_file(path):
    try:
        return path.read_text().strip()
    except OSError:
        return "unavailable"


def gid_table(port):
    rows = []
    for file in sorted((port / "gids").iterdir(), key=lambda p: int(p.name))[:512]:
        value = text_file(file)
        if value == "unavailable" or not value.replace(":", "").strip("0"):
            continue
        rows.append({"index": int(file.name), "gid": value,
                     "type": text_file(port / "gid_attrs/types" / file.name),
                     "netdev": text_file(port / "gid_attrs/ndevs" / file.name)})
    return rows


def net_addresses(netdev):
    r = command(["ip", "-j", "address", "show", "dev", netdev])
    if r["exit"] != 0:
        raise RuntimeError("Cannot read selected interface addresses: " + r["stderr"])
    return json.loads(r["stdout"])


def select_global_gid(rows, interfaces):
    """Use a RoCE-v2 GID matching a configured, non-link-local interface IP."""
    configured = {ipaddress.ip_address(a["local"])
                  for interface in interfaces for a in interface.get("addr_info", [])}
    for row in rows:
        if row["type"] != "RoCE v2":
            continue
        address = ipaddress.IPv6Address(row["gid"])
        comparable = address.ipv4_mapped or address
        if (not comparable.is_link_local and not comparable.is_unspecified
                and not comparable.is_multicast and comparable in configured):
            return row
    raise RuntimeError("No global RoCE-v2 GID matches the selected interface IP")


def counters(directory):
    if not directory.is_dir():
        return {}
    result = {}
    for file in sorted(directory.iterdir())[:128]:
        if file.is_file():
            value = text_file(file)
            result[file.name] = int(value) if value.isdecimal() else value
    return result


def observation(hca):
    dev, number = hca.split(":")
    rdma = Path("/sys/class/infiniband") / dev
    port = rdma / "ports" / number
    rows = gid_table(port)
    netdevs = {r["netdev"] for r in rows if r["netdev"] != "unavailable"}
    if len(netdevs) != 1:
        raise RuntimeError("Selected RDMA port does not identify exactly one interface")
    netdev = netdevs.pop()
    addresses = net_addresses(netdev)
    global_gid = select_global_gid(rows, addresses)
    return {"host": socket.gethostname(), "hca": hca, "netdev": netdev,
            "state": text_file(port / "state"), "rate": text_file(port / "rate"),
            "numa": text_file(rdma / "device/numa_node"), "gids": rows,
            "global_v2_gid": global_gid, "addresses": addresses,
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "port_counters": counters(port / "counters"),
            "hw_counters": counters(port / "hw_counters"),
            "net_counters": counters(Path("/sys/class/net") / netdev / "statistics"),
            "ipv6_routes": command(["ip", "-6", "route", "show", "table", "all", "dev", netdev])}


def control_interface():
    local = socket.gethostbyname(socket.gethostname())
    r = command(["ip", "-j", "address", "show"])
    if r["exit"] != 0:
        raise RuntimeError("Cannot identify the Slurm hostname's IPv4 interface")
    for interface in json.loads(r["stdout"]):
        if any(a.get("local") == local for a in interface.get("addr_info", [])):
            return interface["ifname"]
    raise RuntimeError("No interface owns the Slurm hostname's IPv4 address")


def cpu_numbers(value):
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", value):
        raise RuntimeError("Invalid Slurm CPU list")
    result = []
    for item in value.split(","):
        ends = [int(n) for n in item.split("-")]
        start, end = ends[0], ends[-1]
        if end < start or end >= 4096:
            raise RuntimeError("Unexpected Slurm CPU range")
        result.extend(range(start, end + 1))
    return result


def verify_identity_cpu_map(topology):
    # Slurm CPU_IDs are abstract IDs. NC currently uses consecutive Linux IDs,
    # ordered by socket/core, no SMT and no alternate Slurmd topology settings.
    # Verify that special case instead of guessing on a different machine.
    rows = []
    for line in topology.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        cpu, core, sock = map(int, line.split(","))
        rows.append((sock, core, cpu))
    ordered = sorted(rows)
    if (not ordered or len({(s, c) for s, c, _ in ordered}) != len(ordered)
            or [cpu for _, _, cpu in ordered] != list(range(len(ordered)))):
        raise RuntimeError("CPU topology requires an explicit Slurm-to-Linux mapping")


def allocated_cpu():
    job = os.environ.get("SLURM_JOB_ID", "")
    if not job.isdecimal() or os.environ.get("SLURM_CPUS_PER_TASK") != "1":
        raise RuntimeError("Binding requires this job's one-CPU-per-task allocation")
    config = command(["scontrol", "show", "config"], seconds=5)
    if config["exit"] != 0 or not re.search(
            r"^\s*SlurmdParameters\s*=\s*\(null\)\s*$", config["stdout"], re.M):
        raise RuntimeError("Cannot verify default Slurm CPU topology settings")
    topology = command(["lscpu", "-p=CPU,CORE,SOCKET"])
    if topology["exit"] != 0:
        raise RuntimeError("Cannot read local CPU topology")
    verify_identity_cpu_map(topology["stdout"])
    details = command(["scontrol", "show", "job", "-d", job], seconds=5)
    if details["exit"] != 0:
        raise RuntimeError("Cannot read CPU assignment for the current job")
    host = socket.gethostname().split(".")[0]
    matched = []
    for nodes, cpus in re.findall(r"\bNodes=(\S+)\s+CPU_IDs=(\S+)", details["stdout"]):
        expanded = command(["scontrol", "show", "hostnames", nodes])
        if expanded["exit"] != 0:
            raise RuntimeError("Cannot expand current job's node assignment")
        if host in expanded["stdout"].splitlines():
            matched.extend(cpu_numbers(cpus))
    if len(matched) != 1 or matched[0] not in os.sched_getaffinity(0):
        raise RuntimeError("Cannot confirm exactly one permitted CPU assigned to this host")
    return matched[0]


def exec_bound(argv, root):
    if not argv or not os.path.isabs(argv[0]):
        raise RuntimeError("Bound execution requires an absolute executable")
    cpu = allocated_cpu()
    before = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    after = sorted(os.sched_getaffinity(0))
    if after != [cpu]:
        raise RuntimeError("Process affinity did not match its assigned CPU")
    stage = os.environ["RACK_CLUSTERKIT_SUITE_STAGE"]
    rank = os.environ["SLURM_PROCID"]
    if stage not in ("baseline", "global-gid", "single-path", "tcp-control") or rank not in ("0", "1"):
        raise RuntimeError("Binding only supports the allocated pair and bounded stages")
    record = {"host": socket.gethostname(), "rank": int(rank), "stage": stage,
              "slurm_job_id": os.environ["SLURM_JOB_ID"], "assigned_cpu": cpu,
              "affinity_before": before, "affinity_after": after,
              "mpi_tasks_per_node": 1, "hca": os.environ["RACK_CLUSTERKIT_SUITE_HCA"],
              "gid_index": os.environ.get("UCX_IB_GID_INDEX", "auto"),
              "ucx_rc_paths": os.environ.get("UCX_RC_MLX5_NUM_PATHS", "auto"),
              "ucx_log_level": os.environ.get("UCX_LOG_LEVEL")}
    with (root / f"rank-placement-{stage}-{rank}.json").open("x") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    print(f"BOUND HOST={record['host']} RANK={rank} CPU={cpu} AFFINITY={after}", flush=True)
    os.execv(argv[0], argv)


def summarize(root):
    """Read this attempt's artifacts; retain vendor values and label actual tasks."""
    statuses = {}
    for line in (root / "status.txt").read_text().splitlines():
        for field in line.split():
            key, value = field.split("=", 1)
            statuses[key] = value
    stages = {}
    for stage in ("baseline", "global-gid", "single-path", "tcp-control"):
        files = sorted((root / stage).glob("*/*.json"))
        if not files:
            continue
        placements = [json.loads(file.read_text()) for file in
                      sorted(root.glob(f"rank-placement-{stage}-*.json"))]
        configs = [line for file in sorted(root.glob(f"ucx-{stage}-*.log"))
                   for line in file.read_text().splitlines() if "inter-node cfg#" in line]
        values = []
        for file in files:
            raw = json.loads(file.read_text())
            if raw.get("Testname") not in ("bandwidth", "latency"):
                continue
            values.append({"source": str(file.relative_to(root)),
                           "test": raw["Testname"], "units": raw["Units"],
                           "links": raw["Links"], "vendor_ppn": raw.get("PPN"),
                           "vendor_hca_tag": raw.get("HCA_Tag"),
                           "vendor_technology": raw.get("Technology")})
        stages[stage] = {"metrics": values, "placements": placements,
                         "ucx_inter_node_configuration": configs,
                         "actual_mpi_tasks_per_node": 1 if len(placements) == 2 else None}
    observations = [json.loads(file.read_text()) for file in sorted(root.glob("node-*-before.json"))]
    summary = {"status": statuses, "stages": stages,
               "observed_network": [{"host": r["host"], "hca": r["hca"],
                                      "netdev": r["netdev"], "reported_rate": r["rate"],
                                      "gid": r["global_v2_gid"], "technology": "RoCE v2 / Ethernet"}
                                     for r in observations],
               "performance_acceptance": "UNVERIFIED",
               "warnings": ["Vendor PPN is not the actual MPI task count; use the placement records.",
                            "These short CPU-only checks establish functionality, not performance acceptance.",
                            "Confirm physical/virtual adapter capacity before interpreting bandwidth."]}
    with (root / "summary.json").open("x") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(f"Saved {root / 'summary.json'}; raw vendor output preserved")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["snapshot", "gid", "control-interface", "exec-bound", "summarize"])
    parser.add_argument("--hca", default=os.environ.get("RACK_CLUSTERKIT_SUITE_HCA", ""))
    parser.add_argument("--phase", choices=["before", "between", "after"], default="before")
    parser.add_argument("--outdir", default=os.environ.get("RACK_CLUSTERKIT_SUITE_OUTDIR", ""))
    parser.add_argument("--check-peer", action="store_true")
    tokens = list(sys.argv[1:] if argv is None else argv)
    remaining = []
    if "--" in tokens:
        boundary = tokens.index("--")
        remaining = tokens[boundary + 1:]
        tokens = tokens[:boundary]
    args = parser.parse_args(tokens)
    if args.mode == "exec-bound":
        if not remaining:
            parser.error("exec-bound requires -- followed by the executable and its arguments")
        exec_bound(remaining, Path(args.outdir))
        return
    if remaining:
        parser.error("unexpected arguments")
    if args.mode == "summarize":
        summarize(Path(args.outdir))
        return
    if args.mode == "control-interface":
        print(control_interface())
        return
    if not re.fullmatch(r"[A-Za-z0-9_]+:[1-9][0-9]*", args.hca):
        raise RuntimeError("Invalid RDMA device/port")
    record = observation(args.hca)
    if args.mode == "gid":
        print(record["global_v2_gid"]["index"])
        return
    rank = int(os.environ["SLURM_PROCID"])
    if rank not in (0, 1):
        raise RuntimeError("Diagnostics only support the allocated pair")
    root = Path(args.outdir)
    if args.check_peer:
        peer = json.loads((root / f"node-{1 - rank}-before.json").read_text())
        peer_ip = str(ipaddress.IPv6Address(peer["global_v2_gid"]["gid"]))
        if ipaddress.IPv6Address(peer_ip).ipv4_mapped:
            raise RuntimeError("IPv6 diagnostic expects an IPv6 RoCE address")
        record["peer_route"] = command(["ip", "-6", "route", "get", peer_ip,
                                        "oif", record["netdev"]])
        # Exactly two ICMPv6 echo requests to the other approved node, no scan.
        record["peer_ping"] = command(["ping", "-6", "-I", record["netdev"],
                                       "-c", "2", "-W", "1", "-w", "4", peer_ip], seconds=6)
        record["neighbors"] = command(["ip", "-6", "neigh", "show", "dev", record["netdev"]])
    with (root / f"node-{rank}-{args.phase}.json").open("x") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    print(f"HOST={record['host']} PHASE={args.phase} HCA={args.hca} "
          f"GLOBAL_V2_GID={record['global_v2_gid']['index']} NETDEV={record['netdev']}")
    if args.check_peer:
        print("PEER_ROUTE=" + json.dumps(record["peer_route"]))
        print("PEER_PING=" + json.dumps(record["peer_ping"]))


if __name__ == "__main__":
    main()
