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
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import { useRackCommandsStore } from "../context/RackCommandsStoreContext";
import { rackCommandDotColors } from "../lib/rackCommandDotPalette";

/**
 * Dashboard rack row: blink while a React-started job is in flight; colored dot replays last output (global buffer).
 */
export function RackTableCommandIndicator({ rack }: { rack: string }) {
  const { getRackLast, getPendingForRack, replayLastOutput } = useRackCommandsStore();
  const last = getRackLast(rack);
  const pend = getPendingForRack(rack);
  const busy = pend.length > 0;
  const hint = pend.map((p) => p.label).join(", ");

  if (busy) {
    return (
      <Tooltip title={hint || "Command running…"}>
        <Box
          component="span"
          role="status"
          aria-busy="true"
          sx={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 28,
            height: 28,
            flexShrink: 0,
            "&::after": {
              content: '""',
              display: "block",
              width: 12,
              height: 12,
              borderRadius: "50%",
              bgcolor: "warning.main",
              boxShadow: (t) => `0 0 0 2px ${t.palette.warning.light}`,
              "@keyframes rackCmdDashBlink": {
                "0%, 45%": { opacity: 1 },
                "55%, 100%": { opacity: 0.12 },
              },
              animation: "rackCmdDashBlink 1s steps(1, end) infinite",
            },
          }}
        />
      </Tooltip>
    );
  }

  const c = last ? rackCommandDotColors(last.cmdKind, last.isErr) : null;
  const label = last?.noticeTitle?.trim() || "Last command output";

  return (
    <Tooltip title={last ? `${label}\n${last.preview || ""}`.trim() : "No buffered command output for this rack"}>
      <span>
        <IconButton
          size="small"
          disabled={!last}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            replayLastOutput(rack);
          }}
          sx={{ p: 0.5, width: 28, height: 28 }}
          aria-label="Replay last command output"
        >
          <Box
            component="span"
            sx={{
              display: "inline-block",
              width: 12,
              height: 12,
              borderRadius: "50%",
              bgcolor: c?.bg ?? "action.disabledBackground",
              boxShadow: c ? `0 0 0 2px ${c.ring}` : "none",
            }}
          />
        </IconButton>
      </span>
    </Tooltip>
  );
}
