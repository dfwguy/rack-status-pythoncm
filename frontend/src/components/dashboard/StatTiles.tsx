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
import type { ReactNode } from "react";
import { Card, Flex, Text } from "@nvidia/foundations-react-core";

const compactFormatter = new Intl.NumberFormat("en-US", {
  notation: "compact",
  compactDisplay: "short",
  maximumFractionDigits: 1,
});

/**
 * Abbreviate large counts so tiles don't overflow at production scale:
 * 250 → "250", 1100 → "1.1K", 1_000_000 → "1M". Non-finite values pass through.
 */
function formatCompact(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  return compactFormatter.format(n);
}

/**
 * Dashboard tiles styled after the NVIDIA app "Statistics" row: a small label on
 * top, then a large value (with an optional smaller unit), evenly sized and
 * baseline-aligned across the row. Proportion principle (UX framework 3.3) —
 * hierarchy comes from the type scale, not ad-hoc sizes.
 */

interface StatTileProps {
  label: string;
  value: ReactNode;
  /** Small unit shown next to the value, e.g. "racks", "%". */
  unit?: string;
  /** Accent color for the value (semantic — green/red/yellow). Defaults to primary text. */
  accentColor?: string;
  caption?: string;
}

export function StatTile({ label, value, unit, accentColor, caption }: StatTileProps) {
  const isNum = typeof value === "number" && Number.isFinite(value);
  return (
    <Card style={{ padding: 16, height: "100%", display: "flex", flexDirection: "column", gap: 6 }}>
      <Text
        kind="label/regular/sm"
        style={{ color: "var(--text-color-secondary)", textTransform: "uppercase", letterSpacing: 0.06 }}
      >
        {label}
      </Text>
      <Flex align="baseline" gap="2" style={{ marginTop: "auto" }}>
        <Text kind="display/sm" style={accentColor ? { color: accentColor } : undefined}>
          {isNum ? (
            <span title={(value as number).toLocaleString()}>{formatCompact(value as number)}</span>
          ) : (
            value
          )}
        </Text>
        {unit && (
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            {unit}
          </Text>
        )}
      </Flex>
      {caption && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {caption}
        </Text>
      )}
    </Card>
  );
}

interface InfoTileProps {
  label: string;
  value: ReactNode;
  hint?: string;
  /** Render the value in a monospace style for versions / technical identifiers. */
  mono?: boolean;
}

export function InfoTile({ label, value, hint, mono }: InfoTileProps) {
  const display =
    typeof value === "string" || typeof value === "number"
      ? value === "" || value == null
        ? "—"
        : value
      : value;
  return (
    <Card style={{ padding: 16, height: "100%", display: "flex", flexDirection: "column", gap: 6 }}>
      <Text
        kind="label/regular/sm"
        style={{ color: "var(--text-color-secondary)", textTransform: "uppercase", letterSpacing: 0.06 }}
      >
        {label}
      </Text>
      <Text
        kind={mono ? "mono/lg" : "title/md"}
        style={{ wordBreak: "break-word", lineHeight: 1.25 }}
      >
        {display}
      </Text>
      {hint && (
        <Text
          kind="mono/sm"
          style={{ color: "var(--text-color-secondary)", marginTop: "auto", wordBreak: "break-word" }}
        >
          {hint}
        </Text>
      )}
    </Card>
  );
}
