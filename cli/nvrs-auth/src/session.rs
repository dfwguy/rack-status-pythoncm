// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! The login, refresh and logout exchanges, driven over an [`nvrs_client::Client`].
//!
//! Mirrors `srpLogin` in `frontend/src/lib/srpClient.ts`: two legs against `/api/auth/srp/*`, then
//! **verify the server's proof** before trusting the session. The tokens themselves arrive as
//! `Set-Cookie` (they are never in the JSON body), so we lift them out of the captured cookies.

use nvrs_client::{Client, Error as ClientError, Method};
use serde_json::{json, Value};

use crate::srp;
use crate::store::{token_from_cookies, Tokens};
use crate::AuthError;

const ACCESS_COOKIE: &str = "nvis_access";
const REFRESH_COOKIE: &str = "nvis_refresh";

/// What a successful login yields: the tokens to store, and the identity the server confirmed.
#[derive(Debug, Clone)]
pub struct LoginOutcome {
    pub tokens: Tokens,
    pub login: String,
    pub display_name: String,
    pub principal: String,
    /// The bootstrap admin logs in with this set; the caller must force a password change next.
    pub must_change_password: bool,
}

/// Run both legs of the SRP login. `client` must be built for the target deployment with no bearer
/// token (this *obtains* one). The password is consumed by the handshake and never sent.
pub async fn login(
    client: &Client,
    login: &str,
    password: &str,
) -> Result<LoginOutcome, AuthError> {
    let canonical = srp::canonical_login(login);
    let handshake = srp::start(&canonical, password);

    // Leg 1: send A, receive salt + B + a challenge token. An unknown login gets an
    // indistinguishable decoy here (still a 200), so a failure surfaces only at leg 2.
    let begin = client
        .send_json_with_cookies(
            Method::POST,
            "/api/auth/srp/begin",
            Some(&json!({ "login": canonical, "A": handshake.a_hex() })),
            &[],
        )
        .await
        .map_err(map_login_error)?;

    let salt = str_field(&begin.value, "salt")?;
    let b = str_field(&begin.value, "B")?;
    let token = str_field(&begin.value, "token")?;

    let proof = handshake.proof(&salt, &b)?;

    // Leg 2: send the proof; on success the server sets the session cookies and returns its own
    // proof HAMK plus the identity.
    let finish = client
        .send_json_with_cookies(
            Method::POST,
            "/api/auth/srp/finish",
            Some(&json!({ "token": token, "M": proof.m_hex })),
            &[],
        )
        .await
        .map_err(map_login_error)?;

    // Verify the server authenticated itself before trusting anything it said or set.
    let hamk = str_field(&finish.value, "HAMK")?;
    if !proof.verify_server(&hamk) {
        return Err(AuthError::ServerAuthFailed);
    }

    let access = token_from_cookies(&finish.set_cookies, ACCESS_COOKIE)
        .ok_or_else(|| AuthError::Protocol("login succeeded but set no access cookie".into()))?;
    let refresh = token_from_cookies(&finish.set_cookies, REFRESH_COOKIE)
        .ok_or_else(|| AuthError::Protocol("login succeeded but set no refresh cookie".into()))?;

    let user = finish.value.get("user").cloned().unwrap_or(Value::Null);
    Ok(LoginOutcome {
        tokens: Tokens { access, refresh },
        login: user
            .get("login")
            .and_then(Value::as_str)
            .unwrap_or(&canonical)
            .to_string(),
        display_name: user
            .get("display_name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        principal: user
            .get("principal")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        must_change_password: finish
            .value
            .get("must_change_password")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

/// Change the signed-in user's password. `client` must carry the session's bearer token — the
/// routes take the login from the token, not the request, so a caller can only change *their own*
/// password (and being logged in is not enough: leg 1 re-proves the current password, defeating a
/// stolen session).
///
/// Mirrors the login exchange deliberately — same two legs, same SRP — with one addition: leg 2
/// also carries the **new** salt and verifier, derived locally, so the new password never reaches
/// the server any more than the old one did. On success the server revokes **every** session,
/// including this one; the caller must log in again, which is why this returns `()` rather than a
/// fresh session.
pub async fn change_password(
    client: &Client,
    login: &str,
    current_password: &str,
    new_password: &str,
) -> Result<(), AuthError> {
    let canonical = srp::canonical_login(login);
    let handshake = srp::start(&canonical, current_password);

    // Leg 1: prove the current password. A wrong one is answered uniformly (a 401) at leg 2, just
    // like a login, so nothing here distinguishes it from a healthy begin.
    let begin = client
        .send_json(
            Method::POST,
            "/api/auth/password/begin",
            &[],
            Some(&json!({ "A": handshake.a_hex() })),
        )
        .await
        .map_err(map_login_error)?;

    let salt = str_field(&begin, "salt")?;
    let b = str_field(&begin, "B")?;
    let token = str_field(&begin, "token")?;

    let proof = handshake.proof(&salt, &b)?;

    // Derive the new credential locally; only salt and verifier are sent.
    let (new_salt, new_verifier) = srp::enroll(&canonical, new_password);

    // Leg 2: the current-password proof and the new verifier together.
    let finish = client
        .send_json(
            Method::POST,
            "/api/auth/password/finish",
            &[],
            Some(&json!({
                "token": token,
                "M": proof.m_hex,
                "new_salt": new_salt,
                "new_verifier": new_verifier,
            })),
        )
        .await
        .map_err(map_login_error)?;

    if finish.get("ok").and_then(Value::as_bool) != Some(true) {
        let msg = finish
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("the server rejected the password change");
        return Err(AuthError::Protocol(msg.to_string()));
    }
    Ok(())
}

/// Renew the access token using the refresh token (sent as a cookie, which is what the endpoint
/// reads). Returns the fresh access token.
///
/// A refusal is reported with the server's own words and the refresh token's own expiry, never as a
/// bare "session expired": a refresh token that has not expired and is refused anyway is a
/// server-side decision the operator needs to see, not a timeout to shrug at.
pub async fn refresh(client: &Client, refresh_token: &str) -> Result<String, AuthError> {
    let resp = client
        .send_json_with_cookies(
            Method::POST,
            "/api/auth/refresh",
            None,
            &[(REFRESH_COOKIE, refresh_token)],
        )
        .await
        .map_err(|e| match e {
            ClientError::Unauthorized { message } => AuthError::RefreshRefused {
                server: message,
                expires_at: crate::store::unverified_expiry(refresh_token),
            },
            other => AuthError::Client(other),
        })?;

    token_from_cookies(&resp.set_cookies, ACCESS_COOKIE)
        .ok_or_else(|| AuthError::Protocol("refresh set no access cookie".into()))
}

/// End the session server-side, revoking the presented tokens by `jti`. Sends both tokens as cookies
/// (the route reads them).
///
/// A 401 means the server revoked nothing. Usually that is because both tokens were already dead,
/// but against a server that gates logout on a live access token it also happens with a refresh
/// token that is still valid — so it is returned as [`AuthError::LogoutRefused`], not swallowed.
pub async fn logout(client: &Client, tokens: &Tokens) -> Result<(), AuthError> {
    match client
        .send_json_with_cookies(
            Method::POST,
            "/api/auth/logout",
            None,
            &[
                (ACCESS_COOKIE, &tokens.access),
                (REFRESH_COOKIE, &tokens.refresh),
            ],
        )
        .await
    {
        Ok(_) => Ok(()),
        Err(ClientError::Unauthorized { message }) => {
            Err(AuthError::LogoutRefused { server: message })
        }
        Err(other) => Err(AuthError::Client(other)),
    }
}

/// A 401 anywhere in the login exchange is the server's uniform "authentication failed" — it will
/// not (and must not) say whether the login was unknown or the password wrong. Everything else is a
/// genuine transport/server fault the operator should see verbatim.
fn map_login_error(e: ClientError) -> AuthError {
    match e {
        ClientError::Unauthorized { .. } => AuthError::InvalidCredentials,
        other => AuthError::Client(other),
    }
}

fn str_field(value: &Value, key: &str) -> Result<String, AuthError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| AuthError::Protocol(format!("response missing string field {key:?}")))
}
