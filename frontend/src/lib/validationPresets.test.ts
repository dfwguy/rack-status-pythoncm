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
import { buildValidationPresetRequest, formatPresetRequirements, presetLocalContainerDir } from "./validationPresets";

describe("buildValidationPresetRequest", () => {
  it("deduplicates and naturally sorts exact nodes without inventing a host pattern", () => {
    expect(
      buildValidationPresetRequest({
        presetId: "exemplar-system-info",
        scope: "nodes",
        nodes: ["rack-a-10", "rack-a-02", "rack-a-02"],
        racks: ["rack-a"],
        logDir: " /cluster/logs ",
        storage: "local",
        rsyncToLocal: true,
      }),
    ).toEqual({
      preset_id: "exemplar-system-info",
      scope: "nodes",
      nodes: ["rack-a-02", "rack-a-10"],
      racks: ["rack-a"],
      log_dir: "/cluster/logs",
      storage: "local",
      rsync_to_local: true,
    });
  });

  it("builds a multirack request from exact selected rack ids", () => {
    expect(
      buildValidationPresetRequest({
        presetId: "llama31-405b-fp8",
        scope: "multirack",
        racks: ["j02", "d01", "j02"],
        storage: "nfs",
        rsyncToLocal: false,
      }),
    ).toMatchObject({
      preset_id: "llama31-405b-fp8",
      scope: "multirack",
      racks: ["d01", "j02"],
      storage: "nfs",
      rsync_to_local: false,
    });
  });
});

describe("formatPresetRequirements", () => {
  it("renders server requirement maps for the operator", () => {
    expect(formatPresetRequirements({ requires_infiniband: true, cuda_version: ">=13.3" })).toEqual([
      "requires infiniband",
      "cuda version: >=13.3",
    ]);
  });
});

describe("presetLocalContainerDir", () => {
  const base = { id: "nemo-26.04.01", label: "NeMo", description: "", kind: "nemo" };

  it("reads the cluster's node image cache from the server-reported defaults", () => {
    expect(presetLocalContainerDir({ ...base, defaults: { local_container_dir: " /opt/containers " } })).toBe(
      "/opt/containers",
    );
  });

  it("is undefined until the server says, rather than guessing a path", () => {
    expect(presetLocalContainerDir(undefined)).toBeUndefined();
    expect(presetLocalContainerDir(base)).toBeUndefined();
    expect(presetLocalContainerDir({ ...base, defaults: { local_container_dir: "" } })).toBeUndefined();
  });
});
