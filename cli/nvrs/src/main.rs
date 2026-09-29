// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs` — the NVIDIA Rack Status command-line client.
//!
//! Phase 0: profiles (`nvrs config …`), a generic passthrough (`nvrs api …`) that can reach any
//! `/api/*` endpoint, and a `whoami` that probes the auth gate. Login (SRP) and typed domain verbs
//! land in later phases; the passthrough means every endpoint is usable in the meantime.

mod api;
mod auth;
mod config;
mod output;

use anyhow::Result;
use clap::{Parser, Subcommand};

use config::Config;

/// Top-level CLI. Global flags apply to every subcommand.
#[derive(Parser, Debug)]
#[command(
    name = "nvrs",
    version,
    about = "NVIDIA Rack Status CLI",
    long_about = "Run tests, validations and reports against a Rack Status deployment from the terminal."
)]
struct Cli {
    /// Deployment profile to target. Defaults to the active profile (`nvrs config use`).
    #[arg(long, short = 'p', global = true)]
    profile: Option<String>,

    /// Emit machine-readable JSON instead of formatted tables.
    #[arg(long, global = true)]
    json: bool,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Manage deployment profiles (one per cluster — never mix clusters).
    #[command(subcommand)]
    Config(ConfigCmd),

    /// Call any `/api/*` endpoint directly. The full surface, before typed verbs exist for it.
    Api(api::ApiArgs),

    /// Log in to the active profile over SRP and store the session.
    Login {
        /// Login name. Prompted for if omitted.
        login: Option<String>,
        /// Read the password from stdin instead of prompting (for scripts/CI).
        #[arg(long)]
        password_stdin: bool,
    },

    /// Log out of the active profile: revoke the session and forget it locally.
    Logout,

    /// Report whether the active profile has a valid session (probes `/api/auth-check`).
    Whoami,
}

#[derive(Subcommand, Debug)]
enum ConfigCmd {
    /// Print the path to the config file.
    Path,
    /// List configured profiles and which one is active.
    List,
    /// Show one profile (defaults to the active one).
    Show { name: Option<String> },
    /// Set (or create) a profile's base URL, e.g. https://localhost:5000.
    SetUrl { name: String, url: String },
    /// Set a profile's TLS trust: `system`, `insecure`, or a path to a PEM CA to pin.
    SetTls { name: String, value: String },
    /// Set a profile's operator label (sent as X-Rack-Operator for attribution).
    SetOperator { name: String, operator: String },
    /// Make a profile the active default.
    Use { name: String },
}

fn main() {
    let cli = Cli::parse();
    let code = match run(cli) {
        Ok(()) => 0,
        Err(err) => {
            eprintln!("error: {err:#}");
            exit_code_for(&err)
        }
    };
    std::process::exit(code);
}

/// Map an error to a process exit code so a script can branch on *why* a command failed. A transport
/// error keeps its specific code whether it surfaces bare or wrapped inside an [`nvrs_auth::AuthError`];
/// an auth-specific failure (bad credentials, expired session, impostor server) is "not authorized".
fn exit_code_for(err: &anyhow::Error) -> i32 {
    if let Some(e) = err.downcast_ref::<nvrs_client::Error>() {
        return e.exit_code();
    }
    if let Some(e) = err.downcast_ref::<nvrs_auth::AuthError>() {
        return match e {
            nvrs_auth::AuthError::Client(c) => c.exit_code(),
            nvrs_auth::AuthError::InvalidCredentials
            | nvrs_auth::AuthError::SessionExpired
            | nvrs_auth::AuthError::ServerAuthFailed => 77,
            _ => 1,
        };
    }
    1
}

fn run(cli: Cli) -> Result<()> {
    match cli.command {
        Command::Config(cmd) => run_config(cmd, cli.profile.as_deref(), cli.json),
        Command::Api(args) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(api::run(args, &cfg, &name, profile, cli.json))
        }
        Command::Login {
            login,
            password_stdin,
        } => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(auth::login(&cfg, &name, profile, login, password_stdin))
        }
        Command::Logout => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(auth::logout(&cfg, &name, profile))
        }
        Command::Whoami => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(api::whoami(&cfg, &name, profile, cli.json))
        }
    }
}

/// Drive an async command on a fresh multi-thread runtime. Config-only commands stay synchronous
/// and never build one.
fn block_on<F: std::future::Future<Output = Result<()>>>(fut: F) -> Result<()> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(fut)
}

fn run_config(cmd: ConfigCmd, profile_override: Option<&str>, json: bool) -> Result<()> {
    match cmd {
        ConfigCmd::Path => {
            println!("{}", Config::path()?.display());
            Ok(())
        }
        ConfigCmd::List => {
            let cfg = Config::load()?;
            output::print_profiles(&cfg, json);
            Ok(())
        }
        ConfigCmd::Show { name } => {
            let cfg = Config::load()?;
            // Precedence: the positional name, then the global --profile, then the active profile.
            let requested = name.as_deref().or(profile_override);
            let (name, profile) = cfg.resolve(requested)?;
            output::print_profile(&name, profile, json);
            Ok(())
        }
        ConfigCmd::SetUrl { name, url } => {
            // Validate now, so a typo fails at `set-url` rather than at the first call.
            let parsed =
                url::Url::parse(&url).map_err(|e| anyhow::anyhow!("invalid URL {url:?}: {e}"))?;
            if !matches!(parsed.scheme(), "http" | "https") {
                anyhow::bail!("base URL must be http(s): got {url:?}");
            }
            let mut cfg = Config::load()?;
            let entry = cfg
                .profiles
                .entry(name.clone())
                .or_insert_with(|| config::Profile {
                    base_url: url.clone(),
                    tls: config::TlsSetting::default(),
                    operator: None,
                });
            entry.base_url = url;
            if cfg.active.is_none() {
                // First profile becomes active, so a lone profile "just works".
                cfg.active = Some(name.clone());
            }
            cfg.save()?;
            println!("profile {name:?} base URL set");
            Ok(())
        }
        ConfigCmd::SetTls { name, value } => {
            let setting = match value.as_str() {
                "system" | "insecure" => config::TlsSetting::Named(value),
                path => config::TlsSetting::CaCert {
                    ca_cert: path.to_string(),
                },
            };
            let mut cfg = Config::load()?;
            let profile = cfg
                .profiles
                .get_mut(&name)
                .ok_or_else(|| anyhow::anyhow!("no profile named {name:?}"))?;
            profile.tls = setting;
            cfg.save()?;
            println!("profile {name:?} TLS trust set");
            Ok(())
        }
        ConfigCmd::SetOperator { name, operator } => {
            let mut cfg = Config::load()?;
            let profile = cfg
                .profiles
                .get_mut(&name)
                .ok_or_else(|| anyhow::anyhow!("no profile named {name:?}"))?;
            profile.operator = Some(operator);
            cfg.save()?;
            println!("profile {name:?} operator set");
            Ok(())
        }
        ConfigCmd::Use { name } => {
            let mut cfg = Config::load()?;
            if !cfg.profiles.contains_key(&name) {
                anyhow::bail!(
                    "no profile named {name:?}; create it with `nvrs config set-url {name} <url>`"
                );
            }
            cfg.active = Some(name.clone());
            cfg.save()?;
            println!("active profile is now {name:?}");
            Ok(())
        }
    }
}
