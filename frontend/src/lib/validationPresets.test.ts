import { describe, expect, it } from "vitest";
import { buildValidationPresetRequest, formatPresetRequirements } from "./validationPresets";

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
      local_container_dir: "/tmp/containers",
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
