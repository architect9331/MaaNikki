//! Failure-only diagnostics; the successful framework load path does not call this module.

use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::Read;
use std::path::Path;

#[derive(Clone, Serialize)]
pub struct FailureNotice {
    pub message: String,
    pub recovery: &'static str,
}

pub struct LoadFailure {
    pub notice: FailureNotice,
    pub details: Value,
}

fn integrity(directory: &Path) -> Value {
    let path = directory.join("integrity.json");
    let manifest = match std::fs::read(&path) {
        Ok(data) => match serde_json::from_slice::<Value>(&data) {
            Ok(value) => value,
            Err(error) => return json!({"status": "invalid", "error": error.to_string()}),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            // Older full packages and third-party SDK directories have no inventory.
            return json!({"status": "unavailable"});
        }
        Err(error) => return json!({"status": "unreadable", "error": error.to_string()}),
    };
    let Some(files) = manifest.get("files").and_then(Value::as_object) else {
        return json!({"status": "invalid"});
    };
    if manifest["schema_version"] != 1 || !files.contains_key("MaaFramework.dll") {
        return json!({"status": "invalid"});
    }
    // Accept only sibling DLL names, never paths supplied by the inventory.
    if files.iter().any(|(name, entry)| {
        name.contains(['/', '\\', ':'])
            || !name.to_ascii_lowercase().ends_with(".dll")
            || entry["size"].as_u64().is_none()
            || !entry["sha256"].as_str().is_some_and(|hash| {
                hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
    }) {
        return json!({"status": "invalid"});
    }
    let mut missing = Vec::new();
    let mut mismatched = Vec::new();
    let mut unreadable = Vec::new();
    for (name, expected) in files {
        let result = (|| -> std::io::Result<bool> {
            let mut file = std::fs::File::open(directory.join(name))?;
            if file.metadata()?.len() != expected["size"].as_u64().unwrap() {
                return Ok(false);
            }
            let mut hash = Sha256::new();
            let mut buffer = [0u8; 64 * 1024];
            loop {
                let read = file.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                hash.update(&buffer[..read]);
            }
            Ok(format!("{:x}", hash.finalize())
                .eq_ignore_ascii_case(expected["sha256"].as_str().unwrap()))
        })();
        match result {
            Ok(true) => {}
            Ok(false) => mismatched.push(name),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => missing.push(name),
            Err(error) => unreadable.push(json!({"file": name, "error": error.to_string()})),
        }
    }
    json!({
        "status": if missing.is_empty() && mismatched.is_empty() && unreadable.is_empty() {
            "verified"
        } else { "failed" },
        "maafw_version": manifest["maafw_version"], "checked_files": files.len(),
        "missing": missing, "mismatched": mismatched, "unreadable": unreadable,
    })
}

#[cfg(windows)]
fn native_probe(path: &Path, flags: u32) -> Value {
    use std::error::Error;
    // Match the binding's LOAD_WITH_ALTERED_SEARCH_PATH for app-local DLLs.
    // This probe runs only after binding initialization failed; no framework API is called.
    match unsafe { libloading::os::windows::Library::load_with_flags(path, flags) } {
        Ok(library) => {
            drop(library);
            json!({"file": path, "status": "loaded"})
        }
        Err(error) => {
            let mut chain = vec![error.to_string()];
            let mut code = None;
            let mut source = error.source();
            while let Some(error) = source {
                chain.push(error.to_string());
                if let Some(error) = error.downcast_ref::<std::io::Error>() {
                    code = error.raw_os_error();
                }
                source = error.source();
            }
            json!({"file": path, "status": "failed", "win32_code": code, "error_chain": chain})
        }
    }
}

pub fn diagnose(path: Option<&Path>, error: &str) -> LoadFailure {
    let mut category = "unknown";
    let mut message = "框架加载失败，详细原因已保存到日志，可修复安装后重试。";
    let mut recovery = "retry";
    let mut details = json!({"binding_error": error});
    details["process_arch"] = json!(std::env::consts::ARCH);
    if let Some(path) = path {
        let directory = path.parent().unwrap_or_else(|| Path::new("."));
        let manifest = integrity(directory);
        let metadata = std::fs::metadata(path);
        details["file_exists"] = json!(metadata.as_ref().is_ok_and(|meta| meta.is_file()));
        if let Err(error) = &metadata {
            details["file_error"] = json!(error.to_string());
            details["file_error_code"] = json!(error.raw_os_error());
        }
        let missing = manifest["missing"]
            .as_array()
            .is_some_and(|items| !items.is_empty());
        let mismatched = manifest["mismatched"]
            .as_array()
            .is_some_and(|items| !items.is_empty());
        let unreadable = manifest["unreadable"]
            .as_array()
            .is_some_and(|items| !items.is_empty());
        let main_missing = metadata
            .as_ref()
            .is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound);
        details["integrity"] = manifest;
        if missing || main_missing {
            category = "files_missing";
            message = "框架文件不完整，请下载完整包并解压到同一目录后重试。";
            recovery = "download_full_package";
        } else if mismatched {
            category = "files_mismatched";
            message = "框架文件与发行版本不一致，请用完整包修复安装后重试。";
            recovery = "download_full_package";
        } else if unreadable {
            category = "files_unreadable";
            message = "框架文件读取失败，请检查目录权限和安全软件隔离记录后重试。";
        } else {
            #[cfg(windows)]
            {
                let probe = native_probe(
                    &path.canonicalize().unwrap_or_else(|_| path.to_path_buf()),
                    0x8,
                );
                let code = probe["win32_code"].as_i64();
                details["native_probe"] = probe;
                match code {
                    Some(126) => {
                        category = "dependency_unavailable";
                        message = "框架依赖加载失败，请用完整包修复安装后重试。";
                        recovery = "download_full_package";
                        let mut runtimes = Vec::new();
                        for name in ["vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"] {
                            let local = directory.join(name);
                            // Use the system directory only when no app-local copy is present.
                            // A damaged local copy must be repaired with the full package.
                            let result = if local.exists() {
                                native_probe(&local.canonicalize().unwrap_or(local), 0x8)
                            } else {
                                native_probe(Path::new(name), 0x800)
                            };
                            if result["win32_code"] == 126 {
                                category = "vcredist_unavailable";
                                message =
                                    "Microsoft Visual C++ 运行库加载失败，安装运行库后可重试。";
                                recovery = "install_vcredist";
                            }
                            runtimes.push(result);
                        }
                        details["vc_runtime_probes"] = json!(runtimes);
                    }
                    Some(193) => {
                        category = "invalid_binary";
                        message = "框架或依赖文件的格式、架构不匹配，请使用 Windows x64 完整包。";
                        recovery = "download_full_package";
                    }
                    Some(5) => {
                        category = "access_denied";
                        message = "系统拒绝访问框架文件，请检查目录权限和安全软件隔离记录后重试。";
                    }
                    _ => {}
                }
            }
        }
    }
    details["category"] = json!(category);
    LoadFailure {
        notice: FailureNotice {
            message: message.into(),
            recovery,
        },
        details,
    }
}
