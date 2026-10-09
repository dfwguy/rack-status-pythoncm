// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs spec` — browse the deployment's own OpenAPI contract.
//!
//! Lists every `/api/*` operation the deployment serves, with the docstring summary the backend
//! generates. This is how an operator discovers what `nvrs api` can call before typed verbs exist
//! for it, and fetching it also warms the cache the `api` path uses for its off-contract warnings.

use anyhow::Result;
use nvrs_openapi::{Loaded, Operation, DEFAULT_MAX_AGE};

use crate::api::client_for;
use crate::config::{Config, Profile};
use crate::output;

/// Fetch (or load from cache) the contract and print it: a filtered endpoint list, or full details.
pub async fn run(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    filter: Option<String>,
    details: bool,
    refresh: bool,
    format: output::Format,
) -> Result<()> {
    let base_url = url::Url::parse(&profile.base_url)?;
    let loaded = load_with_auth(cfg, profile_name, profile, &base_url, refresh).await?;

    if let Some(s) = output::structured(&loaded.raw, format) {
        // The server's exact document, for tooling (compact JSON to pipe into `jq`, or YAML).
        println!("{s}");
        return Ok(());
    }

    let needle = filter.as_deref().map(str::to_lowercase);
    let matches: Vec<(&str, &str, &Operation)> = loaded
        .spec
        .operations()
        .into_iter()
        .filter(|(method, path, op)| match &needle {
            None => true,
            Some(n) => {
                method.to_lowercase().contains(n)
                    || path.to_lowercase().contains(n)
                    || op.summary.to_lowercase().contains(n)
            }
        })
        .collect();

    let source = if loaded.from_cache { " (cached)" } else { "" };
    println!(
        "{} v{} — {} of {} operations{}",
        if loaded.spec.info.title.is_empty() {
            "Rack Status API"
        } else {
            &loaded.spec.info.title
        },
        loaded.spec.info.version,
        matches.len(),
        loaded.spec.operations().len(),
        source,
    );
    if matches.is_empty() {
        println!("no operations match {:?}", filter.unwrap_or_default());
        return Ok(());
    }
    println!();

    if details {
        print_details(&matches);
    } else {
        print_list(&matches);
    }
    Ok(())
}

fn print_list(matches: &[(&str, &str, &Operation)]) {
    let width = matches.iter().map(|(m, ..)| m.len()).max().unwrap_or(6);
    for (method, path, op) in matches {
        if op.summary.is_empty() {
            println!("{method:<width$}  {path}");
        } else {
            println!("{method:<width$}  {path}  —  {}", op.summary);
        }
    }
}

fn print_details(matches: &[(&str, &str, &Operation)]) {
    for (method, path, op) in matches {
        println!("{method} {path}");
        if !op.summary.is_empty() {
            println!("  {}", op.summary);
        }
        let params = &op.parameters;
        if !params.is_empty() {
            println!("  parameters:");
            for p in params {
                let req = if p.required { " (required)" } else { "" };
                println!("    {:<8} {}{}", p.location, p.name, req);
            }
        }
        if !op.tags.is_empty() {
            println!("  tags: {}", op.tags.join(", "));
        }
        println!();
    }
}

/// Load the spec, refreshing the stored access token once if the fetch is rejected with a 401. Cache
/// hits (the common case) need no auth and return immediately.
async fn load_with_auth(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    base_url: &url::Url,
    refresh: bool,
) -> Result<Loaded> {
    use nvrs_auth::TokenStore;

    let store = TokenStore::open_default()?;
    let env_token = std::env::var("NVRS_TOKEN").ok().filter(|s| !s.is_empty());
    let (token, stored) = crate::auth::resolve_access(&store, profile_name, env_token.as_deref())?;
    let refresh_token = if env_token.is_none() {
        stored.map(|t| t.refresh)
    } else {
        None
    };

    let client = client_for(cfg, profile, token)?;
    match nvrs_openapi::load(&client, base_url, refresh, DEFAULT_MAX_AGE).await {
        Ok(loaded) => Ok(loaded),
        Err(nvrs_openapi::OpenApiError::Client(nvrs_client::Error::Unauthorized { .. }))
            if refresh_token.is_some() =>
        {
            let new = crate::auth::refresh_and_store(
                cfg,
                profile,
                profile_name,
                &store,
                refresh_token.as_deref().unwrap(),
            )
            .await?;
            let client = client_for(cfg, profile, Some(new))?;
            Ok(nvrs_openapi::load(&client, base_url, refresh, DEFAULT_MAX_AGE).await?)
        }
        Err(e) => Err(e.into()),
    }
}
