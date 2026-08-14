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
import { useState } from "react";
import { Collapsible, AnimatedChevron, Button } from "@nvidia/foundations-react-core";

export interface CollapsibleLogProps {
  /** The log text to show. Renders nothing when empty. */
  text: string;
  /** Short noun for the toggle label, e.g. "output", "stderr". */
  label?: string;
  /** Start expanded (default collapsed so long logs don't push the page down). */
  defaultOpen?: boolean;
  /** "error" tints the text red (for stderr). */
  tone?: "default" | "error";
  /** Max height (px) of the expanded log before it scrolls internally. */
  maxHeight?: number;
}

/**
 * Collapsible, height-capped log block built on the KUI Collapsible. Collapsed by
 * default: a result panel with a very long log otherwise forces the user to scroll
 * past the whole dump to reach the next card. The trigger shows the line count and an
 * AnimatedChevron; when expanded the <pre> scrolls inside a bounded box rather than
 * growing the page.
 */
export function CollapsibleLog({
  text,
  label = "output",
  defaultOpen = false,
  tone = "default",
  maxHeight = 360,
}: CollapsibleLogProps) {
  const [open, setOpen] = useState(defaultOpen);
  if (!text) return null;
  const lines = text.split("\n").length;
  const triggerText = open
    ? `Hide ${label}`
    : `Show ${label} (${lines} line${lines === 1 ? "" : "s"})`;
  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      slotTrigger={
        <Button asChild kind="secondary" color="neutral" size="small">
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            {triggerText}
            <AnimatedChevron />
          </div>
        </Button>
      }
    >
      <pre
        style={{
          margin: "4px 0 0",
          padding: 0,
          fontFamily: "var(--font-mono, ui-monospace, monospace)",
          fontSize: "0.8em",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight,
          overflow: "auto",
          color: tone === "error" ? "var(--text-color-error, #ef4444)" : "inherit",
        }}
      >
        {text}
      </pre>
    </Collapsible>
  );
}
