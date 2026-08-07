import { validationTargetFromNodes } from "./validationTargetFromNodes";
import type { NvisNamingSchema } from "../types/naming";

export type { NvisNamingSchema };

export function sortedSpecs(selected: Set<string>): string[] {
  return [...selected].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/** All trays ct01–ct18 for a rack (`pNN-rMM-ct01` … `ct18`) — used for bulk rack-table runs. */
export function fullRackCt01to18NodeSet(rack: string): Set<string> {
  const rk = rack.trim();
  const out = new Set<string>();
  if (!rk) return out;
  for (let i = 1; i <= 18; i++) {
    out.add(`${rk}-ct${String(i).padStart(2, "0")}`);
  }
  return out;
}

/**
 * UI node ids are `pNN-rMM-ct01`; power/GPU/services APIs expect tokens
 * `pNN-rMM-ct[01]` so `merge_ct_individuals_for_power` can merge to `pNN-rMM-ct[01-18]`
 * (same as legacy checkboxes).
 */
export function powerSpecTokensFromNodeIds(
  rack: string,
  selected: Set<string>,
  naming?: NvisNamingSchema,
): string[] {
  const rackCanon = rack.trim();
  const rackKey = rackCanon.toLowerCase();
  const joiner = naming?.ct_joiner ?? "-ct";
  const nums: number[] = [];
  for (const id of sortedSpecs(selected)) {
    const esc = rackKey.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const escJoin = joiner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const re = new RegExp(`^${esc}${escJoin}(\\d+)$`, "i");
    const m = id.trim().match(re);
    if (m) nums.push(parseInt(m[1], 10));
    else if (naming?.node_id_mode === "full_hostname" && id.toLowerCase().startsWith(rackKey)) {
      const tail = id.slice(rackCanon.length);
      const tm = tail.toLowerCase().match(/^(\d{2,})$/);
      if (tm) nums.push(parseInt(tm[1], 10));
    }
  }
  const uniq = [...new Set(nums)].sort((a, b) => a - b);
  if (uniq.length === 0) return [];
  const mx = uniq[uniq.length - 1]!;
  const w = mx < 100 ? 2 : String(mx).length;
  return uniq.map((n) => `${rackCanon}${joiner}[${String(n).padStart(w, "0")}]`);
}

export function attachLogDir(body: Record<string, unknown>, logDir?: string): Record<string, unknown> {
  const d = logDir?.trim();
  if (d) body.log_dir = d;
  return body;
}

/**
 * Options fragment for ``POST /api/hw-check-async`` — merge with ``specsCommandBody(...)``.
 * Non-empty ``checker_args`` overrides ``preset`` (server parses with shlex).
 */
export function hwCheckOptionsBody(opts: { preset?: string; checker_args?: string }): Record<string, unknown> {
  const raw = (opts.checker_args ?? "").trim();
  if (raw.length > 0) {
    return { checker_args: raw };
  }
  const p = (opts.preset ?? "default").trim().toLowerCase() || "default";
  return { preset: p };
}

/** POST bodies that use `specs: string[]` (power, GPU reset, services, bug report, hw check). */
export function specsCommandBody(
  rack: string,
  logDir: string | undefined,
  selected: Set<string>,
  naming?: NvisNamingSchema,
): Record<string, unknown> {
  return attachLogDir({ rack, specs: powerSpecTokensFromNodeIds(rack, selected, naming) }, logDir);
}

/** Bodies with optional Slurm / validation `target` from selected trays. */
export function slurmTargetBody(
  rack: string,
  selected: Set<string>,
  naming?: NvisNamingSchema,
): Record<string, unknown> {
  const body: Record<string, unknown> = { rack };
  const t = validationTargetFromNodes(rack, selected, naming);
  if (t) body.target = t;
  return body;
}

/** Per-launch knobs shared by the NCCL/NEMO launch surfaces. */
export interface LaunchOptions {
  /** NCCL debug level: "VERSION" | "WARN" | "INFO" | "TRACE". "off"/"" leaves the script default. */
  ncclDebug?: string;
  /** Extra environment variables forwarded to the job (e.g. OMPI_MCA_coll_hcoll_enable=0). */
  env?: Record<string, string>;
  /**
   * NCCL library version for the direct NCCL microbenchmark: "2.30.7" (vetted default),
   * "system" (host build, 2.28.9), or another vetted "X.Y.Z". The sbatch loads the matching
   * libnccl from the NFS workspace via LD_LIBRARY_PATH. Ignored by container/NEMO launches.
   */
  ncclVersion?: string;
  /**
   * NCCL sweep rounds — how many times to run the all_reduce sweep in one job. Stage-1
   * acceptance requires 5 passing rounds (the modal defaults to 5). Integer ≥ 1.
   */
  iters?: number;
  /**
   * Run the stage-1 NCCL sweep before the NEMO pretrain. ``run_llmb_nemo.sh`` submits both
   * by default; unchecking skips straight to NEMO (``NVIS_LLMB_NEMO_WITH_NCCL=0``), which is
   * what you want when the rack already has a fresh NCCL result.
   */
  withNccl?: boolean;
  /**
   * NEMO training iterations. 50 = short perf benchmark, 2010 = the long soak. Sets both
   * ``NVIS_NEMO_MAX_STEPS`` (what the job runs) and ``NEMO_TARGET_STEPS`` (what the dashboard
   * requires before it calls the run finished) — they must agree or a short run never
   * reaches the "done" label.
   */
  maxSteps?: number;
}

const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Parse a "KEY=VALUE per line" textarea into a validated env map. Invalid lines
 * (no `=`, bad key name) are dropped. Blank lines and `# comments` are ignored.
 */
export function parseEnvVars(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of (text ?? "").split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    const val = line.slice(eq + 1).trim();
    if (!ENV_KEY_RE.test(key)) continue;
    out[key] = val;
  }
  return out;
}

/** Attach optional launch knobs (NCCL debug, custom env vars) to a POST body. */
export function attachLaunchOptions(body: Record<string, unknown>, opts?: LaunchOptions): Record<string, unknown> {
  const dbg = (opts?.ncclDebug ?? "").trim();
  if (dbg && dbg.toLowerCase() !== "off") body.nccl_debug = dbg;
  if (opts?.env && Object.keys(opts.env).length > 0) body.env = opts.env;
  const ver = (opts?.ncclVersion ?? "").trim();
  if (ver) body.nccl_version = ver;
  const iters = opts?.iters;
  if (typeof iters === "number" && Number.isFinite(iters) && iters >= 1) {
    body.iters = Math.round(iters);
  }
  if (typeof opts?.withNccl === "boolean") body.with_nccl = opts.withNccl;
  const steps = opts?.maxSteps;
  if (typeof steps === "number" && Number.isFinite(steps) && steps >= 1) {
    body.max_steps = Math.round(steps);
  }
  return body;
}

export function validationFamilyBody(
  rack: string,
  logDir: string | undefined,
  selected: Set<string>,
  naming?: NvisNamingSchema,
  opts?: LaunchOptions,
): Record<string, unknown> {
  const body = attachLogDir({ rack }, logDir);
  const t = validationTargetFromNodes(rack, selected, naming);
  if (t) body.target = t;
  return attachLaunchOptions(body, opts);
}

/** Llama API expects `nodes_spec` (Slurm fragment); use bracket target when possible. */
export function llamaNodesBody(
  rack: string,
  logDir: string | undefined,
  selected: Set<string>,
  naming?: NvisNamingSchema,
): Record<string, unknown> {
  const body = attachLogDir({ rack }, logDir);
  const t = validationTargetFromNodes(rack, selected, naming);
  const tokens = powerSpecTokensFromNodeIds(rack, selected, naming);
  body.nodes_spec = t ?? tokens.join(",");
  return body;
}

/** A single {name, mac} row used by macChangeBody / MacChangeDialog. */
export interface MacInterfaceRow {
  name: string;
  mac: string;
}

/**
 * Body for /api/bmc-recovery-async — same shape as specsCommandBody plus an
 * optional `iface` (server defaults to "rf0" when omitted).
 */
export function bmcRecoveryBody(
  rack: string,
  logDir: string | undefined,
  selected: Set<string>,
  iface?: string,
): Record<string, unknown> {
  const body = specsCommandBody(rack, logDir, selected);
  const trimmed = (iface ?? "").trim();
  if (trimmed) body.iface = trimmed;
  return body;
}

/**
 * Body for /api/mac-change-async — single-node form with optional node MAC,
 * a list of interface rows (empty rows are dropped), and the dry-run flag.
 */
export function macChangeBody(
  rack: string,
  logDir: string | undefined,
  node: string,
  nodeMac: string,
  interfaces: MacInterfaceRow[],
  dryRun: boolean,
): Record<string, unknown> {
  const body: Record<string, unknown> = { rack, node };
  const trimmedMac = (nodeMac ?? "").trim();
  if (trimmedMac) body.node_mac = trimmedMac;
  const rows = (interfaces || [])
    .map((r) => ({ name: (r.name ?? "").trim(), mac: (r.mac ?? "").trim() }))
    .filter((r) => r.name && r.mac);
  if (rows.length > 0) body.interfaces = rows;
  body.dry_run = !!dryRun;
  return attachLogDir(body, logDir);
}
