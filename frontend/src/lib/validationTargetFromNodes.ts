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

/**
 * Build `pNN-rMM-ct[01,03-05]` from selected tray hostnames for sysinfo / validation APIs.
 * Returns null if no parsable ct ids for this rack.
 */
export function validationTargetFromNodes(rack: string, selected: Iterable<string>): string | null {
  const rackLower = rack.trim().toLowerCase();
  const esc = rackLower.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`^${esc}-ct(\\d{2})$`, "i");
  const nums: number[] = [];
  for (const id of selected) {
    const m = id.trim().match(re);
    if (!m) continue;
    nums.push(parseInt(m[1], 10));
  }
  if (nums.length === 0) return null;
  nums.sort((a, b) => a - b);
  return `${rackLower}-ct[${compressCtNums(nums)}]`;
}
