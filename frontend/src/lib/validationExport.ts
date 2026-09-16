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
import type { PostRunNode } from "../types/api";
import type { EtaPrediction, NodeValidationState, SuGroup, ValidationPoint, ValidationStatus } from "./validationStatus";

export const VALIDATION_EXPORT_SCHEMA = "rack-status-validation-v2";
export const PRINT_WIDTH = 3300;
export const PRINT_HEIGHT = 2550;

export interface ValidationExportNode {
  node: PostRunNode;
  su: string;
  state: NodeValidationState;
  prediction: EtaPrediction;
}

export const VALIDATION_CSV_COLUMNS = [
  "schema_version", "generated_at", "selected_mode", "su", "rack", "node", "overall_status",
  "running", "source_freshness", "evidence_timestamp", "predicted_completion", "eta_confidence",
  "test_name", "test_status", "metric", "metric_unit", "job_id", "failure_reason", "source", "provenance",
] as const;

function csvSafe(value: unknown): string {
  let text = String(value ?? "").replace(/[\r\n\t]+/g, " ").trim();
  if (/^[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

export function validationCsv(
  items: ValidationExportNode[],
  modeLabel: string,
  generatedAt: string,
  sourceFreshness: string,
): string {
  const rows: string[][] = [];
  for (const item of items) {
    const points: Array<ValidationPoint | null> = item.state.points.length ? item.state.points : [null];
    for (const point of points) {
      rows.push([
        VALIDATION_EXPORT_SCHEMA,
        generatedAt,
        modeLabel,
        item.su,
        item.node.rack,
        item.node.node,
        item.state.status,
        item.state.running ? "true" : "false",
        sourceFreshness,
        point?.timestamp || "",
        item.prediction.eta || item.prediction.state,
        item.prediction.confidence,
        point?.name || "",
        point?.rawStatus || "NO_DATA",
        point?.metric || "",
        point?.metricUnit || "",
        point?.jobId || "",
        point?.reason || "",
        point?.source || "",
        point?.provenance || "",
      ]);
    }
  }
  return [VALIDATION_CSV_COLUMNS.map(csvSafe).join(","), ...rows.map((row) => row.map(csvSafe).join(","))].join("\r\n") + "\r\n";
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

const STATUS_COLOR: Record<ValidationStatus, string> = {
  pass: "#2e7d32",
  attention: "#c62828",
  warning: "#f9a825",
  not_run: "#7f1d1d",
  running: "#f9a825",
  unknown: "#757575",
};

const SEGMENT_NOT_RUN = "#a94b4b";
const SEGMENT_RUNNING = "#f9a825";
const SEGMENT_UNKNOWN = "#757575";
const FAILURE_COLORS = ["#3568a8", "#6e52a8", "#287f8c", "#795548", "#9b4572", "#4f5c9c", "#327f8c", "#754c8f"];

export function failureColor(name: string): string {
  let hash = 0;
  for (const char of name) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return FAILURE_COLORS[Math.abs(hash) % FAILURE_COLORS.length];
}

export function segmentColor(point: ValidationPoint): string {
  if (point.status === "pass") return STATUS_COLOR.pass;
  if (point.status === "not_run") return SEGMENT_NOT_RUN;
  if (point.status === "running") return SEGMENT_RUNNING;
  if (point.status === "warning") return STATUS_COLOR.warning;
  if (point.status === "unknown") return SEGMENT_UNKNOWN;
  return STATUS_COLOR.attention;
}

export function validationGradient(state: NodeValidationState): string {
  if (!state.visiblePoints.length) return STATUS_COLOR[state.status];
  const width = 100 / state.visiblePoints.length;
  const stops = state.visiblePoints.flatMap((point, index) => {
    const start = (index * width).toFixed(3);
    const end = ((index + 1) * width).toFixed(3);
    const color = segmentColor(point);
    return [`${color} ${start}%`, `${color} ${end}%`];
  });
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

export interface ValidationMapRack {
  rack: string;
  nodes: Array<{ node: PostRunNode; state: NodeValidationState }>;
}

export interface ValidationMapOptions {
  groups: SuGroup<{ node: PostRunNode; state: NodeValidationState }>[];
  modeLabel: string;
  generatedAt: string;
  freshness: string;
  clusterPrediction: EtaPrediction;
}

const CANVAS = {
  padX: 40,
  titleY: 30,
  mapY: 104,
  suLabelW: 68,
  rackW: 174,
  rackGap: 6,
  separatorGap: 18,
  rowH: 151,
  rackLabelH: 19,
  nodeGap: 2,
  nodeH: 18,
} as const;

function drawText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, font: string, color = "#111827"): void {
  ctx.font = font;
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function rackX(column: number): number {
  const separators = (column >= 4 ? 1 : 0) + (column >= 8 ? 1 : 0) + (column >= 12 ? 1 : 0);
  return CANVAS.padX + CANVAS.suLabelW + column * (CANVAS.rackW + CANVAS.rackGap) + separators * CANVAS.separatorGap;
}

function shortRack(rack: string): string {
  const parts = rack.split("-");
  return parts.slice(-2).join("-");
}

function nodeSlot(node: string): string {
  return node.split("-").at(-1) || node;
}

function drawSeparator(ctx: CanvasRenderingContext2D, x: number, y: number, height: number, dashed: boolean): void {
  ctx.save();
  ctx.strokeStyle = "#111111";
  ctx.lineWidth = 3;
  ctx.setLineDash(dashed ? [9, 7] : []);
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x, y + height);
  ctx.stroke();
  ctx.restore();
}

export function drawValidationMap(ctx: CanvasRenderingContext2D, options: ValidationMapOptions): void {
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, PRINT_WIDTH, PRINT_HEIGHT);
  ctx.textBaseline = "top";
  const stamp = new Date(options.generatedAt || Date.now()).toLocaleString();
  drawText(ctx, `health-check · ${options.modeLabel} · ${stamp}`, CANVAS.padX, CANVAS.titleY, "700 28px system-ui");
  drawText(ctx, `Live Rack Status evidence · ${options.freshness}`, CANVAS.padX, 67, "16px system-ui", "#4b5563");
  drawText(ctx, `Cluster prediction: ${options.clusterPrediction.label} (${options.clusterPrediction.confidence} confidence)`, 1710, 67, "16px system-ui", "#4b5563");

  options.groups.slice(0, 14).forEach((group, rowIndex) => {
    const y = CANVAS.mapY + rowIndex * CANVAS.rowH;
    drawText(ctx, group.id, CANVAS.padX, y + 38, "700 17px system-ui");
    drawText(ctx, group.rowKey, CANVAS.padX, y + 59, "13px ui-monospace", "#4b5563");
    [4, 8, 12].forEach((column) => {
      const x = rackX(column) - (CANVAS.separatorGap / 2) - 3;
      drawSeparator(ctx, x, y, 129, column !== 8);
    });
    group.racks.slice(0, 16).forEach((rack, column) => {
      const x = rackX(column);
      drawText(ctx, shortRack(rack.rack), x, y, "700 13px ui-monospace");
      const nodeW = (CANVAS.rackW - CANVAS.nodeGap * 2) / 3;
      rack.nodes.slice(0, 18).forEach((entry, index) => {
        const nx = x + (index % 3) * (nodeW + CANVAS.nodeGap);
        const ny = y + CANVAS.rackLabelH + Math.floor(index / 3) * (CANVAS.nodeH + CANVAS.nodeGap);
        const points = entry.state.visiblePoints;
        if (!points.length) {
          ctx.fillStyle = STATUS_COLOR[entry.state.status];
          ctx.fillRect(nx, ny, nodeW, CANVAS.nodeH);
        } else {
          const segmentW = nodeW / points.length;
          points.forEach((point, pointIndex) => {
            ctx.fillStyle = segmentColor(point);
            ctx.fillRect(nx + pointIndex * segmentW, ny, segmentW + 0.25, CANVAS.nodeH);
          });
        }
        ctx.strokeStyle = STATUS_COLOR[entry.state.status];
        ctx.lineWidth = entry.state.status === "pass" ? 1.5 : 2.5;
        ctx.strokeRect(nx + 0.5, ny + 0.5, nodeW - 1, CANVAS.nodeH - 1);
        drawText(ctx, nodeSlot(entry.node.node), nx + 3, ny + 3, "700 9px ui-monospace", "#ffffff");
      });
    });
  });

  const legendY = 2265;
  const legend = [
    ["all done / good", STATUS_COLOR.pass], ["failed / blocked", STATUS_COLOR.attention],
    ["warning / incomplete", STATUS_COLOR.warning], ["actively running", STATUS_COLOR.running],
    ["all NOT_RUN / never checked", STATUS_COLOR.not_run], ["NOT_RUN segment", SEGMENT_NOT_RUN],
    ["unknown / no evidence", STATUS_COLOR.unknown],
  ];
  drawText(ctx, "Legend", CANVAS.padX, legendY, "700 17px system-ui");
  let x = CANVAS.padX;
  for (const [label, color] of legend) {
    ctx.fillStyle = color;
    ctx.fillRect(x, legendY + 28, 22, 16);
    drawText(ctx, label, x + 28, legendY + 27, "13px system-ui", "#374151");
    x += 28 + ctx.measureText(label).width + 28;
  }
  drawText(ctx, "Up to 15 check segments are drawn; every check and its raw source status remain available in details and exports.", CANVAS.padX, legendY + 60, "13px system-ui", "#4b5563");
  drawText(ctx, options.clusterPrediction.methodology, CANVAS.padX, legendY + 86, "12px system-ui", "#6b7280");
  drawText(ctx, `${options.groups.reduce((sum, group) => sum + group.racks.reduce((rackSum, rack) => rackSum + rack.nodes.length, 0), 0)} nodes · ${options.groups.reduce((sum, group) => sum + group.racks.length, 0)} racks · ${options.groups.length} SUs`, CANVAS.padX, legendY + 113, "12px system-ui", "#6b7280");
}

export function createValidationCanvas(options: ValidationMapOptions): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = PRINT_WIDTH;
  canvas.height = PRINT_HEIGHT;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("This browser cannot create the validation graphic.");
  drawValidationMap(ctx, options);
  return canvas;
}

function ascii(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

function concat(parts: Uint8Array[]): Uint8Array {
  const length = parts.reduce((sum, part) => sum + part.length, 0);
  const result = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

export function jpegDataUrlBytes(dataUrl: string): Uint8Array {
  const base64 = dataUrl.split(",", 2)[1] || "";
  const binary = atob(base64);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

/** Set the JFIF density fields so 3300x2550 opens as an 11x8.5 inch, 300-DPI image. */
export function setJpegDpi(jpeg: Uint8Array, dpi = 300): Uint8Array {
  const out = Uint8Array.from(jpeg);
  const hasJfif = out.length >= 18
    && out[0] === 0xff && out[1] === 0xd8 && out[2] === 0xff && out[3] === 0xe0
    && String.fromCharCode(...out.slice(6, 11)) === "JFIF\0";
  if (!hasJfif) return out;
  const density = Math.max(1, Math.min(65_535, Math.round(dpi)));
  out[13] = 1; // dots per inch
  out[14] = (density >> 8) & 0xff;
  out[15] = density & 0xff;
  out[16] = (density >> 8) & 0xff;
  out[17] = density & 0xff;
  return out;
}

function pdfEscape(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[^\x20-\x7e]/g, "?")
    .replace(/\\/g, "\\\\")
    .replace(/\(/g, "\\(")
    .replace(/\)/g, "\\)");
}

function pdfDocument(objects: Uint8Array[]): Uint8Array {
  const parts: Uint8Array[] = [ascii("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n")];
  const offsets = [0];
  let cursor = parts[0].length;
  objects.forEach((object, index) => {
    offsets.push(cursor);
    const wrapped = concat([ascii(`${index + 1} 0 obj\n`), object, ascii("\nendobj\n")]);
    parts.push(wrapped);
    cursor += wrapped.length;
  });
  const xrefOffset = cursor;
  const xref = ["xref", `0 ${objects.length + 1}`, "0000000000 65535 f ", ...offsets.slice(1).map((offset) => `${String(offset).padStart(10, "0")} 00000 n `)].join("\n");
  parts.push(ascii(`${xref}\ntrailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`));
  return concat(parts);
}

/** Wrap the selected 3300x2550 JPEG in a deterministic one-page 11x8.5 landscape PDF. */
export function jpegPdf(jpeg: Uint8Array, width = PRINT_WIDTH, height = PRINT_HEIGHT): Uint8Array {
  const mapStream = ascii("q 792 0 0 612 0 0 cm /Im0 Do Q");
  return pdfDocument([
    ascii("<< /Type /Catalog /Pages 2 0 R >>"),
    ascii("<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
    ascii("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 792 612] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"),
    concat([ascii(`<< /Type /XObject /Subtype /Image /Width ${width} /Height ${height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>\nstream\n`), jpeg, ascii("\nendstream")]),
    concat([ascii(`<< /Length ${mapStream.length} >>\nstream\n`), mapStream, ascii("\nendstream")]),
  ]);
}

/** Map first, followed by compact evidence pages for every node/check in the active selection. */
export function validationReportPdf(
  jpeg: Uint8Array,
  items: ValidationExportNode[],
  modeLabel: string,
  generatedAt: string,
  sourceFreshness: string,
  clusterPrediction: EtaPrediction,
): Uint8Array {
  const lines = [
    `Validation Status | ${modeLabel} | generated ${generatedAt}`,
    `Sources: ${sourceFreshness}`,
    `Cluster prediction: ${clusterPrediction.label} | confidence ${clusterPrediction.confidence}`,
    "Exception appendix (all passing nodes remain represented on the map; CSV contains every check row).",
    "SU  RACK               NODE                         STATUS      ETA/STATE                 CONF",
  ];
  const exceptions = items.filter((item) => item.state.status !== "pass");
  if (!exceptions.length) lines.push("No exception nodes in this selection.");
  for (const item of exceptions) {
    lines.push(
      `${item.su.padEnd(4)}${item.node.rack.slice(-18).padEnd(19)}${item.node.node.slice(-29).padEnd(30)}${item.state.status.padEnd(12)}${(item.prediction.eta || item.prediction.state).slice(0, 25).padEnd(26)}${item.prediction.confidence}`,
    );
    const exceptionPoints = item.state.points.filter((point) => point.status !== "pass");
    if (!exceptionPoints.length) {
      lines.push("    NO_DATA | no evidence reported; node is not treated as healthy");
      continue;
    }
    const evidence = exceptionPoints.slice(0, 3).map((point) => [
        point.name,
        point.rawStatus,
        point.jobId ? `job=${point.jobId}` : "",
        point.timestamp ? `at=${point.timestamp}` : "",
        point.metric ? `${point.metric}${point.metricUnit ? ` ${point.metricUnit}` : ""}` : "",
        point.reason,
      ].filter(Boolean).join(" | ")).join(" || ");
    const more = exceptionPoints.length > 3 ? ` || +${exceptionPoints.length - 3} more in CSV` : "";
    lines.push(`    ${evidence}${more}`.slice(0, 166));
  }
  const linesPerPage = 48;
  const chunks: string[][] = [];
  for (let index = 0; index < lines.length; index += linesPerPage) chunks.push(lines.slice(index, index + linesPerPage));

  // Object numbers: 1 catalog, 2 pages, 3 font, 4 image, 5 map content, 6 map page,
  // then one content/page pair per evidence page.
  const pageRefs = ["6 0 R", ...chunks.map((_chunk, index) => `${8 + index * 2} 0 R`)];
  const mapStream = ascii("q 792 0 0 612 0 0 cm /Im0 Do Q");
  const objects: Uint8Array[] = [
    ascii("<< /Type /Catalog /Pages 2 0 R >>"),
    ascii(`<< /Type /Pages /Kids [${pageRefs.join(" ")}] /Count ${pageRefs.length} >>`),
    ascii("<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"),
    concat([ascii(`<< /Type /XObject /Subtype /Image /Width ${PRINT_WIDTH} /Height ${PRINT_HEIGHT} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>\nstream\n`), jpeg, ascii("\nendstream")]),
    concat([ascii(`<< /Length ${mapStream.length} >>\nstream\n`), mapStream, ascii("\nendstream")]),
    ascii("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 792 612] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"),
  ];
  chunks.forEach((chunk, index) => {
    const body = `BT /F1 8 Tf 28 582 Td 11 TL ${chunk.map((line) => `(${pdfEscape(line)}) Tj T*`).join(" ")} ET`;
    const stream = ascii(body);
    const contentObject = 7 + index * 2;
    objects.push(concat([ascii(`<< /Length ${stream.length} >>\nstream\n`), stream, ascii("\nendstream")]));
    objects.push(ascii(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 792 612] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentObject} 0 R >>`));
  });
  return pdfDocument(objects);
}

export function fileSlug(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "validation";
}
