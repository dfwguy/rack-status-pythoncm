/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * Shows a one-time password — the only moment it exists anywhere.
 *
 * The server keeps a verifier, not a password, so this value cannot be fetched again by anyone
 * including the operator who just created the account. That makes this dialog load-bearing in a way
 * most dialogs are not: dismissing it without copying leaves an account nobody can sign in to, and
 * the only remedy is another reset.
 *
 * So it is deliberately awkward to dismiss by accident. No backdrop click, no escape key, no
 * auto-close — the operator has to say they have copied it. That is a worse experience than a
 * toast and a better outcome than a locked-out user.
 */

import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import { Banner, Button, Text } from "@nvidia/foundations-react-core";
import { Check, Copy } from "lucide-react";
import { useState } from "react";

import type { IssuedCredential } from "../../types/identity";

export function IssuedCredentialDialog({
  credential,
  onClose,
}: {
  credential: IssuedCredential | null;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  if (credential === null) return null;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(credential.password);
      setCopied(true);
    } catch {
      // Clipboard access is denied over plain HTTP and in some embedded browsers. The password is
      // on screen and selectable, so this is a convenience failing, not the operation.
      setCopied(false);
    }
  };

  const dismiss = () => {
    setCopied(false);
    onClose();
  };

  return (
    <Dialog
      open
      // Deliberately no `onClose`. MUI only dismisses through the handler it is given, and the
      // `open` prop above is driven solely by the button below — so a backdrop click and a stray
      // Escape both do nothing, which is the point: either is exactly how an unrecoverable value
      // gets lost. (`disableEscapeKeyDown` is not on this MUI version's DialogProps, and is not
      // needed once `onClose` is absent.)
      maxWidth="sm"
      fullWidth
    >
      <DialogTitle>One-time password for {credential.login}</DialogTitle>
      <DialogContent>
        <div style={{ marginBottom: 16 }}>
          <Banner status="warning">
            This is the only time this password is shown. It is not stored — only its verifier is —
            so it cannot be retrieved again by anyone.
          </Banner>
        </div>

        <Text
          kind="mono/sm"
          style={{
            display: "block",
            padding: "12px 16px",
            marginBottom: 12,
            borderRadius: 4,
            background: "var(--background-color-secondary, rgba(255,255,255,0.06))",
            userSelect: "all",
            wordBreak: "break-all",
          }}
        >
          {credential.password}
        </Text>

        <Button kind="secondary" size="small" onClick={copy} style={{ marginBottom: 16 }}>
          {copied ? <Check size={16} /> : <Copy size={16} />}
          <span style={{ marginLeft: 6 }}>{copied ? "Copied" : "Copy password"}</span>
        </Button>

        <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
          Give it to <strong>{credential.login}</strong> over a channel you trust. They will be
          required to choose their own password when they first sign in, and until they do, anyone
          holding this string can sign in as them.
        </Text>
      </DialogContent>
      <DialogActions>
        {/* The only way out. Wording chosen so clicking it is an assertion, not an acknowledgement. */}
        <Button kind="primary" onClick={dismiss}>
          I have copied the password
        </Button>
      </DialogActions>
    </Dialog>
  );
}
