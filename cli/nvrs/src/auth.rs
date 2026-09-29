// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs login` / `nvrs logout`, and the token plumbing the `api`/`whoami` paths share.
//!
//! The store account is the profile name, so each cluster's session is kept separate — logging in to
//! Boston never touches the Mayo session.

use std::io::{IsTerminal, Read};

use anyhow::{Context, Result};
use nvrs_auth::{TokenStore, Tokens};

use crate::api::client_for;
use crate::config::{Config, Profile};

/// The account key a profile's tokens are stored under.
pub fn account(profile_name: &str) -> &str {
    profile_name
}

/// Resolve the access token for a call: an explicit `--token`/`$NVRS_TOKEN` wins (the bridge for
/// scripts), otherwise the stored session for this profile.
pub fn resolve_access(
    store: &TokenStore,
    profile_name: &str,
    explicit: Option<&str>,
) -> Result<(Option<String>, Option<Tokens>)> {
    let stored = store.get(account(profile_name))?;
    let token = explicit
        .map(str::to_owned)
        .or_else(|| stored.as_ref().map(|t| t.access.clone()));
    Ok((token, stored))
}

/// Exchange the refresh token for a fresh access token and persist it. Used on a 401 mid-call.
pub async fn refresh_and_store(
    cfg: &Config,
    profile: &Profile,
    profile_name: &str,
    store: &TokenStore,
    refresh_token: &str,
) -> Result<String> {
    // A client with no bearer: refresh authenticates via the refresh cookie, not a header.
    let anon = client_for(cfg, profile, None)?;
    let new_access = nvrs_auth::refresh(&anon, refresh_token).await?;
    store.set_access(account(profile_name), new_access.clone())?;
    Ok(new_access)
}

/// `nvrs login`: run the SRP exchange and store the resulting session.
pub async fn login(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    login_arg: Option<String>,
    password_stdin: bool,
) -> Result<()> {
    let login_name = match login_arg {
        Some(l) => l,
        None => prompt_line(&format!("Login for {profile_name:?}: "))?,
    };
    let password = read_password(password_stdin)?;
    if password.is_empty() {
        anyhow::bail!("password must not be empty");
    }

    let client = client_for(cfg, profile, None)?;
    let outcome = nvrs_auth::login(&client, &login_name, &password).await?;

    let store = TokenStore::open_default()?;
    store.set(account(profile_name), outcome.tokens)?;

    let who = if outcome.display_name.is_empty() {
        outcome.login.clone()
    } else {
        outcome.display_name.clone()
    };
    println!(
        "logged in to {profile_name:?} as {who} ({})",
        outcome.principal
    );
    if outcome.must_change_password {
        // FR-10a: the bootstrap admin must change its password. The change exchange is a later
        // phase, so we surface it loudly rather than pretending the session is fully usable.
        eprintln!(
            "note: this account must change its password before it can do much. \
             `nvrs passwd` is not implemented yet — use the web UI for now."
        );
    }
    Ok(())
}

/// `nvrs logout`: revoke the session server-side and forget it locally.
pub async fn logout(cfg: &Config, profile_name: &str, profile: &Profile) -> Result<()> {
    let store = TokenStore::open_default()?;
    match store.get(account(profile_name))? {
        None => {
            println!("not logged in to {profile_name:?}");
            Ok(())
        }
        Some(tokens) => {
            let client = client_for(cfg, profile, None)?;
            // Best-effort server-side revocation; forget locally regardless of the outcome, so a
            // logout always clears the credential from this machine.
            let revoked = nvrs_auth::logout(&client, &tokens).await;
            store.remove(account(profile_name))?;
            match revoked {
                Ok(()) => println!("logged out of {profile_name:?}"),
                Err(e) => {
                    println!("cleared local session for {profile_name:?} (server logout: {e})")
                }
            }
            Ok(())
        }
    }
}

fn prompt_line(prompt: &str) -> Result<String> {
    use std::io::Write;
    print!("{prompt}");
    std::io::stdout().flush().ok();
    let mut line = String::new();
    std::io::stdin()
        .read_line(&mut line)
        .context("reading input")?;
    Ok(line.trim().to_string())
}

/// Read the password without echo from the TTY, or a single line from stdin when `--password-stdin`
/// is set or stdin is not a terminal (CI, a pipe).
fn read_password(password_stdin: bool) -> Result<String> {
    if password_stdin || !std::io::stdin().is_terminal() {
        let mut buf = String::new();
        std::io::stdin()
            .read_to_string(&mut buf)
            .context("reading password from stdin")?;
        Ok(buf.trim_end_matches(['\n', '\r']).to_string())
    } else {
        rpassword::prompt_password("Password: ").context("reading password")
    }
}
