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
 * Knowledge entries for the Infrastructure domain (Ethernet fabric, IB/NVLink,
 * Storage). These are preview pages backed by mock data today.
 */
import type { HelpContent } from "../types";

export const infrastructureContent: HelpContent[] = [
  {
    id: "infraEthernet",
    kind: "view",
    title: "Ethernet fabric",
    domain: "Infrastructure",
    routePath: "/infrastructure/ethernet",
    summary: "Ethernet fabric topology and switch/link health (preview).",
    description:
      "The Ethernet page is intended to show the cluster's Ethernet fabric: switches, links, and their health. It is currently a preview backed by mock data and carries a preview banner.",
    features: ["Ethernet switch/link overview.", "Topology-oriented layout.", "Preview banner indicating mock data."],
    howTo: [{ task: "View Ethernet fabric", steps: ["Open Infrastructure > Ethernet.", "Review switches and links (preview/mock data today)."] }],
    tips: ["This page is a preview; values are mock data until wired to live telemetry."],
    related: ["infraIbNvlink", "infraStorage", "previewBanner"],
    keywords: ["ethernet", "fabric", "switch", "link", "network", "infrastructure", "preview"],
    aria: { label: "Ethernet fabric page", description: "Preview of the cluster Ethernet fabric: switches, links, and health (currently mock data)." },
    aiContext:
      "The Ethernet page previews the cluster's Ethernet fabric (switches, links, health). It is currently a preview backed by mock data with a preview banner.",
  },
  {
    id: "infraIbNvlink",
    kind: "view",
    title: "InfiniBand / NVLink",
    domain: "Infrastructure",
    routePath: "/infrastructure/ib-nvlink",
    summary: "InfiniBand and NVLink fabric health (preview).",
    description:
      "The IB / NVLink page is intended to show InfiniBand and NVLink fabric topology and health — critical for GPU interconnect performance. It is currently a preview backed by mock data.",
    features: ["InfiniBand fabric view.", "NVLink interconnect view.", "Preview banner indicating mock data."],
    howTo: [{ task: "View IB/NVLink fabric", steps: ["Open Infrastructure > IB / NVLink.", "Review the fabric (preview/mock data today)."] }],
    tips: ["NVLink/IB health is a common root cause of degraded NCCL bandwidth; cross-check with parser results."],
    related: ["infraEthernet", "infraStorage", "parserResultsDialog"],
    keywords: ["infiniband", "ib", "nvlink", "fabric", "interconnect", "gpu", "preview"],
    aria: { label: "InfiniBand and NVLink page", description: "Preview of InfiniBand and NVLink fabric topology and health (currently mock data)." },
    aiContext:
      "The IB / NVLink page previews InfiniBand and NVLink fabric topology and health (key for GPU interconnect). Currently a preview backed by mock data.",
  },
  {
    id: "infraStorage",
    kind: "view",
    title: "Storage",
    domain: "Infrastructure",
    routePath: "/infrastructure/storage",
    summary: "Cluster storage capacity and health (preview).",
    description:
      "The Storage page is intended to show cluster storage systems, capacity, and health — including head-node NFS, often a provisioning bottleneck. It is currently a preview backed by mock data.",
    features: ["Storage capacity overview.", "Health indicators.", "Preview banner indicating mock data."],
    howTo: [{ task: "View storage status", steps: ["Open Infrastructure > Storage.", "Review capacity and health (preview/mock data today)."] }],
    tips: ["Head-node NFS saturation is a frequent provisioning bottleneck; watch storage health during energise/validation."],
    related: ["infraEthernet", "infraIbNvlink"],
    keywords: ["storage", "nfs", "capacity", "disk", "filesystem", "infrastructure", "preview"],
    aria: { label: "Storage page", description: "Preview of cluster storage capacity and health (currently mock data)." },
    aiContext: "The Storage page previews cluster storage capacity and health (incl. head-node NFS). Currently a preview backed by mock data.",
  },
];
