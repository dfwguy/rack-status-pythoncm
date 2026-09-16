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
import { describe, expect, it } from "vitest";
import type { PostRunNode } from "../types/api";
import type { EtaPrediction, NodeValidationState, ValidationPoint } from "./validationStatus";
import { jpegPdf, segmentColor, setJpegDpi, validationCsv, validationGradient, validationReportPdf, VALIDATION_EXPORT_SCHEMA } from "./validationExport";

const point = (name: string, rawStatus: string, reason = ""): ValidationPoint => ({
  id: name, domain: name, name, status: rawStatus === "DONE" ? "pass" : "attention", rawStatus,
  reason, source: "bringup", timestamp: "2026-09-02T11:00:00Z", jobId: "147001", metric: "", metricUnit: "",
});
const node = { node: "ink50-dh01-a01-11", rack: "ink50-dh01-a01" } as PostRunNode;
const prediction: EtaPrediction = {
  state: "estimated", eta: "2026-09-02T14:00:00Z", label: "2:00 PM", confidence: "medium",
  completed: 1, total: 2, blocked: 0, running: 1, samples: 5, ratePerHour: 2, methodology: "real transitions",
};

describe("validation exports", () => {
  it("exports only points supplied by the active selection with versioned, formula-safe CSV", () => {
    const selected = point("dcgmi-r4", "FAIL", "=cmd|' /C calc'!A0");
    const state: NodeValidationState = { status: "attention", running: false, points: [selected], visiblePoints: [selected], tooltip: "" };
    const csv = validationCsv([{ node, su: "SU1", state, prediction }], "DCGM", "2026-09-02T12:00:00Z", "fresh");
    expect(csv).toContain(VALIDATION_EXPORT_SCHEMA);
    expect(csv).toContain("dcgmi-r4");
    expect(csv).not.toContain("llm-pretrain");
    expect(csv).toContain("'=cmd");
    expect(csv).toContain("predicted_completion");
  });

  it("uses red for every hard-failure segment and caps the graphic at visible points", () => {
    const checks = Array.from({ length: 18 }, (_, index) => point(`failed-${index}`, "FAIL"));
    const state: NodeValidationState = { status: "attention", running: false, points: checks, visiblePoints: checks.slice(0, 15), tooltip: "" };
    expect(segmentColor(checks[0])).toBe("#c62828");
    expect((validationGradient(state).match(/%/g) || [])).toHaveLength(30);
  });

  it("distinguishes warning, running, NOT_RUN, and unknown segment colors", () => {
    const withStatus = (status: ValidationPoint["status"]): ValidationPoint => ({ ...point(status, status), status });
    expect(segmentColor(withStatus("warning"))).toBe("#f9a825");
    expect(segmentColor(withStatus("running"))).toBe("#f9a825");
    expect(segmentColor(withStatus("not_run"))).toBe("#a94b4b");
    expect(segmentColor(withStatus("unknown"))).toBe("#757575");
  });

  it("wraps the high-resolution JPEG in an 11x8.5 landscape PDF", () => {
    const pdf = jpegPdf(Uint8Array.from([0xff, 0xd8, 0xff, 0xd9]));
    const text = new TextDecoder().decode(pdf);
    expect(text.startsWith("%PDF-1.4")).toBe(true);
    expect(text).toContain("/MediaBox [0 0 792 612]");
    expect(text).toContain("/Width 3300 /Height 2550");
    expect(text).toContain("/DCTDecode");
  });

  it("marks browser JPEG output as 300 DPI", () => {
    const jfif = Uint8Array.from([0xff,0xd8,0xff,0xe0,0x00,0x10,0x4a,0x46,0x49,0x46,0x00,0x01,0x01,0x00,0x00,0x01,0x00,0x01]);
    const result = setJpegDpi(jfif, 300);
    expect(result[13]).toBe(1);
    expect([result[14], result[15], result[16], result[17]]).toEqual([1, 44, 1, 44]);
  });

  it("adds selection-aware evidence pages to the printable PDF", () => {
    const selected = point("llm-pretrain-solo", "FAIL", "loss diverged");
    const state: NodeValidationState = { status: "attention", running: false, points: [selected], visiblePoints: [selected], tooltip: "" };
    const pdf = validationReportPdf(
      Uint8Array.from([0xff, 0xd8, 0xff, 0xd9]),
      [{ node, su: "SU1", state, prediction }],
      "llm-pretrain-solo",
      "2026-09-02T12:00:00Z",
      "bring-up fresh",
      prediction,
    );
    const text = new TextDecoder().decode(pdf);
    expect(text).toContain("llm-pretrain-solo");
    expect(text).toContain("loss diverged");
    expect(text).toContain("job=147001");
    expect(text).toContain("/Count 2");
  });
});
