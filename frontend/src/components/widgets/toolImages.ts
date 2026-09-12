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
// toolImages — the card banner for an action.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Keyed on `category`, the same field the card's badge shows, so the picture and the label
// always agree about what kind of thing the action is. A tool may override with `image`, but
// the normal case is no override: the shipped set covers every category in the catalog's
// vocabulary, so a tool added next month gets sensible artwork with no asset work.
//
// Imported rather than referenced by URL so Vite fingerprints them into the bundle. A path
// string would have to survive the SPA being served from `/spa/` as well as `/`, which is
// exactly the kind of thing that 404s only in the deployment.
import hardware from "../../assets/tool-categories/hardware.jpg";
import maintenance from "../../assets/tool-categories/maintenance.jpg";
import networking from "../../assets/tool-categories/networking.jpg";
import performance from "../../assets/tool-categories/performance.jpg";
import setup from "../../assets/tool-categories/setup.jpg";
import workflow from "../../assets/tool-categories/workflow.jpg";
import validation from "../../assets/tool-categories/validation.jpg";
import type { BoardAction } from "./boardActions";

/** Mirrors `catalog.models.TOOL_CATEGORIES`. */
export const CATEGORY_IMAGES: Record<string, string> = {
  validation,
  performance,
  hardware,
  networking,
  maintenance,
  setup,
  // Synthetic category: workflows are not catalog tools but need a banner like everything else.
  workflow,
};

/**
 * The banner for *tool*: its own override, else its category's, else validation's.
 *
 * The final fallback matters because `category` is a free string on the wire — the backend
 * warns on an unknown one rather than rejecting it, so the UI must not render a broken image
 * for a category this bundle has never heard of.
 */
export function actionImage(action: BoardAction): string {
  const override = (action.tool?.image || "").trim();
  if (override) return override;
  return CATEGORY_IMAGES[action.category] ?? CATEGORY_IMAGES.validation;
}
