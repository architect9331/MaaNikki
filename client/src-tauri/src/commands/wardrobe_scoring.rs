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
    let blessing = blessing_profile(&role["blessing"]);
    let catalog = read_json(&get_exe_directory()?.join("resource/data/wardrobe_scoring.json"))?
        .ok_or("缺少搭配计分资料，请更新资源。")?;
    if catalog["schema_version"] != 1 {
        return Err("搭配计分资料版本不支持。".into());
    }
    let crafting_catalog =
        read_json(&get_exe_directory()?.join("resource/data/crafting_catalog.json"))?
            .unwrap_or(Value::Null);
    Ok(json!({
        "account_key": key, "uid": role["uid"], "snapshot": snapshot,
        "blessing": blessing,
        "settings": wardrobe.get("scoring"), "catalog": catalog,
        "materials": role.get("materials"), "crafting_catalog": crafting_catalog,
    }))
}

/// Pursuit snapshots do not read lock states, so their own lock list is empty even
/// though they are the newest. Locks come from the last snapshot that read them, plus
/// the pieces a later pursuit confirmed locked, which are always five-star full level.
fn blessing_profile(blessing: &Value) -> Option<Value> {
    let updated_at = |value: &&Value| {
        value["record"]["updated_at"]
            .as_str()
            .unwrap_or("")
            .to_owned()
    };
    let snapshots: Vec<&Value> = ["inventory", "last_scan"]
        .iter()
        .filter_map(|name| blessing.get(name).filter(|value| value.is_object()))
        .collect();
    let latest = snapshots.iter().copied().max_by_key(updated_at)?;
    let read = snapshots
        .iter()
        .copied()
        .filter(|value| {
            value["locks"]["groups"]
                .as_array()
                .is_some_and(|groups| !groups.is_empty())
        })
        .max_by_key(updated_at);
    let mut locks = read.map_or_else(|| latest["locks"].clone(), |value| value["locks"].clone());
    let newer = read.is_none_or(|value| updated_at(&value) < updated_at(&latest));
    if newer && latest["pursuit"]["existing_colors_checked"] == true {
        let confirmed = latest["pursuit"]["parts"].as_array().into_iter().flatten().flat_map(|part| {
            part["locked_colors"].as_array().into_iter().flatten().map(|color| {
                json!({"name": part["name"], "slot": part["slot"], "color": color, "level": 10, "locked_count": 1})
            })
        });
        let confirmed: Vec<Value> = confirmed.collect();
        match locks.get_mut("groups").and_then(Value::as_array_mut) {
            Some(groups) => groups.extend(confirmed),
            None => locks = json!({"groups": confirmed}),
        }
    }
    Some(json!({"locks": locks, "items": latest["items"]}))
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

#[tauri::command]
pub async fn save_material_plan(account_key: String, mut materials: Value) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        if !valid_account_key(&account_key) {
            return Err("账号 UID 无效。".into());
        }
        if !materials.is_object() {
            return Err("材料规划数据格式无效。".into());
        }
        materials["updated_at"] = json!(chrono::Utc::now().to_rfc3339());
        update_role(&account_key, |role| {
            if active_key()? != account_key {
                return Err("账号已切换，请刷新页面。".into());
            }
            role["materials"] = materials;
            Ok(())
        })
    })
    .await
    .map_err(|error| error.to_string())?
}

fn find_x6game_dirs() -> Vec<std::path::PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(local_app_data) = std::env::var("LOCALAPPDATA") {
        let ini_path =
            std::path::PathBuf::from(local_app_data).join("InfinityNikki Launcher/config.ini");
        if let Ok(content) = std::fs::read_to_string(&ini_path) {
            for line in content.lines() {
                if let Some(rest) = line.trim().strip_prefix("gameDir=") {
                    let dir = std::path::PathBuf::from(rest.trim().trim_matches('"'));
                    candidates.push(dir.clone());
                    candidates.push(dir.join("X6Game"));
                    candidates.push(dir.join("InfinityNikki/X6Game"));
                }
            }
        }
    }
    let mut valid = Vec::new();
    for dir in candidates {
        if dir.join("Saved/GamePlayPhotos").is_dir() || dir.join("ScreenShot").is_dir() {
            if !valid.contains(&dir) {
                valid.push(dir);
            }
        }
    }
    valid
}

#[tauri::command]
pub async fn list_game_photos() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let mut photos = Vec::new();
        for x6 in find_x6game_dirs() {
            let gp = x6.join("Saved/GamePlayPhotos");
            if let Ok(uids) = std::fs::read_dir(&gp) {
                for uid_entry in uids.flatten() {
                    let uid_name = uid_entry.file_name().to_string_lossy().to_string();
                    let hq = uid_entry.path().join("NikkiPhotos_HighQuality");
                    if let Ok(files) = std::fs::read_dir(&hq) {
                        for file in files.flatten() {
                            let path = file.path();
                            let ext = path
                                .extension()
                                .and_then(|e| e.to_str())
                                .unwrap_or("")
                                .to_ascii_lowercase();
                            if !matches!(ext.as_str(), "jpg" | "jpeg" | "png") {
                                continue;
                            }
                            let modified = file
                                .metadata()
                                .ok()
                                .and_then(|m| m.modified().ok())
                                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                                .map(|d| d.as_secs())
                                .unwrap_or(0);
                            photos.push(json!({
                                "path": path.to_string_lossy(),
                                "filename": file.file_name().to_string_lossy(),
                                "uid": uid_name,
                                "source": "高清相册",
                                "modified": modified,
                            }));
                        }
                    }
                }
            }
            let ss = x6.join("ScreenShot");
            if let Ok(files) = std::fs::read_dir(&ss) {
                for file in files.flatten() {
                    let path = file.path();
                    let ext = path
                        .extension()
                        .and_then(|e| e.to_str())
                        .unwrap_or("")
                        .to_ascii_lowercase();
                    if !matches!(ext.as_str(), "jpg" | "jpeg" | "png") {
                        continue;
                    }
                    let modified = file
                        .metadata()
                        .ok()
                        .and_then(|m| m.modified().ok())
                        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                        .map(|d| d.as_secs())
                        .unwrap_or(0);
                    photos.push(json!({
                        "path": path.to_string_lossy(),
                        "filename": file.file_name().to_string_lossy(),
                        "uid": "",
                        "source": "游戏截图",
                        "modified": modified,
                    }));
                }
            }
        }
        photos.sort_by_key(|item| std::cmp::Reverse(item["modified"].as_u64().unwrap_or(0)));
        photos.truncate(30);
        Ok(json!({ "photos": photos }))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn read_game_photo_base64(path: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        use base64::Engine;
        let target = std::fs::canonicalize(&path).map_err(|_| "无法读取所选游戏照片。")?;
        let ext = target
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if !matches!(ext.as_str(), "jpg" | "jpeg" | "png") {
            return Err("仅支持读取 JPG 或 PNG 格式照片。".into());
        }
        let allowed = find_x6game_dirs()
            .into_iter()
            .any(|x6| std::fs::canonicalize(&x6).is_ok_and(|root| target.starts_with(root)));
        if !allowed {
            return Err("该路径不在已识别的游戏相册目录中。".into());
        }
        let bytes = std::fs::read(&target).map_err(|_| "读取照片文件失败。")?;
        let mime = if ext == "png" {
            "image/png"
        } else {
            "image/jpeg"
        };
        Ok(format!(
            "data:{};base64,{}",
            mime,
            base64::engine::general_purpose::STANDARD.encode(bytes)
        ))
    })
    .await
    .map_err(|e| e.to_string())?
}
