// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! End-to-end transport tests: a real reqwest client against a hand-rolled HTTP/1.1 server.
//!
//! A hand-rolled server rather than a mock-server crate keeps the dependency (and CI) footprint
//! small, and the server is trivial: read one request, write one canned response. It exists to prove
//! the parts the unit tests cannot — that `send_json` completes a socket round-trip, decodes the
//! envelope, and maps status codes to the right [`nvrs_client::Error`] — over plain HTTP so no TLS
//! setup is needed.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

use nvrs_client::{Client, ClientConfig, Error, Method};
use url::Url;

/// Serve exactly one request, replying with `status`/`body`, then close. Returns the base URL to
/// point a client at. Runs on a background thread so the async client can drive it.
fn serve_once(status: &'static str, content_type: &'static str, body: &'static str) -> Url {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
    let addr = listener.local_addr().unwrap();
    thread::spawn(move || {
        if let Ok((stream, _)) = listener.accept() {
            handle(stream, status, content_type, body);
        }
    });
    Url::parse(&format!("http://{addr}")).unwrap()
}

fn handle(mut stream: TcpStream, status: &str, content_type: &str, body: &str) {
    // Drain the request head (and any body) enough that the client's write completes before we
    // reply; a client whose request is never read can see a reset instead of our response.
    let mut buf = [0u8; 4096];
    let _ = stream.read(&mut buf);
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

fn client(base: Url) -> Client {
    Client::new(ClientConfig::new(base)).expect("build client")
}

#[tokio::test]
async fn success_envelope_is_returned_whole() {
    let base = serve_once(
        "200 OK",
        "application/json",
        r#"{"ok":true,"runs":[1,2,3]}"#,
    );
    let value = client(base)
        .send_json(Method::GET, "/api/workflow-runs", &[], None)
        .await
        .expect("call succeeds");
    assert_eq!(value["ok"], serde_json::json!(true));
    assert_eq!(value["runs"], serde_json::json!([1, 2, 3]));
}

#[tokio::test]
async fn ok_false_on_200_becomes_api_error() {
    // The server audits against this, but if it ever ships an ok:false in a 200 we must not report
    // success. The message comes from the envelope's `error`.
    let base = serve_once(
        "200 OK",
        "application/json",
        r#"{"ok":false,"error":"job_id is required"}"#,
    );
    let err = client(base)
        .send_json(Method::GET, "/api/run-validation-status", &[], None)
        .await
        .unwrap_err();
    match err {
        Error::Api { status, message } => {
            assert_eq!(status, 200);
            assert_eq!(message, "job_id is required");
        }
        other => panic!("expected Api error, got {other:?}"),
    }
}

#[tokio::test]
async fn forbidden_carries_the_server_message() {
    let base = serve_once(
        "403 FORBIDDEN",
        "application/json",
        r#"{"ok":false,"error":"disabled (RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED)"}"#,
    );
    let err = client(base)
        .send_json(Method::POST, "/api/tools/download-containers", &[], None)
        .await
        .unwrap_err();
    match err {
        Error::Forbidden(msg) => assert!(msg.contains("disabled"), "got {msg:?}"),
        other => panic!("expected Forbidden, got {other:?}"),
    }
    assert_eq!(Error::Forbidden("x".into()).exit_code(), 77);
}

#[tokio::test]
async fn unauthorized_is_its_own_variant() {
    // 401 must be distinguishable from any other failure: it is the one the caller retries after a
    // token refresh. The body is deliberately uniform on the server, so we don't parse it.
    let base = serve_once(
        "401 UNAUTHORIZED",
        "application/json",
        r#"{"ok":false,"error":"authentication required"}"#,
    );
    let err = client(base)
        .send_json(Method::GET, "/api/status", &[], None)
        .await
        .unwrap_err();
    assert!(err.is_unauthorized(), "got {err:?}");
}

#[tokio::test]
async fn catalog_plural_errors_survive_the_round_trip() {
    // The catalog blueprint answers with `errors` (a list); the transport must render it, not drop
    // it. This is the end-to-end proof of the envelope wrinkle the unit test covers in isolation.
    let base = serve_once(
        "400 BAD REQUEST",
        "application/json",
        r#"{"ok":false,"errors":["name is required","id must be a slug"]}"#,
    );
    let err = client(base)
        .send_json(Method::POST, "/api/catalog/platforms", &[], None)
        .await
        .unwrap_err();
    match err {
        Error::Api { status, message } => {
            assert_eq!(status, 400);
            assert_eq!(message, "name is required; id must be a slug");
        }
        other => panic!("expected Api error, got {other:?}"),
    }
}
