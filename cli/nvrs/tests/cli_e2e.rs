// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! End-to-end tests: the compiled `nvrs` binary against a hand-rolled multi-request server.
//!
//! Unit tests cover the pure logic; this drives the actual binary the way an operator would — parse
//! args, resolve a profile, make the HTTP calls, render — and asserts on stdout and exit codes. The
//! server answers by request path so one instance can serve a whole session, including the
//! submit-then-poll sequence `run --follow` depends on.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;

/// Start a server that answers Rack Status endpoints until the process ends. Returns its base URL.
fn start_server() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    // Poll counter: the first status check is still running, later ones are done.
    let polls = Arc::new(AtomicUsize::new(0));
    thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let polls = polls.clone();
            thread::spawn(move || handle(stream, polls));
        }
    });
    format!("http://{addr}")
}

fn handle(mut stream: TcpStream, polls: Arc<AtomicUsize>) {
    let mut buf = [0u8; 4096];
    let n = stream.read(&mut buf).unwrap_or(0);
    let head = String::from_utf8_lossy(&buf[..n]);
    let line = head.lines().next().unwrap_or("");
    let mut parts = line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let raw_path = parts.next().unwrap_or("");
    let path = raw_path.split('?').next().unwrap_or("");

    let body = match (method, path) {
        ("GET", "/api/rack-locks") => {
            r#"{"ok":true,"now":"t","me":"Rafael","locks":{"p17-r01":{"operator":"Rafael","kind":"claim","detail":"burn-in","since":"11:00","expires_at":"13:00"}}}"#.to_string()
        }
        ("POST", "/api/run-validation-async") => {
            r#"{"ok":true,"job_id":"j1","rack":"p17-r01"}"#.to_string()
        }
        ("GET", "/api/run-validation-status") => {
            if polls.fetch_add(1, Ordering::SeqCst) == 0 {
                r#"{"ok":true,"job_id":"j1","phase":"running","log_dir":"/logs/j1"}"#.to_string()
            } else {
                r#"{"ok":true,"job_id":"j1","phase":"done","returncode":0,"log_dir":"/logs/j1"}"#.to_string()
            }
        }
        ("GET", "/api/catalog/platforms") => {
            r#"{"ok":true,"items":[{"id":"gb200","name":"GB200 NVL72"},{"id":"h100","name":"H100"}]}"#.to_string()
        }
        ("DELETE", "/api/catalog/tools/old") => r#"{"ok":true,"deleted":"old"}"#.to_string(),
        ("GET", "/api/identity/users") => {
            r#"{"ok":true,"users":[{"login":"rafael","display_name":"Rafael","disabled":false}]}"#.to_string()
        }
        ("GET", "/api/policies/preview") => {
            r#"{"ok":true,"subject":"user:rafael","actions":[{"action":"rack.power:on","effect":"allow"},{"action":"identity.user:create","effect":"deny"}]}"#.to_string()
        }
        _ => r#"{"ok":false,"error":"not found"}"#.to_string(),
    };

    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

/// Run the compiled binary with an isolated config/state, returning (stdout, exit_code).
fn run(tmp: &std::path::Path, args: &[&str]) -> (String, i32) {
    let output = Command::new(env!("CARGO_BIN_EXE_nvrs"))
        .env("NVRS_CONFIG", tmp.join("config.toml"))
        .env("NVRS_STATE", tmp.join("tokens.json"))
        .env("NVRS_CACHE", tmp.join("cache"))
        .env_remove("NVRS_TOKEN")
        .args(args)
        .output()
        .expect("run nvrs");
    (
        String::from_utf8_lossy(&output.stdout).to_string(),
        output.status.code().unwrap_or(-1),
    )
}

#[test]
fn rack_list_and_run_follow_end_to_end() {
    let base = start_server();
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path();

    // Configure a profile pointing at the fake server.
    let (_, code) = run(dir, &["config", "set-url", "fake", &base]);
    assert_eq!(code, 0);

    // rack list renders the locked rack.
    let (out, code) = run(dir, &["rack", "list"]);
    assert_eq!(code, 0, "rack list should succeed");
    assert!(out.contains("p17-r01"), "table should list the rack: {out}");
    assert!(
        out.contains("burn-in"),
        "table should show the detail: {out}"
    );

    // run --follow submits, polls through running -> done, and exits 0.
    let (out, code) = run(
        dir,
        &[
            "run",
            "validation",
            "--rack",
            "p17-r01",
            "--follow",
            "--interval",
            "1",
        ],
    );
    assert_eq!(code, 0, "a successful job should exit 0: {out}");
    assert!(out.contains("submitted validation"), "{out}");
    assert!(
        out.contains("[running]"),
        "should show the running phase: {out}"
    );
    assert!(out.contains("[done]"), "should show the done phase: {out}");
    assert!(out.contains("completed"), "{out}");
}

#[test]
fn unknown_suite_exits_nonzero() {
    let base = start_server();
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path();
    run(dir, &["config", "set-url", "fake", &base]);

    let (_out, code) = run(dir, &["run", "bogus-suite"]);
    assert_ne!(code, 0, "an unknown suite should be an error");
}

#[test]
fn admin_verbs_render_their_collections() {
    let base = start_server();
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path();
    run(dir, &["config", "set-url", "fake", &base]);

    // catalog list renders the items array as a table.
    let (out, code) = run(dir, &["catalog", "platforms", "list"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("gb200") && out.contains("H100"), "{out}");

    // identity users list.
    let (out, code) = run(dir, &["identity", "users", "list"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("rafael"), "{out}");

    // policy preview flattens the actions array.
    let (out, code) = run(dir, &["policy", "preview"]);
    assert_eq!(code, 0, "{out}");
    assert!(
        out.contains("rack.power:on") && out.contains("allow"),
        "{out}"
    );

    // a delete confirmation.
    let (out, code) = run(dir, &["catalog", "tools", "delete", "old"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("deleted old"), "{out}");
}

#[test]
fn unknown_catalog_kind_exits_nonzero() {
    let base = start_server();
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path();
    run(dir, &["config", "set-url", "fake", &base]);

    let (_out, code) = run(dir, &["catalog", "platform", "list"]); // singular typo
    assert_ne!(code, 0, "an unknown catalog kind should be an error");
}
