//! Sync and list currently available CN redeem codes for the task options UI.
use super::data_update::resolve_data_file;
use chrono::{FixedOffset, NaiveDate, NaiveDateTime, TimeZone, Utc};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;
use std::time::Duration;

const BILIWIKI_REDEEM_URL: &str = "https://wiki.biligame.com/wxnn/api.php?action=parse&page=%E5%85%91%E6%8D%A2%E7%A0%81%E5%90%88%E9%9B%86&prop=wikitext&format=json";
const QQBOT_REDEEM_URL: &str = "https://qqbot.maanikki.cn/v1/redeem-codes";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncedRedeemCode {
    pub code: String,
    pub reward: String,
    pub expires_at: Option<String>,
    pub source: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SyncedRedeemCodesResponse {
    pub codes: Vec<SyncedRedeemCode>,
    pub synced_at: String,
    pub online: bool,
    pub warnings: Vec<String>,
}

fn tz_cn() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).expect("valid +08:00 offset")
}

fn subobject_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\{\{#subobject:[^|}]*\|([^}]+)\}\}").unwrap())
}

fn expiry_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[T\s/-]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?(.*)$",
        )
        .unwrap()
    })
}

/// Parse expiry text or unix timestamp into `(unix_seconds, display_date_str)` in UTC+8.
fn parse_expiry_value(val: &Value) -> Option<(i64, String)> {
    let tz = tz_cn();
    if let Some(num) = val.as_f64() {
        let secs = num as i64;
        if let Some(dt) = chrono::DateTime::from_timestamp(secs, 0) {
            let local = dt.with_timezone(&tz);
            return Some((secs, local.format("%Y-%m-%d").to_string()));
        }
        return None;
    }
    let raw = val.as_str()?.trim();
    if raw.is_empty() {
        return None;
    }
    if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(raw) {
        let local = dt.with_timezone(&tz);
        return Some((dt.timestamp(), local.format("%Y-%m-%d").to_string()));
    }
    if let Some(caps) = expiry_re().captures(raw) {
        let y: i32 = caps.get(1)?.as_str().parse().ok()?;
        let mo: u32 = caps.get(2)?.as_str().parse().ok()?;
        let d: u32 = caps.get(3)?.as_str().parse().ok()?;
        let date = NaiveDate::from_ymd_opt(y, mo, d)?;
        let (hh, mm, ss) = if let Some(hm) = caps.get(4) {
            let h: u32 = hm.as_str().parse().ok()?;
            let m: u32 = caps.get(5)?.as_str().parse().ok()?;
            let s: u32 = caps
                .get(6)
                .and_then(|v| v.as_str().parse().ok())
                .unwrap_or(0);
            (h, m, s)
        } else {
            (23, 59, 59)
        };
        let naive: NaiveDateTime = date.and_hms_opt(hh, mm, ss)?;
        let dt = tz.from_local_datetime(&naive).single()?;
        return Some((dt.timestamp(), format!("{:04}-{:02}-{:02}", y, mo, d)));
    }
    None
}

async fn fetch_biliwiki(
    client: &reqwest::Client,
    now_ts: i64,
) -> Result<(Vec<SyncedRedeemCode>, HashSet<String>), String> {
    let resp = client
        .get(BILIWIKI_REDEEM_URL)
        .send()
        .await
        .map_err(|e| format!("百科源请求失败：{}", e))?;
    if !resp.status().is_success() {
        return Err(format!("百科源返回状态 {}", resp.status().as_u16()));
    }
    let root: Value = resp
        .json()
        .await
        .map_err(|e| format!("百科源解析失败：{}", e))?;
    let wikitext = root
        .pointer("/parse/wikitext/*")
        .and_then(Value::as_str)
        .unwrap_or("");

    let mut active: Vec<SyncedRedeemCode> = Vec::new();
    let mut expired: HashSet<String> = HashSet::new();

    for caps in subobject_re().captures_iter(wikitext) {
        let body = caps.get(1).map(|m| m.as_str()).unwrap_or("");
        let mut fields: HashMap<&str, &str> = HashMap::new();
        for part in body.split('|') {
            if let Some((k, v)) = part.split_once('=') {
                fields.insert(k.trim(), v.trim());
            }
        }
        if fields.get("平台").copied() != Some("国服") {
            continue;
        }
        let code = fields.get("兑换码").copied().unwrap_or("").trim();
        if code.is_empty() || code.chars().any(char::is_whitespace) {
            continue;
        }
        let reward = fields
            .get("奖励")
            .or_else(|| fields.get("内容"))
            .copied()
            .unwrap_or("")
            .trim()
            .to_string();
        let exp_raw = fields.get("期限").copied().unwrap_or("");
        let parsed_exp = parse_expiry_value(&Value::String(exp_raw.to_string()));
        if let Some((exp_ts, disp)) = parsed_exp {
            if exp_ts <= now_ts {
                expired.insert(code.to_lowercase());
            } else {
                active.push(SyncedRedeemCode {
                    code: code.to_string(),
                    reward,
                    expires_at: Some(disp),
                    source: "百科".to_string(),
                });
            }
        } else {
            active.push(SyncedRedeemCode {
                code: code.to_string(),
                reward,
                expires_at: None,
                source: "百科".to_string(),
            });
        }
    }
    Ok((active, expired))
}

async fn fetch_qqbot(
    client: &reqwest::Client,
    now_ts: i64,
) -> Result<(Vec<SyncedRedeemCode>, HashSet<String>), String> {
    let resp = client
        .get(QQBOT_REDEEM_URL)
        .send()
        .await
        .map_err(|e| format!("实时源请求失败：{}", e))?;
    if !resp.status().is_success() {
        return Err(format!("实时源返回状态 {}", resp.status().as_u16()));
    }
    let root: Value = resp
        .json()
        .await
        .map_err(|e| format!("实时源解析失败：{}", e))?;
    let mut active: Vec<SyncedRedeemCode> = Vec::new();
    let mut expired: HashSet<String> = HashSet::new();

    if let Some(arr) = root.get("codes").and_then(Value::as_array) {
        for item in arr {
            let Some(obj) = item.as_object() else {
                continue;
            };
            let code = obj.get("code").and_then(Value::as_str).unwrap_or("").trim();
            if code.is_empty() || code.chars().any(char::is_whitespace) {
                continue;
            }
            let reward = obj
                .get("reward")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            let parsed_exp = obj.get("expires_at").and_then(parse_expiry_value);
            if let Some((exp_ts, disp)) = parsed_exp {
                if exp_ts <= now_ts {
                    expired.insert(code.to_lowercase());
                    continue;
                }
                active.push(SyncedRedeemCode {
                    code: code.to_string(),
                    reward,
                    expires_at: Some(disp),
                    source: "实时".to_string(),
                });
            } else {
                if let Some(pub_ts) = obj.get("published_at").and_then(Value::as_f64) {
                    if pub_ts > 0.0 && (now_ts as f64 - pub_ts) > 30.0 * 86400.0 {
                        continue;
                    }
                }
                active.push(SyncedRedeemCode {
                    code: code.to_string(),
                    reward,
                    expires_at: None,
                    source: "实时".to_string(),
                });
            }
        }
    }
    Ok((active, expired))
}

fn load_local_catalog(now_ts: i64) -> (Vec<SyncedRedeemCode>, HashSet<String>) {
    let mut active: Vec<SyncedRedeemCode> = Vec::new();
    let mut expired: HashSet<String> = HashSet::new();
    let Ok(path) = resolve_data_file("resource/data/redeem_codes.json") else {
        return (active, expired);
    };
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return (active, expired);
    };
    let cleaned = raw.trim_start_matches('\u{feff}');
    let Ok(root) = serde_json::from_str::<Value>(cleaned) else {
        return (active, expired);
    };
    if let Some(arr) = root.get("codes").and_then(Value::as_array) {
        for item in arr {
            let Some(obj) = item.as_object() else {
                continue;
            };
            let code = obj.get("code").and_then(Value::as_str).unwrap_or("").trim();
            if code.is_empty() || code.chars().any(char::is_whitespace) {
                continue;
            }
            let reward = obj
                .get("reward")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            let parsed_exp = obj.get("expires_at").and_then(parse_expiry_value);
            if let Some((exp_ts, disp)) = parsed_exp {
                if exp_ts <= now_ts {
                    expired.insert(code.to_lowercase());
                    continue;
                }
                active.push(SyncedRedeemCode {
                    code: code.to_string(),
                    reward,
                    expires_at: Some(disp),
                    source: "内置".to_string(),
                });
            } else {
                active.push(SyncedRedeemCode {
                    code: code.to_string(),
                    reward,
                    expires_at: None,
                    source: "内置".to_string(),
                });
            }
        }
    }
    (active, expired)
}

#[tauri::command]
pub async fn get_synced_redeem_codes(online: bool) -> Result<SyncedRedeemCodesResponse, String> {
    let now = Utc::now();
    let now_ts = now.timestamp();
    let mut warnings: Vec<String> = Vec::new();
    let mut known_expired: HashSet<String> = HashSet::new();
    let mut raw_list: Vec<SyncedRedeemCode> = Vec::new();

    if online {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .user_agent("MaaNikki/1.0")
            .build()
            .map_err(|e| format!("初始化网络请求失败：{}", e))?;

        let (wiki_res, qqbot_res) = tokio::join!(
            fetch_biliwiki(&client, now_ts),
            fetch_qqbot(&client, now_ts),
        );

        match wiki_res {
            Ok((active, exp)) => {
                known_expired.extend(exp);
                raw_list.extend(active);
            }
            Err(err) => warnings.push(err),
        }
        match qqbot_res {
            Ok((active, exp)) => {
                known_expired.extend(exp);
                raw_list.extend(active);
            }
            Err(err) => warnings.push(err),
        }
    }

    let (local_active, local_exp) =
        tauri::async_runtime::spawn_blocking(move || load_local_catalog(now_ts))
            .await
            .map_err(|e| e.to_string())?;
    known_expired.extend(local_exp);
    raw_list.extend(local_active);

    let mut merged: Vec<SyncedRedeemCode> = Vec::new();
    let mut index_by_key: HashMap<String, usize> = HashMap::new();
    for item in raw_list {
        let key = item.code.to_lowercase();
        if known_expired.contains(&key) {
            continue;
        }
        if let Some(&idx) = index_by_key.get(&key) {
            if merged[idx].reward.is_empty() && !item.reward.is_empty() {
                merged[idx].reward = item.reward;
            }
            if merged[idx].expires_at.is_none() && item.expires_at.is_some() {
                merged[idx].expires_at = item.expires_at;
            }
        } else {
            index_by_key.insert(key, merged.len());
            merged.push(item);
        }
    }

    Ok(SyncedRedeemCodesResponse {
        codes: merged,
        synced_at: now.with_timezone(&tz_cn()).format("%H:%M:%S").to_string(),
        online,
        warnings,
    })
}
