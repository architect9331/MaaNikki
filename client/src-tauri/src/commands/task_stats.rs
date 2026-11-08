use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;
use std::sync::{LazyLock, Mutex};
use std::time::Instant;

use chrono::{DateTime, Local, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Emitter};

use super::utils::{get_app_data_dir, write_atomic};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TaskStatRecord {
    pub runs: u64,
    pub completed: u64,
    pub total_duration_secs: u64,
    pub estimated_saved_secs: u64,
    pub last_run_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskStatisticsFile {
    pub version: u32,
    pub first_recorded_at: Option<String>,
    pub total_runs: u64,
    pub completed_runs: u64,
    pub total_duration_secs: u64,
    pub total_saved_secs: u64,
    #[serde(default)]
    pub assistive_runs: u64,
    #[serde(default)]
    pub assistive_duration_secs: u64,
    #[serde(default)]
    pub active_days: Vec<String>,
    #[serde(default)]
    pub tasks: BTreeMap<String, TaskStatRecord>,
    #[serde(default)]
    pub assistive_tasks: BTreeMap<String, TaskStatRecord>,
    #[serde(default)]
    pub assist_counters: BTreeMap<String, u64>,
    #[serde(default)]
    pub processed_perf_files: BTreeMap<String, BTreeMap<String, u64>>,
}

impl Default for TaskStatisticsFile {
    fn default() -> Self {
        Self {
            version: 2,
            first_recorded_at: None,
            total_runs: 0,
            completed_runs: 0,
            total_duration_secs: 0,
            total_saved_secs: 0,
            assistive_runs: 0,
            assistive_duration_secs: 0,
            active_days: Vec::new(),
            tasks: BTreeMap::new(),
            assistive_tasks: BTreeMap::new(),
            assist_counters: BTreeMap::new(),
            processed_perf_files: BTreeMap::new(),
        }
    }
}

struct ActiveTaskRun {
    instance_id: String,
    task_name: String,
    started_at: Option<Instant>,
}

static ACTIVE_TASKS: LazyLock<Mutex<HashMap<(String, i64), ActiveTaskRun>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static STATS_FILE_LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));

fn stats_file_path() -> Result<PathBuf, String> {
    Ok(get_app_data_dir()?
        .join("config")
        .join("task_statistics.json"))
}

fn is_assistive_task(instance_id: &str, task_name: &str) -> bool {
    matches!(task_name, "exploration" | "current_fishing" | "fishing")
        || instance_id == "maanikki-exploration"
        || instance_id == "maanikki-fishing"
}

fn normalize_assistive_name(task_name: &str) -> &'static str {
    match task_name {
        "current_fishing" | "fishing" => "current_fishing",
        _ => "exploration",
    }
}

fn manual_time_profile(task_name: &str) -> (u64, u64) {
    match task_name {
        "realm_challenge" => (180, 45),
        "zhaoxi" => (240, 60),
        "xinghai" => (210, 60),
        "starlight_crystals" => (240, 60),
        "home" => (180, 45),
        "dig" => (60, 20),
        "monthly_pass" => (35, 15),
        "mailbox" => (35, 15),
        "weekly_free_gifts" => (50, 20),
        "monthly_crystal_exchange" => (50, 20),
        "crown" => (360, 90),
        "glow_material_prep" => (150, 45),
        "wardrobe" => (240, 60),
        "custom_route" => (180, 45),
        "__MXU_PRETASK__start_game" | "start_game" => (60, 15),
        _ => (90, 30),
    }
}

fn estimate_saved_secs(task_name: &str, actual_duration_secs: u64, succeeded: bool) -> u64 {
    if !succeeded {
        return (actual_duration_secs / 2).max(5);
    }
    let (min_manual, overhead) = manual_time_profile(task_name);
    actual_duration_secs.max(min_manual) + overhead
}
fn infer_earliest_date() -> Option<String> {
    let data_dir = get_app_data_dir().ok()?;
    let candidates = [
        data_dir.join("config").join("config.json"),
        data_dir.join("config").join("account_state.json"),
        data_dir.join("config").join("accounts").join("index.json"),
    ];
    let mut earliest: Option<std::time::SystemTime> = None;
    for path in candidates {
        if let Ok(meta) = std::fs::metadata(&path) {
            let stamp = meta.created().or_else(|_| meta.modified()).ok();
            if let Some(time) = stamp {
                earliest = Some(match earliest {
                    Some(prev) if prev <= time => prev,
                    _ => time,
                });
            }
        }
    }
    earliest.map(|time| DateTime::<Utc>::from(time).to_rfc3339())
}

fn sync_assist_performance_counters(stats: &mut TaskStatisticsFile) -> bool {
    let Ok(data_dir) = get_app_data_dir() else {
        return false;
    };
    let perf_dir = data_dir.join("logs").join("performance");
    let Ok(entries) = std::fs::read_dir(&perf_dir) else {
        return false;
    };
    let mut changed = false;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Some(filename) = path
            .file_name()
            .and_then(|n| n.to_str())
            .map(|s| s.to_string())
        else {
            continue;
        };
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(doc) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let Some(counters) = doc.get("counters").and_then(|v| v.as_object()) else {
            continue;
        };
        let mut current_file_counts = BTreeMap::new();
        for (raw_key, key) in [
            ("assist.Pickup", "Pickup"),
            ("assist.Dialogue", "Dialogue"),
            ("assist.Clear", "Clear"),
            ("assist.Fishing", "Fishing"),
        ] {
            if let Some(val) = counters.get(raw_key).and_then(|v| v.as_u64()) {
                if val > 0 {
                    current_file_counts.insert(key.to_string(), val);
                }
            }
        }
        if current_file_counts.is_empty() {
            continue;
        }
        let prev_counts = stats
            .processed_perf_files
            .get(&filename)
            .cloned()
            .unwrap_or_default();
        if prev_counts != current_file_counts {
            for (k, new_val) in &current_file_counts {
                let old_val = prev_counts.get(k).copied().unwrap_or(0);
                if *new_val > old_val {
                    let delta = *new_val - old_val;
                    let total = stats.assist_counters.entry(k.clone()).or_insert(0);
                    *total = total.saturating_add(delta);
                }
            }
            stats
                .processed_perf_files
                .insert(filename, current_file_counts);
            changed = true;
        }
    }
    if stats.processed_perf_files.len() > 500 {
        let excess = stats.processed_perf_files.len() - 500;
        let keys: Vec<String> = stats
            .processed_perf_files
            .keys()
            .take(excess)
            .cloned()
            .collect();
        for k in keys {
            stats.processed_perf_files.remove(&k);
        }
        changed = true;
    }
    changed
}

fn migrate_and_recompute(stats: &mut TaskStatisticsFile) -> bool {
    let mut changed = false;
    for legacy_key in ["exploration", "current_fishing", "fishing"] {
        if let Some(removed) = stats.tasks.remove(legacy_key) {
            let target_key = normalize_assistive_name(legacy_key).to_string();
            let entry = stats.assistive_tasks.entry(target_key).or_default();
            entry.runs = entry.runs.saturating_add(removed.runs);
            entry.completed = entry.completed.saturating_add(removed.completed);
            entry.total_duration_secs = entry
                .total_duration_secs
                .saturating_add(removed.total_duration_secs);
            entry.estimated_saved_secs = 0;
            if removed.last_run_at > entry.last_run_at {
                entry.last_run_at = removed.last_run_at;
            }
            changed = true;
        }
    }
    for entry in stats.assistive_tasks.values_mut() {
        if entry.estimated_saved_secs != 0 {
            entry.estimated_saved_secs = 0;
            changed = true;
        }
    }
    if stats.version < 2 || changed {
        stats.version = 2;
        stats.total_runs = stats.tasks.values().map(|r| r.runs).sum();
        stats.completed_runs = stats.tasks.values().map(|r| r.completed).sum();
        stats.total_duration_secs = stats.tasks.values().map(|r| r.total_duration_secs).sum();
        stats.total_saved_secs = stats.tasks.values().map(|r| r.estimated_saved_secs).sum();
        stats.assistive_runs = stats.assistive_tasks.values().map(|r| r.runs).sum();
        stats.assistive_duration_secs = stats
            .assistive_tasks
            .values()
            .map(|r| r.total_duration_secs)
            .sum();
        changed = true;
    }
    if sync_assist_performance_counters(stats) {
        changed = true;
    }
    changed
}

fn load_stats_unlocked() -> TaskStatisticsFile {
    let mut stats = stats_file_path()
        .ok()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|text| serde_json::from_str::<TaskStatisticsFile>(&text).ok())
        .unwrap_or_default();
    if stats.first_recorded_at.is_none() {
        stats.first_recorded_at = infer_earliest_date();
    }
    if migrate_and_recompute(&mut stats) {
        save_stats_unlocked(&stats);
    }
    stats
}

fn save_stats_unlocked(stats: &TaskStatisticsFile) {
    let Ok(path) = stats_file_path() else {
        return;
    };
    let Ok(bytes) = serde_json::to_vec_pretty(stats) else {
        return;
    };
    let _ = write_atomic(&path, bytes);
}

pub fn register_posted_task(instance_id: &str, maa_task_id: i64, task_name: &str) {
    if let Ok(mut guard) = ACTIVE_TASKS.lock() {
        guard.insert(
            (instance_id.to_string(), maa_task_id),
            ActiveTaskRun {
                instance_id: instance_id.to_string(),
                task_name: task_name.to_string(),
                started_at: None,
            },
        );
    }
}

pub fn on_task_start(instance_id: &str, maa_task_id: i64) {
    if let Ok(mut guard) = ACTIVE_TASKS.lock() {
        if let Some(active) = guard.get_mut(&(instance_id.to_string(), maa_task_id)) {
            active.started_at = Some(Instant::now());
        }
    }
}

pub fn on_task_finished(app: &AppHandle, instance_id: &str, maa_task_id: i64, succeeded: bool) {
    let active = ACTIVE_TASKS
        .lock()
        .ok()
        .and_then(|mut guard| guard.remove(&(instance_id.to_string(), maa_task_id)));
    let Some(active) = active else {
        return;
    };
    let duration_secs = active
        .started_at
        .map(|start| start.elapsed().as_secs().max(1))
        .unwrap_or(5);
    let assistive = is_assistive_task(&active.instance_id, &active.task_name);
    let now_iso = Utc::now().to_rfc3339();
    let today = Local::now().format("%Y-%m-%d").to_string();

    if let Ok(_lock) = STATS_FILE_LOCK.lock() {
        let mut stats = load_stats_unlocked();
        if stats.first_recorded_at.is_none() {
            stats.first_recorded_at = Some(now_iso.clone());
        }
        if !stats.active_days.iter().any(|day| day == &today) {
            stats.active_days.push(today);
            stats.active_days.sort();
            if stats.active_days.len() > 3650 {
                let excess = stats.active_days.len() - 3650;
                stats.active_days.drain(0..excess);
            }
        }
        if assistive {
            let key = normalize_assistive_name(&active.task_name).to_string();
            stats.assistive_runs = stats.assistive_runs.saturating_add(1);
            stats.assistive_duration_secs =
                stats.assistive_duration_secs.saturating_add(duration_secs);
            let entry = stats.assistive_tasks.entry(key).or_default();
            entry.runs = entry.runs.saturating_add(1);
            if succeeded {
                entry.completed = entry.completed.saturating_add(1);
            }
            entry.total_duration_secs = entry.total_duration_secs.saturating_add(duration_secs);
            entry.estimated_saved_secs = 0;
            entry.last_run_at = Some(now_iso);
            let _ = sync_assist_performance_counters(&mut stats);
        } else {
            let saved_secs = estimate_saved_secs(&active.task_name, duration_secs, succeeded);
            stats.total_runs = stats.total_runs.saturating_add(1);
            if succeeded {
                stats.completed_runs = stats.completed_runs.saturating_add(1);
            }
            stats.total_duration_secs = stats.total_duration_secs.saturating_add(duration_secs);
            stats.total_saved_secs = stats.total_saved_secs.saturating_add(saved_secs);
            let entry = stats.tasks.entry(active.task_name).or_default();
            entry.runs = entry.runs.saturating_add(1);
            if succeeded {
                entry.completed = entry.completed.saturating_add(1);
            }
            entry.total_duration_secs = entry.total_duration_secs.saturating_add(duration_secs);
            entry.estimated_saved_secs = entry.estimated_saved_secs.saturating_add(saved_secs);
            entry.last_run_at = Some(now_iso);
        }
        save_stats_unlocked(&stats);
    }
    let _ = app.emit("task-statistics-changed", ());
}

#[tauri::command]
pub async fn get_task_statistics() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let _lock = STATS_FILE_LOCK.lock().map_err(|e| e.to_string())?;
        let stats = load_stats_unlocked();
        serde_json::to_value(stats).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}
