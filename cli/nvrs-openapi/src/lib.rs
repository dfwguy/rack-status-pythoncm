// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs-openapi` — the CLI's view of the deployment's contract. **Phase 2; not yet implemented.**
//!
//! The backend serves a live OpenAPI 3.1 document at `GET /api/openapi.json`, generated from the
//! route table on every request so it cannot drift from the code (`backend/openapi.py`). This crate
//! fetches and caches it so `nvrs api spec` can browse the surface and `nvrs api <VERB> <path>` can
//! validate a path and its query parameters before sending. Note the document covers `/api/*` only
//! — it excludes `/oidc/*`, `/.well-known/*` and the `/ws/terminal` WebSocket.

/// The path the spec is served from, behind the normal auth gate.
pub const SPEC_PATH: &str = "/api/openapi.json";
