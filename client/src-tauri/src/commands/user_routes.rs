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

fn is_user_runnable_point(point: &Value) -> bool {
    let action = point["action"].as_str().unwrap_or("");
    match action {
        "" | "teleport" | "wait" | "collect" | "insect" | "fishing" | "bottle" | "key" => true,
        "macro" => matches!(
            point["params"]["id"].as_str(),
            Some("starsea_fall" | "zhaoxi_attack")
        ),
        _ => false,
    }
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
            let (count, runnable) = if is_collection {
                (value["routes"].as_array()?.len(), true)
            } else {
                let pts = value["points"].as_array()?;
                (pts.len(), pts.iter().all(is_user_runnable_point))
            };
            Some(json!({
                "id": path.file_stem()?.to_str()?, "title": value["title"], "map": value["map"],
                "kind": if is_collection { "route_collection" } else { "route" },
                "points": count, "teleport": value["teleport"], "builtin": builtin,
                "runnable": runnable,
                "path": path.to_string_lossy(),
                "modified": entry.metadata().ok()?.modified().ok()?
                    .duration_since(std::time::UNIX_EPOCH).ok()?.as_secs(),
            }))
        })
        .collect();
    if builtin {
        routes.sort_by(|a, b| {
            let a_col = a["kind"].as_str() == Some("route_collection");
            let b_col = b["kind"].as_str() == Some("route_collection");
            b_col
                .cmp(&a_col)
                .then_with(|| a["id"].as_str().unwrap_or("").cmp(b["id"].as_str().unwrap_or("")))
        });
    } else {
        routes.sort_by_key(|route| std::cmp::Reverse(route["modified"].as_u64().unwrap_or(0)));
    }
    routes
}

#[tauri::command]
pub fn list_routes() -> Result<Value, String> {
    let exe_dir = get_exe_directory()?;
    let mut builtin = summaries(exe_dir.join("resource/routes/gingerbread"), true);
    builtin.extend(summaries(exe_dir.join("resource/routes/daily"), true));
    Ok(json!({ "user": summaries(routes_dir()?, false), "builtin": builtin }))
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

#[tauri::command]
pub async fn generate_gingerbread_routes(params: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let exe_dir = get_exe_directory()?;
        let py_exe = if cfg!(windows) {
            exe_dir.join("python/python.exe")
        } else {
            exe_dir.join("python/bin/python3")
        };
        let python = if py_exe.is_file() {
            py_exe
        } else {
            PathBuf::from("python")
        };
        let req_str = serde_json::to_string(&params).map_err(|_| "生成请求参数无效。")?;
        let code = concat!(
            "import sys, json; ",
            "sys.stdout.reconfigure(encoding='utf-8'); ",
            "sys.stderr.reconfigure(encoding='utf-8'); ",
            "from pathlib import Path; ",
            "root = Path.cwd(); ",
            "sys.path.insert(0, str(root / 'agent')); ",
            "from navigation.material_route_generator import generate_from_request_dict; ",
            "req = json.loads(sys.argv[1]); ",
            "res = generate_from_request_dict(root, req); ",
            "print(json.dumps(res, ensure_ascii=False))"
        );

        let mut cmd = std::process::Command::new(python);
        cmd.current_dir(&exe_dir).args(["-c", code, &req_str]);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000);
        }
        let output = cmd
            .output()
            .map_err(|e| format!("无法启动路线生成器：{e}"))?;
        if !output.status.success() {
            let err = String::from_utf8_lossy(&output.stderr);
            return Err(format!("路线生成失败：{}", err.trim()));
        }
        let out_str = String::from_utf8_lossy(&output.stdout);
        serde_json::from_str::<Value>(out_str.trim())
            .map_err(|e| format!("解析生成路线结果失败：{e}"))
    })
    .await
    .map_err(|e| format!("路线生成任务异常：{e}"))?
}

#[cfg(target_os = "windows")]
static CURSOR_LOCK_ACTIVE: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
#[cfg(target_os = "windows")]
static CURSOR_LOCK_X: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);
#[cfg(target_os = "windows")]
static CURSOR_LOCK_Y: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);

#[tauri::command]
pub fn poll_cursor_lock_delta(active: bool) -> [i32; 2] {
    #[cfg(target_os = "windows")]
    {
        use std::sync::atomic::Ordering;
        if !active {
            CURSOR_LOCK_ACTIVE.store(false, Ordering::SeqCst);
            return [0, 0];
        }
        let Ok(pt) = winsafe::GetCursorPos() else {
            return [0, 0];
        };
        if !CURSOR_LOCK_ACTIVE.swap(true, Ordering::SeqCst) {
            CURSOR_LOCK_X.store(pt.x, Ordering::SeqCst);
            CURSOR_LOCK_Y.store(pt.y, Ordering::SeqCst);
            return [0, 0];
        }
        let ax = CURSOR_LOCK_X.load(Ordering::SeqCst);
        let ay = CURSOR_LOCK_Y.load(Ordering::SeqCst);
        let dx = pt.x - ax;
        let dy = pt.y - ay;
        if dx != 0 || dy != 0 {
            let _ = winsafe::SetCursorPos(ax, ay);
        }
        [dx, dy]
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = active;
        [0, 0]
    }
}
