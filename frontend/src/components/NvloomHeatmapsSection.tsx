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
import Box from "@mui/material/Box";
import { Text, Card } from "@nvidia/foundations-react-core";
import {
  getNodesDescription,
  getStatsValues,
  type NvloomTestcase,
  parseNvloomCliLog,
} from "../lib/nvloomHeatmapParse";
import { textColorForYlGnNorm, ylGnColor } from "../lib/ylGnHeatmap";

function heatmapLimits(tc: NvloomTestcase): { vmin: number; vmax: number } {
  const flat: number[] = [];
  for (const row of tc.matrix) {
    for (const v of row) {
      if (Number.isFinite(v)) flat.push(v);
    }
  }
  if (!flat.length) return { vmin: 0, vmax: 1 };
  const vmin = 0;
  const vmax = Math.max(...flat);
  return { vmin, vmax: vmax > vmin ? vmax : vmin + 1 };
}

interface OneHeatmapProps {
  tc: NvloomTestcase;
}

function OneNvloomHeatmap({ tc }: OneHeatmapProps) {
  const { vmin, vmax } = heatmapLimits(tc);
  const stats = getStatsValues(tc.matrix);
  const nRows = tc.matrix.length;
  const nCols = tc.matrix[0]?.length ?? 0;
  const fontPx = Math.max(9, Math.min(14, Math.round(72 / Math.max(nRows, nCols, 8))));

  const thick = "3px solid #111";
  const thin = "1px solid rgba(255,255,255,0.35)";

  return (
    <Card
      style={{
        padding: 16,
        marginBottom: 20,
        maxWidth: "100%",
        boxSizing: "border-box",
        overflow: "hidden",
      }}
    >
      <Text kind="body/bold/md" style={{ display: "block", marginBottom: 4 }}>
        {tc.name}
      </Text>
      <Text
        kind="body/regular/sm"
        style={{
          color: "var(--text-color-secondary)",
          marginBottom: 12,
          display: "block",
          overflowWrap: "anywhere",
          wordBreak: "break-word",
        }}
      >
        {getNodesDescription(tc.labelsY)}
      </Text>
      <Box
        sx={{
          display: "flex",
          flexDirection: { xs: "column", md: "row" },
          gap: 2,
          alignItems: "stretch",
          width: "100%",
          maxWidth: "100%",
          minWidth: 0,
        }}
      >
        <Box
          sx={{
            flex: "1 1 0%",
            minWidth: 0,
            maxWidth: "100%",
            overflowX: "auto",
            overflowY: "visible",
            WebkitOverflowScrolling: "touch",
          }}
        >
          <table
            style={{
              borderCollapse: "collapse",
              fontSize: fontPx,
              fontFamily: "monospace",
              width: "max-content",
              maxWidth: "none",
            }}
          >
            <thead>
              <tr>
                <th style={{ padding: 4 }} />
                {tc.labelsX.map((lx, j) => {
                  const colSep = tc.columnsSeparators.includes(j);
                  return (
                    <th
                      key={`h-${j}`}
                      style={{
                        padding: "4px 6px",
                        borderLeft: colSep ? thick : undefined,
                        fontWeight: 600,
                        textAlign: "center",
                      }}
                    >
                      {lx}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {tc.matrix.map((row, i) => {
                const rowSep = tc.rowSeparators.includes(i);
                return (
                  <tr key={`r-${i}`} style={{ borderTop: rowSep ? thick : undefined }}>
                    <td
                      style={{
                        padding: "4px 8px 4px 0",
                        textAlign: "right",
                        whiteSpace: "nowrap",
                        color: "var(--text-color-secondary)",
                        verticalAlign: "middle",
                      }}
                    >
                      {tc.labelsY[i] ?? String(i)}
                    </td>
                    {row.map((v, j) => {
                      const colSep = tc.columnsSeparators.includes(j);
                      const nan = !Number.isFinite(v);
                      const norm = nan ? 0 : (v - vmin) / (vmax - vmin || 1);
                      const bg = nan ? "rgba(128,128,128,0.25)" : ylGnColor(norm);
                      return (
                        <td
                          key={`c-${i}-${j}`}
                          style={{
                            borderLeft: colSep ? thick : thin,
                            borderTop: thin,
                            backgroundColor: bg,
                            color: nan ? "var(--text-color-secondary)" : textColorForYlGnNorm(norm),
                            textAlign: "center",
                            minWidth: 44,
                            padding: "6px 4px",
                            fontWeight: 500,
                          }}
                        >
                          {nan ? "—" : Math.round(v).toString()}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Box>
        <Box
          sx={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 1,
            width: 56,
            flexShrink: 0,
            alignSelf: { xs: "center", md: "stretch" },
          }}
        >
          <Text kind="body/regular/xs" style={{ color: "var(--text-color-secondary)" }}>
            {tc.unit}
          </Text>
          <Box
            sx={{
              width: 22,
              flex: 1,
              minHeight: 120,
              borderRadius: 1,
              border: "1px solid rgba(0,0,0,0.2)",
              background: "linear-gradient(to top, rgb(0,104,55), rgb(173,221,142), rgb(255,255,229))",
            }}
          />
          <Text kind="body/regular/xs" style={{ fontFamily: "monospace" }}>
            {Math.round(vmax)}
          </Text>
          <Text kind="body/regular/xs" style={{ fontFamily: "monospace" }}>
            {Math.round(vmin)}
          </Text>
        </Box>
      </Box>

      {stats && (
        <Box sx={{ mt: 2 }}>
          <Text kind="body/bold/sm" style={{ marginBottom: 6 }}>
            summary
          </Text>
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 1, maxWidth: 360 }}>
            {(["min", "avg", "max"] as const).map((k, idx) => {
              const val = k === "min" ? stats.min : k === "avg" ? stats.avg : stats.max;
              const norm = (val - vmin) / (vmax - vmin || 1);
              return (
                <Box
                  key={k}
                  sx={{
                    textAlign: "center",
                    py: 1.5,
                    px: 1,
                    borderRadius: 1,
                    backgroundColor: ylGnColor(norm),
                    color: textColorForYlGnNorm(norm),
                    fontFamily: "monospace",
                    fontWeight: 600,
                    fontSize: 14,
                  }}
                >
                  <Text kind="body/regular/xs" style={{ opacity: 0.85, display: "block", marginBottom: 4 }}>
                    {k}
                  </Text>
                  {idx === 1 ? val.toFixed(1) : Math.round(val).toString()}
                </Box>
              );
            })}
          </Box>
        </Box>
      )}
    </Card>
  );
}

export interface NvloomHeatmapsSectionProps {
  logPath: string;
  content: string;
  loading?: boolean;
  error?: string;
}

export function NvloomHeatmapsSection({ logPath, content, loading, error }: NvloomHeatmapsSectionProps) {
  if (loading) {
    return (
      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
        Loading NVLOOM log…
      </Text>
    );
  }
  if (error) {
    return <Text kind="body/regular/md">{error}</Text>;
  }
  if (!content.trim()) {
    return (
      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
        No NVLOOM log text (no file under <span style={{ fontFamily: "monospace" }}>log_dir/nvloom/</span> for this rack,
        or log is empty).
      </Text>
    );
  }

  const { testcases } = parseNvloomCliLog(content);
  if (!testcases.length) {
    return (
      <Box>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
          Log loaded but no heatmap blocks were parsed (expected <span style={{ fontFamily: "monospace" }}>Process</span>,{" "}
          <span style={{ fontFamily: "monospace" }}>Running &lt;name&gt;</span>, matrix rows, then{" "}
          <span style={{ fontFamily: "monospace" }}>Min …</span>).
        </Text>
        {logPath && (
          <Text kind="mono/sm" style={{ wordBreak: "break-all", opacity: 0.9 }}>
            {logPath}
          </Text>
        )}
      </Box>
    );
  }

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 0, minWidth: 0, maxWidth: "100%" }}>
      {logPath && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
          Source: <span style={{ fontFamily: "monospace", wordBreak: "break-all" }}>{logPath}</span>
        </Text>
      )}
      {testcases.map((tc) => (
        <OneNvloomHeatmap key={tc.name} tc={tc} />
      ))}
    </Box>
  );
}
