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
      "Dragonfly Settings holds everything the reconcile needs to stand up this cluster's P2P instance: the Manager and app-server addresses, how the reconcile reaches the app server over SSH (a host in its own right, not the cluster's control node), the compute-peer inventory pattern, the install/storage paths, and the campaign defaults -- wave size and pacing plus the wave plan (Sequential or Rack-staged, the stage ladder, and the fill wave size used once racks hold a copy). Secrets are generated on the app server and never stored here. Missing pieces are named, and Provision stays disabled until they are filled in; provisioning starts a reconcile run tracked under Workflows.",
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
      "Dragonfly Settings is the per-cluster NVIS-P2P instance configuration for the active cluster (backend P2pSettings). It edits the Manager/app-server addresses, the app-server SSH connection the reconcile runs over (the app server is its own host, not the cluster control node), the compute-peer inventory pattern and source path, install/storage paths, and the rollout defaults (rollout_wave_size, rollout_parallelism, rollout_poll_seconds, rollout_max_launches, rollout_max_unreachable, rollout_timeout_seconds, rollout_strategy sequential|rack-staged, rollout_stages ladder ending at 100%, rollout_fill_wave_size where 0 = the wave size, rollout_stall_seconds default 600 where 0 turns stall handling off, rollout_download_timeout_seconds default 3600 -- the hard limit each node enforces on one download). Secrets are generated on the app server, never stored. It names provisioning blockers and exposes a gated Provision action that starts a reconcile workflow run.",
  },
  {
    id: "p2pCatalog",
    kind: "view",
    title: "Catalog",
    domain: "Artifact Distribution",
    routePath: "/p2p/catalog",
    summary:
      "Edit the active cluster's catalog — the container images (tasks) P2P can distribute — and pin each one to the exact file staged on the origin.",
    description:
      "The Catalog is the list of distributable images for the active cluster, stored by Rack Status as the source of truth. Each entry is a bare *.sqsh filename, a display name, its size in bytes and a SHA-256 pin. Every node verifies a download against that pin, so the pin must match the file staged on the origin byte for byte; a blank pin marks an image added but not yet imported. Saving does not touch the fleet: a Sync operation (Artifact Fleet) projects the catalog onto the cluster's manifest and the nodes, and each node also refreshes its copy of the catalog every 15 minutes. If no catalog has been authored yet, the editor opens with the cluster's current manifest.",
    features: [
      "Per-image order, name, *.sqsh filename, size and SHA-256 pin",
      "Validation of filenames (bare names only, never a path) and pins (64 hex characters, or blank)",
      "Opens with the cluster's current manifest when nothing has been authored yet",
    ],
    howTo: [
      {
        task: "Add a newly staged image",
        steps: [
          "Compute the SHA-256 and byte size of the file staged on the origin.",
          "Add a row with the bare filename, a name, the size and the pin, then Save.",
          "Run Sync from Artifact Fleet so the cluster's manifest and the nodes carry the new pin.",
        ],
      },
    ],
    tips: [
      "A rebuilt image never reproduces an earlier hash: re-pin from the staged file, not from a previous build.",
      "A pin that does not match the staged file fails verification on every node that downloads it.",
    ],
    keywords: ["p2p", "catalog", "task", "image", "sqsh", "sha256", "pin", "manifest", "sync"],
    related: ["p2pArtifacts", "p2pCampaigns"],
    aria: {
      label: "Catalog",
      description: "Edit the container images P2P can distribute and their SHA-256 pins.",
    },
    aiContext:
      "Catalog is the editable list of distributable container images (tasks) for the active cluster's NVIS-P2P instance (backend P2pCatalog, GET/PUT /api/p2p/catalog). Each image has an order, name, bare *.sqsh filename, byte size and a SHA-256 pin that every node verifies downloads against — a pin that does not match the file staged on the origin fails verification fleet-wide, and a rebuilt image always has a new hash. A blank pin means added but not yet imported. Saving changes only Rack Status's copy; the Sync operation projects it onto the cluster manifest and nodes, and nodes also refresh their catalog every 15 minutes.",
  },
  {
    id: "p2pRosters",
    kind: "view",
    title: "Rosters",
    domain: "Artifact Distribution",
    routePath: "/p2p/rosters",
    summary: "Named node lists that a fleet operation or campaign can target instead of the whole inventory.",
    description:
      "A roster is a named list of compute nodes — one hostname per line — stored per cluster. Fleet operations in Artifact Fleet and campaigns can target a roster by name, so a rollout can be scoped to one scalable unit, a set of verified nodes, or a canary group without editing the cluster's inventory. A roster name is free text (it may contain spaces); node entries must be bare hostnames. An operation that names a roster that no longer exists is refused before it reaches the fleet.",
    features: [
      "Named node lists, one hostname per line",
      "Targeting from Artifact Fleet operations and from campaigns",
      "Bare-hostname validation for every node entry",
    ],
    howTo: [
      {
        task: "Roll an image out to a canary group first",
        steps: [
          "Add a roster with a name such as canary and the canary hostnames, then Save.",
          "Create or edit a campaign that targets that roster and launch it.",
          "Once the canaries verify, launch the same image to the whole fleet.",
        ],
      },
    ],
    tips: ["Leaving the roster blank on a campaign or operation targets the cluster's default inventory."],
    keywords: ["p2p", "roster", "nodes", "target", "canary", "scalable unit", "su", "inventory"],
    related: ["p2pCampaigns", "p2pArtifacts"],
    aria: {
      label: "Rosters",
      description: "Named node lists that fleet operations and campaigns can target.",
    },
    aiContext:
      "Rosters are named node lists for the active cluster's NVIS-P2P instance (backend P2pRosters, GET/PUT /api/p2p/rosters). A fleet operation or a campaign can target a roster by name instead of the cluster's default inventory; the resolved node list travels with the operation to the worker. Roster names are free text; node entries must be bare hostnames. An operation naming an unknown roster is refused with a 400.",
  },
  {
    id: "p2pCampaigns",
    kind: "view",
    title: "Campaigns",
    domain: "Artifact Distribution",
    routePath: "/p2p/campaigns",
    summary:
      "Saved distributions — an image, a roster and optional pacing — that you name once and launch to the fleet on demand.",
    description:
      "A campaign pairs one catalog image with a roster (or the whole fleet) and optional pacing overrides: wave size, parallelism, poll interval, launch and unreachable limits, and timeout. A pacing field left at 0 inherits the cluster's rollout default from Dragonfly Settings. A campaign can also choose its wave plan: Sequential slices the roster in order, Rack-staged gives every rack one copy first and then grows every rack up a stage ladder (by default to half, then full) so new nodes pull from rack-mates rather than the app server's seeds; Cluster default uses the plan set in Dragonfly Settings. Launching a campaign asks for confirmation, then queues a distribute operation tagged with the campaign's name; it preheats the seeds and rolls the image out wave by wave, and its progress and log appear in Artifact Fleet. Only one operation runs at a time, so a launch is refused while another is in flight. Re-launching is safe: nodes that already hold the pinned image are skipped.",
    features: [
      "Image × roster × pacing, saved per cluster",
      "Confirm-before-launch, with a link to the launched operation's log",
      "Pacing overrides where 0 inherits the cluster default",
      "Runs tagged with the campaign name in the operations history",
    ],
    howTo: [
      {
        task: "Distribute a new image to the fleet",
        steps: [
          "Add and pin the image in Catalog, then run Sync.",
          "Add a campaign with that image and a roster (blank for the whole fleet), then Save.",
          "Click Launch, confirm, and follow the waves in Artifact Fleet or the operation log.",
        ],
      },
    ],
    tips: [
      "A launch is refused while another operation is running; wait for it to finish.",
      "Nodes deferred as unreachable in one run are picked up by launching the same campaign again.",
      "For big images, Rack-staged with a ladder like 1, 50%, 100% keeps most pulls inside racks; a finer ladder (1, 2, 4, 8, 100%) spreads the load further.",
      "A download that stops writing is restarted after the stall window (Stall, default 10 minutes) rather than holding the whole wave until its timeout.",
    ],
    keywords: ["p2p", "campaign", "launch", "distribute", "rollout", "wave", "pacing", "roster", "image", "rack-staged", "stages", "topology"],
    related: ["p2pCatalog", "p2pRosters", "p2pArtifacts", "p2pSettings"],
    aria: {
      label: "Campaigns",
      description: "Saved image distributions that can be launched to a roster or the whole fleet.",
    },
    aiContext:
      "Campaigns are saved distributions for the active cluster's NVIS-P2P instance (backend P2pCampaigns, GET/PUT /api/p2p/campaigns). A campaign names a catalog image, a roster (blank = whole fleet) and optional pacing overrides (wave_size, parallelism, poll_seconds, max_launches, max_unreachable, timeout_seconds, fill_wave_size, stall_seconds, download_timeout_seconds; 0 inherits the cluster default) and an optional wave plan (strategy sequential|rack-staged, stages ladder such as 1, 50%, 100%; blank inherits). Rack-staged is planned by the worker from the installed topology inventory (<install_root>/current/inventory/topology.tsv, node/SU/rack -- the same file the peers' scheduler labels come from): stage one puts one node in every rack at wave_size (the only stage that loads the seeds), later stages grow every rack to the next rung at fill_wave_size, racks round-robin across SUs; with no topology it falls back to sequential and says so in the log. When the fleet snapshot (<state>/p2p-snapshots/<cluster>.json) is under an hour old the plan also counts nodes that already hold the image toward each rack's rungs (a rack with a copy gets no seed wave), seeds each rack from the reachable peer with the most verified images, and puts unreachable nodes and roster holders in the rack's last stage. Every rollout watches each active download's written bytes: one that has not moved for stall_seconds (default 600, 0 = off) is stopped and relaunched, then deferred as stalled_<N>s_after_<L>_launches once it has used max_launches, instead of holding its wave to the timeout. Each node also enforces download_timeout_seconds (default 3600) on its own transfer -- dfget is killed when it passes -- and a launch replaces a running download older than that instead of re-adopting it; a wave that times out stops the downloads it gives up on. Launch is confirm-gated and enqueues an ordinary distribute operation through POST /api/p2p/operations tagged with the campaign name; the worker preheats the seeds then rolls out in waves. One operation runs at a time (409 otherwise). Re-launching is idempotent: nodes holding the pinned image are skipped by receipt.",
  },
];
