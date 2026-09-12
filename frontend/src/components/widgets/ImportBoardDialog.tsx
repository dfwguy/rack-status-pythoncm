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
// ImportBoardDialog — review a shared board, then apply the parts you accept.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Staged rather than applied, because the two halves of an import have very different reach:
//
//   the layout   is mine. It lands in this browser's localStorage and affects nobody else.
//   a definition is everyone's. Writing a Tool document or a Workflow changes what every
//                operator of this instance sees, and can overwrite one they rely on.
//
// So the definitions are listed with what importing each would *do* here — new, changed (with
// the differing fields named), or identical — and nothing is written that was not ticked. An
// import that silently overwrote a tool would be indistinguishable from the catalog changing
// by itself, which is the worst failure this feature could have.
//
// The apply goes through one authorized endpoint, `POST /api/boards/import`, which carries its
// own action (`catalog.board:import`) and re-decides per item whether this caller may create or
// overwrite — deciding new-versus-existing from the store, never from anything sent from here.
// So the server's verdict is the real gate, including the `script` shape check that stops a
// shared file smuggling a shell payload. This dialog can propose; only the server accepts, and
// an item refused for want of a permission comes back as `skipped` with the reason.
import { useCallback, useMemo, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import {
  Badge,
  Banner,
  Button,
  Card,
  Flex,
  Text,
} from "@nvidia/foundations-react-core";
import { Upload } from "lucide-react";
import { importBoardBundle, type BoardImportResult } from "../../services/catalogService";
import type { WorkflowSummary } from "../../services/workflowService";
import type { Tool } from "../../types/catalog";
import {
  diffBundle,
  parseBundle,
  platformMismatch,
  type BoardBundle,
  type BundleItem,
} from "./boardBundle";

interface ImportBoardDialogProps {
  open: boolean;
  onClose: () => void;
  /** The surface the operator is importing into. A bundle for another one is refused. */
  surface: string;
  localTools: Tool[];
  localWorkflows: WorkflowSummary[];
  localPlatform?: string;
  /** Apply the board arrangement for this browser. */
  onApplyLayout: (bundle: BoardBundle) => void;
  /** Re-read tools and workflows after definitions were written. */
  onImported: () => void;
}

const STATUS_COLORS = { new: "green", changed: "yellow", identical: "gray" } as const;

export function ImportBoardDialog({
  open,
  onClose,
  surface,
  localTools,
  localWorkflows,
  localPlatform,
  onApplyLayout,
  onImported,
}: ImportBoardDialogProps) {
  const [bundle, setBundle] = useState<BoardBundle | null>(null);
  const [error, setError] = useState("");
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState(false);
  const [failures, setFailures] = useState<string[]>([]);

  const items = useMemo(
    () => (bundle ? diffBundle(bundle, localTools, localWorkflows) : []),
    [bundle, localTools, localWorkflows],
  );
  /** Only items carrying a definition can be written; the rest are references. */
  const writable = useMemo(() => items.filter((i) => i.tool || i.workflow), [items]);
  const mismatch = bundle ? platformMismatch(bundle, localPlatform) : "";
  const wrongSurface = bundle && bundle.surface !== surface ? bundle.surface : "";

  const reset = useCallback(() => {
    setBundle(null);
    setError("");
    setAccepted(new Set());
    setFailures([]);
  }, []);

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    reset();
    const result = parseBundle(await file.text());
    if (!result.bundle) {
      setError(result.error);
      return;
    }
    setBundle(result.bundle);
    // Pre-tick what is genuinely new; leave anything that would *overwrite* unticked, so an
    // overwrite is always a deliberate act rather than the default.
    const fresh = diffBundle(result.bundle, localTools, localWorkflows)
      .filter((i) => (i.tool || i.workflow) && i.status === "new")
      .map((i) => i.key);
    setAccepted(new Set(fresh));
  };

  const toggle = (key: string) =>
    setAccepted((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const apply = async () => {
    if (!bundle) return;
    setApplying(true);
    const approve = writable.filter((i) => accepted.has(i.key)).map((i) => i.key);
    let outcome: BoardImportResult[] = [];
    let requestError = "";
    try {
      outcome = (await importBoardBundle(bundle, approve)).results;
    } catch (e: unknown) {
      // A 403 here is the coarse gate: this principal may not import boards at all.
      requestError = e instanceof Error ? e.message : String(e);
    }
    setApplying(false);

    const byName = new Map(writable.map((i) => [i.key, i.name]));
    const problems = requestError
      ? [requestError]
      : outcome
          .filter((r) => r.status !== "applied")
          .map((r) => `${byName.get(r.key) ?? r.key}: ${r.detail}`);
    setFailures(problems);

    if (problems.length === 0) {
      onApplyLayout(bundle);
      onImported();
      onClose();
      reset();
    } else if (!requestError) {
      // Some definitions may have landed even though others were refused, so refresh either
      // way and leave the dialog open with the reasons.
      onImported();
    }
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>Import a board</DialogTitle>
      <DialogContent dividers>
        {!bundle ? (
          <>
            <Text
              kind="body/regular/sm"
              style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)" }}
            >
              Choose a board bundle exported from this or another Greenlight instance. Nothing is
              changed until you review what it contains.
            </Text>
            <MuiButton component="label" variant="outlined" startIcon={<Upload size={16} />}>
              Choose file
              <input
                type="file"
                accept="application/json,.json"
                hidden
                onChange={(e) => void onFile(e.target.files?.[0])}
              />
            </MuiButton>
          </>
        ) : null}

        {error ? (
          <Banner status="error" kind="inline">
            {error}
          </Banner>
        ) : null}

        {bundle ? (
          <>
            <Flex gap="2" wrap="wrap" style={{ marginBottom: 12 }}>
              <Badge kind="outline" color="gray">
                {bundle.scope === "full" ? "full configuration" : "layout only"}
              </Badge>
              {bundle.source.customer ? (
                <Badge kind="outline" color="gray">
                  from {bundle.source.customer}
                </Badge>
              ) : null}
              {bundle.source.platform ? (
                <Badge kind="outline" color="gray">
                  {bundle.source.platform}
                </Badge>
              ) : null}
            </Flex>

            {wrongSurface ? (
              <Banner status="warning" kind="inline">
                This bundle is for the <strong>{wrongSurface}</strong> board, not{" "}
                <strong>{surface}</strong>. Importing it here would put its actions on a board
                they were not arranged for.
              </Banner>
            ) : null}
            {mismatch ? (
              <div style={{ marginTop: 8 }}>
                <Banner status="warning" kind="inline">
                  {mismatch}
                </Banner>
              </div>
            ) : null}

            <Text kind="body/bold/sm" style={{ display: "block", margin: "16px 0 8px" }}>
              {items.length} action{items.length === 1 ? "" : "s"} on this board
            </Text>

            <div style={{ display: "grid", gap: 8, maxHeight: 320, overflowY: "auto" }}>
              {items.map((item) => (
                <ItemRow
                  key={item.key}
                  item={item}
                  checked={accepted.has(item.key)}
                  onToggle={() => toggle(item.key)}
                />
              ))}
            </div>

            {writable.length === 0 ? (
              <div style={{ marginTop: 12 }}>
                <Banner status="info" kind="inline">
                  This bundle carries no definitions, so importing it only rearranges your own
                  board. Any action this instance does not have will simply not appear.
                </Banner>
              </div>
            ) : null}

            {failures.length ? (
              <div style={{ marginTop: 12 }}>
                <Banner status="error" kind="inline">
                  {failures.length} item{failures.length === 1 ? "" : "s"} could not be imported:
                  {" "}
                  {failures.join("; ")}
                </Banner>
              </div>
            ) : null}
          </>
        ) : null}
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <MuiButton
          onClick={() => {
            reset();
            onClose();
          }}
          color="inherit"
        >
          Cancel
        </MuiButton>
        {bundle ? (
          <Button
            kind="primary"
            color="brand"
            size="small"
            onClick={() => void apply()}
            disabled={applying}
          >
            {applying
              ? "Importing…"
              : accepted.size > 0
                ? `Import board and ${accepted.size} definition${accepted.size === 1 ? "" : "s"}`
                : "Import board only"}
          </Button>
        ) : null}
      </DialogActions>
    </Dialog>
  );
}

function ItemRow({
  item,
  checked,
  onToggle,
}: {
  item: BundleItem;
  checked: boolean;
  onToggle: () => void;
}) {
  const canWrite = !!(item.tool || item.workflow);
  return (
    <Card kind="solid" layout="horizontal" style={{ padding: 0 }}>
      <Flex gap="2" align="center" style={{ padding: "8px 12px", minWidth: 0 }}>
        <Checkbox
          checked={checked}
          onChange={onToggle}
          disabled={!canWrite}
          size="small"
          // MUI 9: input attributes go through slotProps, not inputProps.
          slotProps={{ input: { "aria-label": `Import ${item.name}` } }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <Flex gap="2" align="center" wrap="wrap">
            <Text kind="body/bold/sm">{item.name}</Text>
            <Badge kind="outline" color={STATUS_COLORS[item.status]}>
              {item.status}
            </Badge>
            <Badge kind="outline" color="gray">
              {item.kind}
            </Badge>
          </Flex>
          <Text
            kind="body/regular/sm"
            style={{ display: "block", color: "var(--text-color-subtle)" }}
          >
            {describe(item)}
          </Text>
        </div>
      </Flex>
    </Card>
  );
}

/** One line saying what importing this item would actually do here. */
function describe(item: BundleItem): string {
  if (!item.tool && !item.workflow) {
    return item.missingLocally
      ? "Referenced but not present here — this card will not appear."
      : "Already present; only its position is imported.";
  }
  if (item.status === "new") return `Creates a new ${item.kind} on this instance.`;
  if (item.status === "changed") {
    return `Overwrites your ${item.kind}. Differs in: ${item.changedFields.join(", ")}.`;
  }
  return "Identical to your copy; importing changes nothing.";
}
