// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs-client` — the transport half of the Rack Status CLI.
//!
//! This is the Rust counterpart of the SPA's `frontend/src/lib/apiFetch.ts`: one place that talks
//! to the Flask backend, injects the operator/client headers on every call, understands the JSON
//! envelope, and backs off on a 429. It deliberately knows nothing about *login* — obtaining and
//! refreshing a token is `nvrs-auth`'s job — so this crate can be exercised against a fake server
//! with a static bearer token and no crypto.
//!
//! What it does own, because the server's contract demands it and the browser wrapper does the
//! same:
//! * **Operator attribution.** `X-Rack-Operator` / `X-Rack-Client-Id` are added to every request so
//!   server-side rack-lock tracking can name who did what (`frontend/src/lib/apiFetch.ts`,
//!   `backend/api/_common.py:operator_from_request`). The authenticated principal is authoritative;
//!   these are advisory labels.
//! * **TLS trust.** The OVA serves HTTPS with a self-signed cert by default, reached over an ssh
//!   tunnel to `https://localhost:<port>`. A browser prompts; a CLI must be told, per profile,
//!   whether to trust a pinned CA or to skip verification.
//! * **429 backoff.** `/api/auth/srp/begin` is 10/min and the app runs a single worker, so a burst
//!   really does get shed. We honour `Retry-After` rather than hammering.

mod envelope;
mod error;
mod ws;

pub use error::{Error, Result};
pub use ws::{connect_terminal, terminal_url, Message, WsStream};
// Re-exported so the binary can name HTTP verbs and consume a streaming body without taking its own
// direct dependency on reqwest (and risking a version skew with this crate's).
pub use reqwest::{Method, Response};

use std::time::Duration;

use reqwest::StatusCode;
use serde_json::Value;
use url::Url;

/// How the client should verify the deployment's TLS certificate.
#[derive(Clone, Debug)]
pub enum TlsTrust {
    /// Trust the OS/native root store only. Correct once an operator has installed a real cert.
    SystemRoots,
    /// Additionally trust a specific PEM CA/cert — the pinned self-signed OVA cert, typically.
    AddRootPem(Vec<u8>),
    /// Skip verification entirely. Honest about what it is: `--insecure`. For a first contact with
    /// a self-signed box before its cert has been captured; never a default.
    Insecure,
}

/// Everything needed to construct a [`Client`]. Built by the binary from the active profile.
#[derive(Clone, Debug)]
pub struct ClientConfig {
    /// Origin only, e.g. `https://localhost:5000`. Paths are joined onto this.
    pub base_url: Url,
    pub tls: TlsTrust,
    /// Human label for `X-Rack-Operator`. Usually the logged-in display name; `None` omits it.
    pub operator: Option<String>,
    /// Stable per-install id for `X-Rack-Client-Id` (matches the SPA's `nvis-rack-client-id`).
    pub client_id: String,
    /// The access token to send as `Authorization: Bearer`. `None` for anonymous calls (login).
    pub bearer: Option<String>,
    pub timeout: Duration,
    /// How many times to re-send after a 429 before giving up. 0 disables backoff-retry.
    pub max_retries: u32,
}

impl ClientConfig {
    /// A minimal config for `base_url`, system trust, no auth. Tests and `nvrs api` against an
    /// already-trusted host start here and layer on `bearer`/`tls`.
    pub fn new(base_url: Url) -> Self {
        Self {
            base_url,
            tls: TlsTrust::SystemRoots,
            operator: None,
            client_id: String::new(),
            bearer: None,
            timeout: Duration::from_secs(60),
            max_retries: 3,
        }
    }
}

/// A decoded JSON body together with the `Set-Cookie` header values the response carried. Returned
/// by [`Client::send_json_with_cookies`] so the auth layer can lift session tokens out of the
/// cookies the login exchange sets.
pub struct CapturedJson {
    pub value: Value,
    pub set_cookies: Vec<String>,
}

/// A configured HTTP client bound to one deployment. Cheap to clone (reqwest client is an `Arc`).
#[derive(Clone)]
pub struct Client {
    http: reqwest::Client,
    base: Url,
    operator: Option<String>,
    client_id: String,
    bearer: Option<String>,
    max_retries: u32,
}

impl Client {
    /// Build the client, applying the TLS trust policy. Fails only if a pinned PEM cert is
    /// unparseable or reqwest cannot initialise its TLS backend.
    pub fn new(cfg: ClientConfig) -> Result<Self> {
        let mut builder = reqwest::Client::builder()
            .timeout(cfg.timeout)
            .user_agent(concat!("nvrs/", env!("CARGO_PKG_VERSION")));

        builder = match &cfg.tls {
            TlsTrust::SystemRoots => builder,
            TlsTrust::AddRootPem(pem) => {
                let cert = reqwest::Certificate::from_pem(pem).map_err(Error::Transport)?;
                // Augments the native roots rather than replacing them, so a profile can pin the
                // OVA cert and still reach an ordinary HTTPS host.
                builder.add_root_certificate(cert)
            }
            TlsTrust::Insecure => builder.danger_accept_invalid_certs(true),
        };

        let http = builder.build().map_err(Error::Transport)?;
        Ok(Self {
            http,
            base: cfg.base_url,
            operator: cfg.operator,
            client_id: cfg.client_id,
            bearer: cfg.bearer,
            max_retries: cfg.max_retries,
        })
    }

    /// The base origin this client targets. Handy for error messages and `nvrs config`.
    pub fn base_url(&self) -> &Url {
        &self.base
    }

    /// Send a request expecting the JSON envelope back, and return the decoded top-level object.
    ///
    /// This is the workhorse behind `nvrs api` and every typed verb. It:
    /// 1. joins `path` onto the base and appends `query`,
    /// 2. adds the operator/client/auth headers and the optional JSON body,
    /// 3. retries on 429 up to `max_retries`, honouring `Retry-After`,
    /// 4. maps the HTTP status and `{ok:false}` body to a precise [`Error`].
    ///
    /// On success it hands back the whole JSON value (payload fields sit beside `ok`), so a verb
    /// can pluck the field it wants and `nvrs api --json` can print the lot.
    pub async fn send_json(
        &self,
        method: Method,
        path: &str,
        query: &[(String, String)],
        body: Option<&Value>,
    ) -> Result<Value> {
        let url = self.base.join(path).map_err(Error::Url)?;

        let mut attempt = 0u32;
        loop {
            let mut req = self.http.request(method.clone(), url.clone());
            if !query.is_empty() {
                req = req.query(query);
            }
            req = self.apply_headers(req);
            if let Some(b) = body {
                req = req.json(b);
            }

            let resp = req.send().await.map_err(Error::Transport)?;
            let status = resp.status();

            if status == StatusCode::TOO_MANY_REQUESTS && attempt < self.max_retries {
                let wait = retry_after(&resp).unwrap_or_else(|| backoff(attempt));
                attempt += 1;
                tokio::time::sleep(wait).await;
                continue;
            }

            return self.interpret(status, resp).await;
        }
    }

    /// Send a request and hand back the raw [`reqwest::Response`] for streaming.
    ///
    /// The `-stream` endpoints return chunked `text/plain` ending in a `--- exit N ---` sentinel,
    /// and `/api/ai/chat` returns SSE. Both want the body read incrementally, which the envelope
    /// path cannot do. This applies the same headers and auth but leaves the body to the caller.
    /// No 429 retry here: a stream that 429s has not started, and the caller decides.
    pub async fn send_raw(
        &self,
        method: Method,
        path: &str,
        query: &[(String, String)],
        body: Option<&Value>,
    ) -> Result<reqwest::Response> {
        let url = self.base.join(path).map_err(Error::Url)?;
        let mut req = self.http.request(method, url);
        if !query.is_empty() {
            req = req.query(query);
        }
        req = self.apply_headers(req);
        if let Some(b) = body {
            req = req.json(b);
        }
        let resp = req.send().await.map_err(Error::Transport)?;
        let status = resp.status();
        if status.is_success() {
            Ok(resp)
        } else {
            // Fold a failed stream start into the same error shape as send_json.
            Err(self.status_error(status, resp.text().await.unwrap_or_default()))
        }
    }

    /// Send a JSON request and return both the decoded body and the response's `Set-Cookie` values.
    ///
    /// The auth flows need this because the session tokens are delivered *only* as `Set-Cookie`
    /// (`nvis_access` / `nvis_refresh`), never in the JSON body — so the ordinary [`send_json`], which
    /// discards headers, cannot see them. `request_cookies` sets an outgoing `Cookie` header, which
    /// is how `/api/auth/refresh` receives the refresh token (it reads a cookie, not a bearer).
    ///
    /// No 429 retry here: login is a deliberate, interactive act; a rate-limit answer should surface
    /// to the operator rather than being silently slept through.
    pub async fn send_json_with_cookies(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        request_cookies: &[(&str, &str)],
    ) -> Result<CapturedJson> {
        let url = self.base.join(path).map_err(Error::Url)?;
        let mut req = self.apply_headers(self.http.request(method, url));
        if !request_cookies.is_empty() {
            let jar = request_cookies
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect::<Vec<_>>()
                .join("; ");
            req = req.header(reqwest::header::COOKIE, jar);
        }
        if let Some(b) = body {
            req = req.json(b);
        }

        let resp = req.send().await.map_err(Error::Transport)?;
        let status = resp.status();
        let set_cookies: Vec<String> = resp
            .headers()
            .get_all(reqwest::header::SET_COOKIE)
            .iter()
            .filter_map(|v| v.to_str().ok().map(str::to_owned))
            .collect();

        let text = resp.text().await.map_err(Error::Transport)?;
        if !status.is_success() {
            return Err(self.status_error(status, text));
        }
        let value: Value = if text.trim().is_empty() {
            Value::Null
        } else {
            serde_json::from_str(&text).map_err(|e| Error::Decode(e.to_string()))?
        };
        if !envelope::is_ok(&value) {
            return Err(Error::Api {
                status: status.as_u16(),
                message: envelope::extract_error(&value)
                    .unwrap_or_else(|| "request reported failure".into()),
            });
        }
        Ok(CapturedJson { value, set_cookies })
    }

    fn apply_headers(&self, req: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        let mut req = req;
        if let Some(op) = &self.operator {
            req = req.header("X-Rack-Operator", op);
        }
        if !self.client_id.is_empty() {
            req = req.header("X-Rack-Client-Id", &self.client_id);
        }
        if let Some(token) = &self.bearer {
            req = req.bearer_auth(token);
        }
        req
    }

    async fn interpret(&self, status: StatusCode, resp: reqwest::Response) -> Result<Value> {
        let text = resp.text().await.map_err(Error::Transport)?;
        if status.is_success() {
            if text.trim().is_empty() {
                return Ok(Value::Null);
            }
            let value: Value =
                serde_json::from_str(&text).map_err(|e| Error::Decode(e.to_string()))?;
            if envelope::is_ok(&value) {
                Ok(value)
            } else {
                // 200 with ok:false should not happen (the server audits against it), but if it
                // ever does, surface the message rather than pretending it succeeded.
                Err(Error::Api {
                    status: status.as_u16(),
                    message: envelope::extract_error(&value)
                        .unwrap_or_else(|| "request reported failure".into()),
                })
            }
        } else {
            Err(self.status_error(status, text))
        }
    }

    fn status_error(&self, status: StatusCode, text: String) -> Error {
        match status {
            StatusCode::UNAUTHORIZED => Error::Unauthorized {
                message: serde_json::from_str::<Value>(&text)
                    .ok()
                    .as_ref()
                    .and_then(envelope::extract_error),
            },
            StatusCode::TOO_MANY_REQUESTS => Error::RateLimited { retry_after: None },
            _ => {
                let msg = serde_json::from_str::<Value>(&text)
                    .ok()
                    .as_ref()
                    .and_then(envelope::extract_error);
                match (status, msg) {
                    (StatusCode::FORBIDDEN, Some(m)) => Error::Forbidden(m),
                    (StatusCode::FORBIDDEN, None) => Error::Forbidden(text),
                    (_, Some(m)) => Error::Api {
                        status: status.as_u16(),
                        message: m,
                    },
                    (_, None) => Error::Http {
                        status: status.as_u16(),
                        body: text,
                    },
                }
            }
        }
    }
}

/// Parse a `Retry-After` header expressed in whole seconds (the form the server sends).
fn retry_after(resp: &reqwest::Response) -> Option<Duration> {
    resp.headers()
        .get(reqwest::header::RETRY_AFTER)?
        .to_str()
        .ok()?
        .trim()
        .parse::<u64>()
        .ok()
        .map(Duration::from_secs)
}

/// Exponential fallback when a 429 arrives without a `Retry-After`: 1s, 2s, 4s…
fn backoff(attempt: u32) -> Duration {
    Duration::from_secs(1u64 << attempt.min(5))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_and_caps() {
        assert_eq!(backoff(0), Duration::from_secs(1));
        assert_eq!(backoff(1), Duration::from_secs(2));
        assert_eq!(backoff(3), Duration::from_secs(8));
        // Capped so a large attempt count cannot schedule an absurd sleep.
        assert_eq!(backoff(50), Duration::from_secs(32));
    }

    #[test]
    fn config_defaults_are_conservative() {
        let cfg = ClientConfig::new(Url::parse("https://localhost:5000").unwrap());
        assert!(matches!(cfg.tls, TlsTrust::SystemRoots));
        assert!(cfg.bearer.is_none());
        assert_eq!(cfg.max_retries, 3);
    }

    #[test]
    fn base_join_keeps_origin() {
        // A leading-slash path replaces only the path, never the host:port — the guarantee the
        // whole client rests on.
        let base = Url::parse("https://localhost:8443").unwrap();
        assert_eq!(
            base.join("/api/about").unwrap().as_str(),
            "https://localhost:8443/api/about"
        );
    }
}
