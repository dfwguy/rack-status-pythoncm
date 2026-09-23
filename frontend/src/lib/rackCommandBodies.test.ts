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
import { parseEnvVars, attachLaunchOptions, powerSpecTokensFromNodeIds } from "./rackCommandBodies";
import { validationTargetFromNodes } from "./validationTargetFromNodes";

describe("parseEnvVars", () => {
  it("parses KEY=VALUE lines and ignores blanks/comments", () => {
    const env = parseEnvVars("OMPI_MCA_coll_hcoll_enable=0\n\n# a comment\nNCCL_ALGO=Ring");
    expect(env).toEqual({ OMPI_MCA_coll_hcoll_enable: "0", NCCL_ALGO: "Ring" });
  });

  it("keeps values containing '=' and trims surrounding whitespace", () => {
    const env = parseEnvVars("  FOO = a=b=c  ");
    expect(env).toEqual({ FOO: "a=b=c" });
  });

  it("drops invalid keys and lines without '='", () => {
    const env = parseEnvVars("1BAD=x\nBAD-KEY=y\njust_text\nGOOD=1");
    expect(env).toEqual({ GOOD: "1" });
  });
});

describe("attachLaunchOptions", () => {
  it("adds env only when non-empty", () => {
    expect(attachLaunchOptions({}, { env: {} })).toEqual({});
    expect(attachLaunchOptions({}, { env: { OMPI_MCA_coll_hcoll_enable: "0" } })).toEqual({
      env: { OMPI_MCA_coll_hcoll_enable: "0" },
    });
  });

  it("still handles ncclDebug alongside env", () => {
    const body = attachLaunchOptions({}, { ncclDebug: "INFO", env: { FOO: "1" } });
    expect(body).toEqual({ nccl_debug: "INFO", env: { FOO: "1" } });
  });
});

describe("exact node targets", () => {
  it("uses the hostname when no rack is assigned", () => {
    const selected = new Set(["gpu-rno-012"]);
    expect(powerSpecTokensFromNodeIds("", selected)).toEqual(["gpu-rno-012"]);
    expect(validationTargetFromNodes("", selected)).toBe("gpu-rno-012");
  });

  it("uses the hostname when it does not fit the rack template", () => {
    const selected = new Set(["custom-worker-7"]);
    expect(powerSpecTokensFromNodeIds("p5-r01", selected)).toEqual(["custom-worker-7"]);
    expect(validationTargetFromNodes("p5-r01", selected)).toBe("custom-worker-7");
  });
});
