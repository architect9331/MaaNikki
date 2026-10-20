//! Read-only view of the agent's local account journal and collection catalog.
use super::utils::{get_app_data_dir, get_exe_directory};
use serde_json::{json, Value};
use std::path::Path;

fn read_json(path: &Path) -> Result<Option<Value>, String> {
    if !path.is_file() {
        return Ok(None);
    }
    let metadata = std::fs::metadata(path).map_err(|e| e.to_string())?;
    if metadata.len() > 64 * 1024 * 1024 {
        return Err("账号状态文件过大".into());
    }
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    serde_json::from_str(text.trim_start_matches('\u{feff}'))
        .map(Some)
        .map_err(|e| format!("无法读取 {}：{}", path.display(), e))
}

fn read_account_state() -> Result<Value, String> {
    let root = get_app_data_dir()?;
    let account = read_json(&root.join("config/account_state.json"))?;
    let (tasks, blessing) = if let Some(account) = account {
        if account["schema_version"] != 2
            || !account["tasks"].is_object()
            || !account["blessing"].is_object()
        {
            return Err("账号状态 JSON 格式无效".into());
        }
        (account["tasks"].clone(), account["blessing"].clone())
    } else {
        (
            json!({}),
            json!({
                "inventory": read_json(&root.join("config/blessing/inventory.json"))?,
                "last_scan": read_json(&root.join("config/blessing/last_scan.json"))?,
            }),
        )
    };
    let latest = ["inventory", "last_scan"]
        .iter()
        .filter_map(|key| blessing.get(key).filter(|v| v.is_object()))
        .max_by_key(|v| v["record"]["updated_at"].as_str().unwrap_or(""));
    // Ordered stock may be large; it is retained in the journal, never sent to UI.
    let summary = latest.map(|value| {
        let mut record = value.clone();
        if let Some(object) = record.as_object_mut() {
            object.remove("organization");
            object.remove("excluded_items");
            object.remove("unresolved");
            object.remove("items");
        }
        record
    });
    let resources = get_exe_directory()?.join("resource/data");
    Ok(json!({
        "tasks": tasks,
        "blessing": summary,
        "catalog": read_json(&resources.join("blessing_catalog.json"))?,
        "rotation": read_json(&resources.join("xinghai_rotation.json"))?,
    }))
}

#[tauri::command]
pub async fn get_account_state() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(read_account_state)
        .await
        .map_err(|error| error.to_string())?
}
