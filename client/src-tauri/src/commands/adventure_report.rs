//! UID-scoped local adventure daily report reader.
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use super::account_state::{account_keys, read_json, valid_account_key};
use super::utils::get_app_data_dir;

fn report_file_path(root: &Path, key: &str) -> Result<PathBuf, String> {
    if key == "default" {
        return Ok(root.join("config/adventure_reports/default.json"));
    }
    if valid_account_key(key) {
        return Ok(root
            .join("config/adventure_reports/CN")
            .join(format!("{}.json", &key[3..])));
    }
    Err("无效的冒险日报账号标识。".into())
}

fn empty_report(key: &str) -> Value {
    let uid = key
        .strip_prefix("CN:")
        .map(Value::from)
        .unwrap_or(Value::Null);
    json!({
        "schema_version": 1,
        "scope": "adventure_report",
        "account_key": key,
        "uid": uid,
        "updated_at": Value::Null,
        "days": {},
    })
}

fn read_report_file(path: &Path, key: &str) -> Result<Value, String> {
    let Some(doc) = read_json(path)? else {
        return Ok(empty_report(key));
    };
    if doc["schema_version"] != 1 || !doc["days"].is_object() {
        return Ok(empty_report(key));
    }
    Ok(doc)
}

fn active_account_key(root: &Path) -> Option<String> {
    let index_path = root.join("config/accounts/index.json");
    let index = read_json(&index_path).ok().flatten()?;
    index["active_game_account"]
        .as_str()
        .filter(|key| valid_account_key(key))
        .map(str::to_string)
}

fn collect_report_keys(root: &Path) -> Vec<String> {
    let mut keys = BTreeSet::new();
    let cn_dir = root.join("config/adventure_reports/CN");
    if let Ok(entries) = std::fs::read_dir(cn_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|v| v.to_str()) != Some("json") {
                continue;
            }
            if let Some(uid) = path.file_stem().and_then(|v| v.to_str()) {
                let key = format!("CN:{uid}");
                if valid_account_key(&key) {
                    keys.insert(key);
                }
            }
        }
    }
    if let Ok(existing_accounts) = account_keys() {
        for key in existing_accounts {
            keys.insert(key);
        }
    }
    let mut list: Vec<String> = keys.into_iter().collect();
    if root.join("config/adventure_reports/default.json").is_file() {
        list.push("default".to_string());
    }
    list
}

fn build_adventure_report_response(account_key: Option<String>) -> Result<Value, String> {
    let root = get_app_data_dir()?;
    let active_key = active_account_key(&root);
    let mut keys = collect_report_keys(&root);
    if let Some(ref active) = active_key {
        if !keys.iter().any(|item| item == active) {
            keys.insert(0, active.clone());
        }
    }

    let requested = account_key
        .as_deref()
        .map(str::trim)
        .filter(|k| !k.is_empty() && (*k == "default" || valid_account_key(k)))
        .map(str::to_string);

    let selected_key = requested
        .or_else(|| active_key.clone())
        .or_else(|| keys.first().cloned())
        .unwrap_or_else(|| "default".to_string());

    let mut accounts = Vec::new();
    for key in &keys {
        let path = report_file_path(&root, key)?;
        let report = read_report_file(&path, key)?;
        let days_count = report["days"]
            .as_object()
            .map(|days| days.len())
            .unwrap_or(0);
        let label = key
            .strip_prefix("CN:")
            .map(|uid| format!("UID {uid}"))
            .unwrap_or_else(|| "未绑定 UID 记录".to_string());
        accounts.push(json!({
            "key": key,
            "uid": key.strip_prefix("CN:"),
            "label": label,
            "days_count": days_count,
            "updated_at": report["updated_at"],
        }));
    }

    let selected_path = report_file_path(&root, &selected_key)?;
    let selected_report = read_report_file(&selected_path, &selected_key)?;

    Ok(json!({
        "active_account_key": active_key,
        "selected_key": selected_key,
        "accounts": accounts,
        "report": selected_report,
    }))
}

#[tauri::command]
pub async fn get_adventure_report(account_key: Option<String>) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || build_adventure_report_response(account_key))
        .await
        .map_err(|error| error.to_string())?
}
