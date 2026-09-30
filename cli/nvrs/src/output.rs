// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//! Rendering: human tables by default, or structured `--output json|yaml` for scripts.
//!
//! Two audiences, one dataset. An operator reading a terminal wants aligned columns; a script piping
//! into `jq` or `yq` wants the exact envelope. [`Format::Text`] renders tables and human views;
//! [`Format::Json`] is compact (one line, pipe-friendly); [`Format::Yaml`] is block YAML. The table
//! renderers fall back to structured output automatically when the format is not text, so every verb
//! honours `--output` without each one re-implementing the choice.

use comfy_table::{presets::UTF8_FULL, Table};
use serde_json::Value;

use crate::config::{Config, Profile};

/// How to render output. The global `-o/--output` selects it; `Text` is the default.
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq, clap::ValueEnum)]
pub enum Format {
    /// Human-oriented: tables and formatted views.
    #[default]
    Text,
    /// Compact single-line JSON — the exact envelope, for piping into `jq`.
    Json,
    /// Block YAML.
    Yaml,
}

/// Serialise `value` for a structured format, or `None` for [`Format::Text`] (where the caller
/// renders a table or human view instead). The single place json/yaml encoding lives, so every verb
/// agrees on it.
pub fn structured(value: &Value, format: Format) -> Option<String> {
    match format {
        Format::Text => None,
        Format::Json => Some(serde_json::to_string(value).unwrap_or_default()),
        Format::Yaml => Some(
            serde_yaml::to_string(value)
                .unwrap_or_default()
                .trim_end()
                .to_string(),
        ),
    }
}

/// Print a single value. `Text` pretty-prints the JSON (indented, for reading); `Json`/`Yaml` emit
/// the structured form. Used by verbs that show one object rather than a table.
pub fn print_value(value: &Value, format: Format) {
    match structured(value, format) {
        Some(s) => println!("{s}"),
        None => println!(
            "{}",
            serde_json::to_string_pretty(value).unwrap_or_else(|_| value.to_string())
        ),
    }
}

/// Render a list of JSON objects as a table over the given columns, taking each column's value from
/// that key in each row (missing keys render blank). The single place the typed verbs turn rows into
/// aligned output, so they all look the same. Empty input prints `empty` rather than a headerless box.
pub fn print_table(columns: &[&str], rows: &[&serde_json::Map<String, Value>], empty: &str) {
    if rows.is_empty() {
        println!("{empty}");
        return;
    }
    let mut table = Table::new();
    table.load_preset(UTF8_FULL);
    table.set_header(columns.iter().map(|c| c.to_string()).collect::<Vec<_>>());
    for row in rows {
        table.add_row(
            columns
                .iter()
                .map(|c| cell(row.get(*c)))
                .collect::<Vec<_>>(),
        );
    }
    println!("{table}");
}

/// Render a JSON array of objects as a table, inferring columns from the scalar-valued keys that
/// appear across the rows (sorted, so output is stable). For a list whose shape the CLI does not
/// model explicitly — generated reports, say — this shows the data honestly without a hand-picked
/// schema. Falls back to pretty JSON when the value is not an array of objects.
pub fn print_array_table(value: &Value, empty: &str, format: Format) {
    if let Some(s) = structured(value, format) {
        println!("{s}");
        return;
    }
    let Some(items) = value.as_array() else {
        print_value(value, format);
        return;
    };
    let maps: Vec<&serde_json::Map<String, Value>> =
        items.iter().filter_map(Value::as_object).collect();
    if maps.len() != items.len() || maps.is_empty() {
        // Not a clean array of objects (or empty) — don't invent a table.
        if items.is_empty() {
            println!("{empty}");
        } else {
            print_value(value, format);
        }
        return;
    }
    let columns = infer_columns(&maps);
    let column_refs: Vec<&str> = columns.iter().map(String::as_str).collect();
    print_table(&column_refs, &maps, empty);
}

/// The union of every key across the rows, sorted for stable output. Complex values are kept (they
/// render as compact JSON via [`cell`]) rather than dropped, so no field is hidden.
fn infer_columns(maps: &[&serde_json::Map<String, Value>]) -> Vec<String> {
    let mut columns: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for m in maps {
        for k in m.keys() {
            columns.insert(k.clone());
        }
    }
    columns.into_iter().collect()
}

/// Render one JSON value as a compact table cell: scalars as themselves (no quotes on strings),
/// null/absent as blank, and a nested object/array as its compact JSON so no data is silently lost.
pub fn cell(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => b.to_string(),
        Some(Value::Number(n)) => n.to_string(),
        Some(other) => serde_json::to_string(other).unwrap_or_default(),
    }
}

/// The `nvrs config list` view.
pub fn print_profiles(cfg: &Config, format: Format) {
    if format != Format::Text {
        let value = serde_json::to_value(cfg).unwrap_or(Value::Null);
        println!("{}", structured(&value, format).unwrap_or_default());
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
pub fn print_profile(name: &str, profile: &Profile, format: Format) {
    if format != Format::Text {
        let value = serde_json::json!({ "name": name, "profile": profile });
        println!("{}", structured(&value, format).unwrap_or_default());
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn cell_renders_scalars_without_quotes_and_complex_as_json() {
        assert_eq!(cell(None), "");
        assert_eq!(cell(Some(&Value::Null)), "");
        assert_eq!(cell(Some(&json!("p17-r01"))), "p17-r01");
        assert_eq!(cell(Some(&json!(true))), "true");
        assert_eq!(cell(Some(&json!(42))), "42");
        assert_eq!(cell(Some(&json!(["a", "b"]))), r#"["a","b"]"#);
    }

    #[test]
    fn format_default_is_text() {
        assert_eq!(Format::default(), Format::Text);
    }

    #[test]
    fn structured_encodes_json_and_yaml_and_skips_text() {
        let v = json!({"id": "gb200", "ok": true});
        // Text has no structured form — the caller renders a table/human view.
        assert_eq!(structured(&v, Format::Text), None);
        // JSON is compact, one line.
        assert_eq!(
            structured(&v, Format::Json).unwrap(),
            r#"{"id":"gb200","ok":true}"#
        );
        // YAML is block form with the same data, no trailing newline.
        let yaml = structured(&v, Format::Yaml).unwrap();
        assert!(yaml.contains("id: gb200"), "{yaml}");
        assert!(yaml.contains("ok: true"), "{yaml}");
        assert!(!yaml.ends_with('\n'), "trailing newline should be trimmed");
    }

    #[test]
    fn columns_are_the_sorted_union_across_rows() {
        let a = json!({"id": 1, "kind": "x"});
        let b = json!({"id": 2, "racks": ["r1"]});
        let maps: Vec<&serde_json::Map<String, Value>> =
            vec![a.as_object().unwrap(), b.as_object().unwrap()];
        // Union of {id, kind} and {id, racks}, sorted; complex `racks` kept, not dropped.
        assert_eq!(infer_columns(&maps), vec!["id", "kind", "racks"]);
    }
}
