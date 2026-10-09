// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! HTTP-level tests for the session flows that do not need SRP: `refresh` and `logout`.
//!
//! The SRP crypto itself is pinned by the vector test in `src/srp.rs`; these prove the wiring around
//! it — that `refresh` lifts the new access token out of `Set-Cookie`, that a refused refresh
//! reports the server's reason and the token's own expiry, that `refresh` and `logout` present the
//! refresh token as a cookie and no bearer, and that a refused logout is reported, not hidden. A hand-rolled
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

/// A JWT-shaped token whose payload carries `exp`, so the client can report the expiry. The
/// signature is junk on purpose: the client must never need to verify it to say this much.
fn token_expiring_in(secs: i64) -> String {
    use base64::Engine as _;
    let b64 = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let exp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
        + secs;
    format!(
        "{}.{}.not-a-signature",
        b64.encode(r#"{"alg":"ES256","typ":"JWT"}"#),
        b64.encode(format!(r#"{{"exp":{exp},"type":"refresh"}}"#))
    )
}

#[tokio::test]
async fn a_refused_refresh_reports_the_server_and_the_token_expiry() {
    // The Nscale NC signature: the access gate refused before the route read the refresh token.
    let base = serve_once(
        "401 UNAUTHORIZED",
        vec![r#"WWW-Authenticate: Bearer realm="rack-status""#.into()],
        r#"{"ok":false,"error":"authentication required","login":"/login"}"#,
    );
    let err = nvrs_auth::refresh(&client(base), &token_expiring_in(6 * 86_400))
        .await
        .unwrap_err();
    match &err {
        AuthError::RefreshRefused { server, expires_at } => {
            assert_eq!(server.as_deref(), Some("authentication required"));
            assert!(expires_at.is_some(), "the token's own exp must be read");
        }
        other => panic!("expected RefreshRefused, got {other:?}"),
    }
    let msg = err.to_string();
    assert!(msg.contains("authentication required"), "{msg}");
    assert!(msg.contains("had not expired"), "{msg}");
}

#[tokio::test]
async fn a_refused_expired_refresh_says_it_expired() {
    let base = serve_once(
        "401 UNAUTHORIZED",
        vec![],
        r#"{"ok":false,"error":"authentication failed"}"#,
    );
    let err = nvrs_auth::refresh(&client(base), &token_expiring_in(-60))
        .await
        .unwrap_err();
    let msg = err.to_string();
    assert!(msg.contains("authentication failed"), "{msg}");
    assert!(msg.contains("the refresh token has expired"), "{msg}");
}

/// Accept one request, hand its raw head to the test, and answer `status`/`body`.
fn capture_once(
    status: &'static str,
    body: &'static str,
) -> (Url, std::sync::mpsc::Receiver<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut buf = [0u8; 8192];
            let n = stream.read(&mut buf).unwrap_or(0);
            let _ = tx.send(String::from_utf8_lossy(&buf[..n]).into_owned());
            let response = format!(
                "HTTP/1.1 {status}\r\nSet-Cookie: nvis_access=fresh; Path=/\r\n\
                 Content-Type: application/json\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes());
        }
    });
    (Url::parse(&format!("http://{addr}")).unwrap(), rx)
}

#[tokio::test]
async fn refresh_presents_the_cookie_and_no_bearer() {
    // The server authenticates renewal by the refresh cookie alone; a stale bearer alongside it
    // is what the access gate used to judge instead.
    let (base, seen) = capture_once("200 OK", r#"{"ok":true}"#);
    nvrs_auth::refresh(&client(base), "the.refresh.token")
        .await
        .expect("refresh succeeds");
    let head = seen.recv().unwrap().to_ascii_lowercase();
    assert!(
        head.contains("cookie: nvis_refresh=the.refresh.token"),
        "{head}"
    );
    assert!(!head.contains("authorization:"), "{head}");
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
async fn a_refused_logout_is_reported_not_hidden() {
    // A 401 means nothing was revoked. Against a server that gated logout on a live access token
    // (Nscale NC before the fix), that left a valid refresh token behind — so it must be visible.
    let base = serve_once(
        "401 UNAUTHORIZED",
        vec![],
        r#"{"ok":false,"error":"authentication required","login":"/login"}"#,
    );
    let tokens = Tokens {
        access: "a".into(),
        refresh: "r".into(),
    };
    let err = nvrs_auth::logout(&client(base), &tokens).await.unwrap_err();
    assert!(
        matches!(&err, AuthError::LogoutRefused { server } if server.as_deref() == Some("authentication required")),
        "got {err:?}"
    );
    assert!(err.to_string().contains("nothing was revoked"), "{err}");
}
