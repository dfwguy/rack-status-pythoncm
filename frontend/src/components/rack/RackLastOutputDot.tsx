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
import { useRackCommands } from "../../context/RackCommandContext";
import { rackCommandDotColors } from "../../lib/rackCommandDotPalette";

/** Rack detail header: blink while async jobs run; dot replays last buffered output. */
export function RackLastOutputDot() {
  const { rackLastOutput, openText, asyncPendingCount } = useRackCommands();
  const rec = rackLastOutput;
  const c = rec ? rackCommandDotColors(rec.cmdKind, rec.isErr) : null;
  const label = rec?.noticeTitle?.trim() || "Last command output";
  const busy = asyncPendingCount > 0;

  if (busy) {
    return (
      <Tooltip title="Command running…">
        <Box
          component="span"
          role="status"
          aria-busy="true"
          sx={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 36,
            height: 36,
            ml: 0.5,
            "&::after": {
              content: '""',
              display: "block",
              width: 12,
              height: 12,
              borderRadius: "50%",
              bgcolor: "warning.main",
              boxShadow: (t) => `0 0 0 2px ${t.palette.warning.light}`,
              "@keyframes rackCmdHeaderBlink": {
                "0%, 45%": { opacity: 1 },
                "55%, 100%": { opacity: 0.12 },
              },
              animation: "rackCmdHeaderBlink 1s steps(1, end) infinite",
            },
          }}
        />
      </Tooltip>
    );
  }

  return (
    <Tooltip title={rec ? `${label}\n${rec.preview || ""}`.trim() : "No buffered command output for this rack yet"}>
      <span>
        <IconButton
          size="small"
          disabled={!rec}
          onClick={() => {
            if (!rec) return;
            openText(rec.noticeTitle?.trim() || "Last output", rec.full, rec.isErr);
          }}
          sx={{ p: 0.75, ml: 0.5 }}
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
