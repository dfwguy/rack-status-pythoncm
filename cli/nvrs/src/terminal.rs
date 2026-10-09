// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs terminal` — an interactive shell on a compute node (or the head node) over `/ws/terminal`.
//!
//! The backend opens a real PTY and spawns `ssh` to the target with the app's cluster key, then
//! bridges it over a WebSocket: it sends PTY output as frames and accepts keystrokes as frames, with
//! a JSON `{"type":"resize",…}` control message for window size. This drives the client half — put
//! the local terminal in raw mode, pump stdin to the socket and socket to stdout, and forward window
//! resizes — so an operator gets a shell that behaves like `ssh` itself.
//!
//! Raw mode is restored on every exit path by a drop guard, so a dropped connection or a panic never
//! leaves the operator's terminal wedged.

use anyhow::{bail, Result};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, size};
use futures_util::{SinkExt, StreamExt};
use nvrs_client::{connect_terminal, Message};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::signal::unix::{signal, SignalKind};

use crate::config::{Config, Profile};

/// Restores cooked terminal mode when dropped — including on a panic or an early return — so a lost
/// connection never leaves the shell without an echo.
struct RawModeGuard;

impl RawModeGuard {
    fn enable() -> Result<Self> {
        enable_raw_mode()?;
        Ok(RawModeGuard)
    }
}

impl Drop for RawModeGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
    }
}

/// Connect an interactive terminal to `node` (or the head node with `headnode`).
pub async fn run(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    node: Option<String>,
    headnode: bool,
) -> Result<()> {
    let base_url = url::Url::parse(&profile.base_url)?;

    // Build the session query the middleware expects.
    let query: Vec<(&str, &str)> = if headnode {
        vec![("mode", "headnode")]
    } else {
        match node.as_deref() {
            Some(n) if !n.is_empty() => vec![("mode", "node"), ("target", n)],
            _ => bail!("a target is required: pass --node <hostname> or --headnode"),
        }
    };

    let stream = connect_with_refresh(cfg, profile_name, profile, &base_url, &query).await?;

    let target = if headnode {
        "headnode".to_string()
    } else {
        node.unwrap_or_default()
    };
    eprintln!("connected to {target} — exit the remote shell to disconnect");

    let _guard = RawModeGuard::enable()?;
    pump(stream).await
}

/// Open the socket, refreshing the access token once if the handshake is rejected with a 401.
async fn connect_with_refresh(
    cfg: &Config,
    profile_name: &str,
    profile: &Profile,
    base_url: &url::Url,
    query: &[(&str, &str)],
) -> Result<nvrs_client::WsStream> {
    use nvrs_auth::TokenStore;

    let store = TokenStore::open_default()?;
    let env_token = std::env::var("NVRS_TOKEN").ok().filter(|s| !s.is_empty());
    let (token, stored) = crate::auth::resolve_access(&store, profile_name, env_token.as_deref())?;
    let refresh = if env_token.is_none() {
        stored.map(|t| t.refresh)
    } else {
        None
    };

    match connect_terminal(base_url, &profile.tls.to_trust()?, query, token.as_deref()).await {
        Err(nvrs_client::Error::Unauthorized { .. }) if refresh.is_some() => {
            let new = crate::auth::refresh_and_store(
                cfg,
                profile,
                profile_name,
                &store,
                refresh.as_deref().unwrap(),
            )
            .await?;
            Ok(connect_terminal(base_url, &profile.tls.to_trust()?, query, Some(&new)).await?)
        }
        other => Ok(other?),
    }
}

/// The bidirectional pump: PTY output to stdout, keystrokes to the socket, window resizes forwarded.
/// Returns when the socket closes (the remote shell exited) or the server sends an error frame.
async fn pump(stream: nvrs_client::WsStream) -> Result<()> {
    let (mut sink, mut source) = stream.split();
    let mut stdin = tokio::io::stdin();
    let mut stdout = tokio::io::stdout();
    let mut input = [0u8; 4096];
    let mut winch = signal(SignalKind::window_change())?;

    // Send the current size up front so the remote PTY matches this terminal immediately.
    if let Ok((cols, rows)) = size() {
        sink.send(resize_message(cols, rows)).await.ok();
    }

    loop {
        tokio::select! {
            // Socket -> stdout.
            frame = source.next() => {
                match frame {
                    Some(Ok(msg)) => {
                        if let Some(action) = handle_incoming(msg) {
                            match action {
                                Incoming::Output(bytes) => {
                                    stdout.write_all(&bytes).await?;
                                    stdout.flush().await?;
                                }
                                // Raw mode is restored by the caller's guard on return.
                                Incoming::Error(reason) => bail!("terminal: {reason}"),
                                Incoming::Closed => return Ok(()),
                            }
                        }
                    }
                    Some(Err(e)) => bail!("terminal connection error: {e}"),
                    None => return Ok(()),
                }
            }
            // stdin -> socket.
            read = stdin.read(&mut input) => {
                match read {
                    Ok(0) => return Ok(()),      // local stdin closed
                    Ok(n) => {
                        sink.send(Message::Binary(input[..n].to_vec())).await
                            .map_err(|e| anyhow::anyhow!("send failed: {e}"))?;
                    }
                    Err(e) => bail!("reading stdin: {e}"),
                }
            }
            // Window resized -> forward new size.
            _ = winch.recv() => {
                if let Ok((cols, rows)) = size() {
                    sink.send(resize_message(cols, rows)).await.ok();
                }
            }
        }
    }
}

/// What an incoming frame means for the client.
enum Incoming {
    /// Bytes to write to the local terminal.
    Output(Vec<u8>),
    /// The server refused or ended the session with a reason.
    Error(String),
    /// The socket is closing.
    Closed,
}

/// Classify an incoming frame. Binary frames are PTY output. A text frame is either a JSON control
/// message (`session` is informational and swallowed; `error` ends the session) or, when it is not
/// our JSON, raw text to display — the backend sends the "session ended" line that way.
fn handle_incoming(msg: Message) -> Option<Incoming> {
    match msg {
        Message::Binary(b) => Some(Incoming::Output(b)),
        Message::Text(t) => match serde_json::from_str::<serde_json::Value>(&t) {
            Ok(v) if v.get("type").and_then(|x| x.as_str()) == Some("session") => None,
            Ok(v) if v.get("type").and_then(|x| x.as_str()) == Some("error") => {
                Some(Incoming::Error(
                    v.get("error")
                        .and_then(|x| x.as_str())
                        .unwrap_or("session refused")
                        .to_string(),
                ))
            }
            // Not our control JSON — display it (the backend's exit-status line arrives as text).
            _ => Some(Incoming::Output(t.into_bytes())),
        },
        Message::Close(_) => Some(Incoming::Closed),
        // Ping/Pong/Frame carry nothing to show; tungstenite answers pings itself.
        _ => None,
    }
}

/// The resize control frame the backend understands.
fn resize_message(cols: u16, rows: u16) -> Message {
    Message::Text(format!(
        r#"{{"type":"resize","cols":{cols},"rows":{rows}}}"#
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binary_frame_is_output() {
        match handle_incoming(Message::Binary(b"abc".to_vec())) {
            Some(Incoming::Output(b)) => assert_eq!(b, b"abc"),
            _ => panic!("binary should be output"),
        }
    }

    #[test]
    fn session_frame_is_swallowed() {
        let m = Message::Text(r#"{"type":"session","id":"s1"}"#.into());
        assert!(handle_incoming(m).is_none());
    }

    #[test]
    fn error_frame_carries_reason() {
        let m = Message::Text(r#"{"type":"error","error":"Missing target node"}"#.into());
        match handle_incoming(m) {
            Some(Incoming::Error(r)) => assert_eq!(r, "Missing target node"),
            _ => panic!("error frame should surface its reason"),
        }
    }

    #[test]
    fn non_json_text_is_displayed() {
        // The backend's "session ended" line is a plain text frame, not JSON.
        let m = Message::Text("\r\n[n04: session ended, exit 255]\r\n".into());
        match handle_incoming(m) {
            Some(Incoming::Output(b)) => assert!(b.starts_with(b"\r\n[n04")),
            _ => panic!("non-JSON text should be shown"),
        }
    }

    #[test]
    fn resize_message_is_well_formed() {
        match resize_message(120, 40) {
            Message::Text(t) => {
                assert_eq!(t, r#"{"type":"resize","cols":120,"rows":40}"#);
            }
            _ => panic!("resize must be a text frame"),
        }
    }
}
