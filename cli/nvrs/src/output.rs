// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Rendering: human tables by default, raw JSON under `--json`.
//!
//! Two audiences, one dataset. An operator reading a terminal wants aligned columns; a script piping
//! into `jq` wants the exact envelope. `--json` is compact (one line, pipe-friendly); the default
//! JSON view is pretty-printed for reading.

use comfy_table::{presets::UTF8_FULL, Table};
use serde_json::Value;

use crate::config::{Config, Profile};

/// Print an API JSON value. `--json` → compact single line; otherwise pretty, indented.
pub fn print_json(value: &Value, json: bool) {
    if json {
        println!("{}", serde_json::to_string(value).unwrap_or_default());
    } else {
        println!(
            "{}",
            serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string())
        );
    }
}

/// The `nvrs config list` view.
pub fn print_profiles(cfg: &Config, json: bool) {
    if json {
        println!("{}", serde_json::to_string(cfg).unwrap_or_default());
        return;
    }
    if cfg.profiles.is_empty() {
        println!("no profiles configured — add one with `nvrs config set-url <name> <url>`");
        return;
    }
    let mut table = Table::new();
    table.load_preset(UTF8_FULL);
    table.set_header(vec!["", "profile", "base URL", "tls", "operator"]);
    for (name, p) in &cfg.profiles {
        let active = if cfg.active.as_deref() == Some(name.as_str()) {
            "*"
        } else {
            ""
        };
        table.add_row(vec![
            active.to_string(),
            name.clone(),
            p.base_url.clone(),
            p.tls.describe(),
            p.operator.clone().unwrap_or_default(),
        ]);
    }
    println!("{table}");
}

/// The `nvrs config show` view for a single profile.
pub fn print_profile(name: &str, profile: &Profile, json: bool) {
    if json {
        println!(
            "{}",
            serde_json::json!({ "name": name, "profile": profile })
        );
        return;
    }
    println!("profile:  {name}");
    println!("base URL: {}", profile.base_url);
    println!("tls:      {}", profile.tls.describe());
    println!(
        "operator: {}",
        profile.operator.as_deref().unwrap_or("(unset)")
    );
}
