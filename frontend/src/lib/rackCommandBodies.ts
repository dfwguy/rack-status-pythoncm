import { validationTargetFromNodes } from "./validationTargetFromNodes";

export function sortedSpecs(selected: Set<string>): string[] {
  return [...selected].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

/** All trays ct01–ct18 for a rack (`pNN-rMM-ct01` … `ct18`) — used for bulk rack-table runs. */
export function fullRackCt01to18NodeSet(rack: string): Set<string> {
  const rk = rack.trim().toLowerCase();
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
export function powerSpecTokensFromNodeIds(rack: string, selected: Set<string>): string[] {
  const rackLower = rack.trim().toLowerCase();
  const esc = rackLower.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`^${esc}-ct(\\d+)$`, "i");
  const nums: number[] = [];
  for (const id of sortedSpecs(selected)) {
    const m = id.trim().match(re);
    if (!m) continue;
    nums.push(parseInt(m[1], 10));
  }
  const uniq = [...new Set(nums)].sort((a, b) => a - b);
  if (uniq.length === 0) return [];
  const mx = uniq[uniq.length - 1]!;
  const w = mx < 100 ? 2 : String(mx).length;
  return uniq.map((n) => `${rackLower}-ct[${String(n).padStart(w, "0")}]`);
}

export function attachLogDir(body: Record<string, unknown>, logDir?: string): Record<string, unknown> {
  const d = logDir?.trim();
  if (d) body.log_dir = d;
  return body;
}

/** POST bodies that use `specs: string[]` (power, GPU reset, services, bug report, hw check). */
export function specsCommandBody(rack: string, logDir: string | undefined, selected: Set<string>): Record<string, unknown> {
  return attachLogDir({ rack, specs: powerSpecTokensFromNodeIds(rack, selected) }, logDir);
}

/** Bodies with optional Slurm / validation `target` (pNN-rMM-ct[…]) from selected trays. */
export function slurmTargetBody(rack: string, selected: Set<string>): Record<string, unknown> {
  const body: Record<string, unknown> = { rack };
  const t = validationTargetFromNodes(rack, selected);
  if (t) body.target = t;
  return body;
}

export function validationFamilyBody(rack: string, logDir: string | undefined, selected: Set<string>): Record<string, unknown> {
  const body = attachLogDir({ rack }, logDir);
  const t = validationTargetFromNodes(rack, selected);
  if (t) body.target = t;
  return body;
}

/** Llama API expects `nodes_spec` (Slurm fragment); use bracket target when possible. */
export function llamaNodesBody(rack: string, logDir: string | undefined, selected: Set<string>): Record<string, unknown> {
  const body = attachLogDir({ rack }, logDir);
  const t = validationTargetFromNodes(rack, selected);
  const tokens = powerSpecTokensFromNodeIds(rack, selected);
  body.nodes_spec = t ?? tokens.join(",");
  return body;
}
