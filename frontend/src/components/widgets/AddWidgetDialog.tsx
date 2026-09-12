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
// AddWidgetDialog — pick an action to put on the board.
// Answer: whatever the catalog offers for this cluster, plus every workflow.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The candidates are deliberately wider than the current surface. "Add more action buttons
// that bring more tools to run from the UI" is the point — a tool seeded onto the multirack
// board is still a legitimate thing to want on a rack board, and the server already
// established it is compatible with this platform by returning it at all. Workflows are here
// too, and *only* here: a flow someone authored never appears on a default board.
//
// Rendered as cards rather than a text list. These read as a menu of things to choose, so the
// badge and the thumbnail do the recognising and each row carries its own Add button — a list
// of plain rows made the operator read every line to find the one they meant.
import { useMemo, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import { Badge, Banner, Button, Card, Flex, Text, TextInput } from "@nvidia/foundations-react-core";
import { Plus } from "lucide-react";
import type { BoardAction } from "./boardActions";
import { actionImage } from "./toolImages";

interface AddWidgetDialogProps {
  open: boolean;
  actions: BoardAction[];
  onClose: () => void;
  onAdd: (action: BoardAction) => void;
}

const BADGE_COLORS: Record<string, "green" | "blue" | "teal" | "purple" | "yellow" | "gray"> = {
  validation: "green",
  performance: "blue",
  hardware: "teal",
  networking: "purple",
  maintenance: "yellow",
  setup: "gray",
  workflow: "teal",
};

export function AddWidgetDialog({ open, actions, onClose, onAdd }: AddWidgetDialogProps) {
  const [query, setQuery] = useState("");

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return actions;
    return actions.filter((a) =>
      // The script and the kind are searchable too: an operator hunting "the nvloom one" often
      // remembers the script name, not the display name we gave it.
      [a.name, a.subtitle, a.description, a.category, a.kind, a.tool?.script].some((field) =>
        (field ?? "").toLowerCase().includes(q),
      ),
    );
  }, [actions, query]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>Add an action</DialogTitle>
      <DialogContent dividers>
        <TextInput
          value={query}
          onChange={(e) => setQuery((e.target as HTMLInputElement).value)}
          placeholder="Filter by name, script, category or workflow"
          aria-label="Filter actions"
        />
        {matches.length === 0 ? (
          <div style={{ marginTop: 12 }}>
            <Banner status="info" kind="inline">
              {actions.length === 0
                ? "Every action available for this cluster is already on the board."
                : "No action matches that filter."}
            </Banner>
          </div>
        ) : (
          <div
            style={{
              display: "grid",
              gap: 8,
              marginTop: 12,
              maxHeight: 420,
              overflowY: "auto",
              // Room for the scrollbar so a card's Add button is never under it.
              paddingRight: 4,
            }}
          >
            {matches.map((action) => (
              <Card key={action.key} kind="solid" layout="horizontal" style={{ padding: 0 }}>
                <Flex gap="3" align="center" style={{ padding: 12, minWidth: 0 }}>
                  {/* A thumbnail of the same banner the card will carry, so what you pick
                      looks like what you get. */}
                  <img
                    src={actionImage(action)}
                    alt=""
                    aria-hidden="true"
                    style={{
                      width: 72,
                      height: 44,
                      objectFit: "cover",
                      borderRadius: 6,
                      flexShrink: 0,
                    }}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 2 }}>
                      <Text
                        kind="body/bold/sm"
                        style={{
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {action.name}
                      </Text>
                      <Badge kind="outline" color={BADGE_COLORS[action.category] ?? "gray"}>
                        {action.category}
                      </Badge>
                    </Flex>
                    <Text
                      kind="body/regular/sm"
                      style={{
                        display: "block",
                        color: "var(--text-color-subtle)",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                      title={action.description}
                    >
                      {action.subtitle || action.description || action.tool?.script}
                    </Text>
                  </div>
                  <Button
                    kind="secondary"
                    color="brand"
                    size="small"
                    onClick={() => onAdd(action)}
                    aria-label={`Add ${action.name}`}
                    style={{ flexShrink: 0 }}
                  >
                    <Plus size={14} style={{ marginRight: 4 }} />
                    Add
                  </Button>
                </Flex>
              </Card>
            ))}
          </div>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <MuiButton onClick={onClose} color="inherit">
          Close
        </MuiButton>
      </DialogActions>
    </Dialog>
  );
}
