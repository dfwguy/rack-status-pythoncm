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
 * Parse nvloom_cli-style text the same way as workspace/nvloom/cli/plot_heatmaps.py:
 * Process lines, Running <name> blocks, matrix rows until an empty line, then Min… for units.
 * Slurm task labels (e.g. "0: …") are stripped so logs match the Python script's stdin expectations.
 */

export interface NvloomTestcase {
  name: string;
  labelsX: string[];
  labelsY: string[];
  columnsSeparators: number[];
  rowSeparators: number[];
  rackGuidList: string[];
  /** Parsed numeric matrix (NaN for N/A). */
  matrix: number[][];
  unit: string;
}

export interface ParsedNvloomLog {
  testcases: NvloomTestcase[];
}

/** Strip leading `N: ` task label from srun --label output. */
export function stripSlurmTaskPrefix(line: string): string {
  return line.replace(/^\s*\d+:\s*/, "").trim();
}

function parseProcessLine(line: string): { processName: string; rackGuid: string } | null {
  if (!line.startsWith("Process")) return null;
  const words = line.split(/\s+/).filter((w) => w.length > 0);
  if (words.length < 6) return null;
  const hostToken = words[2];
  if (hostToken.length < 3 || !hostToken.startsWith("(")) return null;
  const hostname = hostToken.slice(1, -2).split(".")[0] || hostToken;
  const gpuToken = words[4];
  const gpuId = gpuToken.endsWith(":") ? gpuToken.slice(0, -1) : gpuToken;
  const rackGuid = words[words.length - 1] ?? "";
  return { processName: `${hostname}/${gpuId}`, rackGuid };
}

export function prepareRowSeparatorList(rackGuidList: string[]): number[] {
  const rowSeparators: number[] = [];
  for (let idx = 0; idx < rackGuidList.length; idx++) {
    if (idx + 1 < rackGuidList.length && rackGuidList[idx + 1] !== rackGuidList[idx]) {
      rowSeparators.push(idx + 1);
    }
  }
  return rowSeparators;
}

export function parseColumnSeparators(labelsX: string[]): { filteredLabelsX: string[]; columnsSeparators: number[] } {
  let haveDash = false;
  let counter = 0;
  const filteredLabelsX: string[] = [];
  const columnsSeparators: number[] = [];
  for (const labelX of labelsX) {
    if (labelX === "|") {
      columnsSeparators.push(counter);
    } else {
      if (labelX.includes("-")) {
        haveDash = true;
        if (columnsSeparators.length === filteredLabelsX.length) {
          filteredLabelsX.push((labelX.split("-")[0] ?? labelX).trim());
        }
      } else {
        filteredLabelsX.push(labelX);
      }
      counter += 1;
    }
  }
  if (!haveDash) {
    return { filteredLabelsX: [...labelsX], columnsSeparators };
  }
  return { filteredLabelsX, columnsSeparators };
}

export function parseResultLine(line: string): { values: string[]; labelY: string } {
  const parts = line.split(/\s+/).filter((w) => w.length > 0 && w !== "|");
  const labelY = parts[0] ?? "";
  const resultValues = parts.slice(1).map((word) => (word.includes(";") ? word.slice(0, word.indexOf(";")) : word));
  return { values: resultValues, labelY };
}

export function enrichLabelsY(labelsY: string[], processToGpuList: string[]): string[] {
  const defaultList = labelsY.map((_, i) => String(i));
  if (defaultList.length === labelsY.length && labelsY.every((y, i) => y === defaultList[i]) && labelsY.length === processToGpuList.length) {
    return [...processToGpuList];
  }
  return labelsY;
}

export function parseUnit(line: string): string {
  const words = line.split(/\s+/).filter(Boolean);
  if (words.length < 4) return "GB/s";
  return words[3] ?? "GB/s";
}

function convertResultsToFloat(results: string[][]): number[][] {
  return results.map((row) =>
    row.map((x) => {
      if (x === "N/A") return Number.NaN;
      const n = Number.parseFloat(x);
      return Number.isFinite(n) ? n : Number.NaN;
    }),
  );
}

function flattenFinite(matrix: number[][]): number[] {
  const out: number[] = [];
  for (const row of matrix) {
    for (const y of row) {
      if (Number.isFinite(y)) out.push(y);
    }
  }
  return out;
}

export function getStatsValues(matrix: number[][]): { min: number; avg: number; max: number } | null {
  const flat = flattenFinite(matrix);
  if (!flat.length) return null;
  const min = Math.min(...flat);
  const max = Math.max(...flat);
  const avg = flat.reduce((a, b) => a + b, 0) / flat.length;
  return { min, avg, max };
}

function allNan(matrix: number[][]): boolean {
  return matrix.every((row) => row.every((v) => !Number.isFinite(v)));
}

export function parseNvloomCliLog(raw: string): ParsedNvloomLog {
  const rawLines = raw.split("\n");
  const lines = rawLines
    .map((ln) => stripSlurmTaskPrefix(ln))
    .filter((ln) => ln !== "" && !ln.startsWith("sleeping for"));

  const processToGpuList: string[] = [];
  const rackGuidList: string[] = [];
  let rowSeparators: number[] = [];
  const testcases: NvloomTestcase[] = [];

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith("Process")) {
      const pr = parseProcessLine(line);
      if (pr) {
        processToGpuList.push(pr.processName);
        rackGuidList.push(pr.rackGuid);
      }
    }

    const runParts = line.split(/\s+/).filter(Boolean);
    if (line.startsWith("Running") && runParts.length === 2) {
      if (rowSeparators.length === 0 && rackGuidList.length > 0) {
        rowSeparators = prepareRowSeparatorList(rackGuidList);
      }
      const name = runParts[1] ?? "unknown";
      i += 1;
      if (i >= lines.length) break;

      if (lines[i].includes("WAIVED")) {
        i += 1;
        continue;
      }

      const headerTokens = lines[i].split(/\s+/).filter(Boolean);
      const { filteredLabelsX, columnsSeparators } = parseColumnSeparators(headerTokens);
      i += 1;

      const labelsY: string[] = [];
      const results: string[][] = [];

      while (i < lines.length) {
        const ln = lines[i];
        const toks = ln.split(/\s+/).filter(Boolean);
        if (ln.startsWith("Min") || toks.length === 0) break;
        const { values, labelY } = parseResultLine(ln);
        results.push(values);
        labelsY.push(labelY);
        i += 1;
      }

      while (i < lines.length && !lines[i].startsWith("Min")) {
        i += 1;
      }
      if (i >= lines.length) break;

      const unit = parseUnit(lines[i]);
      const labelsYUse = enrichLabelsY(labelsY, processToGpuList);
      const matrix = convertResultsToFloat(results);

      if (!allNan(matrix) && matrix.length > 0 && matrix[0].length > 0) {
        testcases.push({
          name,
          labelsX: filteredLabelsX,
          labelsY: labelsYUse,
          columnsSeparators,
          rowSeparators,
          rackGuidList: [...rackGuidList],
          matrix,
          unit,
        });
      }
      i += 1;
      continue;
    }

    i += 1;
  }

  return { testcases };
}

export function getNodesDescription(labelsY: string[]): string {
  const nodes = [...new Set(labelsY.map((lb) => lb.split("/")[0] ?? lb))].filter(Boolean);
  if (nodes.length === 0) return "";
  if (nodes.length === 1) return nodes[0] ?? "";
  return `${nodes.sort().join(", ")} (${nodes.length} nodes)`;
}
