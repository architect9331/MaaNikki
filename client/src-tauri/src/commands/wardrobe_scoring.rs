//! Score settings share the same UID journal and lock as wardrobe scanning.
use super::{
    account_state::{read_json, read_role, update_role, valid_account_key},
    utils::{get_app_data_dir, get_exe_directory},
};
use serde_json::{json, Value};

fn active_key() -> Result<String, String> {
    let index = read_json(&get_app_data_dir()?.join("config/accounts/index.json"))?
        .ok_or("请先读取衣柜，确认游戏账号。")?;
    index["active_game_account"]
        .as_str()
        .filter(|key| valid_account_key(key))
        .map(str::to_owned)
        .ok_or("请先读取衣柜，确认游戏账号。".into())
}

fn load() -> Result<Value, String> {
    let key = active_key()?;
    let role = read_role(&key)?;
    let wardrobe = &role["wardrobe"];
    let mut snapshot = wardrobe
        .get("inventory")
        .filter(|value| value["complete"] == true)
        .or_else(|| wardrobe.get("last_scan"))
        .cloned()
        .unwrap_or(Value::Null);
    if let Some(parts) = snapshot.get_mut("parts").and_then(Value::as_object_mut) {
        for part in parts.values_mut() {
            if let Some(object) = part.as_object_mut() {
                object.remove("head_images");
            }
        }
    }
    let blessing = ["inventory", "last_scan"]
        .iter()
        .filter_map(|name| role["blessing"].get(name).filter(|value| value.is_object()))
        .max_by_key(|value| value["record"]["updated_at"].as_str().unwrap_or(""));
    let catalog = read_json(&get_exe_directory()?.join("resource/data/wardrobe_scoring.json"))?
        .ok_or("缺少搭配计分资料，请更新资源。")?;
    if catalog["schema_version"] != 1 {
        return Err("搭配计分资料版本不支持。".into());
    }
    Ok(json!({
        "account_key": key, "uid": role["uid"], "snapshot": snapshot,
        "blessing": blessing.map(|value| json!({"locks": value["locks"], "items": value["items"]})),
        "settings": wardrobe.get("scoring"), "catalog": catalog,
    }))
}

fn nonnegative(value: &Value) -> bool {
    value
        .as_f64()
        .is_some_and(|number| number.is_finite() && number >= 0.0)
}

fn validate(settings: &Value) -> Result<(), String> {
    if settings["schema_version"] != 1 {
        return Err("搭配设置版本无效。".into());
    }
    for (name, length) in [("flat", 5), ("stat", 6), ("part", 6)] {
        if !settings["heart"][name]
            .as_array()
            .is_some_and(|values| values.len() == length && values.iter().all(nonnegative))
        {
            return Err("无限之心加成应为非负数。".into());
        }
    }
    if !nonnegative(&settings["heart"]["display"]) {
        return Err("展示加成应为非负数。".into());
    }
    let levels = settings["levels"].as_object().ok_or("缺少衣服养成设置。")?;
    if levels.len() > 12000
        || levels.iter().any(|(key, value)| {
            key.is_empty()
                || key.len() > 20
                || !key.bytes().all(|byte| byte.is_ascii_digit())
                || !value.as_u64().is_some_and(|level| level <= 11)
        })
    {
        return Err("衣服等级应为 0～10 级或焕新。".into());
    }
    let thresholds = settings["rank_thresholds"]
        .as_object()
        .ok_or("缺少关卡分数设置。")?;
    if thresholds.len() > 1000
        || thresholds.values().any(|entry| {
            !entry.as_object().is_some_and(|values| {
                values.len() <= 16
                    && values
                        .iter()
                        .all(|(key, value)| key.parse::<u32>().is_ok() && nonnegative(value))
            })
        })
    {
        return Err("关卡目标分数应为非负数。".into());
    }
    Ok(())
}

#[tauri::command]
pub async fn get_wardrobe_scoring() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(load)
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn save_wardrobe_scoring(account_key: String, mut settings: Value) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        validate(&settings)?;
        if !valid_account_key(&account_key) {
            return Err("账号 UID 无效。".into());
        }
        settings["updated_at"] = json!(chrono::Utc::now().to_rfc3339());
        update_role(&account_key, |role| {
            if active_key()? != account_key {
                return Err("账号已切换，请刷新搭配计算页面。".into());
            }
            if !role["wardrobe"].is_object() {
                return Err("请先读取当前账号的衣柜。".into());
            }
            role["wardrobe"]["scoring"] = settings;
            Ok(())
        })
    })
    .await
    .map_err(|error| error.to_string())?
}
