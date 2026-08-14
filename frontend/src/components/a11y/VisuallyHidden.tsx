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
import type { CSSProperties, ReactNode } from "react";

/**
 * Renders content that is hidden visually but available to screen readers /
 * assistive technology (and the user's own TTS). Used to attach rich page and
 * component descriptions from the knowledge registry without changing layout.
 */
const visuallyHiddenStyle: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0, 0, 0, 0)",
  whiteSpace: "nowrap",
  border: 0,
};

interface VisuallyHiddenProps {
  children: ReactNode;
  /** Optional id so the element can be referenced via `aria-describedby`. */
  id?: string;
  /** When set, screen readers announce changes to this region politely. */
  live?: "polite" | "assertive";
}

export function VisuallyHidden({ children, id, live }: VisuallyHiddenProps) {
  return (
    <span id={id} style={visuallyHiddenStyle} aria-live={live} aria-atomic={live ? true : undefined}>
      {children}
    </span>
  );
}

export default VisuallyHidden;
