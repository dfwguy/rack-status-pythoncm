// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs-openapi` — the CLI's view of the deployment's own contract.
//!
//! The backend serves a live OpenAPI 3.1 document at `GET /api/openapi.json`, generated from the
//! route table on every request so it cannot drift from the code (`backend/openapi.py`). This crate
//! fetches and caches it, models the parts a CLI needs (paths, methods, parameters, the docstring
//! summary), and answers two questions: "what can I call?" (`nvrs spec`) and "does this call name a
//! real endpoint?" (a best-effort check before `nvrs api` sends).
//!
//! The document is a *map of the surface*, not a schema for every payload: request bodies are
//! free-form and query-parameter detection is derived from the handler source, so it is authoritative
//! for paths/methods and advisory for bodies. This crate treats it exactly that way — path/method
//! checks are trusted; it does not pretend to validate a body it was never given a schema for. The
//! document covers `/api/*` only (not `/oidc/*`, `/.well-known/*`, or the `/ws/terminal` WebSocket).

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, SystemTime};

use serde::Deserialize;
use serde_json::Value;
use url::Url;

/// The path the spec is served from, behind the normal auth gate.
pub const SPEC_PATH: &str = "/api/openapi.json";

/// How long a cached spec is considered fresh before a plain `load` re-fetches it.
pub const DEFAULT_MAX_AGE: Duration = Duration::from_secs(24 * 60 * 60);

/// Anything the discovery layer can fail with.
#[derive(Debug, thiserror::Error)]
pub enum OpenApiError {
    #[error(transparent)]
    Client(#[from] nvrs_client::Error),
    #[error("could not parse the OpenAPI document: {0}")]
    Parse(String),
    #[error("spec cache: {0}")]
    Cache(String),
}

/// The subset of the OpenAPI document the CLI uses. Unknown fields are ignored, so the server can
/// add to the document without breaking us.
#[derive(Debug, Default, Deserialize)]
pub struct Spec {
    #[serde(default)]
    pub openapi: String,
    #[serde(default)]
    pub info: Info,
    #[serde(default)]
    pub paths: BTreeMap<String, PathItem>,
}

#[derive(Debug, Default, Deserialize)]
pub struct Info {
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub version: String,
}

/// One path's operations. OpenAPI keys these by lowercase HTTP method.
#[derive(Debug, Default, Deserialize)]
pub struct PathItem {
    #[serde(default)]
    pub get: Option<Operation>,
    #[serde(default)]
    pub post: Option<Operation>,
    #[serde(default)]
    pub put: Option<Operation>,
    #[serde(default)]
    pub patch: Option<Operation>,
    #[serde(default)]
    pub delete: Option<Operation>,
}

impl PathItem {
    /// `(METHOD, operation)` for each verb this path defines, method upper-cased for display.
    pub fn operations(&self) -> Vec<(&'static str, &Operation)> {
        let mut out = Vec::new();
        for (name, op) in [
            ("GET", &self.get),
            ("POST", &self.post),
            ("PUT", &self.put),
            ("PATCH", &self.patch),
            ("DELETE", &self.delete),
        ] {
            if let Some(op) = op {
                out.push((name, op));
            }
        }
        out
    }

    fn method_names(&self) -> Vec<String> {
        self.operations()
            .iter()
            .map(|(m, _)| m.to_string())
            .collect()
    }
}

#[derive(Debug, Default, Deserialize)]
pub struct Operation {
    #[serde(default)]
    pub summary: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub parameters: Vec<Parameter>,
    #[serde(default)]
    pub tags: Vec<String>,
}

impl Operation {
    /// The query parameters this operation reads (per the handler source).
    pub fn query_params(&self) -> Vec<&Parameter> {
        self.parameters
            .iter()
            .filter(|p| p.location == "query")
            .collect()
    }
}

#[derive(Debug, Deserialize)]
pub struct Parameter {
    pub name: String,
    #[serde(rename = "in")]
    pub location: String,
    #[serde(default)]
    pub required: bool,
}

/// The result of checking a `(method, path)` against the contract.
#[derive(Debug, PartialEq, Eq)]
pub enum CallCheck {
    /// The path exists and defines this method.
    Ok,
    /// No path in the contract matches (after template matching). A typo, or a stale cache.
    UnknownPath,
    /// The path exists but not for this method; carries the methods it does define.
    MethodNotAllowed(Vec<String>),
}

impl Spec {
    /// Every operation, as `(METHOD, path, operation)`, ordered by path then method.
    pub fn operations(&self) -> Vec<(&'static str, &str, &Operation)> {
        let mut out = Vec::new();
        for (path, item) in &self.paths {
            for (method, op) in item.operations() {
                out.push((method, path.as_str(), op));
            }
        }
        out
    }

    /// Find the [`PathItem`] whose template matches a concrete request path, e.g.
    /// `/api/rack-locks/p17-r01` matches the template `/api/rack-locks/{rack}`.
    pub fn match_path(&self, request_path: &str) -> Option<(&str, &PathItem)> {
        let req = normalize(request_path);
        self.paths
            .iter()
            .find(|(template, _)| path_matches(template, &req))
            .map(|(t, item)| (t.as_str(), item))
    }

    /// Best-effort check of a call against the contract.
    pub fn check_call(&self, method: &str, request_path: &str) -> CallCheck {
        match self.match_path(request_path) {
            None => CallCheck::UnknownPath,
            Some((_, item)) => {
                let method = method.to_ascii_uppercase();
                if item.operations().iter().any(|(m, _)| *m == method) {
                    CallCheck::Ok
                } else {
                    CallCheck::MethodNotAllowed(item.method_names())
                }
            }
        }
    }
}

/// Strip a query string and trailing slash so path matching compares only the path.
fn normalize(path: &str) -> String {
    let no_query = path.split(['?', '#']).next().unwrap_or(path);
    let trimmed = no_query.trim_end_matches('/');
    if trimmed.is_empty() {
        "/".to_string()
    } else {
        trimmed.to_string()
    }
}

/// Does an OpenAPI path template match a concrete path? Same number of segments, and each template
/// segment either equals the request segment or is a `{param}` placeholder.
fn path_matches(template: &str, request: &str) -> bool {
    let t = normalize(template);
    let ts: Vec<&str> = t.split('/').collect();
    let rs: Vec<&str> = request.split('/').collect();
    if ts.len() != rs.len() {
        return false;
    }
    ts.iter()
        .zip(&rs)
        .all(|(tseg, rseg)| (tseg.starts_with('{') && tseg.ends_with('}')) || tseg == rseg)
}

/// A loaded spec together with its raw JSON (so `--json` can print the server's exact document) and
/// whether it came from the cache.
pub struct Loaded {
    pub raw: Value,
    pub spec: Spec,
    pub from_cache: bool,
}

fn parse(raw: Value) -> Result<Spec, OpenApiError> {
    serde_json::from_value(raw).map_err(|e| OpenApiError::Parse(e.to_string()))
}

/// Fetch the document fresh from the deployment.
pub async fn fetch(client: &nvrs_client::Client) -> Result<Loaded, OpenApiError> {
    let raw = client
        .send_json(nvrs_client::Method::GET, SPEC_PATH, &[], None)
        .await?;
    let spec = parse(raw.clone())?;
    Ok(Loaded {
        raw,
        spec,
        from_cache: false,
    })
}

/// Load the cached spec for a deployment if it exists and is younger than `max_age`.
pub fn load_cached(base_url: &Url, max_age: Duration) -> Option<Loaded> {
    let path = cache_file(base_url)?;
    let meta = std::fs::metadata(&path).ok()?;
    if let Ok(modified) = meta.modified() {
        if SystemTime::now()
            .duration_since(modified)
            .unwrap_or(max_age)
            > max_age
        {
            return None;
        }
    }
    let text = std::fs::read_to_string(&path).ok()?;
    let raw: Value = serde_json::from_str(&text).ok()?;
    let spec = parse(raw.clone()).ok()?;
    Some(Loaded {
        raw,
        spec,
        from_cache: true,
    })
}

/// Write a fetched spec to the cache. Best-effort placement failures are surfaced, not swallowed.
pub fn save_cache(base_url: &Url, raw: &Value) -> Result<(), OpenApiError> {
    let Some(path) = cache_file(base_url) else {
        return Ok(());
    };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)
            .map_err(|e| OpenApiError::Cache(format!("creating {}: {e}", dir.display())))?;
    }
    let text = serde_json::to_string(raw).map_err(|e| OpenApiError::Cache(e.to_string()))?;
    std::fs::write(&path, text)
        .map_err(|e| OpenApiError::Cache(format!("writing {}: {e}", path.display())))
}

/// Load the spec: from the cache when fresh (and `refresh` is false), otherwise fetch and cache.
pub async fn load(
    client: &nvrs_client::Client,
    base_url: &Url,
    refresh: bool,
    max_age: Duration,
) -> Result<Loaded, OpenApiError> {
    if !refresh {
        if let Some(cached) = load_cached(base_url, max_age) {
            return Ok(cached);
        }
    }
    let loaded = fetch(client).await?;
    // A cache write failure should not fail the command — the spec is in hand either way.
    let _ = save_cache(base_url, &loaded.raw);
    Ok(loaded)
}

/// Where a deployment's cached spec lives: `$XDG_CACHE_HOME/nvrs/openapi-<host>_<port>.json`
/// (or `~/.cache/...`; `NVRS_CACHE` overrides the directory). `None` if no home can be found.
fn cache_file(base_url: &Url) -> Option<PathBuf> {
    let dir = if let Some(p) = std::env::var_os("NVRS_CACHE").filter(|p| !p.is_empty()) {
        PathBuf::from(p)
    } else {
        let base = std::env::var_os("XDG_CACHE_HOME")
            .map(PathBuf::from)
            .filter(|p| !p.as_os_str().is_empty())
            .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))?;
        base.join("nvrs")
    };
    Some(dir.join(format!("openapi-{}.json", host_key(base_url))))
}

/// A filesystem-safe key for a deployment: host and port with non-alphanumerics folded to `_`.
fn host_key(base_url: &Url) -> String {
    let host = base_url.host_str().unwrap_or("unknown");
    let port = base_url.port_or_known_default().unwrap_or(0);
    let raw = format!("{host}_{port}");
    raw.chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn spec() -> Spec {
        serde_json::from_value(json!({
            "openapi": "3.1.0",
            "info": {"title": "NVIS Rack Status API", "version": "3.1"},
            "paths": {
                "/api/status": {"get": {"summary": "Rack status", "parameters": [
                    {"name": "rack", "in": "query", "required": false, "schema": {"type": "string"}}
                ]}},
                "/api/rack-locks": {"get": {"summary": "List locks"}},
                "/api/rack-locks/{rack}": {
                    "post": {"summary": "Claim a rack", "parameters": [
                        {"name": "rack", "in": "path", "required": true, "schema": {"type": "string"}}
                    ]},
                    "delete": {"summary": "Release a rack"}
                }
            }
        }))
        .unwrap()
    }

    #[test]
    fn operations_are_flattened_and_counted() {
        let s = spec();
        // GET /api/status, GET /api/rack-locks, POST + DELETE /api/rack-locks/{rack} = 4.
        assert_eq!(s.operations().len(), 4);
        assert_eq!(s.info.title, "NVIS Rack Status API");
    }

    #[test]
    fn concrete_path_matches_template() {
        let s = spec();
        let check = s.check_call("POST", "/api/rack-locks/p17-r01");
        assert_eq!(check, CallCheck::Ok);
    }

    #[test]
    fn query_string_and_trailing_slash_are_ignored_in_matching() {
        let s = spec();
        assert_eq!(s.check_call("GET", "/api/status?rack=p17"), CallCheck::Ok);
        assert_eq!(s.check_call("GET", "/api/rack-locks/"), CallCheck::Ok);
    }

    #[test]
    fn unknown_path_is_reported() {
        assert_eq!(
            spec().check_call("GET", "/api/nope"),
            CallCheck::UnknownPath
        );
        // A different segment count must not match a template.
        assert_eq!(
            spec().check_call("GET", "/api/rack-locks/a/b"),
            CallCheck::UnknownPath
        );
    }

    #[test]
    fn wrong_method_lists_the_allowed_ones() {
        match spec().check_call("GET", "/api/rack-locks/p17-r01") {
            CallCheck::MethodNotAllowed(methods) => {
                assert!(methods.contains(&"POST".to_string()));
                assert!(methods.contains(&"DELETE".to_string()));
                assert!(!methods.contains(&"GET".to_string()));
            }
            other => panic!("expected MethodNotAllowed, got {other:?}"),
        }
    }

    #[test]
    fn method_check_is_case_insensitive() {
        assert_eq!(spec().check_call("get", "/api/status"), CallCheck::Ok);
    }

    #[test]
    fn query_params_are_filtered_from_path_params() {
        let s = spec();
        let (_, item) = s.match_path("/api/status").unwrap();
        let op = item.get.as_ref().unwrap();
        let q = op.query_params();
        assert_eq!(q.len(), 1);
        assert_eq!(q[0].name, "rack");
    }

    #[test]
    fn host_key_is_filesystem_safe() {
        let url = Url::parse("https://rack.mayo.example:5000").unwrap();
        assert_eq!(host_key(&url), "rack_mayo_example_5000");
    }
}
