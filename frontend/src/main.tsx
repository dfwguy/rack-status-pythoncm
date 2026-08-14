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
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Vendored Kaizen UI Foundations CSS (see styles/kaizen/README.md). Must load
// before our own styles so index.css/kaizen-oss.css can override it.
import "./styles/kaizen/base.css";
import "./styles/kaizen/components.css";
import "./index.css";
import "./kaizen-oss.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
