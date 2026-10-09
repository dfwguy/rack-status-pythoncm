// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! `nvrs` — the NVIDIA Rack Status command-line client.
//!
//! Phase 0: profiles (`nvrs config …`), a generic passthrough (`nvrs api …`) that can reach any
//! `/api/*` endpoint, and a `whoami` that probes the auth gate. Login (SRP) and typed domain verbs
//! land in later phases; the passthrough means every endpoint is usable in the meantime.

mod admin;
mod api;
mod auth;
mod config;
mod output;
mod spec;
mod terminal;
mod verbs;

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

    /// Output format: text (tables), json, or yaml.
    #[arg(
        long,
        short = 'o',
        global = true,
        default_value = "text",
        value_name = "FORMAT"
    )]
    output: output::Format,

    /// Shorthand for `--output json`. Overrides --output when set.
    #[arg(long, global = true)]
    json: bool,

    #[command(subcommand)]
    command: Command,
}

impl Cli {
    /// The effective output format: `--json` wins as a convenience shorthand, else `--output`.
    fn format(&self) -> output::Format {
        if self.json {
            output::Format::Json
        } else {
            self.output
        }
    }
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Manage deployment profiles (one per cluster — never mix clusters).
    #[command(subcommand)]
    Config(ConfigCmd),

    /// Call any `/api/*` endpoint directly. The full surface, before typed verbs exist for it.
    Api(api::ApiArgs),

    /// Compute-plane status.
    #[command(subcommand)]
    Compute(ComputeCmd),

    /// Advisory rack locks: see, claim and release racks.
    #[command(subcommand)]
    Rack(RackCmd),

    /// Run a validation suite (submit, and with --follow poll it to completion).
    Run {
        /// Suite to run. Omit to list the available suites.
        suite: Option<String>,
        /// Target rack (added to the request body as `rack`).
        #[arg(long)]
        rack: Option<String>,
        /// Target node/spec (added to the request body as `target`).
        #[arg(long)]
        target: Option<String>,
        /// Extra string body field, repeatable: --field key=value.
        #[arg(long = "field", short = 'f', value_name = "KEY=VALUE")]
        field: Vec<String>,
        /// Raw JSON body: inline or @file. Mutually exclusive with --field.
        #[arg(long)]
        body: Option<String>,
        /// Poll the job to completion, printing phase changes, and exit with its result.
        #[arg(long)]
        follow: bool,
        /// Seconds between polls when following.
        #[arg(long, default_value_t = 3)]
        interval: u64,
    },

    /// Open an interactive shell on a node (or the head node) over the web terminal.
    Terminal {
        /// Target node hostname.
        #[arg(long)]
        node: Option<String>,
        /// Connect to the cluster head node instead of a compute node.
        #[arg(long)]
        headnode: bool,
    },

    /// Tail live logs.
    #[command(subcommand)]
    Logs(LogsCmd),

    /// Reports this deployment has generated.
    #[command(subcommand)]
    Reports(ReportsCmd),

    /// Catalog CRUD: platforms, tools and projects — what the product can run.
    Catalog {
        /// Collection: platforms, tools or projects.
        kind: String,
        #[command(subcommand)]
        op: CrudCmd,
    },

    /// Identity administration: users and groups.
    #[command(subcommand)]
    Identity(IdentityCmd),

    /// Authorization policies and what a principal may do.
    #[command(subcommand)]
    Policy(PolicyCmd),

    /// Browse the deployment's OpenAPI contract: every endpoint, method and summary.
    Spec {
        /// Show only endpoints whose method, path or summary contains this text.
        filter: Option<String>,
        /// Show full details (parameters, tags) for each matching endpoint.
        #[arg(long)]
        details: bool,
        /// Re-fetch from the deployment instead of using the cached copy.
        #[arg(long)]
        refresh: bool,
    },

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

    /// Change the signed-in user's password (re-proves the current one; revokes all sessions).
    Passwd {
        /// Read the current and new password from stdin as two lines (current, then new).
        #[arg(long)]
        password_stdin: bool,
    },

    /// Report whether the active profile has a valid session (probes `/api/auth-check`).
    Whoami {
        /// Also list every action allowed for the current principal.
        #[arg(long)]
        grants: bool,
    },
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

#[derive(Subcommand, Debug)]
enum ComputeCmd {
    /// The main rack status payload.
    Status,
}

#[derive(Subcommand, Debug)]
enum RackCmd {
    /// Show which racks are locked and by whom.
    List,
    /// Claim a rack (requires an operator set on the profile).
    Lock {
        rack: String,
        /// A note recorded with the claim.
        #[arg(long)]
        note: Option<String>,
        /// Seconds until the claim expires.
        #[arg(long)]
        ttl: Option<u64>,
        /// Take the rack even if someone else holds it.
        #[arg(long)]
        force: bool,
    },
    /// Release a rack you hold.
    Unlock { rack: String },
}

#[derive(Subcommand, Debug)]
enum ReportsCmd {
    /// List generated reports.
    List,
}

#[derive(Subcommand, Debug)]
enum CrudCmd {
    /// List entries.
    List,
    /// Show one entry by id.
    Get { id: String },
    /// Create an entry from --field/--body.
    Create {
        #[arg(long = "field", short = 'f', value_name = "KEY=VALUE")]
        field: Vec<String>,
        #[arg(long)]
        body: Option<String>,
    },
    /// Update an entry by id from --field/--body.
    Update {
        id: String,
        #[arg(long = "field", short = 'f', value_name = "KEY=VALUE")]
        field: Vec<String>,
        #[arg(long)]
        body: Option<String>,
    },
    /// Delete an entry by id.
    Delete { id: String },
}

#[derive(Subcommand, Debug)]
enum IdentityCmd {
    /// User administration.
    #[command(subcommand)]
    Users(UserCmd),
    /// Group administration.
    #[command(subcommand)]
    Groups(GroupCmd),
}

#[derive(Subcommand, Debug)]
enum UserCmd {
    List,
    Get {
        login: String,
    },
    Create {
        #[arg(long = "field", short = 'f', value_name = "KEY=VALUE")]
        field: Vec<String>,
        #[arg(long)]
        body: Option<String>,
    },
    /// Disable a user (or re-enable with --enable).
    Disable {
        login: String,
        #[arg(long)]
        enable: bool,
    },
    Delete {
        login: String,
    },
}

#[derive(Subcommand, Debug)]
enum GroupCmd {
    List,
    Create {
        name: String,
        #[arg(long)]
        description: Option<String>,
    },
    Delete {
        name: String,
    },
    /// Add a user to a group.
    AddMember {
        group: String,
        login: String,
    },
    /// Remove a user from a group.
    RemoveMember {
        group: String,
        login: String,
    },
}

#[derive(Subcommand, Debug)]
enum PolicyCmd {
    /// List attached policies.
    List,
    /// Every action with allow/deny for a principal (yours by default).
    Preview {
        #[arg(long)]
        principal: Option<String>,
    },
}

#[derive(Subcommand, Debug)]
enum LogsCmd {
    /// Stream a live `tail -f` of a run log (or a Slurm job's stdout with --job-path).
    Tail {
        /// The log directory the run wrote to.
        #[arg(long)]
        dir: String,
        /// A specific log basename within the directory (NEMO rack log otherwise).
        #[arg(long)]
        file: Option<String>,
        /// A Slurm job stdout path to resolve and tail instead of the rack log.
        #[arg(long)]
        job_path: Option<String>,
    },
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
            | nvrs_auth::AuthError::RefreshRefused { .. }
            | nvrs_auth::AuthError::LogoutRefused { .. }
            | nvrs_auth::AuthError::ServerAuthFailed => 77,
            _ => 1,
        };
    }
    1
}

fn run(cli: Cli) -> Result<()> {
    let format = cli.format();
    match cli.command {
        Command::Config(cmd) => run_config(cmd, cli.profile.as_deref(), format),
        Command::Api(args) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(api::run(args, &cfg, &name, profile, format))
        }
        Command::Compute(ComputeCmd::Status) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(verbs::compute_status(&cfg, &name, profile, format))
        }
        Command::Rack(cmd) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(async {
                match cmd {
                    RackCmd::List => verbs::rack_list(&cfg, &name, profile, format).await,
                    RackCmd::Lock {
                        rack,
                        note,
                        ttl,
                        force,
                    } => {
                        verbs::rack_lock(&cfg, &name, profile, &rack, note, ttl, force, format)
                            .await
                    }
                    RackCmd::Unlock { rack } => {
                        verbs::rack_unlock(&cfg, &name, profile, &rack, format).await
                    }
                }
            })
        }
        Command::Run {
            suite,
            rack,
            target,
            field,
            body,
            follow,
            interval,
        } => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(verbs::run_suite(
                &cfg, &name, profile, suite, rack, target, field, body, follow, interval, format,
            ))
        }
        Command::Terminal { node, headnode } => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(terminal::run(&cfg, &name, profile, node, headnode))
        }
        Command::Logs(LogsCmd::Tail {
            dir,
            file,
            job_path,
        }) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(verbs::logs_tail(&cfg, &name, profile, dir, file, job_path))
        }
        Command::Reports(ReportsCmd::List) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(verbs::reports_list(&cfg, &name, profile, format))
        }
        Command::Catalog { kind, op } => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            let ctx = admin::Ctx {
                cfg: &cfg,
                name: &name,
                profile,
                format,
            };
            block_on(async {
                match op {
                    CrudCmd::List => admin::catalog_list(&ctx, &kind).await,
                    CrudCmd::Get { id } => admin::catalog_get(&ctx, &kind, &id).await,
                    CrudCmd::Create { field, body } => {
                        admin::catalog_create(&ctx, &kind, &field, body.as_deref()).await
                    }
                    CrudCmd::Update { id, field, body } => {
                        admin::catalog_update(&ctx, &kind, &id, &field, body.as_deref()).await
                    }
                    CrudCmd::Delete { id } => admin::catalog_delete(&ctx, &kind, &id).await,
                }
            })
        }
        Command::Identity(cmd) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            let ctx = admin::Ctx {
                cfg: &cfg,
                name: &name,
                profile,
                format,
            };
            block_on(async {
                match cmd {
                    IdentityCmd::Users(UserCmd::List) => admin::users_list(&ctx).await,
                    IdentityCmd::Users(UserCmd::Get { login }) => {
                        admin::users_get(&ctx, &login).await
                    }
                    IdentityCmd::Users(UserCmd::Create { field, body }) => {
                        admin::users_create(&ctx, &field, body.as_deref()).await
                    }
                    IdentityCmd::Users(UserCmd::Disable { login, enable }) => {
                        admin::users_set_disabled(&ctx, &login, !enable).await
                    }
                    IdentityCmd::Users(UserCmd::Delete { login }) => {
                        admin::users_delete(&ctx, &login).await
                    }
                    IdentityCmd::Groups(GroupCmd::List) => admin::groups_list(&ctx).await,
                    IdentityCmd::Groups(GroupCmd::Create { name, description }) => {
                        admin::groups_create(&ctx, &name, description).await
                    }
                    IdentityCmd::Groups(GroupCmd::Delete { name }) => {
                        admin::groups_delete(&ctx, &name).await
                    }
                    IdentityCmd::Groups(GroupCmd::AddMember { group, login }) => {
                        admin::group_add_member(&ctx, &group, &login).await
                    }
                    IdentityCmd::Groups(GroupCmd::RemoveMember { group, login }) => {
                        admin::group_remove_member(&ctx, &group, &login).await
                    }
                }
            })
        }
        Command::Policy(cmd) => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            let ctx = admin::Ctx {
                cfg: &cfg,
                name: &name,
                profile,
                format,
            };
            block_on(async {
                match cmd {
                    PolicyCmd::List => admin::policy_list(&ctx).await,
                    PolicyCmd::Preview { principal } => {
                        admin::policy_preview(&ctx, principal.as_deref()).await
                    }
                }
            })
        }
        Command::Spec {
            filter,
            details,
            refresh,
        } => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(spec::run(
                &cfg, &name, profile, filter, details, refresh, format,
            ))
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
        Command::Passwd { password_stdin } => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(auth::passwd(&cfg, &name, profile, password_stdin))
        }
        Command::Whoami { grants } => {
            let cfg = Config::load()?;
            let (name, profile) = cfg.resolve(cli.profile.as_deref())?;
            block_on(async {
                api::whoami(&cfg, &name, profile, format).await?;
                if grants {
                    let ctx = admin::Ctx {
                        cfg: &cfg,
                        name: &name,
                        profile,
                        format,
                    };
                    admin::policy_preview(&ctx, None).await?;
                }
                Ok(())
            })
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

fn run_config(
    cmd: ConfigCmd,
    profile_override: Option<&str>,
    format: output::Format,
) -> Result<()> {
    match cmd {
        ConfigCmd::Path => {
            println!("{}", Config::path()?.display());
            Ok(())
        }
        ConfigCmd::List => {
            let cfg = Config::load()?;
            output::print_profiles(&cfg, format);
            Ok(())
        }
        ConfigCmd::Show { name } => {
            let cfg = Config::load()?;
            // Precedence: the positional name, then the global --profile, then the active profile.
            let requested = name.as_deref().or(profile_override);
            let (name, profile) = cfg.resolve(requested)?;
            output::print_profile(&name, profile, format);
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
