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
/**
 * Parse `sinfo -R --long` text: header row column starts (Slurm fixed-width layout).
 * Skips stray date-only lines between rows. Legacy-aligned.
 */
export function parseSinfoRStdout(stdout: string): {
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
    const t = lines[i];
    if (/\bREASON\b/.test(t) && /\bNODELIST\b/.test(t)) {
      headerIdx = i;
      break;
    }
  }
  if (headerIdx < 0) return { headers: [], rows: [], useRaw: true };
  const headerLine = lines[headerIdx]!;
  const starts: number[] = [];
  const headers: string[] = [];
  const re = /\S+/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(headerLine)) !== null) {
    starts.push(m.index);
    headers.push(m[0]);
  }
  if (headers.length < 2 || starts.length < 2) return { headers: [], rows: [], useRaw: true };
  const rowFromFixed = (line: string): string[] => {
    const L = line.length;
    const row: string[] = [];
    for (let c = 0; c < starts.length; c++) {
      const from = starts[c]!;
      const to = c + 1 < starts.length ? starts[c + 1]! : L;
      const chunk = from < L ? line.slice(from, Math.min(to, L)) : "";
      row.push(chunk.trim());
    }
    return row;
  };
  const rows: string[][] = [];
  const dateOnly = /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\s+/i;
  for (let j = headerIdx + 1; j < lines.length; j++) {
    const line = lines[j]!;
    if (!line.length) continue;
    if (dateOnly.test(line) && !/\bREASON\b/.test(line)) continue;
    const row = rowFromFixed(line);
    if (row.some((cell) => cell.length)) rows.push(row);
  }
  return { headers, rows, useRaw: false };
}
