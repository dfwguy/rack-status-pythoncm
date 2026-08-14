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
import { Text } from "@nvidia/foundations-react-core";

interface FooterNotesProps {
  footnotes: string[];
}

export function FooterNotes({ footnotes }: FooterNotesProps) {
  if (!footnotes.length) return null;

  return (
    <Box sx={{ mt: 2 }}>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        {footnotes.map((note, i) => (
          <li key={i}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {note}
            </Text>
          </li>
        ))}
      </ul>
    </Box>
  );
}
