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
 * Entry point of the static tracker page (`frontend/tracker/index.html`).
 *
 * No router, no session, no `/api` calls: the dataset is embedded in the page by the backend,
 * so the page works on a host that cannot reach the cluster. Same Kaizen CSS, same theme
 * provider and the same `DeploymentTracker` body as the in-app view.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../../../styles/kaizen/base.css";
import "../../../styles/kaizen/components.css";
import "../../../index.css";
import "../../../kaizen-oss.css";
import { ThemeProvider } from "../../../context/ThemeProvider";
import { readEmbeddedReport } from "../lib/embedded";
import { StaticTrackerPage } from "./StaticTrackerPage";

const report = readEmbeddedReport(document);
if (report?.meta.cluster) document.title = `${report.meta.cluster} · Deployment Tracker`;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <StaticTrackerPage report={report} />
    </ThemeProvider>
  </StrictMode>,
);
