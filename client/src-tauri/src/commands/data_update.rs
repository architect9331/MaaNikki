//! Silent background hot-updater and dual-layer overlay resolver for game data.
use std::collections::{BTreeMap, HashMap};
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex, OnceLock, RwLock,
};
use std::time::Duration;

use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};
use chrono::Utc;
use log::{debug, info, warn};
use reqwest::header::{ACCEPT, AUTHORIZATION};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, State};
use tokio::sync::Notify;

use super::account_state::{bump_account_state_revision, file_stamp, read_json, FileStamp};
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
    "resource/data/infinite_heart_catalog.json",
    "resource/data/resonance_catalog.json",
    "resource/data/item_sources.json",
    "resource/navigation/materials_catalog_v1.json",
    "resource/data/redeem_codes.json",
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
    #[serde(default)]
    count: Option<usize>,
    #[serde(default)]
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

#[derive(Clone, Debug, Deserialize, Serialize)]
struct SuitThumbsMeta {
    schema_version: u32,
    tag: String,
    asset: String,
    size: u64,
    sha256: String,
    count: usize,
    updated_at: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SuitThumbsStatus {
    pub state: String,
    pub available: bool,
    pub count: usize,
    pub version: Option<String>,
    pub error: Option<String>,
}

struct SuitThumbArchiveIndex {
    path: PathBuf,
    stamp: FileStamp,
    entries: HashMap<String, usize>,
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
static SUIT_THUMBS_STATUS: OnceLock<RwLock<SuitThumbsStatus>> = OnceLock::new();
static SUIT_THUMBS_SYNCING: AtomicBool = AtomicBool::new(false);
static SUIT_THUMBS_INDEX: OnceLock<Mutex<Option<SuitThumbArchiveIndex>>> = OnceLock::new();

fn suit_thumbs_status_cell() -> &'static RwLock<SuitThumbsStatus> {
    SUIT_THUMBS_STATUS.get_or_init(|| {
        RwLock::new(SuitThumbsStatus {
            state: "idle".into(),
            available: false,
            count: 0,
            version: None,
            error: None,
        })
    })
}

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

fn safe_manifest_rel_path(rel: &str) -> Option<String> {
    let trimmed = rel.trim().replace('\\', "/");
    let stripped = trimmed.trim_start_matches("./");
    if stripped.is_empty()
        || stripped.len() > 180
        || !stripped.starts_with("resource/")
        || stripped.contains("..")
        || stripped.contains("//")
        || stripped.ends_with('/')
    {
        return None;
    }
    if !Path::new(stripped)
        .components()
        .all(|c| matches!(c, Component::Normal(_)))
    {
        return None;
    }
    if !stripped
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"-_./".contains(&b))
    {
        return None;
    }
    Some(stripped.to_owned())
}

fn canonical_rel_path(rel: &str) -> Option<&'static str> {
    let norm = safe_manifest_rel_path(rel)?;
    ALLOWED_OVERLAY_FILES
        .iter()
        .copied()
        .find(|&allowed| allowed == norm)
}

fn find_bundle_spec<'a>(bundle: &'a BundleSpec, norm_rel: &str) -> Option<&'a FileSpec> {
    if let Some(spec) = bundle.files.get(norm_rel) {
        return Some(spec);
    }
    bundle
        .files
        .iter()
        .find(|(k, _)| safe_manifest_rel_path(k).as_deref() == Some(norm_rel))
        .map(|(_, v)| v)
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
    init_suit_thumbs_status();
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
        .connect_timeout(Duration::from_secs(6))
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

fn validate_bundle_header(kind: &str, bundle: &BundleSpec) -> Result<(), String> {
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
    Ok(())
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
        validate_bundle_header(kind, bundle)?;
        let mut known_count = 0usize;
        for (rel, spec) in &bundle.files {
            if safe_manifest_rel_path(rel).is_none() || spec.size == 0 || !valid_hex64(&spec.sha256)
            {
                return Err(format!("数据分包 {kind} 包含非法或无效条目: {rel}"));
            }
            if canonical_rel_path(rel).is_some() {
                known_count += 1;
            } else {
                debug!("[data-update] Skipping unrecognized future entry in {kind}: {rel}");
            }
        }
        if known_count == 0 {
            return Err(format!("数据分包 {kind} 不包含当前版本支持的条目"));
        }
    }
    if let Some(bundle) = manifest.bundles.get("suit_thumbs") {
        validate_bundle_header("suit_thumbs", bundle)?;
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
                    match serde_json::from_slice::<DataManifest>(&bytes) {
                        Ok(manifest) => match validate_manifest(&manifest) {
                            Ok(()) => return Ok(manifest),
                            Err(reason) => {
                                warn!("[data-update] Manifest rejected from {url}: {reason}");
                                last_err = format!("{url}: 清单校验未通过 ({reason})");
                            }
                        },
                        Err(e) => {
                            last_err = format!("{url}: 清单解析失败 ({e})");
                        }
                    }
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
        let Some(norm) = safe_manifest_rel_path(entry.name()) else {
            return Err(format!("数据包包含非法路径: {}", entry.name()));
        };
        let Some(spec) = find_bundle_spec(bundle, &norm) else {
            return Err(format!("数据包包含清单外文件: {norm}"));
        };
        if entry.size() != spec.size {
            return Err(format!("文件大小与清单不符: {norm}"));
        }
        if let Some(canonical) = canonical_rel_path(&norm) {
            let target = stage_dir.join(canonical);
            if let Some(parent) = target.parent() {
                std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            let mut out = std::fs::File::create(&target).map_err(|e| e.to_string())?;
            std::io::copy(&mut entry, &mut out).map_err(|e| e.to_string())?;
        } else {
            let mut hasher = Sha256::new();
            let mut buf = [0u8; 64 * 1024];
            let mut total = 0u64;
            loop {
                let n = entry.read(&mut buf).map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                total += n as u64;
                hasher.update(&buf[..n]);
            }
            let digest = format!("{:x}", hasher.finalize());
            if total != spec.size || digest != spec.sha256.to_ascii_lowercase() {
                return Err(format!("数据包扩展文件校验失败: {norm}"));
            }
            debug!("[data-update] Verified and skipped future overlay file: {norm}");
        }
    }

    for (rel, spec) in &bundle.files {
        let Some(canonical) = canonical_rel_path(rel) else {
            continue;
        };
        let target = stage_dir.join(canonical);
        let (sz, digest) = sha256_file(&target)?;
        if sz != spec.size || digest != spec.sha256.to_ascii_lowercase() {
            return Err(format!("解压文件校验失败: {canonical}"));
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
            continue;
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

const MAX_THUMB_BYTES: u64 = 2 * 1024 * 1024;
const MAX_THUMB_BATCH: usize = 64;

fn valid_suit_thumb_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 32
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn suit_thumbs_media_paths(app_data: &Path) -> (PathBuf, PathBuf) {
    let dir = app_data.join("data_overlay").join("media");
    (dir.join("suit_thumbs.zip"), dir.join("suit_thumbs.json"))
}

fn resolve_suit_thumbs_zip() -> Option<PathBuf> {
    if let Ok(app_data) = get_app_data_dir() {
        let candidate = suit_thumbs_media_paths(&app_data).0;
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    if let Ok(exe_dir) = get_exe_directory() {
        let fallback = exe_dir.join("resource/data/suit_thumbs.zip");
        if fallback.is_file() {
            return Some(fallback);
        }
    }
    None
}

fn validate_suit_thumbs_zip(path: &Path) -> Result<usize, String> {
    let file = std::fs::File::open(path).map_err(|e| format!("无法打开预览图包: {e}"))?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("无法解析预览图包: {e}"))?;
    let mut count = 0usize;
    for i in 0..archive.len() {
        let entry = archive.by_index(i).map_err(|e| e.to_string())?;
        if entry.is_dir() {
            continue;
        }
        if entry.is_symlink() {
            return Err("预览图包包含不允许的符号链接".into());
        }
        let valid_name = entry
            .name()
            .strip_suffix(".webp")
            .is_some_and(valid_suit_thumb_key);
        if !valid_name {
            return Err(format!("预览图包包含无效条目: {}", entry.name()));
        }
        if entry.size() == 0 || entry.size() > MAX_THUMB_BYTES {
            return Err(format!("预览图条目体积异常: {}", entry.name()));
        }
        count += 1;
    }
    if count == 0 {
        return Err("预览图包为空".into());
    }
    Ok(count)
}

fn update_suit_thumbs_status(
    app: Option<&AppHandle>,
    mutate: impl FnOnce(&mut SuitThumbsStatus),
) -> SuitThumbsStatus {
    let snapshot = if let Ok(mut guard) = suit_thumbs_status_cell().write() {
        mutate(&mut guard);
        guard.clone()
    } else {
        get_suit_thumbs_status()
    };
    if let Some(handle) = app {
        let _ = handle.emit("suit-thumbs-status", snapshot.clone());
    }
    snapshot
}

fn invalidate_suit_thumb_index() {
    let cell = SUIT_THUMBS_INDEX.get_or_init(|| Mutex::new(None));
    if let Ok(mut guard) = cell.lock() {
        *guard = None;
    }
}

fn init_suit_thumbs_status() {
    let Some(zip_path) = resolve_suit_thumbs_zip() else {
        return;
    };
    let meta = zip_path
        .with_extension("json")
        .is_file()
        .then(|| std::fs::read_to_string(zip_path.with_extension("json")).ok())
        .flatten()
        .and_then(|raw| serde_json::from_str::<SuitThumbsMeta>(&raw).ok());

    let disk_size = std::fs::metadata(&zip_path).map(|m| m.len()).unwrap_or(0);
    let count = match meta.as_ref().filter(|m| m.size == disk_size && m.count > 0) {
        Some(m) => m.count,
        None => match validate_suit_thumbs_zip(&zip_path) {
            Ok(n) => n,
            Err(err) => {
                warn!("[data-update] Ignoring invalid local suit_thumbs.zip: {err}");
                return;
            }
        },
    };
    let version = meta
        .map(|m| m.tag)
        .or_else(|| Some(format!("local-{disk_size}")));
    update_suit_thumbs_status(None, |st| {
        st.state = "ready".into();
        st.available = true;
        st.count = count;
        st.version = version;
        st.error = None;
    });
}

fn lookup_suit_thumb_indices(path: &Path, keys: &[String]) -> Result<Vec<(String, usize)>, String> {
    let stamp = file_stamp(path);
    let cell = SUIT_THUMBS_INDEX.get_or_init(|| Mutex::new(None));
    let mut guard = cell.lock().map_err(|_| "无法锁定预览图索引".to_string())?;
    let needs_rebuild = guard
        .as_ref()
        .is_none_or(|idx| idx.path != path || idx.stamp != stamp);
    if needs_rebuild {
        let file = std::fs::File::open(path).map_err(|e| format!("无法打开预览图包: {e}"))?;
        let mut archive =
            zip::ZipArchive::new(file).map_err(|e| format!("无法解析预览图包: {e}"))?;
        let mut entries = HashMap::with_capacity(archive.len());
        for i in 0..archive.len() {
            let entry = archive.by_index(i).map_err(|e| e.to_string())?;
            if entry.is_dir() || entry.is_symlink() {
                continue;
            }
            if let Some(stem) = entry.name().strip_suffix(".webp") {
                if valid_suit_thumb_key(stem) {
                    entries.insert(stem.to_owned(), i);
                }
            }
        }
        *guard = Some(SuitThumbArchiveIndex {
            path: path.to_path_buf(),
            stamp,
            entries,
        });
    }
    let idx = guard.as_ref().expect("index initialized");
    Ok(keys
        .iter()
        .filter_map(|k| idx.entries.get(k).map(|&pos| (k.clone(), pos)))
        .collect())
}

fn read_suit_thumbs_entries(keys: &[String]) -> Result<Vec<(String, Vec<u8>)>, String> {
    let valid_keys: Vec<String> = keys
        .iter()
        .filter(|k| valid_suit_thumb_key(k))
        .cloned()
        .collect();
    if valid_keys.is_empty() {
        return Ok(Vec::new());
    }
    let Some(zip_path) = resolve_suit_thumbs_zip() else {
        return Ok(Vec::new());
    };
    let matches = lookup_suit_thumb_indices(&zip_path, &valid_keys)?;
    if matches.is_empty() {
        return Ok(Vec::new());
    }
    let file = std::fs::File::open(&zip_path).map_err(|e| format!("无法读取预览图包: {e}"))?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("无法解析预览图包: {e}"))?;
    let mut out = Vec::with_capacity(matches.len());
    for (key, index) in matches {
        let entry = archive.by_index(index).map_err(|e| e.to_string())?;
        if entry.size() > MAX_THUMB_BYTES {
            continue;
        }
        let mut buf = Vec::with_capacity(entry.size() as usize);
        entry
            .take(MAX_THUMB_BYTES + 1)
            .read_to_end(&mut buf)
            .map_err(|e| e.to_string())?;
        if (buf.len() as u64) <= MAX_THUMB_BYTES && !buf.is_empty() {
            out.push((key, buf));
        }
    }
    Ok(out)
}

pub fn read_suit_thumb_webp(key: &str) -> Result<Option<Vec<u8>>, String> {
    if !valid_suit_thumb_key(key) {
        return Err("无效的套装预览图编号".into());
    }
    let mut rows = read_suit_thumbs_entries(&[key.to_owned()])?;
    Ok(rows.pop().map(|(_, bytes)| bytes))
}

fn atomic_replace_file(src: &Path, dest: &Path) -> Result<(), String> {
    let backup = dest.with_extension("zip.bak");
    let _ = std::fs::remove_file(&backup);
    let had_dest = dest.is_file();
    if had_dest {
        std::fs::rename(dest, &backup).map_err(|e| format!("备份旧预览图包失败: {e}"))?;
    }
    if let Err(err) = std::fs::rename(src, dest) {
        if had_dest {
            let _ = std::fs::rename(&backup, dest);
        }
        return Err(format!("更新预览图包失败: {err}"));
    }
    if had_dest {
        let _ = std::fs::remove_file(&backup);
    }
    Ok(())
}

async fn sync_suit_thumbs_from_manifest(
    app: &AppHandle,
    manifest: &DataManifest,
    app_data: &Path,
    proxy_url: Option<&str>,
) {
    struct SyncReset;
    impl Drop for SyncReset {
        fn drop(&mut self) {
            SUIT_THUMBS_SYNCING.store(false, Ordering::Release);
        }
    }

    if SUIT_THUMBS_SYNCING
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return;
    }
    let _reset = SyncReset;

    let Some(bundle) = manifest.bundles.get("suit_thumbs") else {
        return;
    };

    let (media_zip, meta_path) = suit_thumbs_media_paths(app_data);
    let want_hash = bundle.sha256.to_ascii_lowercase();

    if media_zip.is_file() {
        let zip_check = media_zip.clone();
        if let Ok(Ok((sz, digest))) =
            tokio::task::spawn_blocking(move || sha256_file(&zip_check)).await
        {
            if sz == bundle.size && digest == want_hash {
                let count = bundle.count.unwrap_or_else(|| {
                    std::fs::read_to_string(&meta_path)
                        .ok()
                        .and_then(|s| serde_json::from_str::<SuitThumbsMeta>(&s).ok())
                        .map(|m| m.count)
                        .unwrap_or(0)
                });
                let count = if count > 0 {
                    count
                } else {
                    let z = media_zip.clone();
                    tokio::task::spawn_blocking(move || validate_suit_thumbs_zip(&z))
                        .await
                        .ok()
                        .and_then(Result::ok)
                        .unwrap_or(0)
                };
                if count > 0 {
                    let meta = SuitThumbsMeta {
                        schema_version: 1,
                        tag: bundle.tag.clone(),
                        asset: bundle.asset.clone(),
                        size: bundle.size,
                        sha256: want_hash,
                        count,
                        updated_at: Utc::now().to_rfc3339(),
                    };
                    if let Ok(bytes) = serde_json::to_vec_pretty(&meta) {
                        let _ = write_atomic(&meta_path, &bytes);
                    }
                    update_suit_thumbs_status(Some(app), |st| {
                        st.state = "ready".into();
                        st.available = true;
                        st.count = count;
                        st.version = Some(bundle.tag.clone());
                        st.error = None;
                    });
                    return;
                }
            }
        }
    }

    let had_local = resolve_suit_thumbs_zip().is_some();
    update_suit_thumbs_status(Some(app), |st| {
        st.state = "downloading".into();
        st.available = had_local;
        st.error = None;
    });

    let dl_zip = app_data.join("data_overlay/downloads").join(&bundle.asset);
    if let Err(err) = download_bundle_archive(bundle, &dl_zip, proxy_url).await {
        warn!("[data-update] Failed to download suit_thumbs: {err}");
        let available_now = resolve_suit_thumbs_zip().is_some();
        update_suit_thumbs_status(Some(app), |st| {
            st.state = "error".into();
            st.available = available_now;
            st.error = Some(err);
        });
        return;
    }

    let part_zip = media_zip.with_extension("zip.part");
    let dl_for_copy = dl_zip.clone();
    let part_for_copy = part_zip.clone();
    let media_for_replace = media_zip.clone();
    let install_res = tokio::task::spawn_blocking(move || -> Result<usize, String> {
        if let Some(parent) = part_for_copy.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        std::fs::copy(&dl_for_copy, &part_for_copy)
            .map_err(|e| format!("复制预览图包失败: {e}"))?;
        let count = validate_suit_thumbs_zip(&part_for_copy)?;
        atomic_replace_file(&part_for_copy, &media_for_replace)?;
        Ok(count)
    })
    .await
    .map_err(|e| e.to_string())
    .and_then(|res| res);

    match install_res {
        Ok(count) => {
            invalidate_suit_thumb_index();
            let _ = std::fs::remove_file(&dl_zip);
            let meta = SuitThumbsMeta {
                schema_version: 1,
                tag: bundle.tag.clone(),
                asset: bundle.asset.clone(),
                size: bundle.size,
                sha256: want_hash,
                count,
                updated_at: Utc::now().to_rfc3339(),
            };
            if let Ok(bytes) = serde_json::to_vec_pretty(&meta) {
                let _ = write_atomic(&meta_path, &bytes);
            }
            info!(
                "[data-update] Cached {} suit thumbnails ({})",
                count, bundle.tag
            );
            update_suit_thumbs_status(Some(app), |st| {
                st.state = "ready".into();
                st.available = true;
                st.count = count;
                st.version = Some(bundle.tag.clone());
                st.error = None;
            });
        }
        Err(err) => {
            let _ = std::fs::remove_file(&part_zip);
            warn!("[data-update] Failed to install suit_thumbs: {err}");
            let available_now = resolve_suit_thumbs_zip().is_some();
            update_suit_thumbs_status(Some(app), |st| {
                st.state = "error".into();
                st.available = available_now;
                st.error = Some(err);
            });
        }
    }
}

async fn run_catalog_and_models_sync(
    app: &AppHandle,
    manifest: &DataManifest,
    app_data: &Path,
    exe_dir: &Path,
    proxy_url: Option<&str>,
    maa_state: &MaaState,
) -> Result<(), String> {
    let remote_wardrobe_ver = manifest.stats["wardrobe_version"]
        .as_str()
        .unwrap_or("")
        .to_string();
    let remote_game_items = manifest.stats["game_items"].as_u64().unwrap_or(0) as usize;

    let (builtin_ver, builtin_items) = builtin_wardrobe_metrics(exe_dir);
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
    let need_catalog = catalog_bundle.files.iter().any(|(rel, spec)| {
        canonical_rel_path(rel)
            .is_some_and(|canonical| local_matching_source(canonical, spec, exe_dir).is_none())
    });
    let need_models = models_bundle.files.iter().any(|(rel, spec)| {
        canonical_rel_path(rel)
            .is_some_and(|canonical| local_matching_source(canonical, spec, exe_dir).is_none())
    });

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
        download_bundle_archive(catalog_bundle, &zip_path, proxy_url).await?;
        let cb = catalog_bundle.clone();
        let sd = stage_dir.clone();
        tokio::task::spawn_blocking(move || extract_verified_bundle(&zip_path, &cb, &sd))
            .await
            .map_err(|e| e.to_string())??;
    } else {
        let cb = catalog_bundle.clone();
        let sd = stage_dir.clone();
        let ed = exe_dir.to_path_buf();
        tokio::task::spawn_blocking(move || populate_local_bundle(&cb, &sd, &ed))
            .await
            .map_err(|e| e.to_string())??;
    }

    if need_models {
        let zip_path = dl_dir.join(&models_bundle.asset);
        download_bundle_archive(models_bundle, &zip_path, proxy_url).await?;
        let mb = models_bundle.clone();
        let sd = stage_dir.clone();
        tokio::task::spawn_blocking(move || extract_verified_bundle(&zip_path, &mb, &sd))
            .await
            .map_err(|e| e.to_string())??;
    } else {
        let mb = models_bundle.clone();
        let sd = stage_dir.clone();
        let ed = exe_dir.to_path_buf();
        tokio::task::spawn_blocking(move || populate_local_bundle(&mb, &sd, &ed))
            .await
            .map_err(|e| e.to_string())??;
    }

    if !slot_complete(&stage_dir) {
        return Err("数据分包缺少当前版本所需文件".into());
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
        st.remote_version = Some(manifest.version.clone());
        if !remote_wardrobe_ver.is_empty() {
            st.wardrobe_version = Some(remote_wardrobe_ver);
        }
        st.updated_at = Some(record.published_at);
    });
    info!("[data-update] Activated hot-update {}", manifest.tag);
    Ok(())
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

    run_catalog_and_models_sync(
        app,
        &manifest,
        &app_data,
        &exe_dir,
        proxy_url.as_deref(),
        maa_state,
    )
    .await?;

    sync_suit_thumbs_from_manifest(app, &manifest, &app_data, proxy_url.as_deref()).await;
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

#[tauri::command]
pub fn get_suit_thumbs_status() -> SuitThumbsStatus {
    suit_thumbs_status_cell()
        .read()
        .map(|g| g.clone())
        .unwrap_or(SuitThumbsStatus {
            state: "idle".into(),
            available: false,
            count: 0,
            version: None,
            error: None,
        })
}

pub async fn sync_suit_thumbs_impl(
    app: &AppHandle,
    app_config: &AppConfigState,
) -> Result<SuitThumbsStatus, String> {
    if SUIT_THUMBS_SYNCING.load(Ordering::Acquire) {
        return Ok(get_suit_thumbs_status());
    }
    let app_data = get_app_data_dir()?;
    let (github_pat, proxy_url) = read_network_settings(app_config);
    let manifest = match fetch_manifest(github_pat.as_deref(), proxy_url.as_deref()).await {
        Ok(m) => m,
        Err(err) => {
            let available_now = resolve_suit_thumbs_zip().is_some();
            return Ok(update_suit_thumbs_status(Some(app), |st| {
                st.state = "error".into();
                st.available = available_now;
                st.error = Some(err);
            }));
        }
    };
    sync_suit_thumbs_from_manifest(app, &manifest, &app_data, proxy_url.as_deref()).await;
    Ok(get_suit_thumbs_status())
}

#[tauri::command]
pub async fn sync_suit_thumbs(
    app: AppHandle,
    app_config: State<'_, Arc<AppConfigState>>,
) -> Result<SuitThumbsStatus, String> {
    sync_suit_thumbs_impl(&app, &app_config).await
}

#[tauri::command]
pub async fn get_suit_thumbs_batch(keys: Vec<String>) -> Result<BTreeMap<String, String>, String> {
    let limited: Vec<String> = keys
        .into_iter()
        .filter(|k| valid_suit_thumb_key(k))
        .take(MAX_THUMB_BATCH)
        .collect();
    if limited.is_empty() {
        return Ok(BTreeMap::new());
    }
    tokio::task::spawn_blocking(move || {
        let entries = read_suit_thumbs_entries(&limited)?;
        let mut map = BTreeMap::new();
        for (key, bytes) in entries {
            map.insert(
                key,
                format!("data:image/webp;base64,{}", BASE64_STANDARD.encode(&bytes)),
            );
        }
        Ok(map)
    })
    .await
    .map_err(|e| e.to_string())?
}
