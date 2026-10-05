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
 * App Knowledge / Context Layer — Artifact Distribution (NVIS-P2P) domain.
 *
 * P2P is single-tenant: one Dragonfly instance per cluster, distributing a
 * validated container-image catalog across the compute fleet with peer-assisted
 * transfer. These entries scope the AI assistant and Help page to that surface.
 */
import type { HelpContent } from "../types";

export const p2pContent: HelpContent[] = [
  {
    id: "p2pArtifacts",
    kind: "view",
    title: "Artifact Fleet",
    domain: "Artifact Distribution",
    routePath: "/p2p/artifacts",
    summary:
      "Persist the validated container-image catalog into the object store and materialize it across the compute fleet with peer-assisted transfer.",
    description:
      "The Artifact Fleet is the operator surface for NVIS-P2P on the active cluster. It shows the catalog of required images, object-store health, per-SU readiness and a node coverage heatmap, and it launches fleet operations — preflight, persist, sync, distribute, verify and repair — that move images to every node over Dragonfly's peer network rather than pulling each node from origin. P2P is enabled per cluster, so this surface only appears for clusters that have the capability turned on.",
    features: [
      "Catalog view with per-image canonical presence and object-store import receipts",
      "Per-SU readiness and a 4032-node coverage heatmap (complete / syncing / incomplete / unavailable)",
      "Fleet operations: preflight, persist, sync, distribute, verify, repair",
      "Object-store health and the latest catalog sync wave with completion percentage",
    ],
    howTo: [
      {
        task: "Materialize the catalog across a set of SUs",
        steps: [
          "Confirm object store health and the catalog are green.",
          "Select the target SUs.",
          "Run Preflight, then Sync, and watch the coverage heatmap fill.",
        ],
      },
      {
        task: "Repair a node that is amber (incomplete or unverified)",
        steps: [
          "Select the affected image(s).",
          "Run Repair scoped to those images.",
          "Re-run Verify and confirm the node returns to complete.",
        ],
      },
    ],
    tips: [
      "Amber means reachable but incomplete, bad-sized, or present without a valid receipt — not offline.",
      "Compute nodes have back-to-source disabled; origin traffic is concentrated through the seeds.",
    ],
    keywords: [
      "p2p",
      "dragonfly",
      "artifact",
      "catalog",
      "image",
      "distribution",
      "dfstore",
      "seed",
      "sync",
      "fleet",
      "heatmap",
    ],
    aria: {
      label: "Artifact Fleet",
      description:
        "Distribute the validated container-image catalog across the compute fleet with peer-assisted transfer.",
    },
    aiContext:
      "Artifact Fleet is the NVIS-P2P operator surface for the active cluster. It distributes a validated container-image catalog across the compute fleet using Dragonfly peer-assisted transfer with durable object storage and per-node SHA-256 receipts. Operators run preflight/persist/sync/distribute/verify/repair operations and read a per-SU readiness table and node coverage heatmap. P2P is single-tenant — one instance per cluster — and enabled per project.",
  },
  {
    id: "p2pSettings",
    kind: "view",
    title: "Dragonfly Settings",
    domain: "Artifact Distribution",
    routePath: "/p2p/settings",
    summary:
      "Per-cluster NVIS-P2P instance configuration — addresses, the app-server connection, inventory, paths — and the button that provisions or reconciles the control plane.",
    description:
      "Dragonfly Settings holds everything the reconcile needs to stand up this cluster's P2P instance: the Manager and app-server addresses, how the reconcile reaches the app server over SSH (a host in its own right, not the cluster's control node), the compute-peer inventory pattern, and the install/storage paths. Secrets are generated on the app server and never stored here. Missing pieces are named, and Provision stays disabled until they are filled in; provisioning starts a reconcile run tracked under Workflows.",
    features: [
      "Manager / app-server addresses and the app-server SSH connection",
      "Compute-peer inventory pattern and source path",
      "Install root, catalog (.json) and storage paths, service account",
      "Named provisioning blockers, and a gated Provision (reconcile) action",
    ],
    howTo: [
      {
        task: "Configure and provision a cluster's P2P instance",
        steps: [
          "Enable P2P for the cluster in Projects & Clusters.",
          "Fill in the Manager IP, app-server IP, the app-server SSH user/key, and the inventory pattern.",
          "Save, then click Provision once no blockers remain, and track the run under Workflows.",
        ],
      },
    ],
    tips: [
      "The app server is often a different host from the cluster's control node — set its SSH connection explicitly.",
      "The catalog path must name a .json file; the inventory source is a file path, not a scheduler name.",
    ],
    keywords: ["p2p", "dragonfly", "settings", "provision", "reconcile", "app server", "inventory", "manager"],
    aria: {
      label: "Dragonfly Settings",
      description: "Per-cluster NVIS-P2P instance configuration and provisioning.",
    },
    aiContext:
      "Dragonfly Settings is the per-cluster NVIS-P2P instance configuration for the active cluster (backend P2pSettings). It edits the Manager/app-server addresses, the app-server SSH connection the reconcile runs over (the app server is its own host, not the cluster control node), the compute-peer inventory pattern and source path, and install/storage paths. Secrets are generated on the app server, never stored. It names provisioning blockers and exposes a gated Provision action that starts a reconcile workflow run.",
  },
];
