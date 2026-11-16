//! Silent background hot-updater and dual-layer overlay resolver for game data.
use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, OnceLock, RwLock};
use std::time::Duration;

use log::{debug, info, warn};
use reqwest::header::{ACCEPT, AUTHORIZATION};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter};
use tokio::sync::Notify;

use super::account_state::{bump_account_state_revision, read_json};
use super::app_config::AppConfigState;
use super::types::MaaState;
use super::update_mirrors;
use super::utils::{build_user_agent, get_app_data_dir, get_exe_directory, write_atomic};

const DATA_REPO: &str = "architect9331/MaaNikki-data";
const MAX_MANIFEST_BYTES: usize = 512 * 1024;
const MAX_BUNDLE_BYTES: u64 = 64 * 1024 * 1024;
const CHECK_INTERVAL: Duration = Duration::from_secs(6 * 3600);
const STARTUP_DELAY: Duration = Duration::from_secs(3);

const EMBEDDED_RESONANCE_BYTES: &[u8] = include_bytes!("../data/resonance_catalog.json");
const EMBEDDED_ITEM_SOURCES_BYTES: &[u8] =
    include_bytes!("../../../src/services/scoring/itemSources.json");

const ALLOWED_OVERLAY_FILES: &[&str] = &[
    "resource/data/wardrobe_scoring.json",
    "resource/data/blessing_catalog.json",
    "resource/data/crafting_catalog.json",
    "resource/data/xinghai_rotation.json",
    "resource/data/resonance_catalog.json",
    "resource/data/item_sources.json",
    "resource/navigation/materials_catalog_v1.json",
    "resource/model/wardrobe/labels.json",
    "resource/model/wardrobe/parts.json",
    "resource/model/wardrobe/coverage.json",
    "resource/model/wardrobe/model-contract.json",
    "resource/model/backpack/catalog.json",
    "resource/model/backpack/icons.npz",
    "resource/model/rejuvenate/categories.json",
    "resource/model/rejuvenate/icons.npz",
    "resource/model/blessing_identity.json",
    "resource/model/blessing_identity.onnx",
    "resource/model/blessing_badge.json",
    "resource/model/blessing_badge.onnx",
    "resource/model/blessing_badge.npz",
    "resource/model/blessing_level.json",
    "resource/model/blessing_level.onnx",
];

#[derive(Clone, Debug, Deserialize, Serialize)]
struct FileSpec {
    size: u64,
    sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct BundleSpec {
    tag: String,
    asset: String,
    size: u64,
    sha256: String,
    files: BTreeMap<String, FileSpec>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct DataManifest {
    schema_version: u32,
    version: String,
    tag: String,
    published_at: String,
    #[serde(default)]
    stats: Value,
    bundles: BTreeMap<String, BundleSpec>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct ActiveOverlayRecord {
    schema_version: u32,
    version: String,
    tag: String,
    published_at: String,
    slot: String,
    wardrobe_version: String,
    game_items: usize,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DataUpdateStatus {
    pub state: String,
    pub active_version: Option<String>,
    pub remote_version: Option<String>,
    pub wardrobe_version: Option<String>,
    pub updated_at: Option<String>,
}

static ACTIVE_OVERLAY: OnceLock<RwLock<Option<(ActiveOverlayRecord, PathBuf)>>> = OnceLock::new();
static STATUS_STATE: OnceLock<RwLock<DataUpdateStatus>> = OnceLock::new();
static CHECK_NOTIFY: OnceLock<Notify> = OnceLock::new();

fn overlay_cell() -> &'static RwLock<Option<(ActiveOverlayRecord, PathBuf)>> {
    ACTIVE_OVERLAY.get_or_init(|| RwLock::new(None))
}

fn status_cell() -> &'static RwLock<DataUpdateStatus> {
    STATUS_STATE.get_or_init(|| {
        RwLock::new(DataUpdateStatus {
            state: "idle".into(),
            active_version: None,
            remote_version: None,
            wardrobe_version: None,
            updated_at: None,
        })
    })
}

fn valid_hex64(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn canonical_rel_path(rel: &str) -> Option<&'static str> {
    let trimmed = rel.trim().replace('\\', "/");
    let stripped = trimmed.trim_start_matches("./");
    ALLOWED_OVERLAY_FILES
        .iter()
        .copied()
        .find(|&allowed| allowed == stripped)
}

fn safe_slot_name(slot: &str) -> bool {
    !slot.is_empty()
        && slot.len() <= 40
        && !slot.contains('/')
        && !slot.contains('\\')
        && !slot.contains("..")
        && Path::new(slot)
            .components()
            .all(|c| matches!(c, Component::Normal(_)))
        && slot
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-._".contains(&b))
}

fn sha256_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn sha256_file(path: &Path) -> Result<(u64, String), String> {
    let mut file =
        std::fs::File::open(path).map_err(|e| format!("无法读取 {}: {}", path.display(), e))?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    let mut total = 0u64;
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| format!("读取中断 {}: {}", path.display(), e))?;
        if n == 0 {
            break;
        }
        total += n as u64;
        hasher.update(&buf[..n]);
    }
    Ok((total, format!("{:x}", hasher.finalize())))
}

fn builtin_wardrobe_metrics(exe_dir: &Path) -> (String, usize) {
    let path = exe_dir.join("resource/data/wardrobe_scoring.json");
    let Ok(Some(doc)) = read_json(&path) else {
        return (String::new(), 0);
    };
    let version = doc["version"].as_str().unwrap_or("").to_string();
    let count = doc["game_items"].as_object().map(|m| m.len()).unwrap_or(0);
    (version, count)
}

fn set_sentry_data_tag(version_label: &str) {
    let label = version_label.to_string();
    sentry::configure_scope(|scope| {
        scope.set_tag("data.version", &label);
    });
}

fn slot_complete(slot_dir: &Path) -> bool {
    slot_dir.is_dir()
        && ALLOWED_OVERLAY_FILES
            .iter()
            .all(|rel| slot_dir.join(rel).is_file())
}

pub fn init_on_startup() {
    let Ok(app_data) = get_app_data_dir() else {
        return;
    };
    let Ok(exe_dir) = get_exe_directory() else {
        return;
    };
    let (builtin_ver, builtin_items) = builtin_wardrobe_metrics(&exe_dir);
    let overlay_root = app_data.join("data_overlay");
    let active_path = overlay_root.join("active.json");

    if let Ok(raw) = std::fs::read_to_string(&active_path) {
        if let Ok(record) = serde_json::from_str::<ActiveOverlayRecord>(&raw) {
            let valid = record.schema_version == 1
                && safe_slot_name(&record.slot)
                && slot_complete(&overlay_root.join("slots").join(&record.slot));
            let superseded_by_builtin = (!builtin_ver.is_empty()
                && !record.wardrobe_version.is_empty()
                && builtin_ver > record.wardrobe_version)
                || builtin_items > record.game_items;

            if valid && !superseded_by_builtin {
                let slot_dir = overlay_root.join("slots").join(&record.slot);
                info!(
                    "[data-update] Loaded active overlay {} (wardrobe {})",
                    record.tag, record.wardrobe_version
                );
                set_sentry_data_tag(&record.version);
                if let Ok(mut guard) = status_cell().write() {
                    guard.state = "active".into();
                    guard.active_version = Some(record.version.clone());
                    guard.wardrobe_version = Some(record.wardrobe_version.clone());
                    guard.updated_at = Some(record.published_at.clone());
                }
                if let Ok(mut guard) = overlay_cell().write() {
                    *guard = Some((record, slot_dir));
                }
                return;
            }
            info!("[data-update] Retiring superseded or incomplete overlay; using built-in data");
            let _ = std::fs::remove_file(&active_path);
        }
    }

    set_sentry_data_tag("builtin");
    if let Ok(mut guard) = status_cell().write() {
        guard.wardrobe_version = (!builtin_ver.is_empty()).then_some(builtin_ver);
    }
}

pub fn active_overlay_version() -> String {
    overlay_cell()
        .read()
        .ok()
        .and_then(|g| g.as_ref().map(|(rec, _)| rec.version.clone()))
        .unwrap_or_default()
}

pub fn active_slot_dir() -> Option<PathBuf> {
    overlay_cell()
        .read()
        .ok()
        .and_then(|g| g.as_ref().map(|(_, dir)| dir.clone()))
}

pub fn resolve_overlay_file(rel_path: &str) -> Option<PathBuf> {
    let canonical = canonical_rel_path(rel_path)?;
    let slot_dir = active_slot_dir()?;
    let candidate = slot_dir.join(canonical);
    candidate.is_file().then_some(candidate)
}

pub fn resolve_data_file(rel_path: &str) -> Result<PathBuf, String> {
    if let Some(overlay) = resolve_overlay_file(rel_path) {
        return Ok(overlay);
    }
    Ok(get_exe_directory()?.join(rel_path))
}

fn is_runtime_busy(maa_state: &MaaState) -> bool {
    let Ok(instances) = maa_state.instances.lock() else {
        return false;
    };
    instances.values().any(|inst| {
        inst.tasker.as_ref().is_some_and(|t| t.running()) || !inst.agent_children.is_empty()
    })
}

fn build_client(proxy_url: Option<&str>, timeout_secs: u64) -> Result<reqwest::Client, String> {
    let mut builder = reqwest::Client::builder()
        .user_agent(build_user_agent())
        .connect_timeout(Duration::from_secs(4))
        .timeout(Duration::from_secs(timeout_secs))
        .no_proxy();
    if let Some(proxy) = proxy_url.filter(|s| !s.trim().is_empty()) {
        let reqwest_proxy =
            reqwest::Proxy::all(proxy.trim()).map_err(|e| format!("代理配置无效: {e}"))?;
        builder = builder.proxy(reqwest_proxy);
    }
    builder.build().map_err(|e| e.to_string())
}

fn mirror_urls(raw_github_url: &str) -> Vec<(String, bool)> {
    let mut list = vec![(raw_github_url.to_string(), true)];
    for base in &update_mirrors::CONFIG.github_proxies {
        list.push((
            format!("{}/{}", base.trim_end_matches('/'), raw_github_url),
            false,
        ));
    }
    list
}

fn validate_manifest(manifest: &DataManifest) -> Result<(), String> {
    if manifest.schema_version != 1 {
        return Err(format!(
            "不支持的数据清单协议版本: {}",
            manifest.schema_version
        ));
    }
    if !safe_slot_name(&manifest.tag) || !manifest.tag.starts_with('v') {
        return Err("数据清单标签无效".into());
    }
    for kind in ["catalog", "models"] {
        let bundle = manifest
            .bundles
            .get(kind)
            .ok_or_else(|| format!("数据清单缺少 {kind} 分包"))?;
        if !safe_slot_name(&bundle.tag)
            || !bundle.tag.starts_with('v')
            || !bundle.asset.starts_with("MaaNikki-data-")
            || !bundle.asset.ends_with(".zip")
            || bundle.asset.contains('/')
            || bundle.size == 0
            || bundle.size > MAX_BUNDLE_BYTES
            || !valid_hex64(&bundle.sha256)
        {
            return Err(format!("数据分包 {kind} 元数据无效"));
        }
        for (rel, spec) in &bundle.files {
            if canonical_rel_path(rel).is_none() || spec.size == 0 || !valid_hex64(&spec.sha256) {
                return Err(format!("数据分包 {kind} 包含未授权或无效条目: {rel}"));
            }
        }
    }
    Ok(())
}

async fn fetch_manifest(
    github_pat: Option<&str>,
    proxy_url: Option<&str>,
) -> Result<DataManifest, String> {
    let raw_url = format!("https://raw.githubusercontent.com/{DATA_REPO}/main/manifest.json");
    let mut candidates = mirror_urls(&raw_url);

    let mut last_err = String::new();
    for (url, is_direct) in &candidates {
        let client = match build_client(if *is_direct { proxy_url } else { None }, 12) {
            Ok(c) => c,
            Err(e) => {
                last_err = e;
                continue;
            }
        };
        let mut req = client.get(url);
        if *is_direct {
            if let Some(pat) = github_pat.filter(|s| !s.trim().is_empty()) {
                req = req.header(AUTHORIZATION, format!("Bearer {}", pat.trim()));
            }
        }
        match req.send().await {
            Ok(resp) if resp.status().is_success() => match resp.bytes().await {
                Ok(bytes) if bytes.len() <= MAX_MANIFEST_BYTES => {
                    if let Ok(manifest) = serde_json::from_slice::<DataManifest>(&bytes) {
                        if validate_manifest(&manifest).is_ok() {
                            return Ok(manifest);
                        }
                    }
                    last_err = format!("{url}: 清单校验未通过");
                }
                _ => last_err = format!("{url}: 响应体积异常"),
            },
            Ok(resp) => last_err = format!("{url}: HTTP {}", resp.status()),
            Err(e) => last_err = format!("{url}: {e}"),
        }
    }

    // Fallback: query GitHub Releases latest API if raw.githubusercontent.com was blocked
    let api_url = format!("https://api.github.com/repos/{DATA_REPO}/releases/latest");
    candidates = mirror_urls(&api_url);
    for (url, is_direct) in &candidates {
        let Ok(client) = build_client(if *is_direct { proxy_url } else { None }, 12) else {
            continue;
        };
        let mut req = client
            .get(url)
            .header(ACCEPT, "application/vnd.github+json");
        if *is_direct {
            if let Some(pat) = github_pat.filter(|s| !s.trim().is_empty()) {
                req = req.header(AUTHORIZATION, format!("Bearer {}", pat.trim()));
            }
        }
        let Ok(resp) = req.send().await else { continue };
        if !resp.status().is_success() {
            continue;
        }
        let Ok(rel) = resp.json::<Value>().await else {
            continue;
        };
        let Some(asset_url) = rel["assets"]
            .as_array()
            .and_then(|arr| {
                arr.iter().find(|a| {
                    a["name"]
                        .as_str()
                        .is_some_and(|n| n.ends_with("-manifest.json"))
                })
            })
            .and_then(|a| a["browser_download_url"].as_str())
        else {
            continue;
        };
        for (dl_url, dl_direct) in mirror_urls(asset_url) {
            let Ok(dl_client) = build_client(if dl_direct { proxy_url } else { None }, 12) else {
                continue;
            };
            let Ok(dl_resp) = dl_client.get(&dl_url).send().await else {
                continue;
            };
            if !dl_resp.status().is_success() {
                continue;
            }
            if let Ok(bytes) = dl_resp.bytes().await {
                if bytes.len() <= MAX_MANIFEST_BYTES {
                    if let Ok(manifest) = serde_json::from_slice::<DataManifest>(&bytes) {
                        if validate_manifest(&manifest).is_ok() {
                            return Ok(manifest);
                        }
                    }
                }
            }
        }
    }

    Err(format!("数据清单获取失败: {last_err}"))
}

enum LocalSource {
    Disk(PathBuf),
    Embedded(&'static [u8]),
}

fn local_matching_source(rel: &str, spec: &FileSpec, exe_dir: &Path) -> Option<LocalSource> {
    let want_hash = spec.sha256.to_ascii_lowercase();
    if let Some(overlay_path) = resolve_overlay_file(rel) {
        if let Ok((sz, digest)) = sha256_file(&overlay_path) {
            if sz == spec.size && digest == want_hash {
                return Some(LocalSource::Disk(overlay_path));
            }
        }
    }
    let builtin_path = exe_dir.join(rel);
    if builtin_path.is_file() {
        if let Ok((sz, digest)) = sha256_file(&builtin_path) {
            if sz == spec.size && digest == want_hash {
                return Some(LocalSource::Disk(builtin_path));
            }
        }
    }
    let embedded = match rel {
        "resource/data/resonance_catalog.json" => Some(EMBEDDED_RESONANCE_BYTES),
        "resource/data/item_sources.json" => Some(EMBEDDED_ITEM_SOURCES_BYTES),
        _ => None,
    };
    if let Some(bytes) = embedded {
        if bytes.len() as u64 == spec.size && sha256_bytes(bytes) == want_hash {
            return Some(LocalSource::Embedded(bytes));
        }
    }
    None
}

async fn download_bundle_archive(
    bundle: &BundleSpec,
    dest_zip: &Path,
    proxy_url: Option<&str>,
) -> Result<(), String> {
    let raw_url = format!(
        "https://github.com/{DATA_REPO}/releases/download/{}/{}",
        bundle.tag, bundle.asset
    );
    let want_hash = bundle.sha256.to_ascii_lowercase();
    if dest_zip.is_file() {
        if let Ok((sz, digest)) = sha256_file(dest_zip) {
            if sz == bundle.size && digest == want_hash {
                return Ok(());
            }
        }
    }
    if let Some(parent) = dest_zip.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let part_path = dest_zip.with_extension("zip.part");
    let mut last_err = String::new();

    for (url, is_direct) in mirror_urls(&raw_url) {
        let Ok(client) = build_client(if is_direct { proxy_url } else { None }, 90) else {
            continue;
        };
        let resp = match client.get(&url).send().await {
            Ok(r) if r.status().is_success() => r,
            Ok(r) => {
                last_err = format!("{url}: HTTP {}", r.status());
                continue;
            }
            Err(e) => {
                last_err = format!("{url}: {e}");
                continue;
            }
        };

        use futures_util::StreamExt;
        use std::io::Write;

        let mut out = match std::fs::File::create(&part_path) {
            Ok(f) => f,
            Err(e) => return Err(format!("无法创建临时下载文件: {e}")),
        };
        let mut hasher = Sha256::new();
        let mut downloaded = 0u64;
        let mut stream = resp.bytes_stream();
        let mut failed = false;

        while let Some(chunk) = stream.next().await {
            match chunk {
                Ok(bytes) => {
                    downloaded += bytes.len() as u64;
                    if downloaded > bundle.size || downloaded > MAX_BUNDLE_BYTES {
                        last_err = format!("{url}: 下载大小超出预期");
                        failed = true;
                        break;
                    }
                    if let Err(e) = out.write_all(&bytes) {
                        last_err = format!("写入失败: {e}");
                        failed = true;
                        break;
                    }
                    hasher.update(&bytes);
                }
                Err(e) => {
                    last_err = format!("{url}: 传输中断 {e}");
                    failed = true;
                    break;
                }
            }
        }
        drop(out);
        if failed {
            let _ = std::fs::remove_file(&part_path);
            continue;
        }
        let got_hash = format!("{:x}", hasher.finalize());
        if downloaded == bundle.size && got_hash == want_hash {
            std::fs::rename(&part_path, dest_zip).map_err(|e| e.to_string())?;
            info!(
                "[data-update] Downloaded verified {} via {}",
                bundle.asset, url
            );
            return Ok(());
        }
        let _ = std::fs::remove_file(&part_path);
        last_err = format!("{url}: SHA-256 不匹配");
    }

    Err(format!("下载数据分包 {} 失败: {}", bundle.asset, last_err))
}

fn extract_verified_bundle(
    zip_path: &Path,
    bundle: &BundleSpec,
    stage_dir: &Path,
) -> Result<(), String> {
    let file = std::fs::File::open(zip_path).map_err(|e| e.to_string())?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("无法解析数据压缩包: {e}"))?;

    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
        if entry.is_dir() {
            continue;
        }
        if entry.is_symlink() {
            return Err("数据包包含不允许的符号链接".into());
        }
        let Some(canonical) = canonical_rel_path(entry.name()) else {
            return Err(format!("数据包包含未授权路径: {}", entry.name()));
        };
        let Some(spec) = bundle.files.get(canonical) else {
            return Err(format!("数据包包含清单外文件: {canonical}"));
        };
        if entry.size() != spec.size {
            return Err(format!("文件大小与清单不符: {canonical}"));
        }
        let target = stage_dir.join(canonical);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut out = std::fs::File::create(&target).map_err(|e| e.to_string())?;
        std::io::copy(&mut entry, &mut out).map_err(|e| e.to_string())?;
    }

    for (rel, spec) in &bundle.files {
        let target = stage_dir.join(rel);
        let (sz, digest) = sha256_file(&target)?;
        if sz != spec.size || digest != spec.sha256.to_ascii_lowercase() {
            return Err(format!("解压文件校验失败: {rel}"));
        }
    }
    Ok(())
}

fn populate_local_bundle(
    bundle: &BundleSpec,
    stage_dir: &Path,
    exe_dir: &Path,
) -> Result<(), String> {
    for (rel, spec) in &bundle.files {
        let Some(canonical) = canonical_rel_path(rel) else {
            return Err(format!("未授权路径: {rel}"));
        };
        let src = local_matching_source(canonical, spec, exe_dir)
            .ok_or_else(|| format!("本地缺少可复用的校验文件: {canonical}"))?;
        let target = stage_dir.join(canonical);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        match src {
            LocalSource::Disk(path) => {
                if std::fs::hard_link(&path, &target).is_err() {
                    std::fs::copy(&path, &target).map_err(|e| e.to_string())?;
                }
            }
            LocalSource::Embedded(bytes) => {
                std::fs::write(&target, bytes).map_err(|e| e.to_string())?;
            }
        }
        let (sz, digest) = sha256_file(&target)?;
        if sz != spec.size || digest != spec.sha256.to_ascii_lowercase() {
            return Err(format!("复用文件复核失败: {canonical}"));
        }
    }
    Ok(())
}

fn cleanup_unused_slots(overlay_root: &Path, keep_slot: &str) {
    for sub in ["slots", "staging", "downloads"] {
        let dir = overlay_root.join(sub);
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let name = entry.file_name();
            if sub == "slots" && name.to_string_lossy() == keep_slot {
                continue;
            }
            let path = entry.path();
            let _ = if path.is_dir() {
                std::fs::remove_dir_all(&path)
            } else {
                std::fs::remove_file(&path)
            };
        }
    }
}

fn read_network_settings(app_config: &AppConfigState) -> (Option<String>, Option<String>) {
    let Ok(cfg) = app_config.config.lock() else {
        return (None, None);
    };
    let settings = cfg.get("settings");
    let proxy = settings
        .and_then(|s| s.get("proxy"))
        .and_then(|p| p.get("url"))
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    let pat = settings
        .and_then(|s| s.get("githubPat"))
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    (pat, proxy)
}

fn update_status(app: &AppHandle, mutate: impl FnOnce(&mut DataUpdateStatus)) {
    if let Ok(mut guard) = status_cell().write() {
        mutate(&mut guard);
        let snapshot = guard.clone();
        drop(guard);
        let _ = app.emit("data-update-status", snapshot);
    }
}

async fn run_check_once(
    app: &AppHandle,
    app_config: &AppConfigState,
    maa_state: &MaaState,
) -> Result<(), String> {
    let app_data = get_app_data_dir()?;
    let exe_dir = get_exe_directory()?;
    let (github_pat, proxy_url) = read_network_settings(app_config);

    update_status(app, |st| st.state = "checking".into());
    let manifest = fetch_manifest(github_pat.as_deref(), proxy_url.as_deref()).await?;
    let remote_wardrobe_ver = manifest.stats["wardrobe_version"]
        .as_str()
        .unwrap_or("")
        .to_string();
    let remote_game_items = manifest.stats["game_items"].as_u64().unwrap_or(0) as usize;

    let (builtin_ver, builtin_items) = builtin_wardrobe_metrics(&exe_dir);
    if (!builtin_ver.is_empty()
        && !remote_wardrobe_ver.is_empty()
        && builtin_ver > remote_wardrobe_ver)
        || builtin_items > remote_game_items
    {
        debug!(
            "[data-update] Built-in data is newer than remote {}; skipping",
            manifest.tag
        );
        update_status(app, |st| {
            st.state = "up_to_date".into();
            st.remote_version = Some(manifest.version.clone());
        });
        return Ok(());
    }

    let catalog_bundle = &manifest.bundles["catalog"];
    let models_bundle = &manifest.bundles["models"];
    let need_catalog = catalog_bundle
        .files
        .iter()
        .any(|(rel, spec)| local_matching_source(rel, spec, &exe_dir).is_none());
    let need_models = models_bundle
        .files
        .iter()
        .any(|(rel, spec)| local_matching_source(rel, spec, &exe_dir).is_none());

    if !need_catalog && !need_models {
        update_status(app, |st| {
            st.state = "up_to_date".into();
            st.remote_version = Some(manifest.version.clone());
            if !remote_wardrobe_ver.is_empty() {
                st.wardrobe_version = Some(remote_wardrobe_ver.clone());
            }
        });
        return Ok(());
    }

    info!(
        "[data-update] Syncing {} (catalog={}, models={})",
        manifest.tag, need_catalog, need_models
    );
    update_status(app, |st| {
        st.state = "updating".into();
        st.remote_version = Some(manifest.version.clone());
    });

    let overlay_root = app_data.join("data_overlay");
    let dl_dir = overlay_root.join("downloads");
    let stage_dir = overlay_root.join("staging").join(&manifest.tag);
    if stage_dir.exists() {
        let _ = std::fs::remove_dir_all(&stage_dir);
    }
    std::fs::create_dir_all(&stage_dir).map_err(|e| e.to_string())?;

    if need_catalog {
        let zip_path = dl_dir.join(&catalog_bundle.asset);
        download_bundle_archive(catalog_bundle, &zip_path, proxy_url.as_deref()).await?;
        let cb = catalog_bundle.clone();
        let sd = stage_dir.clone();
        tokio::task::spawn_blocking(move || extract_verified_bundle(&zip_path, &cb, &sd))
            .await
            .map_err(|e| e.to_string())??;
    } else {
        let cb = catalog_bundle.clone();
        let sd = stage_dir.clone();
        let ed = exe_dir.clone();
        tokio::task::spawn_blocking(move || populate_local_bundle(&cb, &sd, &ed))
            .await
            .map_err(|e| e.to_string())??;
    }

    if need_models {
        let zip_path = dl_dir.join(&models_bundle.asset);
        download_bundle_archive(models_bundle, &zip_path, proxy_url.as_deref()).await?;
        let mb = models_bundle.clone();
        let sd = stage_dir.clone();
        tokio::task::spawn_blocking(move || extract_verified_bundle(&zip_path, &mb, &sd))
            .await
            .map_err(|e| e.to_string())??;
    } else {
        let mb = models_bundle.clone();
        let sd = stage_dir.clone();
        let ed = exe_dir.clone();
        tokio::task::spawn_blocking(move || populate_local_bundle(&mb, &sd, &ed))
            .await
            .map_err(|e| e.to_string())??;
    }

    while is_runtime_busy(maa_state) {
        debug!(
            "[data-update] Waiting for running task to finish before activating {}",
            manifest.tag
        );
        tokio::time::sleep(Duration::from_secs(5)).await;
    }

    let slot_dir = overlay_root.join("slots").join(&manifest.tag);
    if let Some(parent) = slot_dir.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    if slot_dir.exists() {
        let _ = std::fs::remove_dir_all(&slot_dir);
    }
    std::fs::rename(&stage_dir, &slot_dir).map_err(|e| format!("切换数据槽位失败: {e}"))?;

    let record = ActiveOverlayRecord {
        schema_version: 1,
        version: manifest.version.clone(),
        tag: manifest.tag.clone(),
        published_at: manifest.published_at.clone(),
        slot: manifest.tag.clone(),
        wardrobe_version: remote_wardrobe_ver.clone(),
        game_items: remote_game_items,
    };
    let active_bytes = serde_json::to_vec_pretty(&record).map_err(|e| e.to_string())?;
    write_atomic(&overlay_root.join("active.json"), &active_bytes)
        .map_err(|e| format!("保存激活记录失败: {e}"))?;

    if let Ok(mut guard) = overlay_cell().write() {
        *guard = Some((record.clone(), slot_dir));
    }
    cleanup_unused_slots(&overlay_root, &manifest.tag);
    set_sentry_data_tag(&record.version);
    super::resonance::reload_catalog(app);
    bump_account_state_revision();
    update_status(app, |st| {
        st.state = "up_to_date".into();
        st.active_version = Some(record.version);
        st.remote_version = Some(manifest.version);
        if !remote_wardrobe_ver.is_empty() {
            st.wardrobe_version = Some(remote_wardrobe_ver);
        }
        st.updated_at = Some(record.published_at);
    });
    info!("[data-update] Activated hot-update {}", manifest.tag);
    Ok(())
}

pub fn start(app: AppHandle, app_config: Arc<AppConfigState>, maa_state: Arc<MaaState>) {
    let notify = CHECK_NOTIFY.get_or_init(Notify::new);
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(STARTUP_DELAY).await;
        loop {
            if let Err(err) = run_check_once(&app, &app_config, &maa_state).await {
                warn!("[data-update] Silent check skipped: {err}");
                update_status(&app, |st| st.state = "idle".into());
            }
            tokio::select! {
                _ = tokio::time::sleep(CHECK_INTERVAL) => {}
                _ = notify.notified() => {}
            }
        }
    });
}

#[tauri::command]
pub fn get_data_update_status() -> DataUpdateStatus {
    status_cell()
        .read()
        .map(|g| g.clone())
        .unwrap_or(DataUpdateStatus {
            state: "idle".into(),
            active_version: None,
            remote_version: None,
            wardrobe_version: None,
            updated_at: None,
        })
}

#[tauri::command]
pub fn trigger_data_update_check() -> DataUpdateStatus {
    if let Some(notify) = CHECK_NOTIFY.get() {
        notify.notify_one();
    }
    get_data_update_status()
}
