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
// ActionCardFace — how an action looks. One implementation, two callers.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// `ToolWidget` (catalog-driven, on a customisable board) and `rack/ActionCard` (hard-coded rack
// controls, on the detail tabs) are different *things* and the same picture. They were built
// separately, so the tabs kept a plain title-and-button card while the board grew artwork, a
// category badge, an ellipsised description and a Details modal. Restyling the tabs to match
// would have produced a second copy of that markup, and the copy is what drifts: the next
// person to change the badge, the hover or the media crop fixes one of them.
//
// So the split is by *concern*, not by feature. Everything visual lives here. What a card
// means — which document it came from, whether a launch binding is honourable, whether a
// destructive action needs the node list confirmed first — stays with its caller.
//
// Two non-obvious things this file knows about `Card`, both learned the hard way:
//
//   - it wraps children in a `.nv-card-content` **grid**, so a `display: flex` on the root
//     puts every `flexShrink` on the wrong element. Hence one `Flex` child, not four.
//   - `.nv-card-root` is `min-height: fit-content`, so a card grows past a fixed-height grid
//     cell rather than being clipped by it — and then draws over the row beneath. That is why
//     the text lines are single-line-ellipsised instead of wrapping, and why the height budget
//     in `widgetLayout.CARD_METRICS` is a budget rather than a suggestion.
import type { CSSProperties, ReactNode } from "react";
import { Info } from "lucide-react";
import { Badge, Button, Card, Flex, Text } from "@nvidia/foundations-react-core";
import Tooltip from "@mui/material/Tooltip";
import { CARD_METRICS, HORIZONTAL_CARD_METRICS, type CardOrientation } from "./widgetLayout";
import { CATEGORY_COLORS, CATEGORY_IMAGES } from "./toolImages";
import { withoutBubbling } from "./cardInteraction";

export interface ActionCardFaceProps {
  title: string;
  /** One line, ellipsised. Optional, but the slot is reserved either way — see below. */
  subtitle?: string;
  /** One line, ellipsised; the full text is the tooltip and lives in full in Details. */
  description: string;
  /** Drives the badge, its colour, and the fallback artwork. */
  category: string;
  /** Explicit artwork, overriding the category's. */
  image?: string;
  orientation?: CardOrientation;
  /** Beside the badge: a Running / Done / Error tag. */
  status?: ReactNode;
  /** KUI's own selected treatment — used for "a launch is in flight", not for a choice. */
  selected?: boolean;
  /** Drawn over the media, hard right: the board's drag grip and remove control. */
  controls?: ReactNode;
  /**
   * Replaces the description, in warning colour, when the action cannot run.
   *
   * In place of rather than beside it: the card has one line for prose, and the reason you
   * cannot press the button is more useful than what the button would have done.
   */
  blockedReason?: string | null;
  /**
   * Open the full detail. Drives both the Details button and the click on the card.
   *
   * Omitting it drops the button *and* `interactive`: KUI is explicit that the hover
   * treatment on a card nothing happens to is a false affordance, so the two ship together.
   */
  onDetails?: () => void;
  /**
   * Label for that button. "Info" for a description; "Results" where the detail *is* the
   * output of the last run, which is what the deep audits open.
   */
  detailsLabel?: string;
  /** The action button(s), bottom-right. Wrap handlers in `withoutBubbling`. */
  actions: ReactNode;
}

export function ActionCardFace({
  title,
  subtitle,
  description,
  category,
  image,
  orientation = "vertical",
  status,
  selected,
  controls,
  blockedReason,
  onDetails,
  detailsLabel = "Info",
  actions,
}: ActionCardFaceProps) {
  // Final fallback matters: `category` is a free string on the wire (the backend warns on an
  // unknown one rather than rejecting it), so a category this bundle never heard of must not
  // render a broken image.
  const art = (image || "").trim() || CATEGORY_IMAGES[category] || CATEGORY_IMAGES.validation;

  return (
    <Card
      kind="solid"
      layout={orientation}
      interactive={!!onDetails}
      selected={!!selected}
      onClick={onDetails}
      // The media is dark artwork, so header content on top of it renders in the light theme.
      mediaTheme="light"
      slotHeader={
        // `.nv-card-media-header` is `position: absolute; inset: 16px`, so it already spans the
        // media. Taking the full width lets the badge sit left and the controls sit hard right,
        // instead of the controls crowding the label.
        <Flex gap="2" align="start" justify="between" style={{ width: "100%" }}>
          <Flex gap="2" align="center" wrap="wrap">
            <Badge kind="solid" color={CATEGORY_COLORS[category] ?? "gray"}>
              {category}
            </Badge>
            {status}
          </Flex>
          {controls}
        </Flex>
      }
      slotMedia={
        <img
          src={art}
          alt=""
          // Decorative: the badge and title carry the meaning, so an empty alt keeps a screen
          // reader from announcing artwork that says nothing the text does not.
          aria-hidden="true"
          // Deliberately unstyled. KUI already gives this image `position: absolute; width:
          // 100%; height: 100%; object-fit: cover` inside `.nv-card-media`, and an inline style
          // here overrides that stylesheet while changing nothing about the wrapper that
          // actually decides the size. An earlier version set `width: 132` for the horizontal
          // case and produced a 132px picture floating in a 700px media column — the size is
          // the wrapper's to give, and index.css pins it from the two custom properties below.
        />
      }
      className={`nvis-action-card nvis-action-card--${orientation}`}
      style={
        {
          width: "100%",
          height: "100%",
          "--nvis-card-media-h": `${CARD_METRICS.media}px`,
          "--nvis-card-media-w": `${HORIZONTAL_CARD_METRICS.mediaWidth}px`,
          "--nvis-card-media-w-max": `${HORIZONTAL_CARD_METRICS.mediaWidthMax}px`,
        } as CSSProperties
      }
    >
      {/* One grid child, so `.nv-card-content`'s own gap does not apply between these lines —
          the spacing below is ours and stays predictable. It fills the content box (see
          index.css) so the button row can sit at the bottom of it. */}
      <Flex direction="col" gap="1" style={{ minWidth: 0, height: "100%" }}>
        <Text
          kind="body/bold/md"
          style={{
            display: "block",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={title}
        >
          {title}
        </Text>

        {/* Omitted entirely when there is none, rather than reserved with a `minHeight`.
            Holding the line open was there to stop two cards in a row being different shapes,
            but a CSS grid row already stretches its cards to a common height and Customise
            mode's cells are fixed — so the reservation bought nothing and cost a blank line on
            every rack control, none of which carries a subtitle. */}
        {subtitle ? (
          <Text
            kind="body/regular/sm"
            style={{
              display: "block",
              color: "var(--text-color-subtle)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={subtitle}
          >
            {subtitle}
          </Text>
        ) : null}

        <Tooltip title={blockedReason ?? description ?? ""} placement="bottom-start">
          <span
            style={{
              display: "block",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              color: blockedReason
                ? "var(--text-color-warning, #d29922)"
                : "var(--text-color-secondary)",
            }}
          >
            <Text kind="body/regular/sm">{blockedReason ?? description}</Text>
          </span>
        </Tooltip>

        {/* `marginTop: auto` pins the row to the bottom of whatever height the card ends up
            with, so a card stretched to match a taller neighbour gains breathing room above
            its buttons instead of a gap beneath them. The 8 is the floor for the compact case
            where there is no slack to take up. */}
        <Flex gap="2" justify="end" style={{ marginTop: "auto", paddingTop: 8 }}>
          {/* Bordered, per this app's buttons-always-have-a-border rule (frontend.mdc); the
              hover-to-grey comes from KUI's own neutral hover, not from local CSS. */}
          {onDetails ? (
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              onClick={withoutBubbling(onDetails)}
            >
              <Info size={14} style={{ marginRight: 6 }} />
              {detailsLabel}
            </Button>
          ) : null}
          {actions}
        </Flex>
      </Flex>
    </Card>
  );
}
