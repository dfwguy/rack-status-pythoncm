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
  attachLaunchOptions,
  fullRackNodeSetFromInventory,
  parseEnvVars,
  powerOnAsyncRequest,
} from "./rackCommandBodies";
import type { NvisNamingSchema } from "../types/naming";

const NSCALE_NAMING: NvisNamingSchema = {
  rack_id: "ink50-dh[01-99]-[a-z][\\d+]",
  ct_id: "ink50-dh[01-99]-[a-z][\\d+]-[\\d+]",
  nvsw_id: "ink50-dh[01-99]-[a-z][\\d+]-[\\d+]-nv",
  ct_joiner: "-",
  node_id_mode: "full_hostname",
};

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

describe("rack action target inventory", () => {
  it("preserves exact Nscale hostnames and non-contiguous tray numbers", () => {
    const nodes = fullRackNodeSetFromInventory(
      "ink50-dh02-k01",
      {
        "ink50-dh02-k01-11": "10.0.0.11",
        "ink50-dh02-k01-18": "10.0.0.18",
        "ink50-dh02-k01-28": "10.0.0.28",
        "INK50-DH02-K01-37": "10.0.0.37",
        "ink50-dh02-k02-11": "10.0.1.11",
        "ink50-dh02-k01-11-nv": "10.0.2.11",
      },
      NSCALE_NAMING,
    );
    expect([...nodes]).toEqual([
      "ink50-dh02-k01-11",
      "ink50-dh02-k01-18",
      "ink50-dh02-k01-28",
      "INK50-DH02-K01-37",
    ]);
    expect([...nodes].some((node) => node.includes("-ct"))).toBe(false);
  });

  it("fails closed for a full-hostname site when live inventory is absent", () => {
    expect(fullRackNodeSetFromInventory("ink50-dh02-k01", {}, NSCALE_NAMING)).toEqual(new Set());
  });

  it("keeps the legacy ct01-ct18 fallback for rack suffix naming", () => {
    const nodes = fullRackNodeSetFromInventory(
      "p05-r01",
      {},
      { ...NSCALE_NAMING, ct_joiner: "-ct", node_id_mode: "rack_ct_suffix" },
    );
    expect(nodes.size).toBe(18);
    expect([...nodes].slice(0, 2)).toEqual(["p05-r01-ct01", "p05-r01-ct02"]);
  });

  it("builds the RackTable power action against the existing async route and specs payload", () => {
    const request = powerOnAsyncRequest(
      "ink50-dh02-k01",
      "/cluster/OVA/logs/nscaletx-16k-validation",
      new Set(["ink50-dh02-k01-11", "ink50-dh02-k01-28"]),
      NSCALE_NAMING,
    );
    expect(request.url).toBe("/api/power-on-async");
    expect(request.body).toEqual({
      rack: "ink50-dh02-k01",
      specs: ["ink50-dh02-k01-[11]", "ink50-dh02-k01-[28]"],
      log_dir: "/cluster/OVA/logs/nscaletx-16k-validation",
    });
  });
});
