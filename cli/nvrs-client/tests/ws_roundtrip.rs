// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Proves the terminal WebSocket transport connects and pumps frames both ways.
//!
//! Over plain `ws://` (no TLS), so it needs no certificate setup: a loopback server sends the
//! `session` text frame and a binary output frame the real backend sends, then echoes one client
//! frame back. This covers `connect_terminal` end to end minus the raw-TTY glue, which is the
//! binary's concern.

use futures_util::{SinkExt, StreamExt};
use nvrs_client::{connect_terminal, Message, TlsTrust};
use tokio::net::TcpListener;
use url::Url;

#[tokio::test]
async fn connect_send_and_receive_frames() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    let server = tokio::spawn(async move {
        let (tcp, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(tcp).await.unwrap();
        // What the backend sends first: a JSON session frame, then PTY output as a binary frame.
        ws.send(Message::Text(
            r#"{"type":"session","id":"s1","target":"n04"}"#.into(),
        ))
        .await
        .unwrap();
        ws.send(Message::Binary(b"hello from pty".to_vec()))
            .await
            .unwrap();
        // Echo the client's first data frame so the test can assert the send path worked.
        if let Some(Ok(msg)) = ws.next().await {
            ws.send(msg).await.unwrap();
        }
    });

    let base = Url::parse(&format!("http://{addr}")).unwrap();
    let mut stream = connect_terminal(
        &base,
        &TlsTrust::SystemRoots,
        &[("mode", "node"), ("target", "n04")],
        None,
    )
    .await
    .expect("connect");

    // First frame: the session JSON (text).
    match stream.next().await.unwrap().unwrap() {
        Message::Text(t) => assert!(t.contains("\"type\":\"session\""), "got {t}"),
        other => panic!("expected session text frame, got {other:?}"),
    }
    // Second frame: PTY output (binary).
    match stream.next().await.unwrap().unwrap() {
        Message::Binary(b) => assert_eq!(b, b"hello from pty"),
        other => panic!("expected binary output frame, got {other:?}"),
    }

    // Send a keystroke frame and expect it echoed.
    stream
        .send(Message::Binary(b"ls\r".to_vec()))
        .await
        .unwrap();
    match stream.next().await.unwrap().unwrap() {
        Message::Binary(b) => assert_eq!(b, b"ls\r"),
        other => panic!("expected echoed keystroke, got {other:?}"),
    }

    server.await.unwrap();
}
