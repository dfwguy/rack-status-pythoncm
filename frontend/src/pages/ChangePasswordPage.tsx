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
 * Change password — shown unavoidably when the bootstrap credential is still in use (FR-10a).
 *
 * The bootstrap password was printed once into a startup log. Anyone who can read that log holds
 * the administrator credential, which is acceptable for the minute it takes to sign in and
 * intolerable as a steady state. So this is not an item in a settings menu the operator may never
 * visit: it stands in front of the dashboard until the password has been changed.
 *
 * Neither password is sent. The current one is proved over SRP and the new one is turned into a
 * salt and verifier in this browser — same disclosure properties as login, in both directions.
 */

import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import { Banner, Button, Card, Text } from "@nvidia/foundations-react-core";
import { type FormEvent, useState } from "react";

import { changePassword } from "../lib/srpClient";

/** Long enough to matter, short enough that nobody writes it on a sticky note. */
const MIN_LENGTH = 12;

export function ChangePasswordPage({
  login,
  onChanged,
}: {
  /**
   * The bare enrolled login (`admin`) — **not** a display name and not a principal.
   *
   * SRP derives `x = H(salt, H(login ":" password))`, so this string has to be byte-identical to
   * what the server enrolled. Passing `"Administrator"` or `"user:admin"` produces a well-formed
   * proof the server can only reject, and the operator is told their password is wrong when it is
   * not. Nullable so a missing value is a visible error rather than a silent guess.
   */
  login: string | null | undefined;
  onChanged: () => void;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Checked here for a fast, obvious answer, and it is *only* a convenience: the client is not a
  // security boundary, so anything that must hold has to hold on the server too.
  const tooShort = next.length > 0 && next.length < MIN_LENGTH;
  const mismatch = confirm.length > 0 && next !== confirm;
  const unchanged = next.length > 0 && next === current;
  const submittable =
    current.length > 0 && next.length >= MIN_LENGTH && next === confirm && !unchanged && !busy;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!login) {
      // Says what is actually wrong. Attempting the exchange with a guessed identity would return
      // "current password is incorrect", sending the operator to re-check a password that is fine.
      setError(
        "Your signed-in identity could not be determined, so the password cannot be changed. " +
          "Sign out and in again.",
      );
      return;
    }
    setError("");
    setBusy(true);
    const result = await changePassword(login, current, next);
    setBusy(false);
    if (result.ok) {
      onChanged();
      return;
    }
    setError(result.error || "Could not change the password");
    setCurrent("");
  };

  return (
    <Box
      sx={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        bgcolor: "background.default",
      }}
    >
      <Card style={{ padding: 40, maxWidth: 440, width: "100%" }}>
        <Text kind="title/lg" style={{ marginBottom: 8, display: "block" }}>
          Choose a new password
        </Text>
        <Text
          kind="body/regular/sm"
          style={{ color: "var(--text-color-secondary)", marginBottom: 24, display: "block" }}
        >
          You are signed in with the one-time password generated at first boot. It was written to
          the startup log, so anyone who can read that log can sign in as you until it is changed.
        </Text>

        {error && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="error">{error}</Banner>
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <TextField
            fullWidth
            type="password"
            label="Current password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            autoFocus
            disabled={busy}
            sx={{ mb: 2 }}
          />
          <TextField
            fullWidth
            type="password"
            label="New password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            disabled={busy}
            error={tooShort || unchanged}
            helperText={
              unchanged
                ? "The new password must differ from the current one"
                : tooShort
                  ? `At least ${MIN_LENGTH} characters`
                  : " "
            }
            sx={{ mb: 1 }}
          />
          <TextField
            fullWidth
            type="password"
            label="Confirm new password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            disabled={busy}
            error={mismatch}
            helperText={mismatch ? "The two entries do not match" : " "}
            sx={{ mb: 2 }}
          />

          <Text
            kind="body/regular/sm"
            style={{ color: "var(--text-color-secondary)", marginBottom: 16, display: "block" }}
          >
            You will be signed out everywhere and asked to sign in again — including here. A
            password change that left existing sessions working would not help against the thing it
            is usually a response to.
          </Text>

          <Button kind="primary" type="submit" disabled={!submittable} style={{ width: "100%" }}>
            {busy ? "Changing…" : "Change password and sign out"}
          </Button>
        </form>
      </Card>
    </Box>
  );
}
