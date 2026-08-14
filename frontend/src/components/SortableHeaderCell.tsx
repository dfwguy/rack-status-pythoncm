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
// SortableHeaderCell.tsx — the clickable column header shared by the Scheduler tables.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
import TableCell from "@mui/material/TableCell";
import TableSortLabel from "@mui/material/TableSortLabel";
import type { ReactNode } from "react";

const NVIDIA_GREEN = "#76B900";

interface SortableHeaderCellProps {
  label: ReactNode;
  /** This cell's column index. */
  col: number;
  /** Column the table is currently sorted by, or null for the feed's own order. */
  sortCol: number | null;
  sortDir: "asc" | "desc";
  onToggle: (col: number) => void;
  /** Named in the tooltip; defaults to the label when it is a plain string. */
  title?: string;
  align?: "left" | "right" | "center";
}

export function SortableHeaderCell({ label, col, sortCol, sortDir, onToggle, title, align }: SortableHeaderCellProps) {
  const active = sortCol === col;
  const name = title ?? (typeof label === "string" ? label : "this column");
  return (
    <TableCell
      align={align}
      sortDirection={active ? sortDir : false}
      sx={{ fontWeight: 700, bgcolor: "action.hover", color: NVIDIA_GREEN, whiteSpace: "nowrap" }}
    >
      <TableSortLabel
        active={active}
        direction={active ? sortDir : "asc"}
        onClick={() => onToggle(col)}
        title={`Sort by ${name} — click again to reverse, a third time to restore the original order`}
        // The header is NVIDIA green; MUI would otherwise grey the label and arrow once active.
        sx={{
          color: "inherit",
          "&:hover, &.Mui-active": { color: "inherit" },
          "& .MuiTableSortLabel-icon": { color: "inherit !important" },
        }}
      >
        {label}
      </TableSortLabel>
    </TableCell>
  );
}
