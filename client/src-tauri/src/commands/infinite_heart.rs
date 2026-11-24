//! Infinite Heart (无限之心) catalog and per-UID scan/override persistence.
use super::{
    account_state::{
        bump_account_state_revision, read_json, read_role, update_role, valid_account_key,
    },
    data_update::resolve_data_file,
    utils::get_app_data_dir,
};
use serde_json::{json, Value};

fn active_account_key() -> Option<String> {
    let index = read_json(&get_app_data_dir().ok()?.join("config/accounts/index.json")).ok()??;
    index["active_game_account"]
        .as_str()
        .filter(|key| valid_account_key(key))
        .map(str::to_owned)
}

fn validate_overrides(overrides: &Value) -> Result<(), String> {
    let map = overrides
        .as_object()
        .ok_or("无限之心手动标记数据格式无效。")?;
    if map.len() > 1000 {
        return Err("无限之心手动标记数量超出上限。".into());
    }
    for (nid, entry) in map {
        if nid.is_empty() || nid.len() > 12 || !nid.bytes().all(|b| b.is_ascii_digit()) {
            return Err("无限之心节点编号无效。".into());
        }
        let obj = entry.as_object().ok_or("无限之心节点状态无效。")?;
        let state = obj
            .get("state")
            .and_then(Value::as_str)
            .ok_or("缺少无限之心节点解锁状态。")?;
        if state != "unlocked" && state != "locked" {
            return Err("无限之心节点状态仅支持已解锁或未解锁。".into());
        }
        if let Some(lv) = obj.get("level") {
            if !lv.as_u64().is_some_and(|v| v <= 20) {
                return Err("无限之心节点等级无效。".into());
            }
        }
    }
    Ok(())
}

fn load() -> Result<Value, String> {
    let catalog_path = resolve_data_file("resource/data/infinite_heart_catalog.json")?;
    let catalog = read_json(&catalog_path)?.ok_or("缺少无限之心资料，请更新资源。")?;
    let key = active_account_key();
    let (uid, last_scan, overrides) = if let Some(ref k) = key {
        match read_role(k) {
            Ok(role) => {
                let section = role.get("infinite_heart").unwrap_or(&Value::Null);
                (
                    role["uid"].as_str().map(str::to_owned),
                    section.get("last_scan").cloned().unwrap_or(Value::Null),
                    section
                        .get("overrides")
                        .cloned()
                        .unwrap_or_else(|| json!({})),
                )
            }
            Err(_) => (None, Value::Null, json!({})),
        }
    } else {
        (None, Value::Null, json!({}))
    };
    Ok(json!({
        "catalog": catalog,
        "account_key": key,
        "uid": uid,
        "last_scan": last_scan,
        "overrides": overrides,
    }))
}

#[tauri::command]
pub async fn get_infinite_heart() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(load)
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn save_infinite_heart_overrides(
    account_key: String,
    overrides: Value,
) -> Result<(), String> {
    if !valid_account_key(&account_key) {
        return Err("账号 UID 无效。".into());
    }
    validate_overrides(&overrides)?;
    tauri::async_runtime::spawn_blocking(move || {
        update_role(&account_key, |role| {
            let root = role.as_object_mut().ok_or("账号存档格式无效。")?;
            let section = root
                .entry("infinite_heart")
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .ok_or("无限之心记录格式无效。")?;
            section.insert("overrides".into(), overrides);
            Ok(())
        })?;
        bump_account_state_revision();
        Ok(())
    })
    .await
    .map_err(|error| error.to_string())?
}
