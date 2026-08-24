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
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import { Button } from "@nvidia/foundations-react-core";
import { SqueuePanel } from "./SqueuePanel";

interface SqueueDialogProps {
  open: boolean;
  onClose: () => void;
  logDir?: string;
}

export function SqueueDialog({ open, onClose, logDir }: SqueueDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth scroll="paper">
      <DialogTitle sx={{ fontWeight: 600 }}>Active jobs</DialogTitle>
      <DialogContent dividers sx={{ pt: 1 }}>
        <SqueuePanel logDir={logDir} active={open} tableMaxHeight={360} />
      </DialogContent>
      <DialogActions>
        <Button kind="secondary" onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
