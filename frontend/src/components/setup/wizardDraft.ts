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
 * wizardDraft — what the setup wizard is collecting, and when a step is answered.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 *
 * Separate from the component for two reasons. It is the half worth unit-testing — "may this
 * step advance" is a rule, not a rendering — and a file that exports React components may not
 * also export helpers without tripping `react-refresh/only-export-components`, which is a CI
 * failure that does not reproduce under `tsc`.
 *
 * The draft is flat and stringly-typed on purpose: it mirrors what the operator typed, and
 * `toProject` is the single place it becomes a `Project`/`Cluster` document. Validating the
 * typed thing and converting once is what stops half-parsed numbers reaching the catalog.
 */
import type { Cluster, Project } from "../../types/catalog";

export interface WizardDraft {
  // Step 1 — project
  projectName: string;
  customer: string;
  description: string;
  // Step 2 — platform and manager
  platformId: string;
  clusterManager: string;
  sitePrefix: string;
  clusterName: string;
  // Step 3 — naming and geometry
  exampleHostname: string;
  rackIdTemplate: string;
  ctIdTemplate: string;
  nvswIdTemplate: string;
  ctJoiner: string;
  pods: string;
  rowsPerPod: string;
  racksPerRow: string;
  traysPerRack: string;
  // Step 4 — connection
  execMode: string;
  sshHost: string;
  sshUser: string;
  /** A path. Key *material* is posted separately and never stored in the draft. */
  sshKeyPath: string;
  remoteBasePath: string;
  logDir: string;
  // Step 5 — scripts
  bootstrapScripts: boolean;
}

export const STEP_TITLES = [
  "Project",
  "Platform",
  "Naming",
  "Connection",
  "Scripts",
  "Review",
] as const;

export const STEP_DESCRIPTIONS = [
  "Who this deployment is for",
  "Hardware and cluster manager",
  "How nodes are named, and how many there are",
  "How Greenlight reaches the cluster",
  "Give this project its own copy of the scripts",
  "Confirm and create",
] as const;

export function emptyDraft(): WizardDraft {
  return {
    projectName: "",
    customer: "",
    description: "",
    platformId: "",
    clusterManager: "bcm",
    sitePrefix: "",
    clusterName: "",
    exampleHostname: "",
    rackIdTemplate: "",
    ctIdTemplate: "",
    nvswIdTemplate: "",
    ctJoiner: "-ct",
    pods: "1",
    rowsPerPod: "",
    racksPerRow: "",
    traysPerRack: "",
    execMode: "ssh",
    sshHost: "",
    sshUser: "root",
    sshKeyPath: "",
    remoteBasePath: "/home/cmsupport/nvis-rack-status",
    logDir: "",
    bootstrapScripts: true,
  };
}

/** The catalog's id rules, applied here so the id shown on the review step is the real one. */
export function slugify(value: string): string {
  return (
    value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 63) || ""
  );
}

export const projectId = (draft: WizardDraft): string => slugify(draft.projectName);

/**
 * The cluster id.
 *
 * Falls back to the project id rather than to the literal `default`, which is the id seeding
 * uses — colliding with it would make an operator's first real cluster indistinguishable from
 * the placeholder the wizard exists to replace.
 */
export const clusterId = (draft: WizardDraft): string =>
  slugify(draft.clusterName) || projectId(draft);

const int = (value: string, fallback = 0): number => {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) ? n : fallback;
};

/**
 * What still has to be answered before *step* can be left.
 *
 * Returns messages rather than a boolean: "Next is greyed out" with no reason is the single
 * most common complaint about wizards, and the caller shows these next to the button.
 */
export function stepProblems(step: number, draft: WizardDraft): string[] {
  const problems: string[] = [];
  switch (step) {
    case 0:
      if (!draft.projectName.trim()) problems.push("Give the project a name.");
      else if (!projectId(draft))
        problems.push("The project name needs at least one letter or digit.");
      if (!draft.customer.trim()) problems.push("Name the customer or site this cluster serves.");
      break;
    case 1:
      if (!draft.platformId) problems.push("Choose the hardware platform.");
      if (!draft.clusterManager) problems.push("Choose the cluster manager.");
      break;
    case 2:
      if (!draft.rackIdTemplate.trim()) problems.push("A rack id template is required.");
      if (!draft.ctIdTemplate.trim()) problems.push("A compute node template is required.");
      if (
        draft.rackIdTemplate.trim() &&
        draft.ctIdTemplate.trim() &&
        !draft.ctIdTemplate.startsWith(draft.rackIdTemplate)
      ) {
        // The backend enforces this too; saying it here means the operator finds out on the
        // step that owns it rather than at the end of the wizard.
        problems.push("The node template must begin with the rack template.");
      }
      if (int(draft.traysPerRack) <= 0) problems.push("Trays per rack must be at least 1.");
      break;
    case 3:
      if (draft.execMode === "ssh") {
        if (!draft.sshHost.trim()) problems.push("An SSH host is required in ssh mode.");
        if (!draft.sshUser.trim()) problems.push("An SSH user is required in ssh mode.");
      }
      break;
    default:
      break;
  }
  return problems;
}

export const canAdvance = (step: number, draft: WizardDraft): boolean =>
  stepProblems(step, draft).length === 0;

/** The `Cluster` this draft describes. */
export function toCluster(draft: WizardDraft): Cluster {
  return {
    id: clusterId(draft),
    name: draft.clusterName.trim() || draft.projectName.trim(),
    platform_id: draft.platformId,
    cluster_manager: draft.clusterManager,
    // The wizard does not ask: Slurm is right for every cluster we ship to today,
    // and a site without it changes one field on the cluster record afterwards.
    scheduler: "slurm",
    site_prefix: draft.sitePrefix.trim(),
    naming: {
      rack_id_template: draft.rackIdTemplate.trim(),
      ct_id_template: draft.ctIdTemplate.trim(),
      nvsw_id_template: draft.nvswIdTemplate.trim(),
      ct_joiner: draft.ctJoiner,
      slurm_hostlist: "",
    },
    geometry: {
      pods: int(draft.pods, 1),
      rows_per_pod: int(draft.rowsPerPod),
      racks_per_row: int(draft.racksPerRow),
      trays_per_rack: int(draft.traysPerRack),
    },
    connection: {
      exec_mode: draft.execMode,
      ssh_host: draft.sshHost.trim(),
      ssh_user: draft.sshUser.trim(),
      // A path, never key material — `storeSshKey` wrote the file and returned this.
      ssh_key: draft.sshKeyPath.trim(),
      remote_base_path: draft.remoteBasePath.trim(),
    },
    // The wizard does not ask for an SU map: empty derives it from the platform's
    // racks_per_su, which is right for every cluster that is not ragged. A site with a
    // partially populated or row-straddling SU layout edits it afterwards.
    scalable_units: [],
  };
}

/**
 * The `Project` document to POST.
 *
 * `source: "operator"` is load-bearing rather than descriptive: `setup_state` treats any
 * project whose source is not `seed` as proof that setup happened, which is what stops the
 * wizard reopening on every login and what keeps it away from deployments that upgrade.
 */
export function toProject(draft: WizardDraft): Project {
  return {
    id: projectId(draft),
    name: draft.projectName.trim(),
    customer: draft.customer.trim(),
    description: draft.description.trim(),
    clusters: [toCluster(draft)],
    source: "operator",
  };
}
