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
/**
 * Confirmation dialog shown by ``RackCommandsStoreContext`` before launching an operation
 * against a rack already locked by another operator. Server-side enforcement is **not**
 * applied -- this is purely advisory so the operator can decide to proceed or back off.
 *
 * Convention: MUI Dialog shell + KUI components inside. Icon from ``lucide-react``.
 */
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import { Banner, Button, Flex, Text } from "@nvidia/foundations-react-core";
import { Lock } from "lucide-react";
import type { RackLockEntry } from "../../hooks/useRackLocks";

export interface LockConflictDialogProps {
  open: boolean;
  /** Per-rack lock info to display. Multi-rack ops list each lock on its own row. */
  conflicts: Array<{ rack: string; lock: RackLockEntry }>;
  onCancel: () => void;
  onProceed: () => void;
}

function formatAge(sinceIso: string): string {
  const t = Date.parse(sinceIso);
  if (Number.isNaN(t)) return "";
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60 > 0 ? ` ${m % 60}m` : ""} ago`;
}

function describeKind(lock: RackLockEntry): string {
  if (lock.kind === "claim") return "manual claim";
  return lock.detail || "operation";
}

export function LockConflictDialog({
  open,
  conflicts,
  onCancel,
  onProceed,
}: LockConflictDialogProps) {
  return (
    <Dialog open={open} onClose={onCancel} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1, fontWeight: 600 }}>
        <Lock size={18} aria-hidden="true" />
        Rack busy
      </DialogTitle>
      <DialogContent dividers>
        <Flex direction="col" gap="4">
          <Banner status="warning" kind="inline">
            <Text kind="body/regular/md">
              Another operator is currently working on this rack. Proceed only if you have
              coordinated with them.
            </Text>
          </Banner>
          <Flex direction="col" gap="2">
            {conflicts.map(({ rack, lock }) => (
              <Text key={rack} kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                <strong style={{ color: "var(--text-color-primary)" }}>{rack}</strong>
                {" — "}
                <span>{lock.operator || "unknown operator"}</span>
                {" • "}
                <span>{describeKind(lock)}</span>
                {lock.since ? <span> • {formatAge(lock.since)}</span> : null}
              </Text>
            ))}
          </Flex>
        </Flex>
      </DialogContent>
      <DialogActions sx={{ p: 2 }}>
        <Button kind="secondary" size="small" onClick={onCancel}>
          Cancel
        </Button>
        <Button kind="primary" color="danger" size="small" onClick={onProceed}>
          Run anyway
        </Button>
      </DialogActions>
    </Dialog>
  );
}
