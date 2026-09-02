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
import { useState, type FormEvent } from "react";
import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import { Card, Text, Button, Banner } from "@nvidia/foundations-react-core";
import { GreenlightMark } from "../components/GreenlightMark";
import { useAuth } from "../context/AuthContext";

export function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    const result = await login(username, password);
    setLoading(false);
    if (!result.ok) {
      setError(result.error || "Login failed");
      // Only the password is cleared. Wiping the username too would make a mistyped password cost
      // two fields to retype, and it protects nothing — the username is not a secret.
      setPassword("");
    }
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
      <Card style={{ padding: 40, maxWidth: 400, width: "100%" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
          <GreenlightMark size={36} />
          <Text kind="title/lg">NVIS Greenlight</Text>
        </div>

        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 24, display: "block" }}>
          Sign in to access the rack validation dashboard.
        </Text>

        {error && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="error">{error}</Banner>
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <TextField
            fullWidth
            type="text"
            label="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            disabled={loading}
            sx={{ mb: 2 }}
          />
          <TextField
            fullWidth
            type="password"
            label="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            sx={{ mb: 2 }}
          />
          <Button
            type="submit"
            kind="primary"
            disabled={loading || !password}
            style={{ width: "100%" }}
          >
            {loading ? "Signing in..." : "Sign In"}
          </Button>
        </form>
      </Card>
    </Box>
  );
}
