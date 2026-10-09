// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! The one error type every Rack Status call can fail with.
//!
//! Modelled on the server's own response-code contract (`docs/API_RESPONSE_CODES.md`): each HTTP
//! class means one thing, and the CLI maps each to a distinct variant so the binary can choose an
//! exit code and a message without re-parsing status numbers. `Unauthorized` is split out from the
//! generic `Api` case precisely because it is the one the caller retries after a token refresh.

use std::time::Duration;

/// The result of any transport call.
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// The request never completed: DNS, connect, TLS handshake, or a broken stream.
    ///
    /// A self-signed OVA cert surfaces here (`invalid peer certificate`) unless the profile opted
    /// into trusting it — the message is preserved so the binary can suggest `--ca-cert`/`--insecure`.
    #[error("could not reach the deployment: {0}")]
    Transport(#[source] reqwest::Error),

    /// HTTP 401 — the credential is missing, expired, or withdrawn. The server deliberately does not
    /// say which (FR-4b), so neither do we — the remedy is always "authenticate", after one refresh.
    ///
    /// `message` is the server's own `error` text, kept verbatim. It does not name the credential
    /// problem, but it does say *who* refused: the access gate answers "authentication required"
    /// before a route runs, while an auth route answers "authentication failed" itself. Dropping it
    /// is what made a renewal refused by the gate indistinguishable from a revoked refresh token.
    #[error("not authenticated (401{}) — run `nvrs login`, or the session expired", .message.as_deref().map(|m| format!(": {m}")).unwrap_or_default())]
    Unauthorized { message: Option<String> },

    /// HTTP 403 — authenticated but refused by policy. Carries the server's message, which names the
    /// action or the disabled feature.
    #[error("refused (403): {0}")]
    Forbidden(String),

    /// HTTP 429 — rate limited. `/api/auth/srp/begin` is 10/min and the whole app runs one worker,
    /// so this is real, not theoretical. `retry_after` is the server's `Retry-After` when present.
    #[error("rate limited (429){}", .retry_after.map(|d| format!("; retry after {}s", d.as_secs())).unwrap_or_default())]
    RateLimited { retry_after: Option<Duration> },

    /// Any other non-2xx with a decodable `{ok:false, error|errors}` body.
    #[error("request failed ({status}): {message}")]
    Api { status: u16, message: String },

    /// A non-2xx whose body was not the JSON envelope (an upstream 502 as HTML, say).
    #[error("request failed ({status}): {body}")]
    Http { status: u16, body: String },

    /// The response was 2xx but its body did not parse as JSON where JSON was expected.
    #[error("could not decode the response body: {0}")]
    Decode(String),

    /// The configured base URL, or a joined path, was not a valid URL.
    #[error("invalid URL: {0}")]
    Url(#[source] url::ParseError),

    /// A WebSocket connection (the interactive terminal) failed to open or was lost.
    #[error("websocket: {0}")]
    WebSocket(String),

    /// TLS setup for a WebSocket connection failed (a bad pinned cert, say).
    #[error("tls: {0}")]
    Tls(String),
}

impl Error {
    /// Whether a fresh access token might make this call succeed. Only a bare 401 qualifies: a 403
    /// is a policy decision that refreshing cannot change, and a 429 needs time, not a new token.
    pub fn is_unauthorized(&self) -> bool {
        matches!(self, Error::Unauthorized { .. })
    }

    /// The process exit code this error should map to. Distinct codes let a shell script branch on
    /// *why* a call failed without scraping stderr.
    pub fn exit_code(&self) -> i32 {
        match self {
            Error::Unauthorized { .. } => 77, // EX_NOPERM-ish: authenticate first
            Error::Forbidden(_) => 77,
            Error::RateLimited { .. } => 75, // EX_TEMPFAIL: try again later
            Error::Transport(_) | Error::Url(_) => 69, // EX_UNAVAILABLE
            _ => 1,
        }
    }
}
