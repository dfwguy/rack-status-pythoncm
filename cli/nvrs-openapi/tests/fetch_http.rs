// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Proves `fetch` completes a real HTTP round trip and parses the document into the model.
//!
//! The parsing/matching logic is unit-tested in `src/lib.rs`; this covers the wire path — that a
//! served `/api/openapi.json` becomes a usable [`nvrs_openapi::Spec`]. A hand-rolled one-shot server
//! keeps the dependency footprint small. Cache placement is env-driven and covered by the offline
//! CLI behaviour rather than here, to avoid mutating process-global env in a parallel test.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;

use nvrs_client::{Client, ClientConfig};
use url::Url;

const SPEC: &str = r#"{
  "openapi": "3.1.0",
  "info": {"title": "NVIS Rack Status API", "version": "3.1"},
  "paths": {
    "/api/status": {"get": {"summary": "Rack status"}},
    "/api/rack-locks/{rack}": {"post": {"summary": "Claim"}, "delete": {"summary": "Release"}}
  }
}"#;

fn serve_spec() -> Url {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    thread::spawn(move || {
        if let Ok((stream, _)) = listener.accept() {
            reply(stream);
        }
    });
    Url::parse(&format!("http://{addr}")).unwrap()
}

fn reply(mut stream: TcpStream) {
    let mut buf = [0u8; 2048];
    let _ = stream.read(&mut buf);
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{SPEC}",
        SPEC.len()
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

#[tokio::test]
async fn fetch_parses_the_served_document() {
    let base = serve_spec();
    let client = Client::new(ClientConfig::new(base)).unwrap();
    let loaded = nvrs_openapi::fetch(&client).await.expect("fetch succeeds");

    assert!(!loaded.from_cache);
    assert_eq!(loaded.spec.info.title, "NVIS Rack Status API");
    // GET /api/status + POST/DELETE on the templated path = 3 operations.
    assert_eq!(loaded.spec.operations().len(), 3);
    assert_eq!(
        loaded.spec.check_call("POST", "/api/rack-locks/p17-r01"),
        nvrs_openapi::CallCheck::Ok
    );
    // The raw document is preserved for `--json`.
    assert_eq!(loaded.raw["openapi"], serde_json::json!("3.1.0"));
}
