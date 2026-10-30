//! Isolated official login. Credentials stay in a Windows DPAPI file.
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Mutex,
};
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

const HOST: &str = "myl.nuanpaper.com";
static LOGIN_GENERATION: AtomicU64 = AtomicU64::new(0);
static LOGIN_WRITE: Mutex<()> = Mutex::new(());

fn directory() -> Result<PathBuf, String> {
    Ok(super::utils::get_app_data_dir()?.join("config/map-mask"))
}

fn desktop(window: &WebviewWindow) -> Result<(), String> {
    if window.label() != "main" {
        return Err("此操作仅可从主窗口发起。".into());
    }
    Ok(())
}

// Shared internally with the read-only resonance worker; never exposed through IPC.
pub(super) fn credentials() -> Result<Value, String> {
    let _guard = LOGIN_WRITE.lock().map_err(|_| "无法读取美鸭梨登录信息。")?;
    let encrypted =
        std::fs::read(directory()?.join("login.dat")).map_err(|_| "请先登录美鸭梨。")?;
    let mut raw = unprotect(&encrypted)?;
    let result = serde_json::from_slice(&raw).map_err(|_| "请重新登录美鸭梨。".to_string());
    raw.fill(0);
    result
}

#[cfg(windows)]
fn unprotect(bytes: &[u8]) -> Result<Vec<u8>, String> {
    use std::ffi::c_void;
    #[repr(C)]
    struct Blob {
        size: u32,
        data: *mut u8,
    }
    #[link(name = "Crypt32")]
    extern "system" {
        fn CryptUnprotectData(
            input: *const Blob,
            description: *mut *mut u16,
            entropy: *const Blob,
            reserved: *const c_void,
            prompt: *const c_void,
            flags: u32,
            output: *mut Blob,
        ) -> i32;
    }
    #[link(name = "Kernel32")]
    extern "system" {
        fn LocalFree(pointer: *mut c_void) -> *mut c_void;
    }
    let source = Blob {
        size: bytes.len() as u32,
        data: bytes.as_ptr() as *mut u8,
    };
    let mut output = Blob {
        size: 0,
        data: std::ptr::null_mut(),
    };
    unsafe {
        if CryptUnprotectData(
            &source,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            1,
            &mut output,
        ) == 0
        {
            return Err("请重新登录美鸭梨。".into());
        }
        let buffer = std::slice::from_raw_parts_mut(output.data, output.size as usize);
        let decrypted = buffer.to_vec();
        buffer.fill(0);
        LocalFree(output.data.cast());
        Ok(decrypted)
    }
}

#[cfg(not(windows))]
fn unprotect(_bytes: &[u8]) -> Result<Vec<u8>, String> {
    Err("美鸭梨登录支持 Windows。".into())
}

#[cfg(windows)]
fn protect(bytes: &[u8]) -> Result<Vec<u8>, String> {
    use std::ffi::c_void;
    #[repr(C)]
    struct Blob {
        size: u32,
        data: *mut u8,
    }
    #[link(name = "Crypt32")]
    extern "system" {
        fn CryptProtectData(
            input: *const Blob,
            description: *const u16,
            entropy: *const Blob,
            reserved: *const c_void,
            prompt: *const c_void,
            flags: u32,
            output: *mut Blob,
        ) -> i32;
    }
    #[link(name = "Kernel32")]
    extern "system" {
        fn LocalFree(pointer: *mut c_void) -> *mut c_void;
    }
    let source = Blob {
        size: bytes.len() as u32,
        data: bytes.as_ptr() as *mut u8,
    };
    let mut output = Blob {
        size: 0,
        data: std::ptr::null_mut(),
    };
    unsafe {
        if CryptProtectData(
            &source,
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            std::ptr::null(),
            1,
            &mut output,
        ) == 0
        {
            return Err("Windows 无法保存美鸭梨登录信息。".into());
        }
        let encrypted = std::slice::from_raw_parts(output.data, output.size as usize).to_vec();
        LocalFree(output.data.cast());
        Ok(encrypted)
    }
}

#[cfg(not(windows))]
fn protect(_bytes: &[u8]) -> Result<Vec<u8>, String> {
    Err("地图标记首版支持 Windows。".into())
}

#[tauri::command]
pub fn pearpal_status(window: WebviewWindow) -> Result<Value, String> {
    desktop(&window)?;
    let folder = directory()?;
    let mut status: Value = std::fs::read(folder.join("status.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .filter(|value: &Value| value.is_object())
        .unwrap_or_else(|| json!({}));
    status["has_login"] = json!(folder.join("login.dat").is_file());
    Ok(status)
}

#[tauri::command]
pub async fn pearpal_open_login(
    app: tauri::AppHandle,
    window: WebviewWindow,
) -> Result<(), String> {
    desktop(&window)?;
    if !cfg!(windows) {
        return Err("地图标记首版支持 Windows。".into());
    }
    if let Some(login) = app.get_webview_window("pearpal-login") {
        let _ = login.show();
        let _ = login.set_focus();
        return Ok(());
    }
    let data = super::utils::get_app_data_dir()?.join("cache/pearpal-webview");
    std::fs::create_dir_all(&data).map_err(|_| "无法创建美鸭梨登录目录。")?;
    LOGIN_GENERATION.fetch_add(1, Ordering::SeqCst);
    let script = r#"
(() => {
  if (location.hostname !== 'myl.nuanpaper.com' || window.top !== window) return;
  let busy = false;
  const timer = setInterval(async () => {
    if (busy || !window.__TAURI_INTERNALS__) return;
    const momoToken = localStorage.getItem('momoToken');
    const momoNid = localStorage.getItem('momoNid');
    if (!momoToken || !momoNid) return;
    busy = true;
    try {
      await window.__TAURI_INTERNALS__.invoke('pearpal_complete_login', { momoToken, momoNid });
      clearInterval(timer);
    } catch {
      busy = false;
    }
  }, 1000);
})();
"#;
    WebviewWindowBuilder::new(
        &app,
        "pearpal-login",
        WebviewUrl::External("https://myl.nuanpaper.com/tools/map".parse().unwrap()),
    )
    .title("美鸭梨登录")
    .inner_size(1100.0, 760.0)
    .min_inner_size(800.0, 600.0)
    .data_directory(data)
    .initialization_script(script)
    .on_navigation(|url| url.scheme() == "https")
    .build()
    .map_err(|_| "美鸭梨登录窗口创建失败。")?;
    Ok(())
}

#[tauri::command]
pub async fn pearpal_complete_login(
    app: tauri::AppHandle,
    window: WebviewWindow,
    momo_token: String,
    momo_nid: String,
) -> Result<(), String> {
    let generation = LOGIN_GENERATION.load(Ordering::SeqCst);
    if window.label() != "pearpal-login"
        || window
            .url()
            .ok()
            .and_then(|url| url.host_str().map(str::to_owned))
            .as_deref()
            != Some(HOST)
    {
        return Err("登录来源无效。".into());
    }
    if momo_token.len() > 8192 || momo_nid.len() > 1024 {
        return Err("登录信息格式无效。".into());
    }
    let token: Value = serde_json::from_str(&momo_token).map_err(|_| "登录信息格式无效。")?;
    let token = token["token"]
        .as_str()
        .filter(|value| !value.is_empty() && value.len() <= 4096 && value.is_ascii())
        .ok_or("登录信息格式无效。")?;
    let nid: Value = serde_json::from_str(&momo_nid).unwrap_or(Value::String(momo_nid));
    let nid = if nid.is_object() {
        nid.get("nid")
            .or_else(|| nid.get("openid"))
            .or_else(|| nid.get("value"))
            .cloned()
            .unwrap_or(Value::Null)
    } else {
        nid
    };
    let openid = match nid {
        Value::String(value) => value,
        Value::Number(value) => value.to_string(),
        _ => return Err("登录账号格式无效。".into()),
    };
    if openid.is_empty() || openid.len() > 128 || !openid.is_ascii() {
        return Err("登录账号格式无效。".into());
    }
    let account = json!({ "token": token, "openid": openid });
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|_| "无法连接美鸭梨。")?;
    let response: Value = client
        .post("https://myl-api.nuanpaper.com/v1/strategy/map/user/info")
        .json(&json!({ "client_id": 1106, "token": token, "openid": openid }))
        .send()
        .await
        .map_err(|_| "美鸭梨连接失败，请稍后重试。")?
        .error_for_status()
        .map_err(|_| "美鸭梨服务暂不可用。")?
        .json()
        .await
        .map_err(|_| "美鸭梨返回格式无效。")?;
    if response["code"] != 0
        || !response["data"]["star"].is_array()
        || !response["data"]["dewdrop"].is_array()
    {
        let _ = app.emit_to(
            "main",
            "pearpal-login-error",
            "登录已过期，请在美鸭梨页面重新登录。",
        );
        return Err("登录已过期，请在美鸭梨页面重新登录。".into());
    }
    let encrypted = protect(&serde_json::to_vec(&account).map_err(|_| "无法保存登录信息。")?)?;
    let _guard = LOGIN_WRITE.lock().map_err(|_| "无法保存登录信息。")?;
    if generation != LOGIN_GENERATION.load(Ordering::SeqCst)
        || app.get_webview_window("pearpal-login").is_none()
    {
        return Err("登录已取消。".into());
    }
    let folder = directory()?;
    std::fs::create_dir_all(&folder).map_err(|_| "无法保存登录信息。")?;
    let temporary = folder.join("login.dat.tmp");
    std::fs::write(&temporary, encrypted).map_err(|_| "无法保存登录信息。")?;
    std::fs::rename(temporary, folder.join("login.dat")).map_err(|_| "无法保存登录信息。")?;
    let status = json!({
        "authenticated": true, "error": "",
        "star_collected": response["data"]["star"].as_array().map(Vec::len),
        "dewdrop_collected": response["data"]["dewdrop"].as_array().map(Vec::len),
    });
    let _ = std::fs::write(folder.join("status.json"), status.to_string());
    drop(_guard);
    super::resonance::login_changed(&app);
    let _ = app.emit_to("main", "pearpal-login-changed", true);
    let _ = window.close();
    Ok(())
}

#[tauri::command]
pub async fn pearpal_logout(app: tauri::AppHandle, window: WebviewWindow) -> Result<(), String> {
    desktop(&window)?;
    {
        let _guard = LOGIN_WRITE.lock().map_err(|_| "无法清理登录信息。")?;
        LOGIN_GENERATION.fetch_add(1, Ordering::SeqCst);
        for name in ["login.dat", "status.json"] {
            let path = directory()?.join(name);
            if path.exists() {
                std::fs::remove_file(path).map_err(|_| "清理美鸭梨登录信息失败。")?;
            }
        }
    }
    let _ = app.emit_to("main", "pearpal-login-changed", false);
    super::resonance::login_changed(&app);
    let login = if let Some(login) = app.get_webview_window("pearpal-login") {
        login
    } else {
        WebviewWindowBuilder::new(
            &app,
            "pearpal-login",
            WebviewUrl::External("about:blank".parse().unwrap()),
        )
        .visible(false)
        .data_directory(super::utils::get_app_data_dir()?.join("cache/pearpal-webview"))
        .build()
        .map_err(|_| "网页登录信息清理失败，应用登录信息已清除。")?
    };
    let result = login
        .clear_all_browsing_data()
        .map_err(|_| "网页登录信息清理失败，应用登录信息已清除。".to_string());
    let _ = login.close();
    result?;
    Ok(())
}
