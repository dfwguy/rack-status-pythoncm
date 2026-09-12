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
// ExportBoardDialog — choose what leaves with the board, then download it.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The choice is the whole dialog, so it is spelled out rather than hidden behind a checkbox
// label. "Layout only" is a reference to tools the other instance must already have; "full
// configuration" carries the definitions so an instance that has never seen them can recreate
// them. The second is what makes a board portable and is also the one whose import writes to
// the receiving instance's shared catalog, so the panel says so here rather than leaving the
// recipient to discover it.
import { useEffect, useMemo, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import { Banner, Button, Flex, RadioGroup, Tag, Text } from "@nvidia/foundations-react-core";
import { Download } from "lucide-react";
import { getWorkflow, type Workflow } from "../../services/workflowService";
import type { BoardAction } from "./boardActions";
import {
  buildBundle,
  bundleFilename,
  type BundleScope,
  type BundleSource,
} from "./boardBundle";

interface ExportBoardDialogProps {
  open: boolean;
  onClose: () => void;
  surface: string;
  board: BoardAction[];
  source: BundleSource;
}

export function ExportBoardDialog({
  open,
  onClose,
  surface,
  board,
  source,
}: ExportBoardDialogProps) {
  const [scope, setScope] = useState<BundleScope>("full");
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const toolCount = board.filter((a) => a.kind === "tool").length;
  const workflowActions = useMemo(() => board.filter((a) => a.kind === "workflow"), [board]);

  // A summary needs the full workflow definitions, and the board only holds summaries. Fetched
  // when the dialog opens rather than on every board render: it is N requests for something
  // nobody looks at until they export.
  useEffect(() => {
    if (!open || scope !== "full" || workflowActions.length === 0) {
      setWorkflows([]);
      return;
    }
    let live = true;
    setLoading(true);
    setError("");
    Promise.all(workflowActions.map((a) => getWorkflow(a.id)))
      .then((full) => {
        if (live) setWorkflows(full);
      })
      .catch((e: unknown) => {
        if (!live) return;
        // Named, not swallowed: exporting a "full" bundle that silently omits a flow would
        // produce a file that looks complete and is not.
        setError(
          `Could not read ${workflowActions.length === 1 ? "the workflow" : "every workflow"} on this board (${
            e instanceof Error ? e.message : String(e)
          }). Export layout only, or retry.`,
        );
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [open, scope, workflowActions]);

  const download = () => {
    const bundle = buildBundle({ surface, board, scope, source, workflows });
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = bundleFilename(bundle);
    a.click();
    // Revoked immediately: the click has already started the download, and leaving the object
    // URL alive pins the blob in memory for the life of the tab.
    URL.revokeObjectURL(url);
    onClose();
  };

  const blocked = scope === "full" && (loading || !!error);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>Export this board</DialogTitle>
      <DialogContent dividers>
        <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)" }}>
          Shares the {board.length} action{board.length === 1 ? "" : "s"} on this board, in their
          current arrangement, as a file another Greenlight instance can import.
        </Text>

        <RadioGroup
          value={scope}
          onValueChange={(v) => setScope(v === "layout" ? "layout" : "full")}
          items={[
            {
              value: "full",
              children: "Full configuration — layout, actions, and their definitions",
            },
            { value: "layout", children: "Layout and actions only — references by id" },
          ]}
        />

        <div style={{ marginTop: 16 }}>
          {scope === "full" ? (
            <Banner status="warning" kind="inline">
              Includes {toolCount} tool definition{toolCount === 1 ? "" : "s"}
              {workflowActions.length
                ? ` and ${workflowActions.length} workflow definition${workflowActions.length === 1 ? "" : "s"}`
                : ""}
              . Importing this writes to the receiving instance&apos;s shared catalog, so the
              import panel there asks for per-item approval. It still needs the same scripts on
              its head node for those actions to run.
            </Banner>
          ) : (
            <Banner status="info" kind="inline">
              Small and safe to pass around: it only records which actions are on the board and
              where. An instance that does not already have them will show the board with those
              cards missing.
            </Banner>
          )}
        </div>

        {error ? (
          <div style={{ marginTop: 12 }}>
            <Banner status="error" kind="inline">
              {error}
            </Banner>
          </div>
        ) : null}

        <Flex gap="2" wrap="wrap" style={{ marginTop: 16 }}>
          <Tag color="gray" kind="outline" readOnly density="compact">
            {surface}
          </Tag>
          {source.platform ? (
            <Tag color="gray" kind="outline" readOnly density="compact">
              {source.platform}
            </Tag>
          ) : null}
          {source.product_version ? (
            <Tag color="gray" kind="outline" readOnly density="compact">
              v{source.product_version}
            </Tag>
          ) : null}
        </Flex>
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <MuiButton onClick={onClose} color="inherit">
          Cancel
        </MuiButton>
        <Button kind="primary" color="brand" size="small" onClick={download} disabled={blocked}>
          <Download size={14} style={{ marginRight: 6 }} />
          {loading ? "Reading workflows…" : "Download"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
