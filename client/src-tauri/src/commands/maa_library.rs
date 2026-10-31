//! MaaFramework 动态库加载及可信状态，所有加载入口共用同一把锁。

use std::path::{Path, PathBuf};

use super::types::MaaState;

struct LoadedLibrary {
    path: PathBuf,
    version: String,
}

#[derive(Default)]
pub struct LibraryState {
    loaded: Option<LoadedLibrary>,
    last_error: Option<String>,
    failure_reported: bool,
}

fn library_filename() -> &'static str {
    #[cfg(windows)]
    return "MaaFramework.dll";
    #[cfg(target_os = "macos")]
    return "libMaaFramework.dylib";
    #[cfg(target_os = "linux")]
    return "libMaaFramework.so";
}

fn load_failed(library: &mut LibraryState, path: Option<&Path>, error: String) -> String {
    let message = match path {
        Some(path) => format!(
            "Failed to load MaaFramework from {}: {}",
            path.display(),
            error
        ),
        None => format!("Failed to locate MaaFramework: {}", error),
    };
    log::error!("{}", message);
    library.last_error = Some(message.clone());
    if !library.failure_reported {
        library.failure_reported = super::telemetry::capture_library_load_failure(path, &error);
    }
    message
}

pub fn load(state: &MaaState, path: Option<PathBuf>) -> Result<String, String> {
    let mut library = state.library.lock().map_err(|e| e.to_string())?;
    if let Some(loaded) = &library.loaded {
        log::debug!("MaaFramework already loaded from {:?}", loaded.path);
        return Ok(loaded.version.clone());
    }

    let path = match path.map(Ok).unwrap_or_else(super::utils::get_maafw_dir) {
        Ok(path) => path,
        Err(error) => return Err(load_failed(&mut library, None, error)),
    };
    let dll_path = if path.is_file()
        || path
            .file_name()
            .is_some_and(|name| name == library_filename())
    {
        path
    } else {
        path.join(library_filename())
    };

    #[cfg(windows)]
    if let Some(directory) = dll_path.parent() {
        if let Err(error) = winsafe::SetDllDirectory(Some(&directory.to_string_lossy())) {
            log::warn!("SetDllDirectoryW failed for {:?}: {}", directory, error);
        }
    }

    if let Err(error) = maa_framework::load_library(&dll_path) {
        return Err(load_failed(&mut library, Some(&dll_path), error));
    }

    // 只有动态库成功加载后才能调用 FFI，并提交可供查询的状态。
    let version = maa_framework::maa_version().to_string();
    log::info!(
        "MaaFramework loaded from {:?}, version: {}",
        dll_path,
        version
    );
    library.loaded = Some(LoadedLibrary {
        path: dll_path,
        version: version.clone(),
    });
    library.last_error = None;
    super::system::set_vcredist_missing(false);
    Ok(version)
}

/// 状态查询只读缓存，不调用尚未加载的动态库。
pub fn initialized_version(state: &MaaState) -> Result<Option<String>, String> {
    let library = state.library.lock().map_err(|e| e.to_string())?;
    Ok(library.loaded.as_ref().and_then(|loaded| {
        if loaded.version.is_empty() || loaded.version == "unknown" {
            None
        } else {
            Some(loaded.version.clone())
        }
    }))
}

pub fn version(state: &MaaState) -> Result<String, String> {
    let library = state.library.lock().map_err(|e| e.to_string())?;
    match &library.loaded {
        Some(loaded) if !loaded.version.is_empty() && loaded.version != "unknown" => {
            Ok(loaded.version.clone())
        }
        Some(_) => Err("MaaFramework version unavailable".to_string()),
        None => Err(library
            .last_error
            .clone()
            .unwrap_or_else(|| "MaaFramework library not loaded".to_string())),
    }
}
