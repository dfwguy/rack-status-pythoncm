/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
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
  const n = headers.length;
  // The last column (NODELIST(REASON)) holds a parenthesized, space-containing reason for
  // pending jobs, e.g. "(launch failed requeued held)". NAME (col 2) may also contain spaces.
  const lastIsReason = /REASON/i.test(headers[n - 1] ?? "");
  const foldName = (front: string[], tail: string[]): string[] | null => {
    // Map `front` tokens onto headers[0 .. n-1-tail.length], folding overflow into NAME.
    const want = n - tail.length;
    if (front.length === want) return [...front, ...tail];
    if (front.length > want && headers[2] === "NAME" && want >= 4) {
      const extra = front.length - want;
      const name = front.slice(2, 3 + extra).join(" ");
      return [front[0]!, front[1]!, name, ...front.slice(3 + extra), ...tail];
    }
    if (front.length < want) {
      return [...front, ...Array.from({ length: want - front.length }, () => ""), ...tail];
    }
    return null;
  };
  const rows: string[][] = [];
  for (let j = headerIdx + 1; j < lines.length; j++) {
    const line = lines[j].trim();
    if (!line) continue;
    const parts = line.split(/\s+/).filter(Boolean);
    if (!parts.length) continue;
    let row: string[] | null;
    if (parts.length === n) {
      row = parts.slice();
    } else if (lastIsReason && parts.length > n && (() => {
      const p = parts.findIndex((tok) => tok.startsWith("("));
      return p >= 0 && p >= n - 1;
    })()) {
      // Parenthesized reason: everything from the first "(" token to EOL is the last column.
      const p = parts.findIndex((tok) => tok.startsWith("("));
      row = foldName(parts.slice(0, p), [parts.slice(p).join(" ")]);
    } else if (parts.length > n && headers[2] === "NAME" && n >= 4) {
      const extra = parts.length - n;
      const name = parts.slice(2, 3 + extra).join(" ");
      row = [parts[0]!, parts[1]!, name, ...parts.slice(3 + extra)];
    } else if (parts.length < n) {
      row = parts.concat(Array.from({ length: n - parts.length }, () => ""));
    } else {
      const alt = line.split(/\s{2,}/).map((c) => c.trim()).filter(Boolean);
      row = alt.length === n ? alt : null;
    }
    if (row && row.length === n) rows.push(row);
    else rows.push([line]);
  }
  if (rows.some((r) => r.length === 1 && headers.length > 1)) return { headers: [], rows: [], useRaw: true };
  return { headers, rows, useRaw: false };
}
