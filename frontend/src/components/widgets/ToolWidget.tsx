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
// ToolWidget — one catalog Tool, as a fixed-size KUI Card.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Every string an operator reads here comes from the Tool document. That is the point of the
// component: "we now ship NCCL 2.31.2" becomes one catalog edit instead of a code change in
// each view that happened to name the version.
//
// Built on `Card`'s own slots (`slotHeader`, `slotMedia`) rather than a hand-rolled flex
// column. An earlier version fought the component — it put `display: flex` on the root, not
// realising `Card` wraps children in a `.nv-card-content` grid, so every `flexShrink` landed
// on the wrong element; and it set an outer height against `.nv-card-root`'s
// `min-height: fit-content`, so the card grew past its cell and the grid item clipped the
// button row off the bottom. Letting `Card` do its own layout is what makes a fixed size work.
//
// The height budget is therefore the card's, not ours: at the app's density
// `.nv-card-content` contributes 24px of padding per side (`--spacing-density-2xl`), so the
// media height plus the content block has to stay under `WIDGET_CARD_HEIGHT`. See
// `widgetLayout.CARD_METRICS`.
import { GripVertical, X } from "lucide-react";
import { Button, Flex, Tag } from "@nvidia/foundations-react-core";
import Tooltip from "@mui/material/Tooltip";
import IconButton from "@mui/material/IconButton";
import { planToolLaunch } from "../../lib/toolLaunch";
import type { BoardAction } from "./boardActions";
import { ActionCardFace } from "./ActionCardFace";
import { withoutBubbling } from "./cardInteraction";
import type { CardOrientation } from "./widgetLayout";

/** Class the grid uses as its drag handle. Only this grip moves a card. */
export const WIDGET_DRAG_HANDLE = "nvis-widget-drag";


interface ToolWidgetProps {
  action: BoardAction;
  /** Disabled upstream — no rack selected, action in flight, cluster unreachable. */
  disabled?: boolean;
  /** Why this particular widget cannot run right now; shown in place of the description. */
  blockedReason?: string | null;
  running?: boolean;
  onRun: (action: BoardAction) => void;
  /** Open the details modal. Always enabled — reading about an action is never blocked. */
  onInfo: (action: BoardAction) => void;
  /** Customise mode: show the drag grip and the remove control. */
  editing?: boolean;
  onRemove?: (action: BoardAction) => void;
  /**
   * `vertical` for a grid (media as a banner on top), `horizontal` for a stack (media in a
   * left-hand column). Chosen by the board from its measured width — see
   * `widgetLayout.cardOrientation`.
   */
  orientation?: CardOrientation;
}

export function ToolWidget({
  action,
  disabled,
  blockedReason,
  running,
  onRun,
  onInfo,
  editing,
  onRemove,
  orientation = "vertical",
}: ToolWidgetProps) {
  // Only a catalog tool has a launch binding to vet. A workflow is launchable by definition:
  // the engine owns its run surface, so there is no document for this build to fail to
  // understand.
  const plan = action.tool ? planToolLaunch(action.tool) : null;
  const unsupported = plan?.kind === "unsupported";
  // A binding this build cannot honour outranks a selection problem: the second is something
  // the operator can fix, the first is not, so saying "select one node" would be misleading.
  const blocker = unsupported && plan?.kind === "unsupported" ? plan.reason : (blockedReason ?? null);

  return (
    <ActionCardFace
      title={action.name}
      subtitle={action.subtitle}
      description={action.description}
      category={action.category}
      image={action.tool?.image}
      orientation={orientation}
      blockedReason={blocker}
      onDetails={() => onInfo(action)}
      // Reinforces the Running tag using the component's own state, rather than inventing a
      // second visual language for "this one is busy".
      selected={!!running}
      status={
        running ? (
          <Tag color="yellow" kind="solid" readOnly density="compact">
            Running...
          </Tag>
        ) : null
      }
      controls={
        editing ? (
          // Grouped on a solid plate: these sit over bright artwork, and a bare icon on a
          // glowing banner is close to invisible. The plate is what makes them findable.
          <Flex
            gap="1"
            align="center"
            style={{
              flexShrink: 0,
              padding: "3px 5px",
              borderRadius: 8,
              background: "var(--background-color-surface-base)",
              border: "1px solid var(--border-color-base)",
              boxShadow: "var(--shadow-md)",
              color: "var(--text-color-primary)",
            }}
          >
            {/* The grip lives here so a drag never starts from the buttons below. */}
            <Tooltip title={`Move ${action.name}`}>
              <span
                className={WIDGET_DRAG_HANDLE}
                style={{ cursor: "grab", display: "inline-flex", padding: 2 }}
                aria-label={`Move ${action.name}`}
              >
                <GripVertical size={20} />
              </span>
            </Tooltip>
            {onRemove ? (
              <Tooltip title={`Remove ${action.name} from this board`}>
                <IconButton
                  size="small"
                  onClick={withoutBubbling(() => onRemove(action))}
                  aria-label={`Remove ${action.name}`}
                  sx={{ p: 0.25, color: "inherit" }}
                >
                  <X size={20} />
                </IconButton>
              </Tooltip>
            ) : null}
          </Flex>
        ) : null
      }
      actions={
        <Button
          kind="secondary"
          color={action.destructive ? "danger" : "brand"}
          size="small"
          disabled={disabled || running || blocker !== null}
          onClick={withoutBubbling(() => onRun(action))}
        >
          {running ? "Running..." : action.buttonLabel || "Run"}
        </Button>
      }
    />
  );
}
