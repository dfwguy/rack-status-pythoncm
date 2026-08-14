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
"""nvis_nccl_ingest — tiered, composable ingest for NCCL sweep results.

One normalised result, several ways of obtaining it, in a fixed order of preference:

    tier 0  JsonSource        nccl-tests' own ``-J`` output          (typed, best)
    tier 1  ExperimentSource  Comms-Perf-Suite per-combination dir   (structured)
    tier 2  ScrapeSource      an aggregated text log                 (last resort)

WHY A CHAIN RATHER THAN A REWRITE
---------------------------------
The scraper is not going away. It is the only thing that can read the thousands of logs already
on disk, anything launched outside our wrappers, and any site that has not adopted the newer
layout. So each tier may DECLINE — by not detecting its input, or by failing on it — and the
chain simply moves down. A tier can never break ingest for the tiers below it.

WHAT EACH TIER BUYS
-------------------
The scraper has to reconstruct, from one concatenated text file, things nobody recorded: which
collective a table belonged to, which of six all_reduce configurations produced it, how many
nodes ran, whether a missing block means "still running" or "lost". Those reconstructions are
where every parsing bug this month came from.

The experiment layout already answers all of it. Each combination is its own file pair:

    1_1_LOG_all_reduce_env1_args1_job140212_iter1.yml   <- identity and geometry
    1_1_LOG_all_reduce_env1_args1_job140212_iter1.txt   <- the size sweep, and NOTHING else

Measured on job 140212: the ``.txt`` holds zero ``NCCL INFO`` lines because per-rank debug output
goes to a sibling directory. No interleaving, so no spliced records, no arming rules, and no
nine-numeric-fields heuristic that once read ``rank 1805`` as 1805 GB/s of bandwidth.

DEGRADATION IS DATA
-------------------
Job 140212 declared 24 combinations and produced 23 files, one of them empty. That is not a
parse failure — it is the result. A combination with a ``.yml`` and an empty ``.txt`` is
``attempted``; one with neither is ``missing``. Both are reported BY NAME, where the scraper
could only say "24 started, 22 concluded" and leave the operator to guess which.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:  # the table reader lives in exactly one place
    import nvis_logscan
except ImportError:  # pragma: no cover - only when deployed without its sibling
    nvis_logscan = None

INGEST_VERSION = 1


# ---------------------------------------------------------------------------------------------
# Model. Source-independent on purpose: a consumer must not be able to tell which tier answered
# except by reading `source`, or the tiers stop being interchangeable.
# ---------------------------------------------------------------------------------------------


@dataclass
class Point:
    """One row of the size sweep."""

    size_bytes: int
    busbw_gbps: float


@dataclass
class Combination:
    """One nccl-tests invocation: a collective at one env/arg setting.

    ``status`` distinguishes the three outcomes the old count-based reporting conflated:
      ok         — ran and produced a table
      attempted  — the runner recorded it, the output is empty (job died, timed out)
      missing    — never recorded at all (the sweep stopped before reaching it)
    """

    name: str
    collective: str
    status: str = "ok"
    binary: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    args: dict[str, str] = field(default_factory=dict)
    series: list[Point] = field(default_factory=list)
    peak_busbw_gbps: float | None = None
    avg_busbw_gbps: float | None = None
    out_of_bounds: str | None = None
    concluded: bool = False
    note: str | None = None


@dataclass
class Sweep:
    """Everything one NCCL job produced, however it was obtained."""

    source: str
    job_id: str | None = None
    node_list: str | None = None
    racks: list[str] = field(default_factory=list)
    num_nodes: int | None = None
    num_processes: int | None = None
    gpus_per_node: int | None = None
    nccl_version: str | None = None
    #: Platform provenance the runner records (GPU/CPU arch, driver, MPI, NIC, switch, OS). Free
    #: to collect and exactly what a validation report has to state about what was measured.
    system: dict[str, str] = field(default_factory=dict)
    #: The env/arg keys that actually DIFFER across combinations, i.e. what the sweep varied.
    #: Everything else is constant background; without this a consumer cannot label two curves.
    sweep_keys: list[str] = field(default_factory=list)
    combinations: list[Combination] = field(default_factory=list)
    #: Tiers that were asked and said no, with the reason. Keeps a silent downgrade visible.
    declined: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    path: str | None = None
    #: Blocks known to exist that are NOT represented as combinations. Only the scrape tier sets
    #: this: an aggregated log can count its missing completion markers but cannot say which
    #: combination each belonged to, so the gap has to be carried as a number.
    unaccounted: int = 0

    @property
    def complete(self) -> bool:
        return (
            bool(self.combinations)
            and self.unaccounted == 0
            and all(c.status == "ok" for c in self.combinations)
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["complete"] = self.complete
        d["ingest_version"] = INGEST_VERSION
        return d


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------

_RACK_RE = re.compile(r"([a-z]+\d+-[a-z]+\d+-[a-z]\d+)", re.IGNORECASE)


def read_flat_yaml(path: str) -> dict:
    """Read the runner's metadata files. NOT a YAML parser — deliberately.

    These files are machine-generated as ``key: "value"`` with at most one level of indentation,
    and the head node has no PyYAML. Anything richer than that is ignored rather than guessed at,
    because a half-right YAML parser is worse than an obviously limited one.
    """
    out: dict = {}
    section: str | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indented = line[:1] in (" ", "\t")
                key, sep, val = line.strip().partition(":")
                if not sep:
                    continue
                key, val = key.strip(), val.strip()
                if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                    val = val[1:-1]
                if not val:  # a section header such as "library:"
                    section = key
                    out[key] = {}
                    continue
                if indented and section and isinstance(out.get(section), dict):
                    out[section][key] = val
                else:
                    section = None
                    out[key] = val
    except OSError:
        return {}
    return out


def parse_cmd_env_args(cmd: str) -> tuple[dict[str, str], dict[str, str]]:
    """Pull the env combination and test flags out of the recorded command line.

    The runner writes the exact invocation, so the parameter set is stated rather than inferred:
    ``env NCCL_NVLS_ENABLE=0 ... run_test .../all_reduce_perf -t1 -g1 ... -GNone``. This is how we
    tell the NVLS-on run from the NVLS-off one, which on job 140212 differ by ~40% (224 vs 158
    GB/s) and which the scraper could only separate by guessing "best block".
    """
    envs: dict[str, str] = {}
    args: dict[str, str] = {}
    if not cmd:
        return envs, args
    for m in re.finditer(r"(?:^|\s)env\s+([A-Z_][A-Z0-9_]*)=(\S*)", cmd):
        envs[m.group(1)] = m.group(2)
    tail = cmd.split("run_test", 1)[1] if "run_test" in cmd else cmd
    # Flags render as -b8 / -GNone: key and value concatenated, no separator.
    for m in re.finditer(r"(?:^|\s)(-[A-Za-z])([^\s]*)", tail):
        args[m.group(1)] = m.group(2)
    return envs, args


def racks_from_nodelist(nodelist: str) -> list[str]:
    """Distinct rack ids in a Slurm node list, in order of appearance."""
    out: list[str] = []
    for m in _RACK_RE.finditer(nodelist or ""):
        rid = m.group(1)
        if rid not in out:
            out.append(rid)
    return out


def _scan_table(path: str) -> dict | None:
    """The nccl-tests table, via the one reader that knows how to read one."""
    if nvis_logscan is None:
        return None
    rec = nvis_logscan.scan(path, inventory=False)
    return rec if rec.get("ok") else None


# ---------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------


class Source:
    """One way of obtaining a :class:`Sweep`.

    Contract, and the whole point of the chain: ``detect`` is cheap and never raises, ``load``
    returns ``None`` rather than raising when it cannot produce a result. A tier that cannot cope
    must step aside quietly so the next one gets its turn.
    """

    name = "source"
    priority = 100

    def detect(self, target: str) -> bool:
        raise NotImplementedError

    def load(self, target: str) -> Sweep | None:
        raise NotImplementedError


class JsonSource(Source):
    """tier 0 — nccl-tests' own ``-J`` output.

    Upstream nccl-tests can write a typed JSON document (documented as far back as v2.17.8, the
    version in our container). Nothing emits it yet: it needs ``-J`` added to the suite's
    ``sweep_args``. The tier is wired up now so that turning it on is a config change rather than
    a code change.

    The schema below is upstream's published one and has NOT been verified against a real run, so
    this deliberately fails closed: anything unexpected returns ``None`` and the experiment tier
    answers instead.
    """

    name = "json"
    priority = 0

    def detect(self, target: str) -> bool:
        return target.endswith(".json") and os.path.isfile(target)

    def load(self, target: str) -> Sweep | None:
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        results = doc.get("results")
        if not isinstance(results, list) or not results:
            return None

        series: list[Point] = []
        oob = 0
        for r in results:
            if not isinstance(r, dict) or r.get("in_place"):
                continue
            size, bw = r.get("size_bytes"), r.get("bus_bw_gbs")
            if isinstance(size, int) and isinstance(bw, (int, float)):
                series.append(Point(size_bytes=size, busbw_gbps=float(bw)))
            oob += int(r.get("errors") or 0)
        if not series:
            return None

        cmd = " ".join(doc.get("command") or [])
        collective = _collective_from_binary(cmd)
        combo = Combination(
            name=os.path.basename(target),
            collective=collective,
            binary=None,
            env={k: str(v) for k, v in (doc.get("environment") or {}).items()},
            series=series,
            peak_busbw_gbps=max(series, key=lambda p: p.size_bytes).busbw_gbps,
            out_of_bounds=str(oob),
            concluded=True,
        )
        return Sweep(
            source=self.name,
            nccl_version=doc.get("nccl_version"),
            combinations=[combo],
            path=target,
        )


def _collective_from_binary(text: str) -> str:
    m = re.search(r"(all_reduce|all_gather|reduce_scatter|alltoall|sendrecv|broadcast|reduce)_perf", text or "")
    return m.group(1) if m else "unknown"


class ExperimentSource(Source):
    """tier 1 — a Comms-Perf-Suite experiment directory.

    One ``.yml``/``.txt`` pair per combination. The yml states identity and geometry (collective,
    node list, node count, job id, and the exact command), so nothing has to be inferred from a
    filename or reconstructed from interleaved output.
    """

    name = "experiment"
    priority = 10

    def detect(self, target: str) -> bool:
        return bool(self._log_dir(target))

    @staticmethod
    def _log_dir(target: str) -> str | None:
        """Accept either the LOG_… directory or the experiment directory above it."""
        if not os.path.isdir(target):
            return None
        if glob.glob(os.path.join(target, "*_iter*.yml")):
            return target
        for sub in sorted(glob.glob(os.path.join(target, "LOG_*"))):
            if glob.glob(os.path.join(sub, "*_iter*.yml")):
                return sub
        return None

    def load(self, target: str) -> Sweep | None:
        log_dir = self._log_dir(target)
        if not log_dir:
            return None

        sweep = Sweep(source=self.name, path=log_dir)
        meta = read_flat_yaml(os.path.join(log_dir, "test_set_metadata.yml"))
        lib = meta.get("library")
        if isinstance(lib, dict):
            sweep.nccl_version = lib.get("version")
        swp = read_flat_yaml(os.path.join(log_dir, "test_sweep_metadata.yml"))
        for sect in ("system_metadata", "network_metadata"):
            block = swp.get(sect)
            if isinstance(block, dict):
                sweep.system.update({k: v for k, v in block.items() if v})

        ymls = sorted(glob.glob(os.path.join(log_dir, "*_iter*.yml")), key=_combo_sort_key)
        if not ymls:
            return None

        for y in ymls:
            info = read_flat_yaml(y)
            stem = os.path.basename(y)[: -len(".yml")]
            txt = os.path.join(log_dir, stem + ".txt")
            envs, args = parse_cmd_env_args(info.get("cmd", ""))

            combo = Combination(
                name=stem,
                collective=str(info.get("test_name") or _collective_from_binary(stem)),
                binary=info.get("test_binary"),
                env=envs,
                args=args,
            )

            if not os.path.isfile(txt) or os.path.getsize(txt) == 0:
                # Recorded but empty: the runner reached this combination and it produced
                # nothing. Named, so the operator is not left diffing counts.
                combo.status = "attempted"
                combo.note = "no output (empty or absent .txt)"
                sweep.combinations.append(combo)
                continue

            rec = _scan_table(txt)
            if rec is None:
                combo.status = "attempted"
                combo.note = "table could not be read"
                sweep.combinations.append(combo)
                continue

            n = rec["nccl"]
            ser = n["series_by_collective"].get(combo.collective) or n["all_reduce_series"]
            combo.series = [
                Point(size_bytes=int(p["size_bytes"]), busbw_gbps=float(p["bus_bw_gbps"]))
                for p in ser
            ]
            if combo.series:
                combo.peak_busbw_gbps = max(combo.series, key=lambda p: p.size_bytes).busbw_gbps
            avg = n["avg_bus_bandwidth"]
            combo.avg_busbw_gbps = float(avg[-1]) if avg else None
            combo.out_of_bounds = n["out_of_bounds"][-1] if n["out_of_bounds"] else None
            combo.concluded = int(n["sweep_blocks_concluded"] or 0) > 0
            if not combo.concluded:
                combo.status = "attempted"
                combo.note = "no completion marker"

            # Identity comes from the yml of whichever combination carries it; they agree.
            sweep.job_id = sweep.job_id or info.get("slurm_id")
            sweep.node_list = sweep.node_list or info.get("node_list")
            sweep.num_nodes = sweep.num_nodes or _as_int(info.get("num_nodes"))
            sweep.num_processes = sweep.num_processes or _as_int(info.get("num_processes"))
            sweep.gpus_per_node = sweep.gpus_per_node or _as_int(info.get("num_process_per_node"))
            sweep.combinations.append(combo)

        # Fall back to any yml for identity when every combination was empty.
        if sweep.job_id is None and ymls:
            info = read_flat_yaml(ymls[0])
            sweep.job_id = info.get("slurm_id")
            sweep.node_list = info.get("node_list")
            sweep.num_nodes = _as_int(info.get("num_nodes"))
            sweep.num_processes = _as_int(info.get("num_processes"))
            sweep.gpus_per_node = _as_int(info.get("num_process_per_node"))

        sweep.racks = racks_from_nodelist(sweep.node_list or "")
        sweep.sweep_keys = varying_keys(sweep.combinations)
        _flag_gaps(sweep)
        return sweep


def varying_keys(combos: list[Combination]) -> list[str]:
    """Which env/arg keys the sweep actually varied.

    A combination carries every variable that was set, most of them identical everywhere
    (NCCL_DEBUG, NCCL_DEBUG_SUBSYS, -b, -e …). Only the ones that differ identify a configuration,
    and those are what a chart legend or a comparison table needs: on job 140212 the answer is
    NCCL_NVLS_ENABLE, NCCL_NCHANNELS_PER_NET_PEER and -G.
    """
    seen: dict[str, set[str]] = {}
    for c in combos:
        for src in (c.env, c.args):
            for k, v in src.items():
                seen.setdefault(k, set()).add(v)
    return sorted(k for k, vals in seen.items() if len(vals) > 1)


def _combo_sort_key(path: str):
    """Sort 1_2 before 1_10: the runner's indices are numeric, the filenames are not."""
    m = re.match(r"(\d+)_(\d+)_", os.path.basename(path))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _flag_gaps(sweep: Sweep) -> None:
    """Note combinations the runner never reached, inferred from the index sequence.

    LIMITATION, stated rather than papered over: nothing in the experiment metadata declares how
    many combinations the sweep was supposed to run, so a run that stops early is only detectable
    up to its highest surviving index. Job 140212 wrote 1..23 of a declared 24 and we can see the
    empty 23 but not the absent 24. The declared count lives in the suite's testset.toml (and in
    the ``tests=N`` manifest our own collector writes), so a caller that has it should pass it
    rather than expect this to guess.
    """
    idx = []
    for c in sweep.combinations:
        m = re.match(r"(\d+)_(\d+)_", c.name)
        if m:
            idx.append(int(m.group(2)))
    if not idx:
        return
    missing = sorted(set(range(1, max(idx) + 1)) - set(idx))
    for i in missing:
        sweep.combinations.append(
            Combination(name=f"combination_{i}", collective="unknown", status="missing",
                        note="no record written; the sweep stopped before this point")
        )
    bad = [c.name for c in sweep.combinations if c.status != "ok"]
    if bad:
        sweep.warnings.append(f"{len(bad)} of {len(sweep.combinations)} combinations incomplete: "
                              + ", ".join(bad[:6]) + (" …" if len(bad) > 6 else ""))


def _as_int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


class ScrapeSource(Source):
    """tier 2 — an aggregated text log, read by the streaming scanner.

    Keeps working for every log already on disk and for anything launched outside our wrappers.
    It cannot recover what the layout above records for free: per-combination identity is gone,
    so several configurations of the same collective arrive as indistinguishable blocks.
    """

    name = "scrape"
    priority = 20

    def detect(self, target: str) -> bool:
        return os.path.isfile(target) and nvis_logscan is not None

    def load(self, target: str) -> Sweep | None:
        rec = _scan_table(target)
        if rec is None:
            return None
        n = rec["nccl"]
        if not n["armed"] and not n["series_by_collective"]:
            return None

        sweep = Sweep(source=self.name, path=target)
        for coll, pts in sorted(n["series_by_collective"].items()):
            series = [
                Point(size_bytes=int(p["size_bytes"]), busbw_gbps=float(p["bus_bw_gbps"]))
                for p in pts
            ]
            sweep.combinations.append(
                Combination(
                    name=coll,
                    collective=coll,
                    series=series,
                    peak_busbw_gbps=(max(series, key=lambda p: p.size_bytes).busbw_gbps
                                     if series else None),
                    concluded=True,
                )
            )
        started = int(n["sweep_blocks_started"] or 0)
        concluded = int(n["sweep_blocks_concluded"] or 0)
        if started > concluded:
            # No per-combination identity here, so the gap can only be counted, not named.
            sweep.unaccounted = started - concluded
            sweep.warnings.append(
                f"{started - concluded} of {started} blocks have no completion marker "
                "(aggregated log: the specific combinations cannot be identified)"
            )
        if not sweep.combinations:
            return None
        return sweep


DEFAULT_SOURCES: list[Source] = [JsonSource(), ExperimentSource(), ScrapeSource()]


def ingest(target: str, sources: list[Source] | None = None) -> Sweep | None:
    """First tier that can read *target* wins; the rest are recorded as declined.

    A source that raises is treated exactly like one that declines. Ingest degrading to a lower
    tier is always preferable to ingest failing, and the reason is kept on the result so the
    downgrade is visible rather than silent.
    """
    chain = sorted(sources or DEFAULT_SOURCES, key=lambda s: s.priority)
    declined: list[str] = []
    for src in chain:
        try:
            if not src.detect(target):
                declined.append(f"{src.name}: not applicable")
                continue
            sweep = src.load(target)
        except Exception as exc:  # noqa: BLE001 - a broken tier must not sink the chain
            declined.append(f"{src.name}: {type(exc).__name__}: {exc}")
            continue
        if sweep is not None:
            sweep.declined = declined
            return sweep
        declined.append(f"{src.name}: no usable result")
    return None


def find_experiment_dir(llmb_install: str, job_id: str) -> str | None:
    """The experiment directory a job wrote, or ``None``.

    Bridges the dashboard's world (a canonical log named after a job) to the runner's world (an
    experiment tree keyed by job id), so tier 1 can be tried for a log the operator clicked on.
    """
    pat = os.path.join(llmb_install, "workloads", "*", "experiments", f"*_{job_id}")
    for cand in sorted(glob.glob(pat)):
        if ExperimentSource().detect(cand):
            return cand
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tiered ingest for NCCL sweep results.")
    ap.add_argument("target", help="experiment dir, aggregated log, or -J json file")
    ap.add_argument("--summary", action="store_true", help="human summary instead of JSON")
    args = ap.parse_args(argv)

    sweep = ingest(args.target)
    if sweep is None:
        print(json.dumps({"ok": False, "error": "no source could read this target"}))
        return 1
    if not args.summary:
        print(json.dumps({"ok": True, **sweep.to_dict()}, indent=2))
        return 0

    print(f"source     : {sweep.source}")
    print(f"job        : {sweep.job_id}  nodes={sweep.num_nodes} procs={sweep.num_processes}")
    print(f"racks      : {', '.join(sweep.racks) or '-'}")
    print(f"nccl       : {sweep.nccl_version or '-'}")
    if sweep.system:
        for k in ("system_name", "gpu_arch_type", "cuda_driver_version", "nics", "switch_type"):
            if sweep.system.get(k):
                print(f"  {k:<20} {sweep.system[k]}")
    print(f"varied     : {', '.join(sweep.sweep_keys) or '(nothing)'}")
    print(f"complete   : {sweep.complete}")
    for c in sweep.combinations:
        peak = f"{c.peak_busbw_gbps:.2f}" if c.peak_busbw_gbps is not None else "-"
        # Only the parameters that distinguish this combination from the others.
        params = {**c.env, **c.args}
        label = " ".join(f"{k}={params[k]}" for k in sweep.sweep_keys if k in params)
        print(f"  {c.status:<9} {c.collective:<15} peak={peak:>8}  {label}"
              + (f"  [{c.note}]" if c.note else ""))
    for w in sweep.warnings:
        print(f"warning    : {w}")
    for d in sweep.declined:
        print(f"declined   : {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
