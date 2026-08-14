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
import { getContent, type HelpContent } from "../content";

/**
 * Returns the knowledge-registry entry for a content id (view or component),
 * plus ready-to-spread ARIA attributes for the element's root.
 *
 * Usage:
 *   const { content, ariaProps } = useHelpContent("rackTable");
 *   return <section {...ariaProps}>...</section>;
 *
 * `ariaProps` provides `aria-label` and, when a description exists, an
 * `aria-describedby` pointing at a hidden description element you render with
 * `descriptionId`.
 */
export interface UseHelpContentResult {
  content: HelpContent | undefined;
  /** Stable id for the hidden description element (for aria-describedby). */
  descriptionId: string;
  /** Spread onto the component root to expose its accessible name/description. */
  ariaProps: {
    "aria-label"?: string;
    "aria-describedby"?: string;
  };
}

export function useHelpContent(id: string): UseHelpContentResult {
  const content = getContent(id);
  const descriptionId = `help-desc-${id}`;
  const ariaProps: UseHelpContentResult["ariaProps"] = {};
  if (content) {
    ariaProps["aria-label"] = content.aria.label;
    if (content.aria.description) {
      ariaProps["aria-describedby"] = descriptionId;
    }
  }
  return { content, descriptionId, ariaProps };
}
