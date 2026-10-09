// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Where the session tokens live between invocations.
//!
//! A `0600` file under the state dir, keyed by profile name, holding the `nvis_access` /
//! `nvis_refresh` pair the login exchange returned. A file rather than the OS keyring for one
//! deliberate reason: operators run this over ssh, often headless, where no Secret Service / Keychain
//! is reachable — a keyring-only store would simply fail there. The trust model is the same as the
//! ssh private key sitting next to it in `~/.ssh`: file permissions on the operator's own account. A
//! keyring backend can be added later behind this same interface without changing callers.
//!
//! The file is separate from `config.toml` (which is safe to read and diff) precisely because this
//! one is not: it holds bearer credentials.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::AuthError;

/// The access + refresh token pair for one deployment.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tokens {
    pub access: String,
    pub refresh: String,
}

/// The whole token file: one entry per profile.
#[derive(Debug, Default, Serialize, Deserialize)]
struct TokenFile {
    #[serde(default)]
    accounts: BTreeMap<String, Tokens>,
}

/// A handle to the on-disk token store.
pub struct TokenStore {
    path: PathBuf,
}

impl TokenStore {
    /// The default store, honouring `NVRS_STATE` (a full file path) then `$XDG_STATE_HOME`, then
    /// `~/.local/state`.
    pub fn open_default() -> Result<Self, AuthError> {
        Ok(Self {
            path: default_path()?,
        })
    }

    /// A store at an explicit path — used by tests and by anyone wanting to override placement.
    pub fn at(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn load(&self) -> Result<TokenFile, AuthError> {
        match fs::read_to_string(&self.path) {
            Ok(text) => serde_json::from_str(&text)
                .map_err(|e| AuthError::Store(format!("parsing {}: {e}", self.path.display()))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(TokenFile::default()),
            Err(e) => Err(AuthError::Store(format!(
                "reading {}: {e}",
                self.path.display()
            ))),
        }
    }

    fn write(&self, file: &TokenFile) -> Result<(), AuthError> {
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir)
                .map_err(|e| AuthError::Store(format!("creating {}: {e}", dir.display())))?;
        }
        let text = serde_json::to_string_pretty(file)
            .map_err(|e| AuthError::Store(format!("serialising tokens: {e}")))?;
        fs::write(&self.path, text)
            .map_err(|e| AuthError::Store(format!("writing {}: {e}", self.path.display())))?;
        restrict_permissions(&self.path)?;
        Ok(())
    }

    /// The tokens for a profile, if any are stored.
    pub fn get(&self, account: &str) -> Result<Option<Tokens>, AuthError> {
        Ok(self.load()?.accounts.remove(account))
    }

    /// Store (or replace) the tokens for a profile.
    pub fn set(&self, account: &str, tokens: Tokens) -> Result<(), AuthError> {
        let mut file = self.load()?;
        file.accounts.insert(account.to_string(), tokens);
        self.write(&file)
    }

    /// Update only the access token for a profile, keeping the refresh token — the shape of a
    /// successful refresh. Errors if the profile has no stored session.
    pub fn set_access(&self, account: &str, access: String) -> Result<(), AuthError> {
        let mut file = self.load()?;
        let entry = file
            .accounts
            .get_mut(account)
            .ok_or_else(|| AuthError::Store(format!("no stored session for {account:?}")))?;
        entry.access = access;
        self.write(&file)
    }

    /// Forget a profile's tokens (logout). Silent if there were none.
    pub fn remove(&self, account: &str) -> Result<(), AuthError> {
        let mut file = self.load()?;
        if file.accounts.remove(account).is_some() {
            self.write(&file)?;
        }
        Ok(())
    }
}

fn default_path() -> Result<PathBuf, AuthError> {
    if let Some(p) = std::env::var_os("NVRS_STATE") {
        if !p.is_empty() {
            return Ok(PathBuf::from(p));
        }
    }
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local").join("state")))
        .ok_or_else(|| {
            AuthError::Store(
                "cannot locate a state directory: set HOME, XDG_STATE_HOME or NVRS_STATE".into(),
            )
        })?;
    Ok(base.join("nvrs").join("tokens.json"))
}

#[cfg(unix)]
fn restrict_permissions(path: &Path) -> Result<(), AuthError> {
    use std::os::unix::fs::PermissionsExt;
    // 0600: this file holds bearer credentials, so it must not be group/other readable. Set after
    // the write, matching the trust model of an ssh private key.
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .map_err(|e| AuthError::Store(format!("chmod 600 {}: {e}", path.display())))
}

#[cfg(not(unix))]
fn restrict_permissions(_path: &Path) -> Result<(), AuthError> {
    // On Windows the ACL from the user profile directory governs access; nothing extra to do here.
    Ok(())
}

/// Pull one cookie's value out of a set of `Set-Cookie` header lines. A header looks like
/// `nvis_access=<value>; Path=/; HttpOnly; SameSite=Lax` — we want the value up to the first `;`.
pub fn token_from_cookies(set_cookies: &[String], name: &str) -> Option<String> {
    let prefix = format!("{name}=");
    for line in set_cookies {
        if let Some(rest) = line.trim_start().strip_prefix(&prefix) {
            let value = rest.split(';').next().unwrap_or("").trim();
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// A token's own `exp` claim, read **without verifying the signature**.
///
/// For diagnosis only — saying "this refresh token had six days left" when the server refuses it.
/// Nothing may be *decided* on this value: the server is the only party that can verify the token,
/// and an unverified claim is whatever the bytes say. Returns `None` for anything that is not a
/// three-segment JWT with a numeric `exp`.
pub fn unverified_expiry(token: &str) -> Option<std::time::SystemTime> {
    use base64::Engine as _;
    let mut parts = token.split('.');
    let (_header, payload, _sig) = (parts.next()?, parts.next()?, parts.next()?);
    if parts.next().is_some() {
        return None;
    }
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(payload.trim_end_matches('='))
        .ok()?;
    let exp = serde_json::from_slice::<serde_json::Value>(&bytes)
        .ok()?
        .get("exp")?
        .as_f64()?;
    if !exp.is_finite() || exp < 0.0 {
        return None;
    }
    Some(std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(exp))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn jwt_with_payload(json: &str) -> String {
        use base64::Engine as _;
        let b64 = base64::engine::general_purpose::URL_SAFE_NO_PAD;
        format!(
            "{}.{}.sig",
            b64.encode(r#"{"alg":"ES256"}"#),
            b64.encode(json)
        )
    }

    #[test]
    fn expiry_is_read_from_the_payload() {
        let t = jwt_with_payload(r#"{"exp":1760000000.25,"sub":"user:op"}"#);
        let got = unverified_expiry(&t).unwrap();
        assert_eq!(
            got.duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis(),
            1_760_000_000_250
        );
    }

    #[test]
    fn expiry_of_a_non_jwt_is_none() {
        assert!(unverified_expiry("opaque").is_none());
        assert!(unverified_expiry("a.b").is_none());
        assert!(unverified_expiry("a.b.c.d").is_none());
        assert!(unverified_expiry(&jwt_with_payload(r#"{"sub":"user:op"}"#)).is_none());
        assert!(unverified_expiry(&jwt_with_payload(r#"{"exp":"soon"}"#)).is_none());
    }

    fn store() -> (tempfile::TempDir, TokenStore) {
        let dir = tempfile::tempdir().unwrap();
        let store = TokenStore::at(dir.path().join("sub").join("tokens.json"));
        (dir, store)
    }

    #[test]
    fn set_get_remove_roundtrip() {
        let (_d, s) = store();
        assert_eq!(s.get("mayo").unwrap(), None);

        let t = Tokens {
            access: "acc".into(),
            refresh: "ref".into(),
        };
        s.set("mayo", t.clone()).unwrap();
        assert_eq!(s.get("mayo").unwrap(), Some(t));

        s.remove("mayo").unwrap();
        assert_eq!(s.get("mayo").unwrap(), None);
        // Removing again is silent.
        s.remove("mayo").unwrap();
    }

    #[test]
    fn accounts_are_independent() {
        let (_d, s) = store();
        s.set(
            "mayo",
            Tokens {
                access: "a1".into(),
                refresh: "r1".into(),
            },
        )
        .unwrap();
        s.set(
            "boston",
            Tokens {
                access: "a2".into(),
                refresh: "r2".into(),
            },
        )
        .unwrap();
        assert_eq!(s.get("mayo").unwrap().unwrap().access, "a1");
        assert_eq!(s.get("boston").unwrap().unwrap().refresh, "r2");
    }

    #[test]
    fn set_access_keeps_refresh() {
        let (_d, s) = store();
        s.set(
            "mayo",
            Tokens {
                access: "old".into(),
                refresh: "keep".into(),
            },
        )
        .unwrap();
        s.set_access("mayo", "new".into()).unwrap();
        let t = s.get("mayo").unwrap().unwrap();
        assert_eq!(t.access, "new");
        assert_eq!(t.refresh, "keep");
    }

    #[test]
    fn set_access_without_session_errors() {
        let (_d, s) = store();
        assert!(s.set_access("ghost", "x".into()).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn token_file_is_0600() {
        use std::os::unix::fs::PermissionsExt;
        let (_d, s) = store();
        s.set(
            "mayo",
            Tokens {
                access: "a".into(),
                refresh: "r".into(),
            },
        )
        .unwrap();
        let mode = fs::metadata(s.path()).unwrap().permissions().mode();
        assert_eq!(
            mode & 0o777,
            0o600,
            "token file must not be group/other readable"
        );
    }

    #[test]
    fn cookie_value_is_extracted() {
        let cookies = vec![
            "nvis_access=eyJhbGc.abc; Path=/; HttpOnly; SameSite=Lax".to_string(),
            "nvis_refresh=eyJy.def; Path=/api/auth; HttpOnly".to_string(),
        ];
        assert_eq!(
            token_from_cookies(&cookies, "nvis_access").as_deref(),
            Some("eyJhbGc.abc")
        );
        assert_eq!(
            token_from_cookies(&cookies, "nvis_refresh").as_deref(),
            Some("eyJy.def")
        );
        assert_eq!(token_from_cookies(&cookies, "nvis_missing"), None);
    }

    #[test]
    fn empty_cookie_value_is_none() {
        // A logout sends `nvis_access=; ...` to clear it; that is not a usable token.
        let cookies = vec!["nvis_access=; Path=/; Max-Age=0".to_string()];
        assert_eq!(token_from_cookies(&cookies, "nvis_access"), None);
    }
}
