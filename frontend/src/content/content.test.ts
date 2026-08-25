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
import { describe, it, expect } from "vitest";
import {
  ALL_CONTENT,
  CONTENT_REGISTRY,
  getContent,
  getViewContent,
  searchContent,
  buildAIContext,
} from "./index";
import { ROUTES } from "../routes";

describe("knowledge registry", () => {
  it("has an entry for every navigable view", () => {
    for (const route of ROUTES) {
      const entry = getViewContent(route.viewId);
      expect(entry, `missing content for view "${route.viewId}"`).toBeDefined();
      expect(entry!.kind).toBe("view");
    }
  });

  it("has globally unique ids", () => {
    const ids = ALL_CONTENT.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(Object.keys(CONTENT_REGISTRY).length).toBe(ALL_CONTENT.length);
  });

  it("has non-empty required fields on every entry", () => {
    for (const entry of ALL_CONTENT) {
      expect(entry.id, "id").toBeTruthy();
      expect(entry.title, `title for ${entry.id}`).toBeTruthy();
      expect(entry.summary, `summary for ${entry.id}`).toBeTruthy();
      expect(entry.description, `description for ${entry.id}`).toBeTruthy();
      expect(entry.aiContext, `aiContext for ${entry.id}`).toBeTruthy();
      expect(entry.aria.label, `aria.label for ${entry.id}`).toBeTruthy();
      expect(entry.aria.description, `aria.description for ${entry.id}`).toBeTruthy();
      expect(entry.keywords.length, `keywords for ${entry.id}`).toBeGreaterThan(0);
      expect(Array.isArray(entry.features), `features for ${entry.id}`).toBe(true);
      expect(Array.isArray(entry.howTo), `howTo for ${entry.id}`).toBe(true);
    }
  });

  it("only references existing ids in `related`", () => {
    for (const entry of ALL_CONTENT) {
      for (const rel of entry.related ?? []) {
        expect(getContent(rel), `related "${rel}" referenced by "${entry.id}"`).toBeDefined();
      }
    }
  });

  it("searches across title, summary, keywords, and features", () => {
    expect(searchContent("validation").length).toBeGreaterThan(0);
    expect(searchContent("squeue").some((e) => e.id === "squeuePanel")).toBe(true);
    expect(searchContent("workflow").some((e) => e.id === "workflows")).toBe(true);
    expect(searchContent("terminal").some((e) => e.id === "terminal")).toBe(true);
    expect(searchContent("").length).toBe(0);
  });

  it("builds non-empty AI context for every view", () => {
    for (const route of ROUTES) {
      expect(buildAIContext(route.viewId).length, route.viewId).toBeGreaterThan(0);
    }
  });

  it("prefers rack-detail context when a rack is selected on the racks view", () => {
    const ctx = buildAIContext("singleRack", "rack-07");
    expect(ctx).toContain("Rack detail");
    expect(ctx).toContain("rack-07");
  });
});
