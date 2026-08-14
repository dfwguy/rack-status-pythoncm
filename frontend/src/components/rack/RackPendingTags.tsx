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
import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";
import { Tag, Text } from "@nvidia/foundations-react-core";
import { useRackCommandsStore } from "../../context/RackCommandsStoreContext";

/**
 * Inline "Running:" indicator for the React-started async jobs still in flight for a rack.
 * Rendered inside the padded rack-detail section (not at the provider root) so it lines up with
 * the page content instead of floating against the top-left edge.
 */
export function RackPendingTags({ rack }: { rack: string }) {
  const { getPendingForRack } = useRackCommandsStore();
  const pending = getPendingForRack(rack);
  if (pending.length === 0) return null;
  return (
    <Box sx={{ mb: 2, display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
        Running:
      </Text>
      <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: "wrap" }}>
        {pending.map((p) => (
          <Tag key={p.id} color="yellow" kind="outline" readOnly density="compact">
            {p.label}
          </Tag>
        ))}
      </Stack>
    </Box>
  );
}
