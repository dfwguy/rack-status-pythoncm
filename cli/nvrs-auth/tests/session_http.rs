// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! HTTP-level tests for the session flows that do not need SRP: `refresh` and `logout`.
//!
//! The SRP crypto itself is pinned by the vector test in `src/srp.rs`; these prove the wiring around
//! it — that `refresh` lifts the new access token out of `Set-Cookie`, that a refused refresh maps
//! to `SessionExpired`, and that `logout` treats an already-gone session as success. A hand-rolled
//! one-shot server keeps the dependency footprint small and lets us emit exact `Set-Cookie` headers.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

use nvrs_auth::{AuthError, Tokens};
use nvrs_client::{Client, ClientConfig};
use url::Url;

/// Serve one request with `status`, the given extra header lines, and `body`. Returns the base URL.
fn serve_once(status: &'static str, extra_headers: Vec<String>, body: &'static str) -> Url {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    thread::spawn(move || {
        if let Ok((stream, _)) = listener.accept() {
            handle(stream, status, &extra_headers, body);
        }
    });
    Url::parse(&format!("http://{addr}")).unwrap()
}

fn handle(mut stream: TcpStream, status: &str, extra_headers: &[String], body: &str) {
    let mut buf = [0u8; 4096];
    let _ = stream.read(&mut buf);
    let mut response = format!("HTTP/1.1 {status}\r\n");
    for h in extra_headers {
        response.push_str(h);
        response.push_str("\r\n");
    }
    response.push_str(&format!(
        "Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    ));
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

fn client(base: Url) -> Client {
    Client::new(ClientConfig::new(base)).unwrap()
}

#[tokio::test]
async fn refresh_lifts_the_new_access_cookie() {
    let base = serve_once(
        "200 OK",
        vec!["Set-Cookie: nvis_access=fresh.access.token; Path=/; HttpOnly; SameSite=Lax".into()],
        r#"{"ok":true,"principal":"user:operator"}"#,
    );
    let new = nvrs_auth::refresh(&client(base), "the.refresh.token")
        .await
        .expect("refresh succeeds");
    assert_eq!(new, "fresh.access.token");
}

#[tokio::test]
async fn refresh_without_a_cookie_is_a_protocol_error() {
    // A 200 that sets no access cookie is malformed — we must not silently return an empty token.
    let base = serve_once("200 OK", vec![], r#"{"ok":true}"#);
    let err = nvrs_auth::refresh(&client(base), "r").await.unwrap_err();
    assert!(matches!(err, AuthError::Protocol(_)), "got {err:?}");
}

#[tokio::test]
async fn a_rejected_refresh_is_session_expired() {
    let base = serve_once(
        "401 UNAUTHORIZED",
        vec![],
        r#"{"ok":false,"error":"authentication failed"}"#,
    );
    let err = nvrs_auth::refresh(&client(base), "stale")
        .await
        .unwrap_err();
    assert!(matches!(err, AuthError::SessionExpired), "got {err:?}");
}

#[tokio::test]
async fn logout_succeeds_on_200() {
    let base = serve_once("200 OK", vec![], r#"{"ok":true}"#);
    let tokens = Tokens {
        access: "a".into(),
        refresh: "r".into(),
    };
    assert!(nvrs_auth::logout(&client(base), &tokens).await.is_ok());
}

#[tokio::test]
async fn logout_treats_gone_session_as_success() {
    // A 401 on logout means the tokens were already invalid; forgetting them locally is still the
    // right outcome, so this must not error.
    let base = serve_once(
        "401 UNAUTHORIZED",
        vec![],
        r#"{"ok":false,"error":"authentication failed"}"#,
    );
    let tokens = Tokens {
        access: "a".into(),
        refresh: "r".into(),
    };
    assert!(nvrs_auth::logout(&client(base), &tokens).await.is_ok());
}
