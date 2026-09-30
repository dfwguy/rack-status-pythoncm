// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Admin verbs: catalog CRUD, identity (users/groups), and policy inspection.
//!
//! These are the configuration surfaces — what the product can run and who may run it. They follow
//! the same shape as the other typed verbs (call through [`crate::api::call_json`], render as a
//! table, `--json` for the envelope), built on a handful of generic CRUD helpers so a new collection
//! is a few lines rather than a new pattern.

use anyhow::{bail, Result};
use nvrs_client::Method;
use serde_json::Value;

use crate::api::call_json;
use crate::config::{Config, Profile};
use crate::output::{self, Format};

/// A resolved profile plus its name, threaded through every call. Cuts the repetition of passing
/// three arguments to every helper.
pub struct Ctx<'a> {
    pub cfg: &'a Config,
    pub name: &'a str,
    pub profile: &'a Profile,
    pub format: Format,
}

impl Ctx<'_> {
    async fn get(&self, path: &str) -> Result<Value> {
        call_json(
            self.cfg,
            self.name,
            self.profile,
            Method::GET,
            path,
            &[],
            None,
            None,
        )
        .await
    }

    async fn send(&self, method: Method, path: &str, body: Option<&Value>) -> Result<Value> {
        call_json(
            self.cfg,
            self.name,
            self.profile,
            method,
            path,
            &[],
            body,
            None,
        )
        .await
    }

    /// GET a list endpoint and render the array under `key` as a table.
    async fn list(&self, path: &str, key: &str, empty: &str) -> Result<()> {
        let value = self.get(path).await?;
        let arr = value.get(key).cloned().unwrap_or(Value::Array(vec![]));
        output::print_array_table(&arr, empty, self.format);
        Ok(())
    }

    /// GET one resource and print the object under `key` (or the whole body if absent).
    async fn item(&self, path: &str, key: &str) -> Result<()> {
        let value = self.get(path).await?;
        let item = value.get(key).cloned().unwrap_or(value);
        output::print_value(&item, self.format);
        Ok(())
    }

    /// Mutate (POST/PUT/DELETE), printing the envelope under `--json` or a confirmation otherwise.
    async fn mutate(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
        confirmation: &str,
    ) -> Result<()> {
        let value = self.send(method, path, body.as_ref()).await?;
        if self.format == Format::Text {
            println!("{confirmation}");
        } else {
            output::print_value(&value, self.format);
        }
        Ok(())
    }
}

// ─── catalog ─────────────────────────────────────────────────────────────────

/// The catalog collections a CLI verb may address, and their list-response key.
fn catalog_key(kind: &str) -> Result<&'static str> {
    match kind {
        "platforms" | "tools" | "projects" => Ok("items"),
        other => bail!("unknown catalog kind {other:?}; expected platforms, tools or projects"),
    }
}

pub async fn catalog_list(ctx: &Ctx<'_>, kind: &str) -> Result<()> {
    let key = catalog_key(kind)?;
    ctx.list(&format!("/api/catalog/{kind}"), key, &format!("no {kind}"))
        .await
}

pub async fn catalog_get(ctx: &Ctx<'_>, kind: &str, id: &str) -> Result<()> {
    catalog_key(kind)?;
    ctx.item(&format!("/api/catalog/{kind}/{id}"), "item").await
}

pub async fn catalog_create(
    ctx: &Ctx<'_>,
    kind: &str,
    fields: &[String],
    body: Option<&str>,
) -> Result<()> {
    catalog_key(kind)?;
    let body = crate::api::build_body(fields, body)?;
    ctx.mutate(
        Method::POST,
        &format!("/api/catalog/{kind}"),
        body,
        &format!("created {kind} entry"),
    )
    .await
}

pub async fn catalog_update(
    ctx: &Ctx<'_>,
    kind: &str,
    id: &str,
    fields: &[String],
    body: Option<&str>,
) -> Result<()> {
    catalog_key(kind)?;
    let body = crate::api::build_body(fields, body)?;
    ctx.mutate(
        Method::PUT,
        &format!("/api/catalog/{kind}/{id}"),
        body,
        &format!("updated {id}"),
    )
    .await
}

pub async fn catalog_delete(ctx: &Ctx<'_>, kind: &str, id: &str) -> Result<()> {
    catalog_key(kind)?;
    ctx.mutate(
        Method::DELETE,
        &format!("/api/catalog/{kind}/{id}"),
        None,
        &format!("deleted {id}"),
    )
    .await
}

// ─── identity: users ─────────────────────────────────────────────────────────

pub async fn users_list(ctx: &Ctx<'_>) -> Result<()> {
    ctx.list("/api/identity/users", "users", "no users").await
}

pub async fn users_get(ctx: &Ctx<'_>, login: &str) -> Result<()> {
    ctx.item(&format!("/api/identity/users/{login}"), "user")
        .await
}

pub async fn users_create(ctx: &Ctx<'_>, fields: &[String], body: Option<&str>) -> Result<()> {
    let body = crate::api::build_body(fields, body)?;
    ctx.mutate(Method::POST, "/api/identity/users", body, "created user")
        .await
}

pub async fn users_set_disabled(ctx: &Ctx<'_>, login: &str, disabled: bool) -> Result<()> {
    let body = serde_json::json!({ "disabled": disabled });
    let word = if disabled { "disabled" } else { "enabled" };
    ctx.mutate(
        Method::PUT,
        &format!("/api/identity/users/{login}/disabled"),
        Some(body),
        &format!("{word} {login}"),
    )
    .await
}

pub async fn users_delete(ctx: &Ctx<'_>, login: &str) -> Result<()> {
    ctx.mutate(
        Method::DELETE,
        &format!("/api/identity/users/{login}"),
        None,
        &format!("deleted {login}"),
    )
    .await
}

// ─── identity: groups ────────────────────────────────────────────────────────

pub async fn groups_list(ctx: &Ctx<'_>) -> Result<()> {
    ctx.list("/api/identity/groups", "groups", "no groups")
        .await
}

pub async fn groups_create(ctx: &Ctx<'_>, name: &str, description: Option<String>) -> Result<()> {
    let mut obj = serde_json::Map::new();
    obj.insert("name".into(), Value::String(name.to_string()));
    if let Some(d) = description {
        obj.insert("description".into(), Value::String(d));
    }
    ctx.mutate(
        Method::POST,
        "/api/identity/groups",
        Some(Value::Object(obj)),
        &format!("created group {name}"),
    )
    .await
}

pub async fn groups_delete(ctx: &Ctx<'_>, name: &str) -> Result<()> {
    ctx.mutate(
        Method::DELETE,
        &format!("/api/identity/groups/{name}"),
        None,
        &format!("deleted group {name}"),
    )
    .await
}

pub async fn group_add_member(ctx: &Ctx<'_>, group: &str, login: &str) -> Result<()> {
    let body = serde_json::json!({ "login": login });
    ctx.mutate(
        Method::POST,
        &format!("/api/identity/groups/{group}/members"),
        Some(body),
        &format!("added {login} to {group}"),
    )
    .await
}

pub async fn group_remove_member(ctx: &Ctx<'_>, group: &str, login: &str) -> Result<()> {
    ctx.mutate(
        Method::DELETE,
        &format!("/api/identity/groups/{group}/members/{login}"),
        None,
        &format!("removed {login} from {group}"),
    )
    .await
}

// ─── policy ──────────────────────────────────────────────────────────────────

pub async fn policy_list(ctx: &Ctx<'_>) -> Result<()> {
    ctx.list("/api/policies", "policies", "no policies").await
}

/// `nvrs policy preview [--principal X]` — every declared action with allow/deny for a principal
/// (the caller's own by default). This is the machine-readable "what am I allowed to do".
pub async fn policy_preview(ctx: &Ctx<'_>, principal: Option<&str>) -> Result<()> {
    let mut query: Vec<(String, String)> = vec![("format".into(), "json".into())];
    if let Some(p) = principal {
        query.push(("principal".into(), p.to_string()));
    }
    let value = call_json(
        ctx.cfg,
        ctx.name,
        ctx.profile,
        Method::GET,
        "/api/policies/preview",
        &query,
        None,
        None,
    )
    .await?;
    let actions = value
        .get("actions")
        .cloned()
        .unwrap_or(Value::Array(vec![]));
    output::print_array_table(&actions, "no actions", ctx.format);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_kind_is_validated() {
        assert!(catalog_key("platforms").is_ok());
        assert!(catalog_key("tools").is_ok());
        assert!(catalog_key("projects").is_ok());
        // A typo is an error with an actionable message, not a request to a bogus path.
        let err = catalog_key("platform").unwrap_err().to_string();
        assert!(err.contains("platforms"), "{err}");
    }
}
