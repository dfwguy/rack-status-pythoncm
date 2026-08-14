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
import { afterEach, describe, expect, it, vi } from "vitest";
import { deleteGeneratedReport, fetchGeneratedReports } from "./generatedReportsService";

afterEach(() => vi.restoreAllMocks());

describe("generatedReportsService", () => {
  it("returns durable report metadata from the archive API", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        ok: true,
        max_kept: 40,
        reports: [{
          id: "a".repeat(32),
          kind: "rack-status",
          format: "pdf",
          filename: "rack-status.pdf",
          size: 1024,
          created: 1_786_608_000,
          cluster: "NscaleTX-GB300",
          nodes: 2016,
        }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }),
    );

    const result = await fetchGeneratedReports();
    expect(result.max_kept).toBe(40);
    expect(result.reports[0]).toMatchObject({ filename: "rack-status.pdf", nodes: 2016 });
    expect(fetch).toHaveBeenCalledWith(
      "/api/reports/generated",
      expect.objectContaining({ credentials: "same-origin", cache: "no-store" }),
    );
  });

  it("surfaces a report-store error instead of presenting an empty archive", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: false, error: "report volume unavailable" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(fetchGeneratedReports()).rejects.toThrow("report volume unavailable");
  });

  it("deletes only the encoded report id requested by the caller", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await deleteGeneratedReport("report/id");
    expect(fetch).toHaveBeenCalledWith(
      "/api/reports/generated/report%2Fid",
      expect.objectContaining({ method: "DELETE", credentials: "same-origin" }),
    );
  });
});
