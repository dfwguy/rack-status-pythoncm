// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs api` — the generic passthrough, and the `whoami` probe.
//!
//! The passthrough exists so the CLI reaches every one of the ~221 `/api/*` operations from day
//! one; typed verbs are curated sugar layered on later. Request bodies are un-schema'd on the server
//! (`backend/openapi.py`), so they are supplied as raw JSON (`--body`) or flat string fields
//! (`--field k=v`) rather than typed flags.
//!
//! Auth in Phase 0 is a bridge: a bearer token from `--token`/`$NVRS_TOKEN`. `nvrs login` (Phase 1)
//! will supply it from the keyring automatically.

use std::time::Duration;

use anyhow::{bail, Context, Result};
use clap::Args;
use nvrs_auth::TokenStore;
use nvrs_client::Method;
use serde_json::Value;

use crate::config::{Config, Profile};
use crate::output;

#[derive(Args, Debug)]
pub struct ApiArgs {
    /// HTTP method: GET, POST, PUT, PATCH, DELETE.
    pub method: String,

    /// Endpoint path, e.g. /api/status or /api/rack-locks/p17-r01.
    pub path: String,

    /// Query parameter, repeatable: --query rack=p17-r01 --query verbose=1
    #[arg(long = "query", short = 'q', value_name = "KEY=VALUE")]
    pub query: Vec<String>,

    /// String body field, repeatable, builds a JSON object: --field target=n04 --field force=true.
    /// Mutually exclusive with --body. Values are sent as JSON strings.
    #[arg(long = "field", short = 'f', value_name = "KEY=VALUE")]
    pub field: Vec<String>,

    /// Raw JSON request body: inline (--body '{"k":1}') or from a file (--body @params.json).
    #[arg(long)]
    pub body: Option<String>,

    /// Stream the response body to stdout as it arrives (for -stream endpoints).
    #[arg(long)]
    pub stream: bool,

    /// Bearer access token. Prefer $NVRS_TOKEN. Phase 1's `nvrs login` removes the need for this.
    #[arg(long, env = "NVRS_TOKEN", hide_env_values = true)]
    pub token: Option<String>,

    /// Skip the best-effort check of the path/method against the cached API contract.
    #[arg(long)]
    pub no_validate: bool,

    /// Give up on the whole request after this many seconds. Raise it for endpoints that compute on
    /// demand: /api/node-stages?wait=1 runs the bring-up fleet status check when its cache has
    /// expired, which takes over two minutes on a ~2,300-node cluster.
    #[arg(long, value_name = "SECS", default_value_t = DEFAULT_TIMEOUT_SECS,
          value_parser = clap::value_parser!(u64).range(1..))]
    pub timeout: u64,
}

/// Request timeout for every call that does not ask for another (`nvrs api --timeout`).
pub const DEFAULT_TIMEOUT_SECS: u64 = 120;

/// Build a transport client for a resolved profile, with an optional bearer token.
pub fn client_for(
    cfg: &Config,
    profile: &Profile,
    token: Option<String>,
) -> Result<nvrs_client::Client> {
    client_with_timeout(
        cfg,
        profile,
        token,
        Duration::from_secs(DEFAULT_TIMEOUT_SECS),
    )
}

/// [`client_for`] with an explicit request timeout.
pub fn client_with_timeout(
    cfg: &Config,
    profile: &Profile,
    token: Option<String>,
    timeout: Duration,
) -> Result<nvrs_client::Client> {
    let base_url = url::Url::parse(&profile.base_url)
        .with_context(|| format!("profile base_url {:?} is not a valid URL", profile.base_url))?;
    let client_cfg = nvrs_client::ClientConfig {
        base_url,
        tls: profile.tls.to_trust()?,
        operator: profile.operator.clone(),
        client_id: cfg.client_id.clone(),
        bearer: token,
        timeout,
        max_retries: 3,
    };
    Ok(nvrs_client::Client::new(client_cfg)?)
}

/// Warn (to stderr) if a call does not match the *cached* API contract. Best-effort by design:
/// cache-only so it never fetches (no latency, no auth needed), never blocks the call (the cache can
/// be stale and the contract is a map, not a straitjacket), and silent when nothing is cached.
/// Populate or refresh the cache with `nvrs spec`.
fn warn_if_off_contract(profile: &Profile, method: &str, path: &str) {
    let Ok(base_url) = url::Url::parse(&profile.base_url) else {
        return;
    };
    let Some(loaded) = nvrs_openapi::load_cached(&base_url, nvrs_openapi::DEFAULT_MAX_AGE) else {
        return;
    };
    match loaded.spec.check_call(method, path) {
        nvrs_openapi::CallCheck::Ok => {}
        nvrs_openapi::CallCheck::UnknownPath => eprintln!(
            "warning: {path} is not in the cached API contract \
             (a typo, or the cache is stale — try `nvrs spec --refresh`); sending anyway"
        ),
        nvrs_openapi::CallCheck::MethodNotAllowed(methods) => eprintln!(
            "warning: {method} is not defined for {path}; the contract allows {}; sending anyway",
            methods.join(", ")
        ),
    }
}

/// Make one authenticated JSON call for a profile, refreshing the access token once on a 401. This
/// is the shared spine every typed verb sits on, so the auth/refresh dance lives in exactly one
/// place. `explicit_token` is an out-of-band `$NVRS_TOKEN`/`--token`; `None` uses the stored session.
#[allow(clippy::too_many_arguments)]
pub async fn call_json(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    method: Method,
    path: &str,
    query: &[(String, String)],
    body: Option<&Value>,
    explicit_token: Option<&str>,
) -> Result<Value> {
    let store = TokenStore::open_default()?;
    let (token, stored) = crate::auth::resolve_access(&store, profile_name, explicit_token)?;
    let refresh = if explicit_token.is_none() {
        stored.map(|t| t.refresh)
    } else {
        None
    };
    let client = client_for(cfg, profile, token)?;
    match client.send_json(method.clone(), path, query, body).await {
        Err(nvrs_client::Error::Unauthorized { .. }) if refresh.is_some() => {
            let new = crate::auth::refresh_and_store(
                cfg,
                profile,
                profile_name,
                &store,
                refresh.as_deref().unwrap(),
            )
            .await?;
            Ok(client_for(cfg, profile, Some(new))?
                .send_json(method, path, query, body)
                .await?)
        }
        other => Ok(other?),
    }
}

/// Like [`call_json`] but for a streaming endpoint: pump the chunked body straight to stdout,
/// refreshing the token once on a 401. Shared by `nvrs api --stream` and the typed `logs tail`.
#[allow(clippy::too_many_arguments)]
pub async fn stream_call(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    method: Method,
    path: &str,
    query: &[(String, String)],
    body: Option<&Value>,
    explicit_token: Option<&str>,
) -> Result<()> {
    let store = TokenStore::open_default()?;
    let (token, stored) = crate::auth::resolve_access(&store, profile_name, explicit_token)?;
    let refresh = if explicit_token.is_none() {
        stored.map(|t| t.refresh)
    } else {
        None
    };
    let client = client_for(cfg, profile, token)?;
    let resp = match client.send_raw(method.clone(), path, query, body).await {
        Err(nvrs_client::Error::Unauthorized { .. }) if refresh.is_some() => {
            let new = crate::auth::refresh_and_store(
                cfg,
                profile,
                profile_name,
                &store,
                refresh.as_deref().unwrap(),
            )
            .await?;
            client_for(cfg, profile, Some(new))?
                .send_raw(method, path, query, body)
                .await?
        }
        other => other?,
    };
    stream_to_stdout(resp).await
}

pub async fn run(
    args: ApiArgs,
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    format: output::Format,
) -> Result<()> {
    let method = parse_method(&args.method)?;
    let query = parse_kv(&args.query).context("parsing --query")?;
    let body = build_body(&args.field, args.body.as_deref())?;

    if !args.no_validate {
        warn_if_off_contract(profile, method.as_str(), &args.path);
    }

    let store = TokenStore::open_default()?;
    let (token, stored) = crate::auth::resolve_access(&store, profile_name, args.token.as_deref())?;
    // Only a *stored* session can be refreshed; an explicit --token/$NVRS_TOKEN is opaque to us.
    let refresh = if args.token.is_none() {
        stored.map(|t| t.refresh)
    } else {
        None
    };
    let timeout = Duration::from_secs(args.timeout);
    let client = client_with_timeout(cfg, profile, token, timeout)?;

    if args.stream {
        let resp = match client
            .send_raw(method.clone(), &args.path, &query, body.as_ref())
            .await
        {
            Err(nvrs_client::Error::Unauthorized { .. }) if refresh.is_some() => {
                let new = crate::auth::refresh_and_store(
                    cfg,
                    profile,
                    profile_name,
                    &store,
                    refresh.as_deref().unwrap(),
                )
                .await?;
                client_with_timeout(cfg, profile, Some(new), timeout)?
                    .send_raw(method, &args.path, &query, body.as_ref())
                    .await?
            }
            other => other?,
        };
        stream_to_stdout(resp).await?;
        return Ok(());
    }

    let value = match client
        .send_json(method.clone(), &args.path, &query, body.as_ref())
        .await
    {
        Err(nvrs_client::Error::Unauthorized { .. }) if refresh.is_some() => {
            let new = crate::auth::refresh_and_store(
                cfg,
                profile,
                profile_name,
                &store,
                refresh.as_deref().unwrap(),
            )
            .await?;
            client_with_timeout(cfg, profile, Some(new), timeout)?
                .send_json(method, &args.path, &query, body.as_ref())
                .await?
        }
        other => other?,
    };
    output::print_value(&value, format);
    Ok(())
}

/// `whoami` — is there a live session for this profile? Probes `/api/auth-check` with the stored
/// token, refreshing once if the access token has expired.
pub async fn whoami(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    format: output::Format,
) -> Result<()> {
    let store = TokenStore::open_default()?;
    let env_token = std::env::var("NVRS_TOKEN").ok().filter(|s| !s.is_empty());
    let (token, stored) = crate::auth::resolve_access(&store, profile_name, env_token.as_deref())?;
    let refresh = if env_token.is_none() {
        stored.map(|t| t.refresh)
    } else {
        None
    };
    let client = client_for(cfg, profile, token)?;

    let mut result = client
        .send_json(Method::GET, "/api/auth-check", &[], None)
        .await;
    // Why renewal failed, when it did. This used to be an `if let Ok(..)`, which threw the reason
    // away and printed a plain "not authenticated" — so a 7-day refresh token refused after one
    // hour looked exactly like a session that had run out. The reason is the whole diagnosis.
    let mut refresh_failure: Option<anyhow::Error> = None;
    if matches!(result, Err(nvrs_client::Error::Unauthorized { .. })) {
        if let Some(refresh_token) = &refresh {
            match crate::auth::refresh_and_store(cfg, profile, profile_name, &store, refresh_token)
                .await
            {
                Ok(new) => {
                    result = client_for(cfg, profile, Some(new))?
                        .send_json(Method::GET, "/api/auth-check", &[], None)
                        .await;
                }
                Err(e) => refresh_failure = Some(e),
            }
        }
    }

    match result {
        Ok(value) => {
            if let Some(s) = output::structured(&value, format) {
                println!("{s}");
            } else {
                let user = value.get("user").and_then(Value::as_str).unwrap_or("?");
                println!("profile {profile_name:?} at {}", client.base_url());
                println!("authenticated as {user}");
                if value.get("must_change_password").and_then(Value::as_bool) == Some(true) {
                    println!("note: password change required");
                }
            }
            Ok(())
        }
        Err(nvrs_client::Error::Unauthorized { .. }) => {
            // The normal "logged out" answer, not an error worth a backtrace — but when a stored
            // session failed to renew, say why, because that is not the normal answer.
            match refresh_failure {
                Some(e) => println!(
                    "profile {profile_name:?} at {}: not authenticated — renewing the stored \
                     session failed: {e:#}",
                    client.base_url()
                ),
                None => println!(
                    "profile {profile_name:?} at {}: not authenticated — run `nvrs login`",
                    client.base_url()
                ),
            }
            Ok(())
        }
        Err(e) => Err(e.into()),
    }
}

fn parse_method(s: &str) -> Result<Method> {
    Method::from_bytes(s.to_ascii_uppercase().as_bytes())
        .with_context(|| format!("invalid HTTP method {s:?}"))
}

/// Split `KEY=VALUE` pairs, erroring on a missing `=` so a typo is caught rather than silently
/// dropped.
fn parse_kv(items: &[String]) -> Result<Vec<(String, String)>> {
    items
        .iter()
        .map(|item| {
            item.split_once('=')
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .with_context(|| format!("expected KEY=VALUE, got {item:?}"))
        })
        .collect()
}

/// Build the JSON body from either `--field` pairs or a raw `--body`, refusing both at once.
pub(crate) fn build_body(fields: &[String], body: Option<&str>) -> Result<Option<Value>> {
    match (fields.is_empty(), body) {
        (true, None) => Ok(None),
        (false, Some(_)) => bail!("use either --field or --body, not both"),
        (false, None) => {
            let pairs = parse_kv(fields).context("parsing --field")?;
            let mut obj = serde_json::Map::new();
            for (k, v) in pairs {
                obj.insert(k, Value::String(v));
            }
            Ok(Some(Value::Object(obj)))
        }
        (true, Some(raw)) => {
            let text = if let Some(path) = raw.strip_prefix('@') {
                std::fs::read_to_string(path)
                    .with_context(|| format!("reading body file {path}"))?
            } else {
                raw.to_string()
            };
            let value: Value = serde_json::from_str(&text).context("--body is not valid JSON")?;
            Ok(Some(value))
        }
    }
}

/// Copy a streaming response body to stdout chunk by chunk, so a long-running `-stream` endpoint
/// prints live rather than buffering to the end.
async fn stream_to_stdout(mut resp: nvrs_client::Response) -> Result<()> {
    use std::io::Write;
    let mut stdout = std::io::stdout().lock();
    while let Some(chunk) = resp.chunk().await.context("reading stream")? {
        stdout.write_all(&chunk).context("writing to stdout")?;
        stdout.flush().ok();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[derive(Parser)]
    struct Harness {
        #[command(flatten)]
        api: ApiArgs,
    }

    fn parse_api(extra: &[&str]) -> Result<ApiArgs, clap::Error> {
        let mut argv = vec!["nvrs-api", "GET", "/api/node-stages"];
        argv.extend_from_slice(extra);
        Harness::try_parse_from(argv).map(|h| h.api)
    }

    #[test]
    fn timeout_defaults_to_two_minutes_and_can_be_raised() {
        assert_eq!(parse_api(&[]).unwrap().timeout, DEFAULT_TIMEOUT_SECS);
        assert_eq!(DEFAULT_TIMEOUT_SECS, 120);
        assert_eq!(parse_api(&["--timeout", "300"]).unwrap().timeout, 300);
    }

    #[test]
    fn timeout_rejects_zero_and_garbage() {
        assert!(parse_api(&["--timeout", "0"]).is_err());
        assert!(parse_api(&["--timeout", "5m"]).is_err());
    }

    #[test]
    fn method_parsing_is_case_insensitive() {
        assert_eq!(parse_method("get").unwrap(), Method::GET);
        assert_eq!(parse_method("Post").unwrap(), Method::POST);
        // A space is not a valid HTTP method token char, so this is rejected. (`-`/`!` are valid
        // token chars, so they would *not* be — the guard is against genuinely malformed input.)
        assert!(parse_method("GET POST").is_err());
    }

    #[test]
    fn kv_requires_equals() {
        assert_eq!(
            parse_kv(&["rack=p17".into()]).unwrap(),
            vec![("rack".to_string(), "p17".to_string())]
        );
        assert!(parse_kv(&["justkey".into()]).is_err());
    }

    #[test]
    fn value_may_contain_equals() {
        // split_once, not split, so a base64-ish value with '=' survives intact.
        assert_eq!(
            parse_kv(&["token=ab==".into()]).unwrap(),
            vec![("token".to_string(), "ab==".to_string())]
        );
    }

    #[test]
    fn fields_build_a_json_object() {
        let body = build_body(&["target=n04".into(), "force=true".into()], None)
            .unwrap()
            .unwrap();
        assert_eq!(body["target"], Value::String("n04".into()));
        // Fields are strings; a caller wanting a real bool uses --body.
        assert_eq!(body["force"], Value::String("true".into()));
    }

    #[test]
    fn field_and_body_together_is_rejected() {
        assert!(build_body(&["a=b".into()], Some("{}")).is_err());
    }

    #[test]
    fn inline_body_must_be_json() {
        assert!(build_body(&[], Some("not json")).is_err());
        assert_eq!(
            build_body(&[], Some(r#"{"n":1}"#)).unwrap().unwrap()["n"],
            Value::from(1)
        );
    }

    #[test]
    fn body_at_file_is_read_from_disk() {
        use std::io::Write;
        // `--body @path` reads the file; a real run passes a params file this way.
        let mut f = tempfile::NamedTempFile::new().unwrap();
        write!(f, r#"{{"target":"n04","force":true}}"#).unwrap();
        let arg = format!("@{}", f.path().display());
        let body = build_body(&[], Some(&arg)).unwrap().unwrap();
        assert_eq!(body["target"], Value::String("n04".into()));
        // From a file, JSON types are preserved — a real bool, unlike --field which sends strings.
        assert_eq!(body["force"], Value::Bool(true));
    }

    #[test]
    fn body_at_missing_file_errors() {
        assert!(build_body(&[], Some("@/no/such/params.json")).is_err());
    }
}
