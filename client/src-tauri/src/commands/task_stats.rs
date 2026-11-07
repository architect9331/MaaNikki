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
    pub active_days: Vec<String>,
    #[serde(default)]
    pub tasks: BTreeMap<String, TaskStatRecord>,
}

impl Default for TaskStatisticsFile {
    fn default() -> Self {
        Self {
            version: 1,
            first_recorded_at: None,
            total_runs: 0,
            completed_runs: 0,
            total_duration_secs: 0,
            total_saved_secs: 0,
            active_days: Vec::new(),
            tasks: BTreeMap::new(),
        }
    }
}

struct ActiveTaskRun {
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
        "custom_route" | "exploration" | "fishing" => (180, 45),
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

fn load_stats_unlocked() -> TaskStatisticsFile {
    let mut stats = stats_file_path()
        .ok()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|text| serde_json::from_str::<TaskStatisticsFile>(&text).ok())
        .unwrap_or_default();
    if stats.first_recorded_at.is_none() {
        stats.first_recorded_at = infer_earliest_date();
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
    let saved_secs = estimate_saved_secs(&active.task_name, duration_secs, succeeded);
    let now_iso = Utc::now().to_rfc3339();
    let today = Local::now().format("%Y-%m-%d").to_string();

    if let Ok(_lock) = STATS_FILE_LOCK.lock() {
        let mut stats = load_stats_unlocked();
        if stats.first_recorded_at.is_none() {
            stats.first_recorded_at = Some(now_iso.clone());
        }
        stats.total_runs = stats.total_runs.saturating_add(1);
        if succeeded {
            stats.completed_runs = stats.completed_runs.saturating_add(1);
        }
        stats.total_duration_secs = stats.total_duration_secs.saturating_add(duration_secs);
        stats.total_saved_secs = stats.total_saved_secs.saturating_add(saved_secs);
        if !stats.active_days.iter().any(|day| day == &today) {
            stats.active_days.push(today);
            stats.active_days.sort();
            if stats.active_days.len() > 3650 {
                let excess = stats.active_days.len() - 3650;
                stats.active_days.drain(0..excess);
            }
        }
        let entry = stats.tasks.entry(active.task_name).or_default();
        entry.runs = entry.runs.saturating_add(1);
        if succeeded {
            entry.completed = entry.completed.saturating_add(1);
        }
        entry.total_duration_secs = entry.total_duration_secs.saturating_add(duration_secs);
        entry.estimated_saved_secs = entry.estimated_saved_secs.saturating_add(saved_secs);
        entry.last_run_at = Some(now_iso);
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
