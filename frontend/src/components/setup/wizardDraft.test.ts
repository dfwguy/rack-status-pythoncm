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
 * The wizard's rules: when a step is answered, and what document it produces.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */
import { describe, expect, it } from "vitest";
import {
  STEP_TITLES,
  canAdvance,
  clusterId,
  emptyDraft,
  projectId,
  stepProblems,
  toProject,
  type WizardDraft,
} from "./wizardDraft";

const filled = (over: Partial<WizardDraft> = {}): WizardDraft => ({
  ...emptyDraft(),
  projectName: "Nscale TX",
  customer: "Nscale",
  platformId: "gb300-nvl72",
  clusterManager: "bcm",
  rackIdTemplate: "ink[01-99]-dh[01-99]-[a-z][01-99]",
  ctIdTemplate: "ink[01-99]-dh[01-99]-[a-z][01-99]-[01-99]",
  traysPerRack: "18",
  sshHost: "192.168.10.2",
  sshUser: "cmods",
  ...over,
});

describe("step gating", () => {
  it("says what is missing rather than only disabling Next", () => {
    // "Next is greyed out and I don't know why" is the complaint this avoids.
    const problems = stepProblems(0, emptyDraft());
    expect(problems.length).toBeGreaterThan(0);
    expect(problems.join(" ")).toMatch(/name/i);
  });

  it("lets a complete step advance", () => {
    for (const step of [0, 1, 2, 3]) {
      expect(canAdvance(step, filled()), `step ${step}: ${stepProblems(step, filled())}`).toBe(true);
    }
  });

  it("refuses a node template that does not extend the rack template", () => {
    // The backend enforces this too; catching it here means the operator finds out on the step
    // that owns it rather than at the end of the wizard.
    const draft = filled({ ctIdTemplate: "other[01-99]-[01-99]" });
    expect(stepProblems(2, draft).join(" ")).toMatch(/begin with the rack template/i);
  });

  it("requires a host only in ssh mode", () => {
    expect(canAdvance(3, filled({ execMode: "local", sshHost: "", sshUser: "" }))).toBe(true);
    expect(canAdvance(3, filled({ execMode: "ssh", sshHost: "" }))).toBe(false);
  });

  it("requires at least one tray", () => {
    expect(canAdvance(2, filled({ traysPerRack: "0" }))).toBe(false);
    expect(canAdvance(2, filled({ traysPerRack: "" }))).toBe(false);
  });

  it("has a title and a rule for every step", () => {
    expect(STEP_TITLES).toHaveLength(6);
  });
});

describe("the document it produces", () => {
  it("marks the project as operator-created", () => {
    // Load-bearing, not descriptive: `setup_state` treats any non-seed project as proof that
    // setup happened, which is what stops the wizard reopening on every login.
    expect(toProject(filled()).source).toBe("operator");
  });

  it("derives ids the catalog will accept", () => {
    expect(projectId(filled({ projectName: "Nscale TX (GB300)" }))).toBe("nscale-tx-gb300");
    expect(projectId(filled({ projectName: "  Mayo  Clinic  " }))).toBe("mayo-clinic");
  });

  it("does not fall back to the id seeding uses", () => {
    // A cluster called `default` would be indistinguishable from the placeholder the wizard
    // exists to replace.
    expect(clusterId(filled({ clusterName: "" }))).toBe("nscale-tx");
    expect(clusterId(filled({ clusterName: "" }))).not.toBe("default");
  });

  it("carries the key path, never key material", () => {
    const project = toProject(filled({ sshKeyPath: "/app/var/secrets/ssh/nscale-tx-gb300.key" }));
    const serialised = JSON.stringify(project);
    expect(project.clusters[0].connection.ssh_key).toMatch(/\.key$/);
    expect(serialised).not.toMatch(/BEGIN [A-Z ]*PRIVATE KEY/);
  });

  it("converts the numeric fields once, at the end", () => {
    const geometry = toProject(filled({ pods: "2", rowsPerPod: "4", racksPerRow: "9" }))
      .clusters[0].geometry;
    expect(geometry).toEqual({ pods: 2, rows_per_pod: 4, racks_per_row: 9, trays_per_rack: 18 });
  });

  it("survives a non-numeric entry rather than writing NaN to the catalog", () => {
    const geometry = toProject(filled({ rowsPerPod: "lots" })).clusters[0].geometry;
    expect(geometry.rows_per_pod).toBe(0);
  });
});
