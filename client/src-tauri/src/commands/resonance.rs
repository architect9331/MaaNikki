//! Read-only CN resonance history. Credentials remain inside the pearpal module.
//! API/catalog reference: dastrokes/gongeo.us-nikki-tracker (MIT).
use chrono::{NaiveDateTime, Utc};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tauri::{Emitter, Manager, WebviewWindow};
use tokio::sync::watch;

const INTERVAL: i64 = 30 * 60;
const API: &str = "https://x6cn-clickhouse.nuanpaper.com/v1/tlog";
const RESPONSE_LIMIT: usize = 8 * 1024 * 1024;
type Pull = (String, String);

#[derive(Clone, Deserialize, Serialize, Default)]
struct Settings {
    enabled: bool,
    active_account: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
struct Banner {
    id: String,
    name: String,
    kind: u8,
    #[serde(default)]
    outfits: Vec<String>,
}

#[derive(Deserialize)]
struct Catalog {
    updated_at: String,
    banners: Vec<Banner>,
    items: BTreeMap<String, Value>,
    #[serde(default)]
    outfits: BTreeMap<String, Value>,
}

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
struct NotebookPull {
    card_pool_id: String,
    pool_cnt: u64,
    result: String,
    rarity: u8,
    times_from_last_five_stars: u64,
    times_from_last_four_stars: u64,
}

#[derive(Clone, Deserialize, Serialize, Default)]
struct Notebook {
    pulls: BTreeMap<String, BTreeMap<u64, NotebookPull>>,
    updated_at: Option<i64>,
}

#[derive(Clone, Deserialize, Serialize, Default)]
struct BannerHistory {
    pulls: Vec<Pull>,
    updated_at: i64,
    possible_gap: bool,
    // The oldest second in the first page of a successfully synchronized,
    // descending API window. Old/imported histories establish this on a full read.
    #[serde(default)]
    incremental_since: Option<i64>,
}

struct BannerFetch {
    pulls: Vec<Pull>,
    incremental_since: Option<i64>,
}

#[derive(Clone, Deserialize, Serialize)]
struct History {
    schema_version: u32,
    account_key: String,
    uid: String,
    region: String,
    last_success: Option<i64>,
    banners: BTreeMap<String, BannerHistory>,
    #[serde(default)]
    notebook: Notebook,
}

#[derive(Clone, Serialize)]
struct AccountSummary {
    key: String,
    uid: String,
    region: String,
    total: usize,
    notebook_total: usize,
    last_success: Option<i64>,
}

struct Inner {
    settings: Settings,
    accounts: Vec<AccountSummary>,
    generation: u64,
    requested: bool,
    busy: bool,
    phase: String,
    error: Option<String>,
    auth_paused: bool,
    failures: u32,
    next_sync: i64,
    banner: Option<String>,
    page: usize,
    completed: usize,
    last_added: usize,
    last_notebook_added: usize,
}

pub struct ResonanceState {
    inner: Mutex<Inner>,
    changed: watch::Sender<u64>,
    catalog: Catalog,
}

fn folder() -> Result<PathBuf, String> {
    Ok(super::utils::get_app_data_dir()?.join("config/resonance"))
}

fn save<T: Serialize>(path: PathBuf, value: &T) -> Result<(), String> {
    let parent = path.parent().ok_or("共鸣记录保存路径无效。")?;
    std::fs::create_dir_all(parent).map_err(|_| "无法创建共鸣记录目录。")?;
    let raw = serde_json::to_vec(value).map_err(|_| "共鸣记录编码失败。")?;
    let temporary = path.with_extension("json.tmp");
    use std::io::Write;
    let mut file = std::fs::File::create(&temporary).map_err(|_| "无法保存共鸣记录。")?;
    file.write_all(&raw)
        .and_then(|_| file.sync_all())
        .map_err(|_| "无法保存共鸣记录。")?;
    drop(file);
    std::fs::rename(&temporary, path).map_err(|_| "无法替换共鸣记录文件。".into())
}

fn valid_key(key: &str) -> bool {
    super::account_state::valid_account_key(key)
}

fn load_history(key: &str) -> Result<History, String> {
    let role = super::account_state::read_role(key)?;
    let history: History = serde_json::from_value(role["resonance"].clone())
        .map_err(|_| "无法读取账号的共鸣记录。")?;
    if history.schema_version != 2
        || history.account_key != key
        || format!("CN:{}", history.uid) != key
        || history.region != "CN"
    {
        return Err("共鸣记录版本或账号不匹配。".into());
    }
    Ok(history)
}

fn save_history(history: &History) -> Result<(), String> {
    if !valid_key(&history.account_key) || history.account_key != format!("CN:{}", history.uid) {
        return Err("共鸣记录账号无效。".into());
    }
    super::account_state::update_role(&history.account_key, |role| {
        role["resonance"] = serde_json::to_value(history).map_err(|_| "共鸣记录编码失败。")?;
        Ok(())
    })
}

fn migrate_history(old: History, marker: &str) -> Result<(), String> {
    let key = format!("CN:{}", old.uid);
    if !valid_key(&key)
        || old.region != "CN"
        || !matches!(old.schema_version, 1 | 2)
        || old.schema_version == 2 && old.account_key != key
    {
        return Err("旧共鸣记录账号无效。".into());
    }
    super::account_state::update_role(&key, |role| {
        if role["migration"]["resonance"][marker] == true {
            return Ok(());
        }
        let mut history = if role["resonance"].is_object() {
            serde_json::from_value::<History>(role["resonance"].clone())
                .map_err(|_| "共鸣记录存档格式无效。")?
        } else {
            History {
                schema_version: 2,
                account_key: key.clone(),
                uid: old.uid.clone(),
                region: "CN".into(),
                last_success: None,
                banners: BTreeMap::new(),
                notebook: Notebook::default(),
            }
        };
        if history.schema_version != 2
            || history.account_key != key
            || history.uid != old.uid
            || history.region != "CN"
        {
            return Err("共鸣记录版本或账号不匹配。".into());
        }
        for (id, entry) in old.banners {
            let current = history.banners.entry(id).or_default();
            let updated_at = current.updated_at.max(entry.updated_at);
            merge(current, entry.pulls).map_err(|e| e.message)?;
            current.possible_gap |= entry.possible_gap;
            current.updated_at = updated_at;
        }
        let notebook_time = history.notebook.updated_at.max(old.notebook.updated_at);
        let old_pulls = old
            .notebook
            .pulls
            .into_values()
            .flat_map(|rows| rows.into_values())
            .collect();
        merge_notebook(&mut history.notebook, old_pulls).map_err(|e| e.message)?;
        history.notebook.updated_at = notebook_time;
        history.last_success = history.last_success.max(old.last_success);
        role["resonance"] = serde_json::to_value(history).map_err(|_| "共鸣记录编码失败。")?;
        role["migration"]["resonance"][marker] = json!(true);
        Ok(())
    })
}

fn account_histories(settings: &mut Settings) -> Result<Vec<AccountSummary>, String> {
    let root = super::utils::get_app_data_dir()?;
    if let Some(journal) = super::account_state::read_json(&root.join("config/account_state.json"))?
    {
        if let Some(accounts) = journal["resonance"]["accounts"].as_object() {
            for (key, value) in accounts {
                let old: History =
                    serde_json::from_value(value.clone()).map_err(|_| "旧共鸣记录格式无效。")?;
                if old.schema_version != 2 || old.account_key != *key {
                    return Err("旧共鸣记录版本或账号不匹配。".into());
                }
                if super::account_state::read_role(key)?["migration"]["resonance"]["journal_v2"]
                    != true
                {
                    migrate_history(old, "journal_v2")?;
                }
            }
        }
    }
    if let Ok(entries) = std::fs::read_dir(folder()?) {
        for entry in entries.flatten() {
            let path = entry.path();
            let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            if path.extension().and_then(|s| s.to_str()) != Some("json") || stem.len() != 64 {
                continue;
            }
            let old: History = serde_json::from_value(
                super::account_state::read_json(&path)?.ok_or("旧共鸣存档不存在。")?,
            )
            .map_err(|_| "旧共鸣存档格式无效。")?;
            if old.schema_version != 1 || old.account_key != stem {
                return Err("旧共鸣存档版本或账号不匹配。".into());
            }
            let key = format!("CN:{}", old.uid);
            if settings.active_account.as_deref() == Some(stem) {
                settings.active_account = Some(key.clone());
            }
            if super::account_state::read_role(&key)?["migration"]["resonance"][stem] != true {
                migrate_history(old, stem)?;
            }
        }
    }
    save(folder()?.join("settings.json"), settings)?;
    let mut accounts = Vec::new();
    for key in super::account_state::account_keys()? {
        if super::account_state::read_role(&key)?["resonance"].is_object() {
            accounts.push(summary(&load_history(&key)?));
        }
    }
    Ok(accounts)
}

fn summary(history: &History) -> AccountSummary {
    AccountSummary {
        key: history.account_key.clone(),
        uid: history.uid.clone(),
        region: history.region.clone(),
        total: history.banners.values().map(|b| b.pulls.len()).sum(),
        notebook_total: history.notebook.pulls.values().map(|p| p.len()).sum(),
        last_success: history.last_success,
    }
}

impl ResonanceState {
    fn snapshot(&self) -> Value {
        let inner = self.inner.lock().unwrap();
        json!({
            "enabled": inner.settings.enabled, "active_account": inner.settings.active_account,
            "accounts": inner.accounts, "busy": inner.busy, "phase": inner.phase,
            "error": inner.error, "auth_paused": inner.auth_paused,
            "next_sync": if inner.settings.enabled && !inner.auth_paused { Some(inner.next_sync) } else { None },
            "banner": inner.banner, "page": inner.page, "completed": inner.completed,
            "banner_count": self.catalog.banners.len(), "last_added": inner.last_added,
            "last_notebook_added": inner.last_notebook_added,
            "catalog_updated_at": self.catalog.updated_at, "interval_minutes": INTERVAL / 60,
        })
    }

    fn publish(&self, app: &tauri::AppHandle) {
        let _ = app.emit_to("main", "resonance-status", self.snapshot());
    }

    fn progress(&self, app: &tauri::AppHandle, banner: &Banner, page: usize, completed: usize) {
        {
            let mut inner = self.inner.lock().unwrap();
            inner.banner = Some(banner.name.clone());
            inner.page = page;
            inner.completed = completed;
        }
        self.publish(app);
    }
}

pub fn start(app: &tauri::AppHandle) {
    let mut error = None;
    let mut settings = folder()
        .and_then(|f| {
            let path = f.join("settings.json");
            if !path.exists() {
                return Ok(Settings::default());
            }
            serde_json::from_slice(&std::fs::read(path).map_err(|_| "无法读取共鸣设置。")?)
                .map_err(|_| "共鸣设置文件损坏。".to_string())
        })
        .unwrap_or_else(|reason| {
            error = Some(reason);
            Settings::default()
        });
    let accounts = account_histories(&mut settings).unwrap_or_else(|reason| {
        error = Some(reason);
        Vec::new()
    });
    let next_sync = settings
        .active_account
        .as_ref()
        .and_then(|key| {
            accounts
                .iter()
                .find(|a| &a.key == key)
                .and_then(|a| a.last_success)
        })
        .map(|time| time + INTERVAL)
        .unwrap_or(0);
    let state = Arc::new(ResonanceState {
        inner: Mutex::new(Inner {
            settings,
            accounts,
            generation: 0,
            requested: false,
            busy: false,
            phase: "idle".into(),
            error,
            auth_paused: false,
            failures: 0,
            next_sync,
            banner: None,
            page: 0,
            completed: 0,
            last_added: 0,
            last_notebook_added: 0,
        }),
        changed: watch::channel(0).0,
        catalog: serde_json::from_str(include_str!("../data/resonance_catalog.json"))
            .expect("embedded resonance catalog"),
    });
    app.manage(state.clone());
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        worker(app, state).await;
    });
}

pub fn login_changed(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Arc<ResonanceState>>() {
        {
            let mut inner = state.inner.lock().unwrap();
            inner.generation += 1;
            inner.auth_paused = false;
            inner.error = None;
            inner.failures = 0;
            inner.next_sync = 0;
        }
        state
            .changed
            .send_modify(|version| *version = version.wrapping_add(1));
        state.publish(app);
    }
}

fn desktop(window: &WebviewWindow) -> Result<(), String> {
    if window.label() != "main" {
        return Err("请在主窗口操作共鸣记录。".into());
    }
    Ok(())
}

#[tauri::command]
pub fn resonance_status(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<ResonanceState>>,
) -> Result<Value, String> {
    desktop(&window)?;
    Ok(state.snapshot())
}

#[tauri::command]
pub fn resonance_set_enabled(
    app: tauri::AppHandle,
    window: WebviewWindow,
    state: tauri::State<'_, Arc<ResonanceState>>,
    enabled: bool,
) -> Result<Value, String> {
    desktop(&window)?;
    {
        let mut inner = state.inner.lock().unwrap();
        let mut settings = inner.settings.clone();
        settings.enabled = enabled;
        save(folder()?.join("settings.json"), &settings)?;
        inner.settings = settings;
        inner.generation += 1;
        inner.auth_paused = false;
        inner.error = None;
        inner.next_sync = 0;
        inner.requested = false;
    }
    state
        .changed
        .send_modify(|version| *version = version.wrapping_add(1));
    state.publish(&app);
    Ok(state.snapshot())
}

#[tauri::command]
pub fn resonance_sync(
    app: tauri::AppHandle,
    window: WebviewWindow,
    state: tauri::State<'_, Arc<ResonanceState>>,
) -> Result<Value, String> {
    desktop(&window)?;
    {
        let mut inner = state.inner.lock().unwrap();
        if inner.busy {
            return Ok(json!({"accepted": false}));
        }
        inner.requested = true;
        inner.auth_paused = false;
        inner.next_sync = 0;
    }
    state
        .changed
        .send_modify(|version| *version = version.wrapping_add(1));
    state.publish(&app);
    Ok(json!({"accepted": true}))
}

#[tauri::command]
pub fn resonance_history(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<ResonanceState>>,
    account_key: String,
) -> Result<Value, String> {
    desktop(&window)?;
    let history = load_history(&account_key)?;
    let mut items: BTreeMap<&str, &Value> = BTreeMap::new();
    for banner in history.banners.values() {
        for (_, item) in &banner.pulls {
            if let Some(info) = state.catalog.items.get(item) {
                items.insert(item, info);
            }
        }
    }
    for banner in history.notebook.pulls.values() {
        for pull in banner.values() {
            if let Some(info) = state.catalog.items.get(&pull.result) {
                items.insert(&pull.result, info);
            }
        }
    }
    // Include all outfit parts so missing pieces have names too.
    for outfit in state.catalog.outfits.values() {
        if let Some(parts) = outfit["items"].as_array() {
            for part in parts.iter().filter_map(Value::as_str) {
                if let Some(info) = state.catalog.items.get(part) {
                    items.insert(part, info);
                }
            }
        }
    }
    Ok(
        json!({"history": history, "banners": state.catalog.banners, "items": items, "outfits": state.catalog.outfits}),
    )
}

#[tauri::command]
pub fn resonance_export(window: WebviewWindow, account_key: String) -> Result<String, String> {
    desktop(&window)?;
    let history = load_history(&account_key)?;
    let pulls: BTreeMap<_, _> = history
        .banners
        .iter()
        .map(|(id, b)| (id, b.pulls.iter().rev().collect::<Vec<_>>()))
        .collect();
    let pearpal: BTreeMap<_, _> = history.notebook.pulls.iter().map(|(id, rows)| {
        (id, rows.values().map(|row| json!({
            "card_pool_id": row.card_pool_id, "pool_cnt": row.pool_cnt, "result": row.result,
            "rarity": row.rarity.to_string(), "times_from_last_five_stars": row.times_from_last_five_stars,
            "times_from_last_four_stars": row.times_from_last_four_stars,
        })).collect::<Vec<_>>())
    }).collect();
    serde_json::to_string_pretty(&json!({
        "schema_version": 1, "source": "MaaNikki", "uid": history.uid,
        "region": history.region, "exported_at": Utc::now().to_rfc3339(), "pulls": pulls, "pearpal": pearpal,
    }))
    .map_err(|_| "共鸣记录导出失败。".into())
}

#[derive(Debug)]
struct SyncError {
    message: String,
    auth: bool,
}
impl From<String> for SyncError {
    fn from(message: String) -> Self {
        Self {
            message,
            auth: false,
        }
    }
}
fn failure(message: &str) -> SyncError {
    message.to_string().into()
}
fn auth_error() -> SyncError {
    SyncError {
        message: "美鸭梨登录已过期，请重新登录。".into(),
        auth: true,
    }
}

fn require_success(response: &Value, operation: &str) -> Result<(), SyncError> {
    let code = response["code"]
        .as_i64()
        .ok_or_else(|| failure("共鸣记录接口格式发生变化。"))?;
    if code == 0 {
        return Ok(());
    }
    // Pause only when the service explicitly identifies an authentication failure.
    // Other business errors retain retry behavior; never echo remote text or credentials.
    let info = response["info"].as_str().unwrap_or("").to_lowercase();
    let auth = ["token", "登录", "登陆", "身份", "鉴权", "认证", "auth"]
        .iter()
        .any(|word| info.contains(word));
    let invalid = [
        "过期",
        "无效",
        "失效",
        "失败",
        "invalid",
        "expired",
        "unauthorized",
    ]
    .iter()
    .any(|word| info.contains(word));
    if (auth && invalid) || info.contains("未登录") || info.contains("not logged") {
        return Err(auth_error());
    }
    Err(failure(&format!(
        "{operation}失败（代码 {code}），稍后重试。"
    )))
}

// Do not propagate reqwest errors: verify URLs contain authentication query parameters.
async fn response_json(request: reqwest::RequestBuilder) -> Result<Value, SyncError> {
    response_value(request, false).await
}

async fn response_value(
    request: reqwest::RequestBuilder,
    compressed: bool,
) -> Result<Value, SyncError> {
    let response = request
        .send()
        .await
        .map_err(|_| failure("共鸣记录连接失败，稍后自动重试。"))?;
    if matches!(response.status().as_u16(), 401 | 403) {
        return Err(auth_error());
    }
    let response = response
        .error_for_status()
        .map_err(|_| failure("共鸣记录服务暂不可用。"))?;
    let mut stream = response.bytes_stream();
    let mut raw = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| failure("共鸣记录下载中断。"))?;
        if raw.len() + chunk.len() > RESPONSE_LIMIT {
            return Err(failure("共鸣记录响应过大。"));
        }
        raw.extend_from_slice(&chunk);
    }
    if compressed
        && !matches!(
            raw.iter().find(|b| !b.is_ascii_whitespace()),
            Some(b'{') | Some(b'[')
        )
    {
        let length =
            snap::raw::decompress_len(&raw).map_err(|_| failure("美鸭梨手账压缩格式发生变化。"))?;
        if length > RESPONSE_LIMIT {
            return Err(failure("美鸭梨手账解压数据过大。"));
        }
        raw = snap::raw::Decoder::new()
            .decompress_vec(&raw)
            .map_err(|_| failure("美鸭梨手账解压失败。"))?;
    }
    serde_json::from_slice(&raw).map_err(|_| failure("共鸣记录接口格式发生变化。"))
}

async fn fetch_notebook(
    client: &reqwest::Client,
    token: &str,
    openid: &str,
) -> Result<Vec<NotebookPull>, SyncError> {
    // This endpoint returns one compressed snapshot. Observed records are
    // grouped by pool with increasing pool_cnt; the sequence is an identity,
    // not a pagination cursor. Keep this independent of paged resonance history.
    let response = response_value(
        client
            .post("https://myl-api.nuanpaper.com/v1/strategy/user/note/book/info")
            .json(&json!({"client_id": 1106, "token": token, "openid": openid})),
        true,
    )
    .await?;
    if response.get("code").is_some() {
        require_success(&response, "美鸭梨手账获取")?;
    }
    let rows = response["info_from_self"]["gacha_list"]
        .as_array()
        .ok_or_else(|| failure("美鸭梨手账记录格式发生变化。"))?;
    let number = |value: &Value| {
        value
            .as_u64()
            .or_else(|| value.as_str().and_then(|s| s.parse().ok()))
            .ok_or_else(|| failure("美鸭梨手账抽数格式发生变化。"))
    };
    rows.iter()
        .map(|row| {
            let card_pool_id = row["card_pool_id"]
                .as_str()
                .filter(|s| !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit()))
                .ok_or_else(|| failure("美鸭梨手账卡池编号无效。"))?
                .to_owned();
            let result = row["result"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| failure("美鸭梨手账物品编号无效。"))?
                .to_owned();
            let rarity = u8::try_from(number(&row["rarity"])?)
                .map_err(|_| failure("美鸭梨手账星级无效。"))?;
            if !(1..=5).contains(&rarity) {
                return Err(failure("美鸭梨手账星级无效。"));
            }
            Ok(NotebookPull {
                card_pool_id,
                result,
                rarity,
                pool_cnt: number(&row["pool_cnt"])?,
                times_from_last_five_stars: number(&row["times_from_last_five_stars"])?,
                times_from_last_four_stars: number(&row["times_from_last_four_stars"])?,
            })
        })
        .collect()
}

fn merge_notebook(previous: &mut Notebook, fetched: Vec<NotebookPull>) -> Result<usize, SyncError> {
    let snapshot_rows = fetched.len();
    let mut additions = BTreeMap::<String, BTreeMap<u64, NotebookPull>>::new();
    for pull in fetched {
        let saved = previous
            .pulls
            .get(&pull.card_pool_id)
            .and_then(|rows| rows.get(&pull.pool_cnt))
            .or_else(|| {
                additions
                    .get(&pull.card_pool_id)
                    .and_then(|rows| rows.get(&pull.pool_cnt))
            });
        if let Some(saved) = saved {
            // The API's per-pool pull ordinal is the stable identity. Preserve
            // the previous snapshot on conflict instead of adding both drops.
            if saved != &pull {
                return Err(failure("美鸭梨手账同一抽数的记录发生冲突，已保留原记录。"));
            }
        } else {
            additions
                .entry(pull.card_pool_id.clone())
                .or_default()
                .insert(pull.pool_cnt, pull);
        }
    }
    // Validate the complete snapshot before mutating the archive. Repeated
    // snapshots allocate only new records instead of cloning all saved pulls;
    // conflicts, including duplicates within the response, leave it untouched.
    let added = additions.values().map(|rows| rows.len()).sum();
    for (pool, rows) in additions {
        previous.pulls.entry(pool).or_default().extend(rows);
    }
    previous.updated_at = Some(Utc::now().timestamp());
    log::debug!(
        "Resonance notebook: single snapshot, {} records, {} added",
        snapshot_rows,
        added
    );
    Ok(added)
}

fn timestamp(value: &str) -> Option<i64> {
    if let Ok(number) = value.parse::<i64>() {
        return Some(if number > 10_000_000_000 {
            number / 1000
        } else {
            number
        });
    }
    NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S")
        .ok()
        .map(|time| time.and_utc().timestamp())
        .or_else(|| {
            chrono::DateTime::parse_from_rfc3339(value)
                .ok()
                .map(|time| time.timestamp())
        })
}

// A complete API window contains all occurrences of each (time, item) pair.
// Multiset union uses the larger count, preserving real repeated drops within a second.
// Full reads cover the entire API window. Incremental reads include every row at
// their boundary second; a partial older second is excluded before merging.
fn merge(previous: &mut BannerHistory, fetched: Vec<Pull>) -> Result<usize, SyncError> {
    let oldest = fetched.iter().filter_map(|p| timestamp(&p.0)).min();
    let newest_saved = previous.pulls.iter().filter_map(|p| timestamp(&p.0)).max();
    if let (Some(oldest), Some(newest)) = (oldest, newest_saved) {
        if oldest > newest {
            previous.possible_gap = true;
        }
    }
    let mut saved = BTreeMap::<Pull, usize>::new();
    for pull in &previous.pulls {
        *saved.entry(pull.clone()).or_default() += 1;
    }
    let mut seen = BTreeMap::<Pull, usize>::new();
    let before = previous.pulls.len();
    for pull in fetched {
        if timestamp(&pull.0).is_none() || pull.1.is_empty() {
            return Err(failure("共鸣记录时间或物品格式发生变化。"));
        }
        let count = seen.entry(pull.clone()).or_default();
        *count += 1;
        if *count > *saved.get(&pull).unwrap_or(&0) {
            previous.pulls.push(pull);
        }
    }
    previous
        .pulls
        .sort_by_key(|pull| timestamp(&pull.0).unwrap_or(0));
    previous.updated_at = Utc::now().timestamp();
    Ok(previous.pulls.len() - before)
}

fn notice(app: &tauri::AppHandle, message: &str) {
    let _ = app.emit_to("main", "resonance-notice", message);
    let visible = app
        .get_webview_window("main")
        .map(|window| {
            window.is_visible().unwrap_or(false) && !window.is_minimized().unwrap_or(true)
        })
        .unwrap_or(false);
    if !visible {
        let _ = notify_rust::Notification::new()
            .summary("共鸣记录")
            .body(message)
            .show();
    }
}

async fn worker(app: tauri::AppHandle, state: Arc<ResonanceState>) {
    let mut changes = state.changed.subscribe();
    loop {
        changes.borrow_and_update();
        let generation = {
            let mut inner = state.inner.lock().unwrap();
            if (inner.requested || (inner.settings.enabled && !inner.auth_paused))
                && Utc::now().timestamp() >= inner.next_sync
            {
                inner.requested = false;
                inner.busy = true;
                inner.phase = "syncing".into();
                inner.error = None;
                inner.banner = None;
                inner.completed = 0;
                inner.last_added = 0;
                inner.last_notebook_added = 0;
                Some(inner.generation)
            } else {
                None
            }
        };
        if let Some(generation) = generation {
            state.publish(&app);
            let outcome = tokio::select! {
                result = synchronize(&app, &state, generation) => Some(result),
                _ = async {
                    loop {
                        let changed = state.inner.lock().unwrap().generation != generation;
                        if changed { break; }
                        if changes.changed().await.is_err() { break; }
                    }
                } => None,
            };
            let mut message = None;
            {
                let mut inner = state.inner.lock().unwrap();
                inner.busy = false;
                inner.banner = None;
                inner.phase = "idle".into();
                if inner.generation == generation {
                    match outcome {
                        Some(Ok(added)) => {
                            inner.last_added = added;
                            inner.failures = 0;
                            inner.next_sync = Utc::now().timestamp() + INTERVAL;
                        }
                        Some(Err(error)) => {
                            inner.phase = if error.auth {
                                "login_required"
                            } else {
                                "error"
                            }
                            .into();
                            inner.error = Some(error.message.clone());
                            inner.auth_paused = error.auth;
                            inner.failures = inner.failures.saturating_add(1);
                            inner.next_sync = Utc::now().timestamp()
                                + (60_i64 * 2_i64.pow(inner.failures.min(6) - 1)).min(INTERVAL);
                            if error.auth {
                                message = Some(error.message);
                            }
                        }
                        None => {}
                    }
                }
                if inner.generation == generation
                    && (inner.last_added > 0 || inner.last_notebook_added > 0)
                {
                    let mut parts = Vec::new();
                    if inner.last_added > 0 {
                        parts.push(format!("共鸣历史新增 {} 条", inner.last_added));
                    }
                    if inner.last_notebook_added > 0 {
                        parts.push(format!("手账新增 {} 条", inner.last_notebook_added));
                    }
                    let added = format!("{}。", parts.join("，"));
                    message = Some(match message {
                        Some(reason) => format!("{added}{reason}"),
                        None => added,
                    });
                }
            }
            state.publish(&app);
            if let Some(message) = message {
                notice(&app, &message);
            }
            continue;
        }
        // Recheck wall time after resume rather than relying on a long monotonic sleep.
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(30)) => {},
            _ = changes.changed() => {},
        }
    }
}

async fn synchronize(
    app: &tauri::AppHandle,
    state: &ResonanceState,
    generation: u64,
) -> Result<usize, SyncError> {
    let credentials = super::pearpal::credentials().map_err(|message| SyncError {
        message,
        auth: true,
    })?;
    let token = credentials["token"].as_str().ok_or_else(auth_error)?;
    let openid = credentials["openid"].as_str().ok_or_else(auth_error)?;
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(25))
        .build()
        .map_err(|_| failure("无法创建共鸣记录连接。"))?;
    let info = response_json(
        client
            .post("https://myl-api.nuanpaper.com/v1/strategy/user/info/get")
            .json(&json!({"client_id":1106,"token":token,"openid":openid})),
    )
    .await?;
    require_success(&info, "美鸭梨账号获取")?;
    let uid = match &info["data"]["role"]["uid"] {
        Value::String(uid) => uid.clone(),
        Value::Number(uid) => uid.to_string(),
        _ => return Err(failure("美鸭梨尚未绑定游戏角色，请在美鸭梨完成绑定。")),
    };
    if uid.is_empty()
        || !uid.bytes().all(|c| c.is_ascii_digit())
        || info["data"]["role"]["zone_id"].as_u64() != Some(1)
    {
        return Err(failure("请登录美鸭梨国服角色。"));
    }
    let account_key = format!("CN:{uid}");
    let role = super::account_state::read_role(&account_key)?;
    let mut history = if role["resonance"].is_object() {
        load_history(&account_key)?
    } else {
        History {
            schema_version: 2,
            account_key: account_key.clone(),
            uid,
            region: "CN".into(),
            last_success: None,
            banners: BTreeMap::new(),
            notebook: Notebook::default(),
        }
    };
    {
        let mut inner = state.inner.lock().unwrap();
        if inner.generation != generation {
            return Err(failure("同步已取消。"));
        }
        let mut settings = inner.settings.clone();
        settings.active_account = Some(account_key.clone());
        save(folder()?.join("settings.json"), &settings)?;
        inner.settings = settings;
    }
    let mut added = 0;
    let mut notebook_added = 0;
    let mut errors: Vec<SyncError> = Vec::new();
    {
        let mut inner = state.inner.lock().unwrap();
        inner.banner = Some("美鸭梨手账".into());
        inner.page = 0;
    }
    state.publish(app);
    match fetch_notebook(&client, token, openid)
        .await
        .and_then(|rows| merge_notebook(&mut history.notebook, rows))
    {
        Ok(count) => {
            notebook_added = count;
            commit_history(app, state, generation, &history, added, notebook_added)?;
        }
        Err(error) => errors.push(error),
    }
    let authorization = async {
        let verification = response_json(client.get(format!("{API}/verify")).query(&[
            ("token", token),
            ("roleid", history.uid.as_str()),
            ("id", openid),
        ]))
        .await?;
        require_success(&verification, "共鸣记录授权")?;
        verification["data"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .ok_or_else(|| failure("共鸣记录授权格式发生变化。"))
    }
    .await;
    let end_time = Utc::now().timestamp().to_string();
    if let Ok(authority) = &authorization {
        for (completed, banner) in state.catalog.banners.iter().enumerate() {
            if completed > 0 {
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
            let mut entry = history.banners.get(&banner.id).cloned().unwrap_or_default();
            let fetched = fetch_banner(
                &client, authority, &end_time, banner, &entry, app, state, completed,
            )
            .await;
            match fetched {
                Ok(fetched) => {
                    let count = merge(&mut entry, fetched.pulls)?;
                    entry.incremental_since = fetched.incremental_since;
                    history.banners.insert(banner.id.clone(), entry);
                    added += count;
                    commit_history(app, state, generation, &history, added, notebook_added)?;
                }
                Err(error) => {
                    log::warn!("Resonance banner {}: {}", banner.id, error.message);
                    let auth = error.auth;
                    errors.push(error);
                    if auth {
                        break;
                    }
                }
            }
            state.inner.lock().unwrap().completed = completed + 1;
        }
    } else if let Err(error) = authorization {
        errors.push(error);
    }
    if !errors.is_empty() {
        return Err(SyncError {
            auth: errors.iter().any(|e| e.auth),
            message: format!(
                "{} 项同步未完成：{} 已保存成功获取的记录。",
                errors.len(),
                errors[0].message
            ),
        });
    }
    history.last_success = Some(Utc::now().timestamp());
    commit_history(app, state, generation, &history, added, notebook_added)?;
    Ok(added)
}

fn commit_history(
    app: &tauri::AppHandle,
    state: &ResonanceState,
    generation: u64,
    history: &History,
    added: usize,
    notebook_added: usize,
) -> Result<(), SyncError> {
    let mut inner = state.inner.lock().unwrap();
    if inner.generation != generation {
        return Err(failure("同步已取消。"));
    }
    save_history(history)?;
    inner.last_added = added;
    inner.last_notebook_added = notebook_added;
    inner
        .accounts
        .retain(|account| account.key != history.account_key);
    inner.accounts.push(summary(history));
    drop(inner);
    state.publish(app);
    Ok(())
}

async fn fetch_banner(
    client: &reqwest::Client,
    authority: &str,
    end_time: &str,
    banner: &Banner,
    previous: &BannerHistory,
    app: &tauri::AppHandle,
    state: &ResonanceState,
    completed: usize,
) -> Result<BannerFetch, SyncError> {
    let mut pulls = Vec::new();
    // The previous first page provides a page-sized overlap without assuming a
    // fixed number of rows or a wall-clock lookback. A checkpoint is written
    // only after a successful full/closed incremental window, never from an
    // imported history or a partially fetched page sequence.
    let since = previous.incremental_since;
    let mut expected_boundary = BTreeMap::<Pull, usize>::new();
    if let Some(since) = since {
        for pull in &previous.pulls {
            if timestamp(&pull.0) == Some(since) {
                *expected_boundary.entry(pull.clone()).or_default() += 1;
            }
        }
    }
    let mut allow_incremental = !expected_boundary.is_empty();
    let mut seen_boundary = BTreeMap::<Pull, usize>::new();
    let mut descending = true;
    let mut last_time = None;
    let mut head_since = None;
    // Resource bound only; never commit an unfinished API window.
    for page in 1..=10_000 {
        state.progress(app, banner, page, completed);
        let response = response_json(
            client
                .get(format!("{API}/query"))
                .header("X-Authority", authority)
                .query(&[
                    ("page", page.to_string()),
                    ("args", banner.id.clone()),
                    ("name", "gacha".into()),
                    ("etime", end_time.into()),
                ]),
        )
        .await?;
        require_success(&response, "卡池记录获取")?;
        let rows = parse_page(&response["data"])?;
        let end = response["data"]["end"]
            .as_bool()
            .ok_or_else(|| failure("共鸣记录分页格式发生变化。"))?;
        let mut crossed_boundary = false;
        // Real API responses are observed newest-first, including page
        // boundaries and repeated timestamps. Keep checking each page and
        // its boundary; unexpected order finishes the full read and clears
        // the incremental checkpoint.
        for pull in &rows {
            let time = timestamp(&pull.0).ok_or_else(|| failure("共鸣记录时间格式发生变化。"))?;
            if last_time.is_some_and(|last| time > last) {
                if descending {
                    log::warn!(
                        "Resonance banner {}: non-descending API order; reading full history",
                        banner.id
                    );
                }
                descending = false;
                allow_incremental = false;
            }
            last_time = Some(time);
            if page == 1 {
                head_since = Some(head_since.map_or(time, |head: i64| head.min(time)));
            }
            if let Some(since) = since {
                crossed_boundary |= time < since;
                if time == since {
                    *seen_boundary.entry(pull.clone()).or_default() += 1;
                }
            }
        }
        if rows.is_empty() && !end {
            descending = false;
            allow_incremental = false;
        }
        pulls.extend(rows);
        if end {
            log::debug!(
                "Resonance banner {}: full window, {} pages, {} records",
                banner.id,
                page,
                pulls.len()
            );
            return Ok(BannerFetch {
                pulls,
                incremental_since: if descending { head_since } else { None },
            });
        }
        if allow_incremental && crossed_boundary {
            let overlap_complete = expected_boundary
                .iter()
                .all(|(pull, count)| seen_boundary.get(pull).unwrap_or(&0) >= count);
            if overlap_complete {
                if let Some(since) = since {
                    // The first strictly older second may be split across the
                    // next page. Exclude it entirely so multiset merging sees
                    // complete counts for every included second, including ten
                    // pulls with repeated items in the same second.
                    pulls.retain(|pull| timestamp(&pull.0).is_some_and(|time| time >= since));
                    log::debug!(
                        "Resonance banner {}: incremental window, {} pages, {} records",
                        banner.id,
                        page,
                        pulls.len()
                    );
                    return Ok(BannerFetch {
                        pulls,
                        incremental_since: head_since.map(|head| head.max(since)),
                    });
                }
            }
            // Retention or a changed response can remove the saved boundary.
            // Finish this same request sequence in full instead of committing
            // an unproven overlap or discarding the existing archive.
            log::warn!(
                "Resonance banner {}: saved overlap missing; reading full history",
                banner.id
            );
            allow_incremental = false;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    Err(failure("共鸣记录页数过多，本次保留原记录。"))
}

fn parse_page(data: &Value) -> Result<Vec<Pull>, SyncError> {
    let rows = data["datas"]
        .as_array()
        .ok_or_else(|| failure("共鸣记录内容格式发生变化。"))?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    // The live API returns [time, GachaResult, GachaCostType]. Read the title
    // schema instead of deserializing a fixed pair, so metadata never becomes
    // a record or breaks history import when the service adds a column.
    let columns = data["title"]
        .as_array()
        .ok_or_else(|| failure("共鸣记录缺少列定义。"))?;
    let column = |name: &str| -> Result<usize, SyncError> {
        let positions: Vec<_> = columns
            .iter()
            .enumerate()
            .filter_map(|(index, value)| (value.as_str() == Some(name)).then_some(index))
            .collect();
        if positions.len() != 1 {
            return Err(failure("共鸣记录列定义发生变化。"));
        }
        Ok(positions[0])
    };
    let time = column("time")?;
    let item = column("GachaResult")?;
    rows.iter()
        .map(|row| {
            let row = row
                .as_array()
                .filter(|row| row.len() == columns.len())
                .ok_or_else(|| failure("共鸣记录行与列定义不一致。"))?;
            let time = row[time]
                .as_str()
                .filter(|value| timestamp(value).is_some())
                .ok_or_else(|| failure("共鸣记录时间格式发生变化。"))?;
            let item = row[item]
                .as_str()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| failure("共鸣记录物品格式发生变化。"))?;
            Ok((time.to_owned(), item.to_owned()))
        })
        .collect()
}
