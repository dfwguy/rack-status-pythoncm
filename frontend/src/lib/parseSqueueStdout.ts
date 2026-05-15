/** Parse default `squeue` text into headers + rows (NAME may contain spaces). Legacy-aligned. */
export function parseSqueueStdout(stdout: string): {
  headers: string[];
  rows: string[][];
  useRaw: boolean;
} {
  const lines = String(stdout || "")
    .split(/\r?\n/)
    .map((l) => l.replace(/\s+$/, ""))
    .filter((l) => l.length);
  if (!lines.length) return { headers: [], rows: [], useRaw: false };
  let headerIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].trim();
    if (/\bJOBID\b/.test(t) && /\bPARTITION\b/.test(t)) {
      headerIdx = i;
      break;
    }
  }
  if (headerIdx < 0) return { headers: [], rows: [], useRaw: true };
  const headerLine = lines[headerIdx].trim();
  const headers = headerLine.split(/\s+/).filter(Boolean);
  if (headers.length < 2) return { headers: [], rows: [], useRaw: true };
  const rows: string[][] = [];
  for (let j = headerIdx + 1; j < lines.length; j++) {
    const line = lines[j].trim();
    if (!line) continue;
    const parts = line.split(/\s+/).filter(Boolean);
    if (!parts.length) continue;
    let row: string[] | null;
    if (parts.length === headers.length) {
      row = parts.slice();
    } else if (parts.length > headers.length && headers[2] === "NAME" && headers.length >= 4) {
      const extra = parts.length - headers.length;
      const name = parts.slice(2, 3 + extra).join(" ");
      row = [parts[0]!, parts[1]!, name, ...parts.slice(3 + extra)];
    } else if (parts.length < headers.length) {
      row = parts.concat(Array.from({ length: headers.length - parts.length }, () => ""));
    } else {
      const alt = line.split(/\s{2,}/).map((c) => c.trim()).filter(Boolean);
      row = alt.length === headers.length ? alt : null;
    }
    if (row && row.length === headers.length) rows.push(row);
    else rows.push([line]);
  }
  if (rows.some((r) => r.length === 1 && headers.length > 1)) return { headers: [], rows: [], useRaw: true };
  return { headers, rows, useRaw: false };
}
