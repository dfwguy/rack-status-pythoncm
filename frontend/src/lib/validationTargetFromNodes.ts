import type { NvisNamingSchema } from "../types/naming";

function compressCtNums(nums: number[]): string {
  const parts: string[] = [];
  let i = 0;
  while (i < nums.length) {
    const start = nums[i];
    let end = start;
    while (i + 1 < nums.length && nums[i + 1] === end + 1) {
      i++;
      end = nums[i];
    }
    if (start === end) parts.push(String(start).padStart(2, "0"));
    else parts.push(`${String(start).padStart(2, "0")}-${String(end).padStart(2, "0")}`);
    i++;
  }
  return parts.join(",");
}

function trayFromNodeId(
  rackKey: string,
  nodeId: string,
  naming?: NvisNamingSchema,
): number | null {
  const id = nodeId.trim().toLowerCase();
  const joiner = naming?.ct_joiner ?? "-ct";
  if (joiner && naming?.node_id_mode !== "full_hostname") {
    const esc = rackKey.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const re = new RegExp(`^${esc}${joiner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\d+)$`, "i");
    const m = id.match(re);
    if (m) return parseInt(m[1], 10);
  }
  if (id.startsWith(rackKey) && id.length > rackKey.length) {
    const tail = id.slice(rackKey.length);
    const m = tail.match(/^(\d{2,})$/);
    if (m) return parseInt(m[1], 10);
  }
  return null;
}

/**
 * Build validation target from selected tray hostnames (pattern follows ``NVIS_*`` templates).
 * Returns null if no parsable ct ids for this rack.
 */
export function validationTargetFromNodes(
  rack: string,
  selected: Iterable<string>,
  naming?: NvisNamingSchema,
): string | null {
  const rackCanon = rack.trim();
  const rackKey = rackCanon.toLowerCase();
  const joiner = naming?.ct_joiner ?? "-ct";
  const nums: number[] = [];
  for (const id of selected) {
    const n = trayFromNodeId(rackKey, id, naming);
    if (n !== null && !Number.isNaN(n)) nums.push(n);
  }
  if (nums.length === 0) return null;
  nums.sort((a, b) => a - b);
  return `${rackCanon}${joiner}[${compressCtNums(nums)}]`;
}
