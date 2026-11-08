//! User route files for the route editor. Routes are declarative data the agent validates again.
use super::{
    account_state::read_json,
    utils::{get_app_data_dir, get_exe_directory, write_atomic},
};
use serde_json::{json, Value};
use std::path::PathBuf;

const MAX_ROUTE_BYTES: usize = 1024 * 1024;

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

fn routes_dir() -> Result<PathBuf, String> {
    Ok(get_app_data_dir()?.join("config/routes"))
}

fn route_path(id: &str) -> Result<PathBuf, String> {
    if !valid_id(id) {
        return Err("路线 ID 无效。".into());
    }
    Ok(routes_dir()?.join(format!("{id}.json")))
}

fn summaries(directory: PathBuf, builtin: bool) -> Vec<Value> {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut routes: Vec<Value> = entries
        .flatten()
        .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "json"))
        .filter_map(|entry| {
            let path = entry.path();
            let value = read_json(&path).ok()??;
            let is_collection = value["kind"].as_str() == Some("route_collection");
            let count = if is_collection {
                value["routes"].as_array()?.len()
            } else {
                value["points"].as_array()?.len()
            };
            Some(json!({
                "id": path.file_stem()?.to_str()?, "title": value["title"], "map": value["map"],
                "kind": if is_collection { "route_collection" } else { "route" },
                "points": count, "teleport": value["teleport"], "builtin": builtin,
                "path": path.to_string_lossy(),
                "modified": entry.metadata().ok()?.modified().ok()?
                    .duration_since(std::time::UNIX_EPOCH).ok()?.as_secs(),
            }))
        })
        .collect();
    routes.sort_by_key(|route| std::cmp::Reverse(route["modified"].as_u64().unwrap_or(0)));
    routes
}

#[tauri::command]
pub fn list_routes() -> Result<Value, String> {
    let builtin = get_exe_directory()?.join("resource/routes/daily");
    Ok(json!({ "user": summaries(routes_dir()?, false), "builtin": summaries(builtin, true) }))
}

#[tauri::command]
pub fn save_user_route(id: String, route: Value) -> Result<String, String> {
    let path = route_path(&id)?;
    let raw = serde_json::to_vec_pretty(&route).map_err(|_| "路线编码失败。")?;
    if raw.len() > MAX_ROUTE_BYTES {
        return Err("路线超过 1 MB，请减少点位。".into());
    }
    write_atomic(&path, &raw).map_err(|_| "无法保存路线。")?;
    Ok(path.to_string_lossy().into_owned())
}

#[tauri::command]
pub fn delete_user_route(id: String) -> Result<(), String> {
    let path = route_path(&id)?;
    if path.is_file() {
        std::fs::remove_file(path).map_err(|_| "无法删除路线文件。")?;
    }
    Ok(())
}
