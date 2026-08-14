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
import { Combobox } from "@nvidia/foundations-react-core";
import type { ComponentProps } from "react";

type ComboboxProps = ComponentProps<typeof Combobox>;

/** Default compact summary shown in place of individual tags ("3 selected"). */
const defaultMultiSelectSummary = (count: number): string =>
  count === 1 ? "1 selected" : `${count} selected`;

export interface MultiSelectComboboxProps {
  items: ComboboxProps["items"];
  value: string[];
  onValueChange: (value: string[]) => void;
  size?: "small" | "medium" | "large";
  disabled?: boolean;
  placeholder?: string;
  /** Forwarded to the inner input for a11y. */
  "aria-label"?: string;
  /** Override the compact summary label; defaults to "<n> selected". */
  formatSummaryLabel?: (count: number) => string;
}

/**
 * Multi-select Combobox that renders a compact "<n> selected" summary instead
 * of a growing row of tags. Many selections then never overflow the trigger or
 * push the surrounding layout around — the failure mode plain tag mode has when
 * a user picks a dozen racks.
 *
 * This is the standard pattern for every multi-select *filter* in the app: use
 * it rather than a bare `<Combobox multiple>` so the behaviour stays uniform.
 * It wraps KUI's documented `multipleMode="count"` (see frontend.mdc / the
 * Kaizen UI Combobox docs) so there is one place to evolve it.
 */
export function MultiSelectCombobox({
  items,
  value,
  onValueChange,
  size = "small",
  disabled,
  placeholder,
  formatSummaryLabel = defaultMultiSelectSummary,
  ...rest
}: MultiSelectComboboxProps) {
  return (
    <Combobox
      items={items}
      multiple
      multipleMode="count"
      value={value}
      onValueChange={onValueChange as (value: string[]) => void}
      size={size}
      disabled={disabled}
      placeholder={placeholder}
      formatSummaryLabel={formatSummaryLabel}
      {...rest}
    />
  );
}
