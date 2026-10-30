//! UID-scoped journals shared with Python AccountState.
use super::utils::{get_app_data_dir, get_exe_directory};
use serde_json::{json, Value};
use std::{
    io::Write,
    path::{Path, PathBuf},
};

pub(super) fn valid_account_key(key: &str) -> bool {
    key.strip_prefix("CN:").is_some_and(|uid| {
        !uid.is_empty() && uid.len() <= 32 && uid.bytes().all(|c| c.is_ascii_digit())
    })
}

fn account_path(key: &str) -> Result<PathBuf, String> {
    if !valid_account_key(key) {
        return Err("账号 UID 无效。".into());
    }
    Ok(get_app_data_dir()?
        .join("config/accounts/CN")
        .join(format!("{}.json", &key[3..])))
}

pub(super) fn read_json(path: &Path) -> Result<Option<Value>, String> {
    if !path.is_file() {
        return Ok(None);
    }
    if std::fs::metadata(path).map_err(|e| e.to_string())?.len() > 64 * 1024 * 1024 {
        return Err("账号状态文件超过 64 MiB。".into());
    }
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    serde_json::from_str(text.trim_start_matches('\u{feff}'))
        .map(Some)
        .map_err(|_| format!("账号 JSON 格式无效：{}", path.display()))
}

pub(super) fn read_role(key: &str) -> Result<Value, String> {
    let path = account_path(key)?;
    let value = read_json(&path)?.unwrap_or_else(|| {
        json!({
            "schema_version": 3, "scope": "game_account", "region": "CN", "uid": &key[3..],
            "tasks": {}, "blessing": {}, "observations": {},
        })
    });
    if value["schema_version"] != 3
        || value["uid"] != key[3..]
        || value["region"] != "CN"
        || !value["tasks"].is_object()
        || !value["blessing"].is_object()
        || !value["observations"].is_object()
        || value.get("migration").is_some_and(|v| !v.is_object())
    {
        return Err("UID 账号存档格式无效。".into());
    }
    Ok(value)
}

pub(super) fn update_role(
    key: &str,
    change: impl FnOnce(&mut Value) -> Result<(), String>,
) -> Result<(), String> {
    let directory = get_app_data_dir()?.join("config/accounts");
    std::fs::create_dir_all(&directory).map_err(|_| "无法创建账号数据目录。")?;
    // Same OS lock file as Python; it covers Python's first-byte lock too.
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join("store.lock"))
        .map_err(|_| "无法打开账号数据锁。")?;
    lock.lock().map_err(|_| "无法锁定账号数据。")?;
    let mut journal = read_role(key)?;
    change(&mut journal)?;
    journal["updated_at"] = json!(chrono::Utc::now().to_rfc3339());
    let raw = serde_json::to_vec_pretty(&journal).map_err(|_| "账号数据编码失败。")?;
    if raw.len() > 64 * 1024 * 1024 {
        return Err("账号状态 JSON 超过 64 MiB。".into());
    }
    let path = account_path(key)?;
    std::fs::create_dir_all(path.parent().ok_or("账号路径无效。")?)
        .map_err(|_| "无法创建账号目录。")?;
    let temporary = path.with_extension("rust.tmp");
    let mut file = std::fs::File::create(&temporary).map_err(|_| "无法保存账号数据。")?;
    file.write_all(&raw)
        .and_then(|_| file.sync_all())
        .map_err(|_| "无法保存账号数据。")?;
    drop(file);
    std::fs::rename(temporary, path).map_err(|_| "无法替换账号数据。")?;
    Ok(())
}

pub(super) fn account_keys() -> Result<Vec<String>, String> {
    let directory = get_app_data_dir()?.join("config/accounts/CN");
    let mut keys = Vec::new();
    if !directory.exists() {
        return Ok(keys);
    }
    for entry in std::fs::read_dir(directory).map_err(|_| "无法读取账号目录。")? {
        let path = entry.map_err(|_| "无法读取账号文件。")?.path();
        if path.extension().and_then(|v| v.to_str()) != Some("json") {
            continue;
        }
        if let Some(uid) = path.file_stem().and_then(|v| v.to_str()) {
            let key = format!("CN:{uid}");
            if valid_account_key(&key) {
                keys.push(key);
            }
        }
    }
    keys.sort();
    Ok(keys)
}

fn read_account_state() -> Result<Value, String> {
    let root = get_app_data_dir()?;
    let index = read_json(&root.join("config/accounts/index.json"))?.unwrap_or(json!({}));
    let key = index["active_game_account"]
        .as_str()
        .filter(|key| valid_account_key(key));
    if let Some(key) = key {
        if !account_path(key)?.is_file() {
            return Err("当前 UID 的账号存档缺失，请恢复对应 JSON。".into());
        }
    }
    let account = key.map(read_role).transpose()?;
    let legacy = if account.is_none() {
        read_json(&root.join("config/account_state.json"))?
    } else {
        None
    };
    let source = account.as_ref().or(legacy.as_ref());
    let tasks = source.map(|v| v["tasks"].clone()).unwrap_or(json!({}));
    let observations = source
        .and_then(|v| v.get("observations"))
        .cloned()
        .unwrap_or(json!({}));
    let blessing = match source.and_then(|v| v.get("blessing")) {
        Some(value) => value.clone(),
        None => json!({
            "inventory": read_json(&root.join("config/blessing/inventory.json"))?,
            "last_scan": read_json(&root.join("config/blessing/last_scan.json"))?,
        }),
    };
    let latest = ["inventory", "last_scan"]
        .iter()
        .filter_map(|name| blessing.get(name).filter(|v| v.is_object()))
        .max_by_key(|v| v["record"]["updated_at"].as_str().unwrap_or(""));
    let summary = latest.map(|value| {
        let mut record = value.clone();
        if let Some(object) = record.as_object_mut() {
            for key in ["organization", "excluded_items", "unresolved", "items"] {
                object.remove(key);
            }
        }
        record
    });
    let resources = get_exe_directory()?.join("resource/data");
    Ok(json!({
        "account_key": key, "uid": account.as_ref().and_then(|v| v["uid"].as_str()),
        "tasks": tasks, "observations": observations, "blessing": summary,
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
