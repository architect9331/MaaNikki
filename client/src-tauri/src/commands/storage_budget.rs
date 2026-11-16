//! 存储空间统计、滚动配额控制与安全日志缓存清理。
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde::Serialize;
use serde_json::Value;

const BYTES_PER_MIB: u64 = 1024 * 1024;
const FRESH_GRACE: Duration = Duration::from_secs(3600);
const MAX_EXPORTS_BUDGET: usize = 5;

const RUN_LOG_KINDS: &[&str] = &[
    "blessing-lock",
    "blessing-pursuit",
    "blessing",
    "blessing-maintain",
    "blessing-hover",
    "wardrobe",
    "daily-upgrade",
    "daily-bless",
    "starlight_crystals",
    "wardrobe_scoring",
];

const FLAT_LOG_KINDS: &[&str] = &[
    "navigation",
    "daily",
    "startup",
    "fishing",
    "minigame_bling",
    "performance",
    "realm",
    "daily-tests",
    "blessing_offline",
    "monthly",
];

#[derive(Debug, Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StorageCleanResult {
    pub deleted_files: u64,
    pub freed_bytes: u64,
}

impl StorageCleanResult {
    fn absorb(&mut self, other: Self) {
        self.deleted_files = self.deleted_files.saturating_add(other.deleted_files);
        self.freed_bytes = self.freed_bytes.saturating_add(other.freed_bytes);
    }
}

#[derive(Debug, Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StorageCategoryUsage {
    pub bytes: u64,
    pub files: u64,
}

impl StorageCategoryUsage {
    fn absorb(&mut self, other: Self) {
        self.bytes = self.bytes.saturating_add(other.bytes);
        self.files = self.files.saturating_add(other.files);
    }
}

#[derive(Debug, Clone, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct StorageUsage {
    pub logs: StorageCategoryUsage,
    pub artifacts: StorageCategoryUsage,
    pub cleanable_cache: StorageCategoryUsage,
    pub config_and_runtime: StorageCategoryUsage,
    pub total_data_bytes: u64,
}

pub(crate) struct ProtectedScope {
    dirs: Vec<String>,
    files: HashSet<String>,
    protected_names: HashSet<String>,
}

impl ProtectedScope {
    pub(crate) fn collect(data_dir: &Path, exclude_file_name: Option<&str>) -> Self {
        let mut dirs = Vec::new();
        let mut files = HashSet::new();
        files.insert(normalize_compare_key(
            &data_dir.join("logs/startup/window_restore.json"),
        ));

        collect_account_evidence_dirs(data_dir, &mut dirs);

        let mut protected_names = HashSet::new();
        for fixed in ["maafw.log", "mxu-tauri.log", "window_restore.json"] {
            protected_names.insert(normalize_name_key(fixed));
        }
        if let Some(name) = exclude_file_name.filter(|s| !s.is_empty()) {
            protected_names.insert(normalize_name_key(name));
        }

        Self {
            dirs,
            files,
            protected_names,
        }
    }

    pub(crate) fn is_protected_dir(&self, path: &Path) -> bool {
        let key = normalize_compare_key(path);
        self.dirs.iter().any(|dir| key == *dir)
    }

    pub(crate) fn is_protected_path(&self, path: &Path) -> bool {
        if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
            if self.protected_names.contains(&normalize_name_key(name)) {
                return true;
            }
        }
        let key = normalize_compare_key(path);
        if self.files.contains(&key) {
            return true;
        }
        self.dirs
            .iter()
            .any(|dir| key == *dir || key.starts_with(&format!("{dir}/")))
    }

    fn contains_protected_descendant(&self, dir: &Path) -> bool {
        let key = normalize_compare_key(dir);
        let prefix = format!("{key}/");
        self.dirs
            .iter()
            .any(|d| *d == key || d.starts_with(&prefix))
            || self.files.iter().any(|f| f.starts_with(&prefix))
    }
}

fn normalize_name_key(name: &str) -> String {
    #[cfg(target_os = "windows")]
    {
        name.to_ascii_lowercase()
    }
    #[cfg(not(target_os = "windows"))]
    {
        name.to_string()
    }
}

fn normalize_compare_key(path: &Path) -> String {
    let mut text = path.to_string_lossy().replace('\\', "/");
    if let Some(stripped) = text.strip_prefix("//?/") {
        text = stripped.to_string();
    }
    while text.len() > 3 && text.ends_with('/') {
        text.pop();
    }
    #[cfg(target_os = "windows")]
    {
        text.to_ascii_lowercase()
    }
    #[cfg(not(target_os = "windows"))]
    {
        text
    }
}

fn collect_evidence_from_value(value: &Value, out: &mut Vec<String>) {
    match value {
        Value::Object(map) => {
            if let Some(Value::String(dir)) = map.get("evidence_dir") {
                let trimmed = dir.trim();
                if !trimmed.is_empty() {
                    let key = normalize_compare_key(Path::new(trimmed));
                    if !out.contains(&key) {
                        out.push(key);
                    }
                }
            }
            for child in map.values() {
                collect_evidence_from_value(child, out);
            }
        }
        Value::Array(items) => {
            for item in items {
                collect_evidence_from_value(item, out);
            }
        }
        _ => {}
    }
}

fn collect_evidence_from_json_file(path: &Path, out: &mut Vec<String>) {
    let Ok(meta) = std::fs::metadata(path) else {
        return;
    };
    if !meta.is_file() || meta.len() > 64 * BYTES_PER_MIB {
        return;
    }
    let Ok(text) = std::fs::read_to_string(path) else {
        return;
    };
    if let Ok(value) = serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}')) {
        collect_evidence_from_value(&value, out);
    }
}

fn collect_account_evidence_dirs(data_dir: &Path, out: &mut Vec<String>) {
    let cn_dir = data_dir.join("config/accounts/CN");
    if let Ok(entries) = std::fs::read_dir(&cn_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) == Some("json") {
                collect_evidence_from_json_file(&path, out);
            }
        }
    }
    collect_evidence_from_json_file(&data_dir.join("config/blessing/inventory.json"), out);
    collect_evidence_from_json_file(&data_dir.join("config/blessing/last_scan.json"), out);
}

fn measure_path(path: &Path) -> StorageCategoryUsage {
    let Ok(meta) = std::fs::symlink_metadata(path) else {
        return StorageCategoryUsage::default();
    };
    if meta.file_type().is_symlink() {
        return StorageCategoryUsage::default();
    }
    if meta.is_file() {
        return StorageCategoryUsage {
            bytes: meta.len(),
            files: 1,
        };
    }
    if !meta.is_dir() {
        return StorageCategoryUsage::default();
    }
    let mut total = StorageCategoryUsage::default();
    let Ok(entries) = std::fs::read_dir(path) else {
        return total;
    };
    for entry in entries.flatten() {
        total.absorb(measure_path(&entry.path()));
    }
    total
}

fn remove_file_counting(path: &Path) -> StorageCleanResult {
    let bytes = std::fs::symlink_metadata(path)
        .ok()
        .filter(|m| m.is_file())
        .map_or(0, |m| m.len());
    match std::fs::remove_file(path) {
        Ok(()) => StorageCleanResult {
            deleted_files: 1,
            freed_bytes: bytes,
        },
        Err(e) => {
            log::debug!("跳过或删除文件失败 [{}]: {}", path.display(), e);
            StorageCleanResult::default()
        }
    }
}

fn remove_entry_unprotected(path: &Path, scope: &ProtectedScope) -> StorageCleanResult {
    let Ok(meta) = std::fs::symlink_metadata(path) else {
        return StorageCleanResult::default();
    };
    if meta.file_type().is_symlink() || scope.is_protected_path(path) {
        return StorageCleanResult::default();
    }
    if meta.is_file() {
        return remove_file_counting(path);
    }
    if !meta.is_dir() {
        return StorageCleanResult::default();
    }
    if !scope.contains_protected_descendant(path) {
        let usage = measure_path(path);
        if std::fs::remove_dir_all(path).is_ok() {
            return StorageCleanResult {
                deleted_files: usage.files.max(1),
                freed_bytes: usage.bytes,
            };
        }
    }
    let mut result = StorageCleanResult::default();
    if let Ok(entries) = std::fs::read_dir(path) {
        for entry in entries.flatten() {
            result.absorb(remove_entry_unprotected(&entry.path(), scope));
        }
    }
    if !scope.is_protected_dir(path) && !scope.contains_protected_descendant(path) {
        let _ = std::fs::remove_dir(path);
    }
    result
}

pub(crate) fn prune_empty_subdirs(dir: &Path, scope: &ProtectedScope, remove_self: bool) {
    let Ok(meta) = std::fs::symlink_metadata(dir) else {
        return;
    };
    if meta.file_type().is_symlink() || !meta.is_dir() || scope.is_protected_dir(dir) {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if entry
            .file_type()
            .is_ok_and(|ft| ft.is_dir() && !ft.is_symlink())
        {
            prune_empty_subdirs(&path, scope, true);
        }
    }
    if remove_self {
        let _ = std::fs::remove_dir(dir);
    }
}

fn is_recent(modified: Option<SystemTime>, now: SystemTime) -> bool {
    match modified {
        Some(m) => match now.duration_since(m) {
            Ok(age) => age < FRESH_GRACE,
            Err(_) => true,
        },
        None => false,
    }
}

struct BudgetItem {
    path: PathBuf,
    name: String,
    bytes: u64,
    files: u64,
    modified: Option<SystemTime>,
}

fn collect_budget_items<F>(dir: &Path, mut filter: F) -> Vec<BudgetItem>
where
    F: FnMut(&Path, &str, std::fs::FileType) -> bool,
{
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut items = Vec::new();
    for entry in entries.flatten() {
        let Ok(ft) = entry.file_type() else {
            continue;
        };
        if ft.is_symlink() {
            continue;
        }
        let path = entry.path();
        let Some(name) = path
            .file_name()
            .and_then(|n| n.to_str())
            .map(str::to_string)
        else {
            continue;
        };
        if !filter(&path, &name, ft) {
            continue;
        }
        let usage = if ft.is_dir() {
            measure_path(&path)
        } else {
            let bytes = entry.metadata().map_or(0, |m| m.len());
            StorageCategoryUsage { bytes, files: 1 }
        };
        let modified = entry.metadata().and_then(|m| m.modified()).ok();
        items.push(BudgetItem {
            path,
            name,
            bytes: usage.bytes,
            files: usage.files,
            modified,
        });
    }
    items
}

fn prune_budget_items(
    mut items: Vec<BudgetItem>,
    min_keep: usize,
    max_count: usize,
    max_bytes: u64,
    respect_fresh: bool,
    now: SystemTime,
    scope: &ProtectedScope,
) -> StorageCleanResult {
    items.sort_by(|a, b| {
        b.modified
            .cmp(&a.modified)
            .then_with(|| b.name.cmp(&a.name))
    });
    let mut kept_count = 0usize;
    let mut kept_bytes = 0u64;
    let mut cleaned = StorageCleanResult::default();

    for (idx, item) in items.into_iter().enumerate() {
        let must_keep =
            scope.is_protected_path(&item.path) || (respect_fresh && is_recent(item.modified, now));
        let under_budget =
            kept_count < max_count && kept_bytes.saturating_add(item.bytes) <= max_bytes;
        if idx < min_keep || must_keep || under_budget {
            kept_count = kept_count.saturating_add(1);
            kept_bytes = kept_bytes.saturating_add(item.bytes);
            continue;
        }
        let removed = remove_entry_unprotected(&item.path, scope);
        if removed.deleted_files > 0 {
            cleaned.absorb(removed);
        } else if item.files == 0 && item.path.is_dir() {
            let _ = std::fs::remove_dir(&item.path);
        }
    }
    cleaned
}

fn is_maafw_backup_log(name: &str, ft: std::fs::FileType) -> bool {
    if !ft.is_file() {
        return false;
    }
    let lower = name.to_ascii_lowercase();
    lower.starts_with("maafw.bak.") && lower.ends_with(".log")
}

pub(crate) fn enforce_storage_budgets_with_scope(
    data_dir: &Path,
    scope: &ProtectedScope,
) -> StorageCleanResult {
    let now = SystemTime::now();
    let mut total = StorageCleanResult::default();
    let debug_dir = data_dir.join("debug");

    // 1. MaaFW 滚动备份日志配额：保留最新 1 份，上限 5 份 / 256 MiB
    total.absorb(prune_budget_items(
        collect_budget_items(&debug_dir, |_, name, ft| is_maafw_backup_log(name, ft)),
        1,
        5,
        256 * BYTES_PER_MIB,
        false,
        now,
        scope,
    ));

    // 2. 启动期 MaaFW 备份日志配额：上限 3 份 / 64 MiB
    let startup_fw = data_dir.join("logs/startup/framework");
    total.absorb(prune_budget_items(
        collect_budget_items(&startup_fw, |_, name, ft| is_maafw_backup_log(name, ft)),
        1,
        3,
        64 * BYTES_PER_MIB,
        false,
        now,
        scope,
    ));

    // 3. debug 子目录配额（messages / on_error / vision / route_telemetry）
    let debug_subdirs: &[(&str, usize, usize, u64)] = &[
        ("messages", 5, 30, 32 * BYTES_PER_MIB),
        ("on_error", 12, 60, 128 * BYTES_PER_MIB),
        ("vision", 20, 100, 256 * BYTES_PER_MIB),
        ("route_telemetry", 10, 30, 16 * BYTES_PER_MIB),
    ];
    for (sub, min_keep, max_count, max_bytes) in debug_subdirs {
        let dir = debug_dir.join(sub);
        total.absorb(prune_budget_items(
            collect_budget_items(&dir, |_, _, ft| ft.is_file()),
            *min_keep,
            *max_count,
            *max_bytes,
            true,
            now,
            scope,
        ));
    }

    // 4. 导出日志历史归档：保留最近 5 份
    total.absorb(prune_budget_items(
        collect_budget_items(&data_dir.join("debug_exports"), |_, _, _| true),
        2,
        MAX_EXPORTS_BUDGET,
        256 * BYTES_PER_MIB,
        true,
        now,
        scope,
    ));

    // 5. 任务运行轮次目录（保留受中断恢复保护的目录 + 最近 3 轮，上限 8 轮 / 64 MiB）
    let logs_dir = data_dir.join("logs");
    for kind in RUN_LOG_KINDS {
        let kind_dir = logs_dir.join(kind);
        total.absorb(prune_budget_items(
            collect_budget_items(&kind_dir, |_, _, ft| ft.is_dir()),
            3,
            8,
            64 * BYTES_PER_MIB,
            true,
            now,
            scope,
        ));
        prune_empty_subdirs(&kind_dir, scope, false);
    }

    // 6. 扁平任务日志目录（保留最近 15 条，上限 60 条 / 64 MiB；保护启动框架目录和窗口还原文件）
    for kind in FLAT_LOG_KINDS {
        let kind_dir = logs_dir.join(kind);
        total.absorb(prune_budget_items(
            collect_budget_items(&kind_dir, |_, name, _| {
                !(*kind == "startup" && name.eq_ignore_ascii_case("framework"))
            }),
            15,
            60,
            64 * BYTES_PER_MIB,
            true,
            now,
            scope,
        ));
        prune_empty_subdirs(&kind_dir, scope, false);
    }

    // 7. cache 根目录遗留临时文件
    let cache_dir = data_dir.join("cache");
    if let Ok(entries) = std::fs::read_dir(&cache_dir) {
        for entry in entries.flatten() {
            let Ok(ft) = entry.file_type() else {
                continue;
            };
            if ft.is_symlink() || !ft.is_file() {
                continue;
            }
            let path = entry.path();
            let lower = path
                .file_name()
                .map(|n| n.to_string_lossy().to_ascii_lowercase())
                .unwrap_or_default();
            let is_temp_residue = lower.ends_with(".downloading")
                || lower.ends_with(".tmp")
                || (lower.starts_with("vc_redist") && lower.ends_with(".exe"));
            if is_temp_residue {
                let modified = entry.metadata().and_then(|m| m.modified()).ok();
                if !is_recent(modified, now) {
                    total.absorb(remove_file_counting(&path));
                }
            }
        }
    }

    total
}

pub(crate) fn enforce_storage_budgets(data_dir: &Path) -> StorageCleanResult {
    let scope = ProtectedScope::collect(data_dir, None);
    enforce_storage_budgets_with_scope(data_dir, &scope)
}

pub(crate) fn remove_expired_protected(
    dir: &Path,
    cutoff: SystemTime,
    scope: &ProtectedScope,
    remove_empty_self: bool,
) -> StorageCleanResult {
    let Ok(meta) = std::fs::symlink_metadata(dir) else {
        return StorageCleanResult::default();
    };
    if meta.file_type().is_symlink() || !meta.is_dir() || scope.is_protected_dir(dir) {
        return StorageCleanResult::default();
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return StorageCleanResult::default();
    };
    let mut cleaned = StorageCleanResult::default();
    for entry in entries.flatten() {
        let Ok(ft) = entry.file_type() else {
            continue;
        };
        if ft.is_symlink() {
            continue;
        }
        let path = entry.path();
        if scope.is_protected_path(&path) {
            continue;
        }
        if ft.is_dir() {
            cleaned.absorb(remove_expired_protected(&path, cutoff, scope, true));
            continue;
        }
        if ft.is_file() {
            let expired = entry
                .metadata()
                .and_then(|m| m.modified())
                .is_ok_and(|modified| modified < cutoff);
            if expired {
                cleaned.absorb(remove_file_counting(&path));
            }
        }
    }
    if remove_empty_self && !scope.contains_protected_descendant(dir) {
        let _ = std::fs::remove_dir(dir);
    }
    cleaned
}

fn clear_logs_subtree(
    dir: &Path,
    scope: &ProtectedScope,
    remove_empty_self: bool,
    only_log_or_dmp: bool,
) -> StorageCleanResult {
    let Ok(meta) = std::fs::symlink_metadata(dir) else {
        return StorageCleanResult::default();
    };
    if meta.file_type().is_symlink() || !meta.is_dir() || scope.is_protected_dir(dir) {
        return StorageCleanResult::default();
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return StorageCleanResult::default();
    };
    let mut cleaned = StorageCleanResult::default();
    for entry in entries.flatten() {
        let Ok(ft) = entry.file_type() else {
            continue;
        };
        if ft.is_symlink() {
            continue;
        }
        let path = entry.path();
        if scope.is_protected_path(&path) {
            continue;
        }
        if ft.is_dir() {
            let name = path
                .file_name()
                .map(|n| n.to_string_lossy().to_ascii_lowercase())
                .unwrap_or_default();
            let sub_only_logs = if only_log_or_dmp
                && matches!(
                    name.as_str(),
                    "on_error" | "vision" | "messages" | "route_telemetry"
                ) {
                false
            } else {
                only_log_or_dmp
            };
            let remove_sub = !matches!(
                name.as_str(),
                "on_error" | "vision" | "messages" | "route_telemetry"
            );
            cleaned.absorb(clear_logs_subtree(&path, scope, remove_sub, sub_only_logs));
            continue;
        }
        if ft.is_file() {
            if only_log_or_dmp {
                let lower = path
                    .file_name()
                    .map(|n| n.to_string_lossy().to_ascii_lowercase())
                    .unwrap_or_default();
                if !lower.ends_with(".log") && !lower.ends_with(".dmp") {
                    continue;
                }
            }
            cleaned.absorb(remove_file_counting(&path));
        }
    }
    if remove_empty_self && !scope.contains_protected_descendant(dir) {
        let _ = std::fs::remove_dir(dir);
    }
    cleaned
}

pub(crate) fn clear_logs_protected(
    data_dir: &Path,
    exclude_file_name: Option<&str>,
) -> StorageCleanResult {
    let scope = ProtectedScope::collect(data_dir, exclude_file_name);
    let mut cleaned = StorageCleanResult::default();

    cleaned.absorb(clear_logs_subtree(
        &data_dir.join("debug"),
        &scope,
        false,
        true,
    ));
    cleaned.absorb(clear_logs_subtree(
        &data_dir.join("debug_exports"),
        &scope,
        false,
        false,
    ));
    let logs_dir = data_dir.join("logs");
    if let Ok(entries) = std::fs::read_dir(&logs_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if entry
                .file_type()
                .is_ok_and(|ft| ft.is_dir() && !ft.is_symlink())
            {
                cleaned.absorb(clear_logs_subtree(&path, &scope, false, false));
                prune_empty_subdirs(&path, &scope, false);
            } else if entry.file_type().is_ok_and(|ft| ft.is_file())
                && !scope.is_protected_path(&path)
            {
                cleaned.absorb(remove_file_counting(&path));
            }
        }
    }
    cleaned
}

fn cleanable_cache_dirs(cache_dir: &Path) -> Vec<PathBuf> {
    let mut dirs = vec![
        cache_dir.join("map-mask/tutorials"),
        cache_dir.join("old"),
        cache_dir.join("debug"),
        cache_dir.join("config"),
    ];
    if let Ok(entries) = std::fs::read_dir(cache_dir) {
        for entry in entries.flatten() {
            let Ok(ft) = entry.file_type() else {
                continue;
            };
            if !ft.is_dir() || ft.is_symlink() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
            if name.starts_with("old_") {
                dirs.push(entry.path());
            }
        }
    }
    dirs
}

fn is_cleanable_cache_root_file(path: &Path, now: SystemTime) -> bool {
    let lower = path
        .file_name()
        .map(|n| n.to_string_lossy().to_ascii_lowercase())
        .unwrap_or_default();
    if lower.ends_with(".downloading") {
        let modified = path.metadata().and_then(|m| m.modified()).ok();
        return !is_recent(modified, now);
    }
    true
}

pub(crate) fn clear_cache_dir(data_dir: &Path) -> StorageCleanResult {
    let cache_dir = data_dir.join("cache");
    if !cache_dir.is_dir() {
        return StorageCleanResult::default();
    }
    let now = SystemTime::now();
    let empty_scope = ProtectedScope {
        dirs: Vec::new(),
        files: HashSet::new(),
        protected_names: HashSet::new(),
    };
    let mut cleaned = StorageCleanResult::default();

    for dir in cleanable_cache_dirs(&cache_dir) {
        if dir.is_dir() && !dir.is_symlink() {
            if dir.file_name().and_then(|n| n.to_str()) == Some("tutorials") {
                cleaned.absorb(clear_logs_subtree(&dir, &empty_scope, false, false));
            } else {
                cleaned.absorb(remove_entry_unprotected(&dir, &empty_scope));
            }
        }
    }
    if let Ok(entries) = std::fs::read_dir(&cache_dir) {
        for entry in entries.flatten() {
            if !entry.file_type().is_ok_and(|ft| ft.is_file()) {
                continue;
            }
            let path = entry.path();
            if is_cleanable_cache_root_file(&path, now) {
                cleaned.absorb(remove_file_counting(&path));
            }
        }
    }
    cleaned
}

pub(crate) fn compute_storage_usage(data_dir: &Path) -> StorageUsage {
    let mut usage = StorageUsage::default();
    let now = SystemTime::now();

    let debug_dir = data_dir.join("debug");
    if let Ok(entries) = std::fs::read_dir(&debug_dir) {
        for entry in entries.flatten() {
            let Ok(ft) = entry.file_type() else {
                continue;
            };
            if ft.is_symlink() {
                continue;
            }
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
            let stat = measure_path(&path);
            if matches!(name.as_str(), "on_error" | "vision") {
                usage.artifacts.absorb(stat);
            } else {
                usage.logs.absorb(stat);
            }
        }
    }

    usage
        .artifacts
        .absorb(measure_path(&data_dir.join("debug_exports")));

    let logs_dir = data_dir.join("logs");
    if let Ok(entries) = std::fs::read_dir(&logs_dir) {
        for entry in entries.flatten() {
            let Ok(ft) = entry.file_type() else {
                continue;
            };
            if ft.is_symlink() {
                continue;
            }
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
            if name == "startup" && ft.is_dir() {
                if let Ok(sub_entries) = std::fs::read_dir(&path) {
                    for sub in sub_entries.flatten() {
                        let sub_name = sub.file_name().to_string_lossy().to_ascii_lowercase();
                        let stat = measure_path(&sub.path());
                        if sub_name == "framework" {
                            usage.logs.absorb(stat);
                        } else {
                            usage.artifacts.absorb(stat);
                        }
                    }
                }
            } else {
                usage.artifacts.absorb(measure_path(&path));
            }
        }
    }

    usage
        .config_and_runtime
        .absorb(measure_path(&data_dir.join("config")));

    let cache_dir = data_dir.join("cache");
    let cleanable_dirs: HashSet<PathBuf> = cleanable_cache_dirs(&cache_dir).into_iter().collect();
    if let Ok(entries) = std::fs::read_dir(&cache_dir) {
        for entry in entries.flatten() {
            let Ok(ft) = entry.file_type() else {
                continue;
            };
            if ft.is_symlink() {
                continue;
            }
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
            if ft.is_file() {
                let stat = measure_path(&path);
                if is_cleanable_cache_root_file(&path, now) {
                    usage.cleanable_cache.absorb(stat);
                } else {
                    usage.config_and_runtime.absorb(stat);
                }
            } else if name == "map-mask" {
                if let Ok(mm_entries) = std::fs::read_dir(&path) {
                    for mm in mm_entries.flatten() {
                        let mm_path = mm.path();
                        let stat = measure_path(&mm_path);
                        if mm
                            .file_name()
                            .to_string_lossy()
                            .eq_ignore_ascii_case("tutorials")
                        {
                            usage.cleanable_cache.absorb(stat);
                        } else {
                            usage.config_and_runtime.absorb(stat);
                        }
                    }
                }
            } else if cleanable_dirs.contains(&path) {
                usage.cleanable_cache.absorb(measure_path(&path));
            } else {
                usage.config_and_runtime.absorb(measure_path(&path));
            }
        }
    }

    usage.total_data_bytes = usage
        .logs
        .bytes
        .saturating_add(usage.artifacts.bytes)
        .saturating_add(usage.cleanable_cache.bytes)
        .saturating_add(usage.config_and_runtime.bytes);
    usage
}
