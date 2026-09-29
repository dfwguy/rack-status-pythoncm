// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Profiles: one named deployment per cluster, and the active one.
//!
//! **Profiles are a safety feature, not a convenience.** The clusters (Mayo / Firmus / Boston …)
//! must never be mixed (`.cursor/rules/cluster-deployments.mdc`), and the surest way to run a drain
//! on the wrong fleet is a single ambient endpoint that quietly points somewhere else. A profile
//! names a base URL, its TLS trust, and the operator label; the active profile is explicit and every
//! mutating command is expected to echo it back before acting.
//!
//! Tokens live in the OS keyring (Phase 1), never here — this file is safe to read and diff.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

/// The whole `config.toml`.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct Config {
    /// The profile used when `--profile` is not given. `None` until the first `nvrs config use`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub active: Option<String>,
    /// Stable per-install id sent as `X-Rack-Client-Id`, mirroring the SPA's `nvis-rack-client-id`.
    /// Generated once and persisted so rack-lock attribution is consistent across invocations.
    #[serde(default)]
    pub client_id: String,
    #[serde(default)]
    pub profiles: BTreeMap<String, Profile>,
}

/// A single deployment target.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Profile {
    /// Origin only, e.g. `https://localhost:5000` (typically an ssh-tunnelled local port).
    pub base_url: String,
    /// How to verify the deployment's certificate. Defaults to the OS trust store.
    #[serde(default)]
    pub tls: TlsSetting,
    /// Optional human label for `X-Rack-Operator`. Attribution only; the token's principal is
    /// authoritative server-side.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub operator: Option<String>,
}

/// Serialised TLS policy. A bare string (`"system"` / `"insecure"`) or a `{ ca_cert = "..." }` table,
/// so the common cases stay terse and the pinned-cert case is still expressible.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum TlsSetting {
    Named(String),
    CaCert { ca_cert: String },
}

impl Default for TlsSetting {
    fn default() -> Self {
        TlsSetting::Named("system".into())
    }
}

impl TlsSetting {
    /// Resolve into the transport's trust policy, reading a pinned cert from disk if named.
    pub fn to_trust(&self) -> Result<nvrs_client::TlsTrust> {
        match self {
            TlsSetting::Named(s) => match s.as_str() {
                "system" => Ok(nvrs_client::TlsTrust::SystemRoots),
                "insecure" => Ok(nvrs_client::TlsTrust::Insecure),
                other => bail!(
                    "unknown tls setting {other:?}; expected \"system\", \"insecure\", or a {{ ca_cert = \"...\" }} table"
                ),
            },
            TlsSetting::CaCert { ca_cert } => {
                let pem = fs::read(ca_cert)
                    .with_context(|| format!("reading pinned CA cert {ca_cert}"))?;
                Ok(nvrs_client::TlsTrust::AddRootPem(pem))
            }
        }
    }

    /// A short human description for `nvrs config list`.
    pub fn describe(&self) -> String {
        match self {
            TlsSetting::Named(s) => s.clone(),
            TlsSetting::CaCert { ca_cert } => format!("ca_cert: {ca_cert}"),
        }
    }
}

impl Config {
    /// Where the config lives. `NVRS_CONFIG` overrides; otherwise `$XDG_CONFIG_HOME/nvrs/config.toml`
    /// (or `~/.config/nvrs/config.toml`). Chosen over the platform "app data" dir so the path is the
    /// same one the docs name on every OS an operator is likely to use.
    pub fn path() -> Result<PathBuf> {
        if let Some(p) = std::env::var_os("NVRS_CONFIG") {
            if !p.is_empty() {
                return Ok(PathBuf::from(p));
            }
        }
        let base = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .filter(|p| !p.as_os_str().is_empty())
            .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
            .context(
                "cannot locate a config directory: set HOME or XDG_CONFIG_HOME or NVRS_CONFIG",
            )?;
        Ok(base.join("nvrs").join("config.toml"))
    }

    /// Load from `path()`, returning a fresh (empty) config if the file does not exist yet. A
    /// missing file is the first-run state, not an error.
    pub fn load() -> Result<Self> {
        Self::load_from(&Self::path()?)
    }

    pub fn load_from(path: &Path) -> Result<Self> {
        match fs::read_to_string(path) {
            Ok(text) => {
                let mut cfg: Config =
                    toml::from_str(&text).with_context(|| format!("parsing {}", path.display()))?;
                if cfg.client_id.is_empty() {
                    cfg.client_id = generate_client_id();
                }
                Ok(cfg)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Config {
                client_id: generate_client_id(),
                ..Default::default()
            }),
            Err(e) => Err(e).with_context(|| format!("reading {}", path.display())),
        }
    }

    /// Persist to `path()`, creating the directory. The file holds no secrets (tokens are in the
    /// keyring), so 0644 is fine; we still create the dir privately.
    pub fn save(&self) -> Result<()> {
        self.save_to(&Self::path()?)
    }

    pub fn save_to(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
        }
        let text = toml::to_string_pretty(self).context("serialising config")?;
        fs::write(path, text).with_context(|| format!("writing {}", path.display()))?;
        Ok(())
    }

    /// Resolve which profile to use: the `--profile` override, else the active one. Errors with an
    /// actionable message rather than silently guessing — pointing at the wrong cluster is the whole
    /// risk this guards against.
    pub fn resolve<'a>(&'a self, requested: Option<&str>) -> Result<(String, &'a Profile)> {
        let name: String = match requested {
            Some(n) => n.to_string(),
            None => self
                .active
                .clone()
                .context("no active profile; run `nvrs config use <name>` or pass --profile")?,
        };
        let profile = self.profiles.get(&name).with_context(|| {
            format!("no profile named {name:?}; add one with `nvrs config set-url {name} <url>`")
        })?;
        Ok((name, profile))
    }
}

/// A stable-enough client id: `cli-<epoch_millis>-<pid>`. Persisted after first generation, so the
/// weak entropy at creation time does not matter — it only has to be distinct per install, and it is
/// an advisory attribution label, not a credential.
fn generate_client_id() -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("cli-{millis}-{}", std::process::id())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_file_is_first_run_not_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nope").join("config.toml");
        let cfg = Config::load_from(&path).unwrap();
        assert!(cfg.profiles.is_empty());
        assert!(cfg.active.is_none());
        assert!(
            !cfg.client_id.is_empty(),
            "a client id is minted on first run"
        );
    }

    #[test]
    fn round_trips_through_disk() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");
        let mut cfg = Config {
            client_id: "cli-fixed-1".into(),
            active: Some("mayo".into()),
            ..Default::default()
        };
        cfg.profiles.insert(
            "mayo".into(),
            Profile {
                base_url: "https://localhost:5000".into(),
                tls: TlsSetting::CaCert {
                    ca_cert: "/etc/nvrs/mayo.pem".into(),
                },
                operator: Some("Rafael".into()),
            },
        );
        cfg.save_to(&path).unwrap();

        let back = Config::load_from(&path).unwrap();
        assert_eq!(back.active.as_deref(), Some("mayo"));
        assert_eq!(back.client_id, "cli-fixed-1");
        let p = &back.profiles["mayo"];
        assert_eq!(p.base_url, "https://localhost:5000");
        assert_eq!(p.operator.as_deref(), Some("Rafael"));
        assert!(
            matches!(&p.tls, TlsSetting::CaCert { ca_cert } if ca_cert == "/etc/nvrs/mayo.pem")
        );
    }

    #[test]
    fn resolve_prefers_override_then_active() {
        let mut cfg = Config {
            active: Some("mayo".into()),
            ..Default::default()
        };
        cfg.profiles.insert(
            "mayo".into(),
            Profile {
                base_url: "https://a".into(),
                tls: TlsSetting::default(),
                operator: None,
            },
        );
        cfg.profiles.insert(
            "boston".into(),
            Profile {
                base_url: "https://b".into(),
                tls: TlsSetting::default(),
                operator: None,
            },
        );

        assert_eq!(cfg.resolve(None).unwrap().0, "mayo");
        assert_eq!(cfg.resolve(Some("boston")).unwrap().0, "boston");
        assert!(
            cfg.resolve(Some("firmus")).is_err(),
            "unknown profile is an error, not a guess"
        );
    }

    #[test]
    fn no_active_profile_is_actionable_error() {
        let cfg = Config::default();
        let err = cfg.resolve(None).unwrap_err().to_string();
        assert!(
            err.contains("nvrs config use"),
            "error should tell the user how to fix it: {err}"
        );
    }

    #[test]
    fn tls_named_settings_map_to_trust() {
        assert!(matches!(
            TlsSetting::Named("system".into()).to_trust().unwrap(),
            nvrs_client::TlsTrust::SystemRoots
        ));
        assert!(matches!(
            TlsSetting::Named("insecure".into()).to_trust().unwrap(),
            nvrs_client::TlsTrust::Insecure
        ));
        assert!(TlsSetting::Named("bogus".into()).to_trust().is_err());
    }
}
