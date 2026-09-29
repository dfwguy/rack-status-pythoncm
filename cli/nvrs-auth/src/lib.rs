// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs-auth` — SRP-6a login and token storage. **Phase 1; not yet implemented.**
//!
//! When built, this crate mirrors `frontend/src/lib/srpClient.ts` exactly: SHA-256, the RFC 5054
//! 4096-bit group (generator 5), a 32-byte salt, and RFC 5054 §2.6 padding **on**, with the padding
//! asymmetry in the client proof `M`. A mismatch does not error — it produces a proof the server
//! rejects, indistinguishable from a wrong password. The correctness gate is therefore a pinned
//! test vector generated from the server's own `backend/identity/srp.py`, replayed here, the same
//! guard `srpClient.test.ts` uses.
//!
//! The token store holds the `nvis_access` / `nvis_refresh` values (extracted from the login
//! response's `Set-Cookie` headers) in the OS keyring, and drives `POST /api/auth/refresh` when the
//! transport reports a 401.

/// The parameters the SRP exchange must match, kept here so Phase 1 wires against a single source.
pub mod params {
    /// Matches `srp.NG_4096` on the server and `N_HEX` in `srpClient.ts`. Filled in Phase 1.
    pub const GROUP_BITS: u32 = 4096;
    /// RFC 5054 generator for the 4096-bit group. Not IKE "group 5" (1536-bit) — a classic mix-up.
    pub const GENERATOR: u8 = 5;
    pub const SALT_BYTES: usize = 32;
}
