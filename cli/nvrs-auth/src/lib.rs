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

pub use session::{change_password, login, logout, refresh, LoginOutcome};
pub use srp::{canonical_login, SrpError};
pub use store::unverified_expiry;
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

    /// The server refused to renew the session.
    ///
    /// This used to be a bare "session expired", which was a guess: the CLI knew only that a 401
    /// came back. On Nscale NC that guess was wrong for a week of refresh tokens — the server's
    /// access gate refused renewal before the route ever read the refresh token, and nothing on
    /// this side could say so. So the error now carries what is actually known: the server's own
    /// words, and whether the refresh token had expired by its own `exp` claim. A refusal of a
    /// token that has *not* expired is a server-side decision worth reporting, not a timeout.
    #[error("{}", refresh_refused_message(server.as_deref(), *expires_at))]
    RefreshRefused {
        /// The server's `error` text, verbatim, when the response carried one.
        server: Option<String>,
        /// The refresh token's own `exp`, read without verification (diagnosis only).
        expires_at: Option<std::time::SystemTime>,
    },

    /// The server refused the logout, so it did not revoke the session's tokens.
    ///
    /// Previously treated as success ("already gone"), which is right only for one of the two
    /// servers this CLI meets. A server that accepts the refresh cookie as logout's credential
    /// refuses only when neither token is valid, so nothing was left to revoke. A server that gates
    /// logout on a live access token refuses once the access token lapses — with the refresh token
    /// still valid and now not revoked. The CLI cannot tell which, so it says what it knows.
    #[error(
        "the server refused the logout{} — nothing was revoked server-side; if the refresh token \
         was still valid it stays usable until it expires",
        .server.as_deref().map(|m| format!(" ({m})")).unwrap_or_default()
    )]
    LogoutRefused {
        /// The server's `error` text, verbatim, when the response carried one.
        server: Option<String>,
    },

    /// The server's response did not have the shape the protocol requires.
    #[error("unexpected server response: {0}")]
    Protocol(String),

    /// The on-disk token store could not be read or written.
    #[error("token store: {0}")]
    Store(String),
}

fn refresh_refused_message(
    server: Option<&str>,
    expires_at: Option<std::time::SystemTime>,
) -> String {
    let said = server.map(|m| format!(" ({m})")).unwrap_or_default();
    let token = match expires_at.map(|t| t.duration_since(std::time::SystemTime::now())) {
        Some(Ok(left)) => format!(
            "; the refresh token had not expired ({} left by its own claims), so the server \
             rejected it for another reason — revoked by a logout or password change, a changed \
             signing key, or a server that would not read it",
            humanize(left)
        ),
        Some(Err(_)) => "; the refresh token has expired".to_owned(),
        None => String::new(),
    };
    format!("the server refused to renew the session{said}{token} — run `nvrs login` again")
}

fn humanize(d: std::time::Duration) -> String {
    let s = d.as_secs();
    match (s / 86_400, (s % 86_400) / 3_600, (s % 3_600) / 60) {
        (0, 0, m) => format!("{m}m"),
        (0, h, m) => format!("{h}h{m:02}m"),
        (days, h, _) => format!("{days}d{h}h"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, SystemTime};

    #[test]
    fn a_refused_live_token_says_so_and_carries_the_server_text() {
        let e = AuthError::RefreshRefused {
            server: Some("authentication required".into()),
            expires_at: Some(SystemTime::now() + Duration::from_secs(6 * 86_400 + 3 * 3_600 + 30)),
        };
        let msg = e.to_string();
        assert!(msg.contains("(authentication required)"), "{msg}");
        assert!(msg.contains("had not expired (6d3h left"), "{msg}");
        assert!(msg.contains("nvrs login"), "{msg}");
    }

    #[test]
    fn a_refused_expired_token_says_it_expired() {
        let e = AuthError::RefreshRefused {
            server: Some("authentication failed".into()),
            expires_at: Some(SystemTime::now() - Duration::from_secs(60)),
        };
        assert!(
            e.to_string().contains("the refresh token has expired"),
            "{e}"
        );
    }

    #[test]
    fn an_unreadable_token_claims_nothing_about_expiry() {
        let e = AuthError::RefreshRefused {
            server: None,
            expires_at: None,
        };
        let msg = e.to_string();
        assert!(!msg.contains("expired"), "{msg}");
        assert!(
            msg.starts_with("the server refused to renew the session —"),
            "{msg}"
        );
    }
}
