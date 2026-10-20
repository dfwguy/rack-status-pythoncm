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
// The campaign request contract. A launch is an ordinary distribute through /operations: it must
// carry the campaign's image, roster and name, and ONLY the pacing fields the campaign overrides --
// a zero means "inherit the cluster default", so sending it would ask the server for a zero wave.
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiJson } from "../lib/apiFetch";
import { getCampaigns, launchCampaign, saveCampaigns, startOperation } from "./fleetService";
import type { CampaignEdit } from "./fleetService";

vi.mock("../lib/apiFetch", () => ({
  apiJson: vi.fn().mockResolvedValue({}),
  apiFetch: vi.fn(),
}));

const mockJson = apiJson as unknown as ReturnType<typeof vi.fn>;

/** The (url, init, parsed body) of the single apiJson call a function made. */
function lastCall(): { url: string; init: RequestInit | undefined; body: Record<string, unknown> | null } {
  expect(mockJson).toHaveBeenCalledTimes(1);
  const [url, init] = mockJson.mock.calls[0] as [string, RequestInit | undefined];
  return { url, init, body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null };
}

const campaign = (patch: Partial<CampaignEdit> = {}): CampaignEdit => ({
  name: "nemo-nccl2323-fleet",
  description: "",
  image: "nvidia+nemo+26.04.01-arm-nccl2.32.3.sqsh",
  roster: "All fleet",
  wave_size: 0,
  parallelism: 0,
  poll_seconds: 0,
  max_launches: 0,
  max_unreachable: 0,
  timeout_seconds: 0,
  fill_wave_size: 0,
  stall_seconds: 0,
  verify_mb_per_second: 0,
  download_timeout_seconds: 0,
  strategy: "",
  stages: [],
  ...patch,
});

afterEach(() => {
  mockJson.mockClear();
});

describe("campaign CRUD", () => {
  it("reads the active cluster's campaigns", async () => {
    await getCampaigns();
    const { url, init } = lastCall();
    expect(url).toBe("/api/p2p/campaigns");
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("replaces the whole campaign document on save", async () => {
    const campaigns = [campaign(), campaign({ name: "nvbandwidth-fleet", image: "nvbandwidth-v0.9-arm.sqsh" })];
    await saveCampaigns(campaigns);
    const { url, init, body } = lastCall();
    expect(url).toBe("/api/p2p/campaigns");
    expect(init?.method).toBe("PUT");
    expect(body).toEqual({ campaigns });
  });
});

describe("launchCampaign", () => {
  it("is a distribute of the campaign's image to its roster, tagged with its name", async () => {
    await launchCampaign(campaign());
    const { url, init, body } = lastCall();
    expect(url).toBe("/api/p2p/operations");
    expect(init?.method).toBe("POST");
    expect(body).toMatchObject({
      action: "distribute",
      sus: [],
      images: ["nvidia+nemo+26.04.01-arm-nccl2.32.3.sqsh"],
      roster: "All fleet",
      campaign: "nemo-nccl2323-fleet",
    });
  });

  it("sends only the pacing fields the campaign overrides", async () => {
    await launchCampaign(campaign({ wave_size: 25, timeout_seconds: 3600 }));
    expect(lastCall().body?.pacing).toEqual({ wave_size: 25, timeout_seconds: 3600 });
  });

  it("sends the wave plan only when the campaign overrides it", async () => {
    await launchCampaign(campaign({ strategy: "rack-staged", stages: ["1", "50%", "100%"], fill_wave_size: 72 }));
    expect(lastCall().body?.pacing).toEqual({ strategy: "rack-staged", stages: ["1", "50%", "100%"], fill_wave_size: 72 });
  });

  it("sends a shortened stall window and download limit as pacing overrides", async () => {
    await launchCampaign(campaign({ stall_seconds: 300, download_timeout_seconds: 1800 }));
    expect(lastCall().body?.pacing).toEqual({ stall_seconds: 300, download_timeout_seconds: 1800 });
  });

  it("sends a large-image verify rate as a pacing override", async () => {
    await launchCampaign(campaign({ verify_mb_per_second: 50 }));
    expect(lastCall().body?.pacing).toEqual({ verify_mb_per_second: 50 });
  });

  it("sends no pacing at all when every field inherits the cluster default", async () => {
    await launchCampaign(campaign());
    expect(lastCall().body?.pacing).toEqual({});
  });

  it("targets the whole fleet when the campaign names no roster", async () => {
    await launchCampaign(campaign({ roster: "" }));
    expect(lastCall().body?.roster).toBe("");
  });
});

describe("startOperation", () => {
  it("defaults roster, pacing and campaign for an ad-hoc operation", async () => {
    await startOperation({ action: "verify", sus: [3] });
    expect(lastCall().body).toEqual({ action: "verify", sus: [3], images: [], roster: "", pacing: {}, campaign: "" });
  });
});
