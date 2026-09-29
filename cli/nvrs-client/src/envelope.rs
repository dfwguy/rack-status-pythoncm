// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! The Rack Status JSON envelope.
//!
//! Every `/api/*` response is `{"ok": true, ...payload}` or `{"ok": false, "error": "..."}`
//! (`docs/API_RESPONSE_CODES.md` §1). Domain fields sit at the *top level* beside `ok`, not under a
//! `data` key, so we keep the whole object and only lift `ok`/`error` out.
//!
//! **The one wrinkle worth encoding:** the catalog blueprint answers errors as
//! `{"ok": false, "errors": ["...", "..."]}` — plural, a list — while everywhere else it is
//! `error`, a single string (`backend/catalog/api.py:50`). A decoder that knows only `error` would
//! render a catalog validation failure as an empty message. `extract_error` reads both.

use serde_json::Value;

/// The message carried by a `{"ok": false, ...}` body, from either the `error` string or the
/// catalog's `errors` list. Returns `None` when neither field is present (or the body is not an
/// object), so the caller can fall back to the raw text.
pub fn extract_error(body: &Value) -> Option<String> {
    let obj = body.as_object()?;
    if let Some(Value::String(s)) = obj.get("error") {
        return Some(s.clone());
    }
    if let Some(Value::Array(items)) = obj.get("errors") {
        let joined: Vec<String> = items
            .iter()
            .map(|v| match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
            .collect();
        if !joined.is_empty() {
            return Some(joined.join("; "));
        }
    }
    None
}

/// Whether a decoded body declares success. A body with no `ok` key is treated as success: some
/// read endpoints (`/api/status`) return a bare payload, and the HTTP status already gated us here.
pub fn is_ok(body: &Value) -> bool {
    match body.as_object().and_then(|o| o.get("ok")) {
        Some(Value::Bool(b)) => *b,
        _ => true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn singular_error_field_is_read() {
        let v = json!({"ok": false, "error": "job_id is required"});
        assert_eq!(extract_error(&v).as_deref(), Some("job_id is required"));
        assert!(!is_ok(&v));
    }

    #[test]
    fn catalog_plural_errors_are_joined() {
        // backend/catalog/api.py returns a list under `errors`; a decoder that only knew `error`
        // would drop the whole message.
        let v = json!({"ok": false, "errors": ["name is required", "id must be a slug"]});
        assert_eq!(
            extract_error(&v).as_deref(),
            Some("name is required; id must be a slug")
        );
    }

    #[test]
    fn payload_without_ok_is_success() {
        let v = json!({"racks": [], "generated_at": 1});
        assert!(is_ok(&v));
        assert_eq!(extract_error(&v), None);
    }

    #[test]
    fn ok_true_with_payload() {
        let v = json!({"ok": true, "runs": [1, 2, 3]});
        assert!(is_ok(&v));
    }

    #[test]
    fn non_object_bodies_do_not_panic() {
        // A bare array or string is not the envelope shape; treat it as success with no error rather
        // than indexing into something that is not a map.
        for v in [json!([1, 2, 3]), json!("just text"), json!(42), json!(null)] {
            assert!(is_ok(&v), "{v} should be treated as ok");
            assert_eq!(extract_error(&v), None, "{v} has no error field");
        }
    }

    #[test]
    fn empty_errors_list_falls_through_to_none() {
        // An `errors: []` carries no message; the caller should fall back to the raw body, not print
        // an empty string.
        let v = json!({"ok": false, "errors": []});
        assert_eq!(extract_error(&v), None);
    }

    #[test]
    fn error_field_wins_over_errors_list() {
        // Both present is not a shape the server emits, but the singular `error` is the documented
        // field, so prefer it deterministically rather than depending on map order.
        let v = json!({"ok": false, "error": "singular", "errors": ["plural"]});
        assert_eq!(extract_error(&v).as_deref(), Some("singular"));
    }
}
