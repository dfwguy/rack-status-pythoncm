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
 * Render the node heatmap to a downloadable image.
 *
 * Drawn to a canvas rather than screenshotting the DOM. Two reasons: the export is for a report
 * that gets pasted into a document, so it wants a light background and a stated title, date and
 * legend regardless of which theme the operator happens to be using; and a DOM screenshot would
 * need a dependency that inlines computed styles, which is fragile with the CSS custom
 * properties Kaizen themes with.
 *
 * Geometry lives in `layoutHeatmap` as plain data so it can be tested without a canvas — jsdom
 * has no 2D context, and pixel output is not what is worth asserting on anyway.
 */
import {
  COMPACT_NODES_PER_ROW,
  COMPACT_RACKS_PER_ROW,
  chunkNodes,
  groupRacksByRow,
  shortRackLabel,
} from "./nodeHeatmap";

export interface HeatmapImageCell {
  id: string;
  /** Solid colour for the square. */
  fill: string;
  /** Outline colour, carrying the same second channel as the on-screen border. */
  border: string;
  /** Slot number, or "" when the naming scheme gives nothing short enough to fit. */
  label: string;
}

export interface HeatmapImageRack {
  rack: string;
  cells: HeatmapImageCell[];
}

export interface HeatmapLegendEntry {
  label: string;
  color: string;
}

export interface HeatmapImageModel {
  title: string;
  /** Cluster / customer line under the title. */
  subtitle: string;
  /** What the colours mean on the current view. */
  legend: HeatmapLegendEntry[];
  racks: HeatmapImageRack[];
}

/** Pixel geometry. Small enough to keep a 49-rack floor near 1000px wide. */
const M = {
  pad: 24,
  titleH: 26,
  subtitleH: 18,
  legendH: 22,
  legendSwatch: 11,
  legendGap: 16,
  rowLabelH: 20,
  rackLabelH: 17,
  rackGapX: 10,
  rackGapY: 12,
  rowGapY: 12,
  // Matches the on-screen compact cell: big enough that the tray number is readable in a
  // document, which is the only reason the number is drawn at all.
  cell: 28,
  cellGap: 3,
  cellFont: 14,
  rackFont: 13,
  rowFont: 14,
} as const;

/** Blocks are as wide as their cells: the label is shortened to fit rather than the reverse. */
const BLOCK_W = M.cell * COMPACT_NODES_PER_ROW + M.cellGap * (COMPACT_NODES_PER_ROW - 1);

export interface PlacedCell extends HeatmapImageCell {
  x: number;
  y: number;
}

export interface PlacedRack {
  rack: string;
  x: number;
  y: number;
  cells: PlacedCell[];
}

export interface PlacedRow {
  row: string;
  x: number;
  y: number;
}

export interface HeatmapLayout {
  width: number;
  height: number;
  racks: PlacedRack[];
  rows: PlacedRow[];
  legendY: number;
}

/**
 * Position every rack and cell, and report the canvas size needed to hold them.
 *
 * Pure: no canvas, no DOM. The drawing pass below only reads these coordinates.
 */
export function layoutHeatmap(model: HeatmapImageModel): HeatmapLayout {
  const rows = groupRacksByRow(model.racks);
  const perRow = COMPACT_RACKS_PER_ROW;
  const width = M.pad * 2 + BLOCK_W * perRow + M.rackGapX * (perRow - 1);

  const placedRacks: PlacedRack[] = [];
  const placedRows: PlacedRow[] = [];
  let y = M.pad + M.titleH + M.subtitleH + M.legendH + M.legendGap;
  const legendY = M.pad + M.titleH + M.subtitleH;

  for (const { row, racks } of rows) {
    if (row) {
      placedRows.push({ row, x: M.pad, y });
      y += M.rowLabelH;
    }
    const tallest = Math.max(...racks.map((r) => Math.ceil(r.cells.length / COMPACT_NODES_PER_ROW)), 1);
    racks.forEach((rack, i) => {
      const col = i % perRow;
      const bandIndex = Math.floor(i / perRow);
      const bandH = M.rackLabelH + tallest * (M.cell + M.cellGap) + M.rackGapY;
      const rx = M.pad + col * (BLOCK_W + M.rackGapX);
      const ry = y + bandIndex * bandH;
      const cells: PlacedCell[] = chunkNodes(rack.cells, COMPACT_NODES_PER_ROW).flatMap(
        (line, lineIndex) =>
          line.map((cell, colIndex) => ({
            ...cell,
            x: rx + colIndex * (M.cell + M.cellGap),
            y: ry + M.rackLabelH + lineIndex * (M.cell + M.cellGap),
          })),
      );
      placedRacks.push({ rack: rack.rack, x: rx, y: ry, cells });
    });
    const bands = Math.ceil(racks.length / perRow);
    y += bands * (M.rackLabelH + tallest * (M.cell + M.cellGap) + M.rackGapY) + M.rowGapY;
  }

  return { width, height: y + M.pad, racks: placedRacks, rows: placedRows, legendY };
}

const INK = "#111827";
const INK_SOFT = "#6b7280";
const PAPER = "#ffffff";

/** Paint the model onto a context already scaled for the device pixel ratio. */
export function drawHeatmap(
  ctx: CanvasRenderingContext2D,
  model: HeatmapImageModel,
  layout: HeatmapLayout,
): void {
  ctx.fillStyle = PAPER;
  ctx.fillRect(0, 0, layout.width, layout.height);
  ctx.textBaseline = "top";

  ctx.fillStyle = INK;
  ctx.font = "600 17px system-ui, -apple-system, Segoe UI, sans-serif";
  ctx.fillText(model.title, M.pad, M.pad);

  ctx.fillStyle = INK_SOFT;
  ctx.font = "12px system-ui, -apple-system, Segoe UI, sans-serif";
  ctx.fillText(model.subtitle, M.pad, M.pad + M.titleH);

  let lx = M.pad;
  for (const entry of model.legend) {
    ctx.fillStyle = entry.color;
    ctx.fillRect(lx, layout.legendY + 2, M.legendSwatch, M.legendSwatch);
    ctx.fillStyle = INK_SOFT;
    ctx.font = "11px system-ui, -apple-system, Segoe UI, sans-serif";
    ctx.fillText(entry.label, lx + M.legendSwatch + 5, layout.legendY + 2);
    lx += M.legendSwatch + 9 + ctx.measureText(entry.label).width + M.legendGap;
  }

  for (const row of layout.rows) {
    ctx.fillStyle = INK;
    ctx.font = `600 ${M.rowFont}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.fillText(row.row, row.x, row.y);
  }

  for (const rack of layout.racks) {
    ctx.fillStyle = INK_SOFT;
    ctx.font = `${M.rackFont}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    // Short label for the same reason as on screen: the row heading carries the prefix.
    ctx.fillText(shortRackLabel(rack.rack), rack.x, rack.y);
    for (const cell of rack.cells) {
      ctx.fillStyle = cell.fill;
      ctx.fillRect(cell.x, cell.y, M.cell, M.cell);
      ctx.strokeStyle = cell.border;
      ctx.lineWidth = 2;
      ctx.strokeRect(cell.x + 1, cell.y + 1, M.cell - 2, M.cell - 2);
      if (cell.label) {
        ctx.fillStyle = readableOn(cell.fill);
        ctx.font = `${M.cellFont}px ui-monospace, SFMono-Regular, Menlo, monospace`;
        const w = ctx.measureText(cell.label).width;
        ctx.fillText(cell.label, cell.x + (M.cell - w) / 2, cell.y + (M.cell - M.cellFont) / 2);
      }
    }
  }
}

/**
 * Black or white text for a swatch, by perceived brightness.
 *
 * Cells are coloured from several scales (status, completion, the blue throughput ramp), so a
 * fixed text colour is unreadable on one end of at least one of them.
 */
export function readableOn(fill: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(fill.trim()) ?? /rgb\((\d+),\s*(\d+),\s*(\d+)\)/i.exec(fill);
  let r = 0;
  let g = 0;
  let b = 0;
  if (m && m[0].startsWith("rgb")) {
    [r, g, b] = [Number(m[1]), Number(m[2]), Number(m[3])];
  } else if (m) {
    const hex = m[1];
    r = parseInt(hex.slice(0, 2), 16);
    g = parseInt(hex.slice(2, 4), 16);
    b = parseInt(hex.slice(4, 6), 16);
  }
  return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? "#111827" : "#ffffff";
}

export type ImageFormat = "png" | "jpeg";

/** Filename with the cluster and date baked in, so downloads do not collide in ~/Downloads. */
export function imageFilename(cluster: string, format: ImageFormat, now = new Date()): string {
  const slug = (cluster || "cluster").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const stamp = now.toISOString().slice(0, 10);
  return `node-heatmap-${slug || "cluster"}-${stamp}.${format === "jpeg" ? "jpg" : "png"}`;
}

/**
 * Render to a Blob at 2x so the small type stays sharp when the image is scaled in a document.
 * Resolves null when the browser cannot give us a 2D context.
 */
export function heatmapImageBlob(
  model: HeatmapImageModel,
  format: ImageFormat,
): Promise<Blob | null> {
  const layout = layoutHeatmap(model);
  const scale = 2;
  const canvas = document.createElement("canvas");
  canvas.width = layout.width * scale;
  canvas.height = layout.height * scale;
  const ctx = canvas.getContext("2d");
  if (!ctx) return Promise.resolve(null);
  ctx.scale(scale, scale);
  drawHeatmap(ctx, model, layout);
  return new Promise((resolve) => {
    // JPEG has no alpha, and the background is already opaque white, so quality is the only knob.
    canvas.toBlob((blob) => resolve(blob), `image/${format}`, format === "jpeg" ? 0.95 : undefined);
  });
}

/** Build the image and hand it to the browser as a download. */
export async function downloadHeatmapImage(
  model: HeatmapImageModel,
  format: ImageFormat,
  cluster: string,
): Promise<void> {
  const blob = await heatmapImageBlob(model, format);
  if (!blob) throw new Error("could not render the heatmap image in this browser");
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = imageFilename(cluster, format);
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}
