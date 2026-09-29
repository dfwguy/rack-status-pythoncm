// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs-auth` — SRP-6a login and the token store for the Rack Status CLI.
//!
//! Three pieces: [`srp`] (the byte-exact client half of the SRP-6a exchange), [`store`] (where the
//! session tokens live between invocations), and the [`login`]/[`refresh`]/[`logout`] flows that
//! drive the exchange over an [`nvrs_client::Client`]. The password never leaves [`srp`]; what the
//! rest of the CLI ever sees is a bearer access token.

mod session;
mod srp;
mod store;

pub use session::{login, logout, refresh, LoginOutcome};
pub use srp::{canonical_login, SrpError};
pub use store::{token_from_cookies, TokenStore, Tokens};

/// Everything the auth layer can fail with.
#[derive(Debug, thiserror::Error)]
pub enum AuthError {
    /// A transport or server error underneath the auth exchange.
    #[error(transparent)]
    Client(#[from] nvrs_client::Error),

    /// A fault computing the client proof (a bad value from the server, essentially never a wrong
    /// password — that surfaces as [`AuthError::InvalidCredentials`]).
    #[error("SRP: {0}")]
    Srp(#[from] SrpError),

    /// The login was rejected. Deliberately does not distinguish an unknown login from a wrong
    /// password — the server refuses to, and echoing more here would rebuild the enumeration oracle
    /// it avoids.
    #[error("invalid username or password")]
    InvalidCredentials,

    /// The server could not prove it holds the verifier (HAMK mismatch). Something is answering that
    /// should not be; a session established here would be a session with an impostor.
    #[error("the server failed to authenticate itself — do not trust this connection")]
    ServerAuthFailed,

    /// A refresh was refused: the session is genuinely gone, not merely stale.
    #[error("session expired — run `nvrs login` again")]
    SessionExpired,

    /// The server's response did not have the shape the protocol requires.
    #[error("unexpected server response: {0}")]
    Protocol(String),

    /// The on-disk token store could not be read or written.
    #[error("token store: {0}")]
    Store(String),
}
